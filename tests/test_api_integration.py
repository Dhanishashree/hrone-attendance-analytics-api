import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient
from pymongo.errors import PyMongoError
import yaml


ROOT = Path(__file__).resolve().parents[1]
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")


def epoch_ms(year, month, day, hour, minute=0, second=0):
    instant = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(instant.timestamp() * 1000)


class MongoApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = yaml.safe_load(
            (ROOT / "openapi.yaml").read_text(encoding="utf-8")
        )
        cls.mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500)
        try:
            cls.mongo.admin.command("ping")
        except PyMongoError as exc:
            cls.mongo.close()
            raise unittest.SkipTest(f"MongoDB unavailable at {MONGO_URI}: {exc}") from exc

        cls.database_name = "hrone_test_" + uuid.uuid4().hex
        cls.database = cls.mongo[cls.database_name]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        env = os.environ.copy()
        env["MONGO_URI"] = MONGO_URI
        env["MONGO_DB"] = cls.database_name
        cls.server = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(cls.port),
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                cls.database.drop()
                cls.mongo.close()
                raise RuntimeError(f"Uvicorn exited during startup with status {cls.server.returncode}")
            try:
                status, payload = cls.request("GET", "/health")
                if status == 200 and payload == {"status": "ok"}:
                    return
            except (OSError, urllib.error.URLError, ValueError):
                pass
            time.sleep(0.2)
        cls.server.terminate()
        cls.server.wait(timeout=5)
        cls.database.drop()
        cls.mongo.close()
        raise RuntimeError("Uvicorn did not become healthy within 20 seconds")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None) is not None and cls.server.poll() is None:
            cls.server.terminate()
            cls.server.wait(timeout=10)
        if getattr(cls, "database", None) is not None:
            cls.mongo.drop_database(cls.database_name)
        if getattr(cls, "mongo", None) is not None:
            cls.mongo.close()

    @classmethod
    def request(cls, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            cls.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def setUp(self):
        self.database.employees.delete_many({})
        self.database.attendance_logs.delete_many({})

    def employee_body(self, code, department="QA", joined_on="2026-01-01", **extra):
        return {
            "emp_code": code,
            "name": code,
            "email": f"{code.lower()}@example.com",
            "department": department,
            "joined_on": joined_on,
            **extra,
        }

    def insert_employee(self, code, department, joined_on="2026-01-01", **extra):
        self.database.employees.insert_one({
            **self.employee_body(code, department, joined_on, **extra),
            "shift_start": extra.get("shift_start", "09:30"),
            "shift_end": extra.get("shift_end", "18:30"),
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        })

    def insert_log(
        self, code, day, status="PRESENT", *, late=0, overtime=0,
        hours=None, half_day=False, punch_in=None, punch_out=None,
    ):
        self.database.attendance_logs.insert_one({
            "emp_code": code,
            "date": day,
            "status": status,
            "punch_in": punch_in,
            "punch_out": punch_out,
            "work_hours": hours,
            "late_minutes": late,
            "overtime_minutes": overtime,
            "half_day": half_day,
            "history": [],
        })

    def assert_contract_response(self, method, path, contract_path, body=None):
        operation = self.contract["paths"][contract_path][method.lower()]
        success_codes = [code for code in operation["responses"] if code.startswith("2")]
        self.assertEqual(len(success_codes), 1, f"{method} {contract_path} needs one success response")
        status, payload = self.request(method, path, body)
        self.assertEqual(status, int(success_codes[0]), f"{method} {contract_path}: {payload}")
        schema = operation["responses"][success_codes[0]]["content"]["application/json"]["schema"]

        def resolve(current):
            if "$ref" in current:
                target = self.contract
                for part in current["$ref"].removeprefix("#/").split("/"):
                    target = target[part]
                current = {**target, **{key: value for key, value in current.items() if key != "$ref"}}
            if "allOf" in current:
                parts = [resolve(part) for part in current["allOf"]]
                merged = {key: value for key, value in current.items() if key != "allOf"}
                properties = dict(merged.get("properties", {}))
                required = set(current.get("required", []))
                for part in parts:
                    if "type" in part:
                        merged.setdefault("type", part["type"])
                    properties.update(part.get("properties", {}))
                    required.update(part.get("required", []))
                    if "enum" in part:
                        merged.setdefault("enum", part["enum"])
                merged["properties"] = properties
                merged["required"] = sorted(required)
                current = merged
            return current

        def validate(value, current, location):
            current = resolve(current)
            if value is None:
                self.assertTrue(current.get("nullable", False), f"{location} is unexpectedly null")
                return
            schema_type = current.get("type")
            if schema_type == "string":
                self.assertIsInstance(value, str, location)
            elif schema_type == "integer":
                self.assertIsInstance(value, int, location)
                self.assertNotIsInstance(value, bool, location)
            elif schema_type == "number":
                self.assertIsInstance(value, (int, float), location)
                self.assertNotIsInstance(value, bool, location)
            elif schema_type == "boolean":
                self.assertIsInstance(value, bool, location)
            if current.get("type") == "object":
                self.assertIsInstance(value, dict, location)
                missing = set(current.get("required", [])) - set(value)
                self.assertFalse(missing, f"{location} missing required contract fields: {sorted(missing)}")
                for name, nested in current.get("properties", {}).items():
                    if name in value:
                        validate(value[name], nested, f"{location}.{name}")
                additional = current.get("additionalProperties")
                if isinstance(additional, dict):
                    declared = current.get("properties", {})
                    for name, nested_value in value.items():
                        if name not in declared:
                            validate(nested_value, additional, f"{location}.{name}")
            elif current.get("type") == "array":
                self.assertIsInstance(value, list, location)
                if "items" in current:
                    for index, item in enumerate(value):
                        validate(item, current["items"], f"{location}[{index}]")
            if "enum" in current:
                self.assertIn(value, current["enum"], location)

        validate(payload, schema, f"{method} {contract_path}")
        return payload

    def test_all_success_endpoint_payloads_match_openapi_required_fields(self):
        employee_path = "/employees"
        attendance_path = "/attendance"
        self.assert_contract_response("GET", "/health", "/health")
        created = self.assert_contract_response(
            "POST", employee_path, employee_path, self.employee_body("EMP0701", "Contract"),
        )
        self.assertEqual(created["emp_code"], "EMP0701")
        self.assert_contract_response("GET", employee_path, employee_path)
        timestamp_in = epoch_ms(2026, 7, 6, 4, 0)
        self.assert_contract_response(
            "POST", "/attendance/punch-in", "/attendance/punch-in",
            {"emp_code": "EMP0701", "punched_at": timestamp_in},
        )
        self.assert_contract_response(
            "POST", "/attendance/punch-out", "/attendance/punch-out",
            {"emp_code": "EMP0701", "punched_at": epoch_ms(2026, 7, 6, 13, 0)},
        )
        self.assert_contract_response("GET", attendance_path, attendance_path)
        self.assert_contract_response(
            "PATCH",
            "/attendance/EMP0701/2026-07-06",
            "/attendance/{emp_code}/{date}",
            {
                "punch_out": epoch_ms(2026, 7, 6, 13, 1),
                "reason": "Correct punch-out",
                "regularized_by": "contract test",
            },
        )
        self.assert_contract_response(
            "GET",
            "/analytics/employees/EMP0701/monthly?month=2026-07",
            "/analytics/employees/{emp_code}/monthly",
        )
        self.assert_contract_response(
            "GET", "/analytics/departments/summary?month=2026-07",
            "/analytics/departments/summary",
        )
        self.assert_contract_response(
            "GET", "/analytics/leaderboard/late?month=2026-07",
            "/analytics/leaderboard/late",
        )
        self.assert_contract_response(
            "GET",
            "/analytics/departments/Contract/trend?from=2026-07-06&to=2026-07-07",
            "/analytics/departments/{department}/trend",
        )
        self.assert_contract_response(
            "GET",
            "/admin/explain/attendance_list",
            "/admin/explain/{endpoint}",
        )

    def test_health_and_unknown_resources_follow_contract(self):
        status, payload = self.request("GET", "/health")
        self.assertEqual((status, payload), (200, {"status": "ok"}))
        status, payload = self.request("POST", "/attendance/punch-in", {
            "emp_code": "EMP9999",
            "punched_at": epoch_ms(2026, 7, 6, 4),
        })
        self.assertEqual(status, 404)
        self.assertIsInstance(payload.get("detail"), str)
        status, payload = self.request("GET", "/analytics/employees/EMP9999/monthly?month=2026-07")
        self.assertEqual(status, 404)
        self.assertIsInstance(payload.get("detail"), str)

    def test_employee_create_duplicate_and_response_schema(self):
        body = self.employee_body("EMP0101")
        status, payload = self.request("POST", "/employees", body)
        self.assertEqual(status, 201)
        self.assertEqual(
            set(payload),
            {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"},
        )
        self.assertIsInstance(payload["created_at"], int)
        self.assertNotIn("_id", payload)
        status, payload = self.request("POST", "/employees", body)
        self.assertEqual(status, 409)
        self.assertIsInstance(payload.get("detail"), str)

    def test_employee_validation_and_derived_fields_are_ignored(self):
        invalid = self.employee_body("not-an-emp-code")
        self.assertEqual(self.request("POST", "/employees", invalid)[0], 422)
        self.insert_employee("EMP0102", "QA")
        status, payload = self.request("POST", "/attendance/punch-in", {
            "emp_code": "EMP0102",
            "punched_at": epoch_ms(2026, 7, 6, 4, 0),
            "status": "PRESENT",
            "late_minutes": 9999,
            "work_hours": 123,
        })
        self.assertEqual(status, 201)
        self.assertEqual(payload["late_minutes"], 0)
        self.assertIsNone(payload["work_hours"])
        self.assertEqual(payload["history"], [])

    def test_epoch_inputs_reject_strings_floats_booleans_and_null(self):
        self.insert_employee("EMP0103", "QA")
        for timestamp in ("1783312500000", 1783312500000.5, True, None):
            with self.subTest(timestamp=timestamp):
                status, payload = self.request("POST", "/attendance/punch-in", {
                    "emp_code": "EMP0103",
                    "punched_at": timestamp,
                })
                self.assertEqual(status, 422)
                self.assertIsInstance(payload.get("detail"), list)
        status, payload = self.request("POST", "/attendance/punch-in", {
            "emp_code": "EMP0103",
            "punched_at": 1_783_312_500,
        })
        self.assertEqual(status, 422)
        self.assertIsInstance(payload.get("detail"), list)

    def test_punch_in_is_race_safe_and_returns_epoch_milliseconds(self):
        self.insert_employee("EMP0104", "QA")
        timestamp = epoch_ms(2026, 7, 6, 4, 0)
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(
                lambda _: self.request("POST", "/attendance/punch-in", {
                    "emp_code": "EMP0104", "punched_at": timestamp,
                })[0],
                range(8),
            ))
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(409), 7)
        status, payload = self.request("GET", "/attendance?emp_code=EMP0104")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["punch_in"], timestamp)

    def test_overnight_punch_in_assigns_shift_start_date(self):
        status, _ = self.request("POST", "/employees", self.employee_body(
            "EMP0105", shift_start="22:00", shift_end="06:00",
        ))
        self.assertEqual(status, 201)
        timestamp = epoch_ms(2026, 7, 6, 21, 30)
        status, payload = self.request("POST", "/attendance/punch-in", {
            "emp_code": "EMP0105", "punched_at": timestamp,
        })
        self.assertEqual(status, 201)
        self.assertEqual(payload["date"], "2026-07-06")

    def test_punch_out_calculates_hours_overtime_and_is_race_safe(self):
        status, _ = self.request("POST", "/employees", self.employee_body(
            "EMP0106", shift_start="22:00", shift_end="06:00",
        ))
        self.assertEqual(status, 201)
        punch_in = epoch_ms(2026, 7, 6, 16, 30)
        self.assertEqual(self.request("POST", "/attendance/punch-in", {
            "emp_code": "EMP0106", "punched_at": punch_in,
        })[0], 201)
        punch_out = epoch_ms(2026, 7, 7, 1, 0)
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(
                lambda _: self.request("POST", "/attendance/punch-out", {
                    "emp_code": "EMP0106", "punched_at": punch_out,
                })[0],
                range(8),
            ))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), 7)
        record = self.database.attendance_logs.find_one({"emp_code": "EMP0106"})
        self.assertEqual(record["work_hours"], 8.5)
        self.assertEqual(record["overtime_minutes"], 30)
        self.assertFalse(record["half_day"])

    def test_attendance_filters_sort_pagination_and_invalid_date(self):
        self.insert_employee("EMP0107", "QA")
        for day in ("2026-07-06", "2026-07-07", "2026-07-08"):
            self.insert_log("EMP0107", day)
        status, page = self.request("GET", "/attendance?date_from=2026-07-07&page=1&page_size=1")
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["date"], "2026-07-08")
        status, payload = self.request("GET", "/attendance?date_from=")
        self.assertEqual(status, 422)
        self.assertIsInstance(payload.get("detail"), list)
        self.assertEqual(self.request("GET", "/attendance?date_from=2026-07-09&date_to=2026-07-01")[0], 422)
        self.assertEqual(self.request("GET", "/attendance?page_size=101")[0], 422)

    def test_regularization_recomputes_appends_audit_and_clears_punches(self):
        self.insert_employee("EMP0108", "QA")
        day_start = epoch_ms(2026, 7, 6, 4, 0)
        status, _ = self.request("POST", "/attendance/punch-in", {
            "emp_code": "EMP0108", "punched_at": epoch_ms(2026, 7, 6, 4, 35),
        })
        self.assertEqual(status, 201)
        status, corrected = self.request("PATCH", "/attendance/EMP0108/2026-07-06", {
            "punch_in": day_start,
            "reason": "Correct punch time",
            "regularized_by": "test operator",
            "late_minutes": 999,
        })
        self.assertEqual(status, 200)
        self.assertEqual(corrected["late_minutes"], 0)
        self.assertEqual(corrected["history"][-1]["changes"]["punch_in"], {
            "from": epoch_ms(2026, 7, 6, 4, 35),
            "to": day_start,
        })
        stored = self.database.attendance_logs.find_one({"emp_code": "EMP0108"})
        self.assertIsInstance(stored["history"][-1]["at"], datetime)
        self.assertIsInstance(stored["history"][-1]["changes"]["punch_in"]["from"], datetime)
        status, leave = self.request("PATCH", "/attendance/EMP0108/2026-07-06", {
            "status": "LEAVE",
            "reason": "Approved leave",
            "regularized_by": "test operator",
        })
        self.assertEqual(status, 200)
        self.assertIsNone(leave["punch_in"])
        self.assertIsNone(leave["punch_out"])
        self.assertEqual(leave["history"][-1]["changes"]["punch_in"]["to"], None)
        self.assertEqual(self.request("PATCH", "/attendance/EMP0108/2026-07-06", {
            "status": "LEAVE",
            "reason": "No actual change",
            "regularized_by": "test operator",
        })[0], 422)

    def test_monthly_analytics_counts_join_date_weekdays_and_weekend_lateness(self):
        self.insert_employee("EMP0201", "Monthly", "2026-07-15")
        self.insert_log("EMP0201", "2026-07-15", late=15, overtime=20, hours=4.5, half_day=True)
        self.insert_log("EMP0201", "2026-07-18", late=10, overtime=40, hours=8.0)
        self.insert_log("EMP0201", "2026-07-21", status="LEAVE")
        status, summary = self.request("GET", "/analytics/employees/EMP0201/monthly?month=2026-07")
        self.assertEqual(status, 200)
        self.assertEqual(summary["working_days"], 13)
        self.assertEqual(summary["present_days"], 0.5)
        self.assertEqual(summary["leave_days"], 1)
        self.assertEqual(summary["late_count"], 2)
        self.assertEqual(summary["total_late_minutes"], 25)
        self.assertEqual(summary["total_overtime_minutes"], 60)
        self.assertEqual(summary["attendance_pct"], 3.8462)

    def test_department_summary_includes_no_log_headcount_and_averages_records(self):
        self.insert_employee("EMP0301", "Summary", "2026-07-01")
        self.insert_employee("EMP0302", "Summary", "2026-07-15")
        self.insert_employee("EMP0303", "Summary", "2026-08-01")
        self.insert_log("EMP0302", "2026-07-15", late=20, hours=4.5, half_day=True)
        self.insert_log("EMP0302", "2026-07-16", late=30, hours=7.5)
        self.insert_log("EMP0302", "2026-07-18", status="PRESENT", hours=9.375)
        self.insert_log("EMP0302", "2026-07-20", status="LEAVE")
        self.insert_log("EMP0302", "2026-07-21", status="ON_DUTY", hours=None)
        status, result = self.request("GET", "/analytics/departments/summary?month=2026-07&department=Summary")
        self.assertEqual(status, 200)
        summary = result["items"][0]
        self.assertEqual(summary["headcount"], 2)
        self.assertEqual(summary["present_days"], 2.5)
        self.assertEqual(summary["avg_work_hours"], 7.13)
        self.assertEqual(summary["late_count"], 2)
        self.assertEqual(summary["total_late_minutes"], 50)
        self.assertEqual(summary["leave_count"], 1)
        self.assertEqual(summary["on_duty_count"], 1)

    def test_late_leaderboard_standard_rank_retains_cutoff_ties(self):
        for code, late in (("EMP0401", 40), ("EMP0402", 30), ("EMP0403", 30), ("EMP0404", 10)):
            self.insert_employee(code, "Leaderboard")
            self.insert_log(code, "2026-07-06", late=late)
        status, result = self.request(
            "GET", "/analytics/leaderboard/late?month=2026-07&department=Leaderboard&limit=2",
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [(item["rank"], item["total_late_minutes"]) for item in result["items"]],
            [(1, 40), (2, 30), (2, 30)],
        )
        self.assertEqual(
            [item["emp_code"] for item in result["items"]],
            ["EMP0401", "EMP0402", "EMP0403"],
        )

    def test_department_trend_gap_fill_headcount_weekends_and_rolling_average(self):
        self.insert_employee("EMP0501", "Trend", "2026-07-06")
        self.insert_employee("EMP0502", "Trend", "2026-07-08")
        self.insert_employee("EMP0503", "Trend", "2026-07-13")
        self.insert_log("EMP0501", "2026-07-06", half_day=True, late=5)
        self.insert_log("EMP0501", "2026-07-07", status="ABSENT")
        self.insert_log("EMP0501", "2026-07-08")
        self.insert_log("EMP0501", "2026-07-11", late=10)
        status, result = self.request(
            "GET", "/analytics/departments/Trend/trend?from=2026-07-06&to=2026-07-12",
        )
        self.assertEqual(status, 200)
        items = result["items"]
        self.assertEqual(len(items), 7)
        self.assertEqual([item["date"] for item in items], [
            "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09",
            "2026-07-10", "2026-07-11", "2026-07-12",
        ])
        self.assertEqual([item["headcount"] for item in items], [1, 1, 2, 2, 2, 2, 2])
        self.assertEqual([item["attendance_rate"] for item in items], [0.5, 0.0, 0.5, 0.0, 0.0, None, None])
        self.assertEqual([item["moving_avg_7d"] for item in items], [0.5, 0.25, 0.3333, 0.25, 0.2, 0.2, 0.2])
        self.assertEqual(items[5]["present_count"], 1)
        self.assertFalse(items[5]["is_working_day"])
        self.assertEqual(items[0]["late_count"], 1)
        self.assertEqual(self.request("GET", "/analytics/departments/NoSuchDept/trend?from=2026-07-06&to=2026-07-07")[0], 404)
        self.assertEqual(self.request("GET", "/analytics/departments/Trend/trend?from=2026-07-06&to=2026-10-10")[0], 422)

    def test_every_explain_endpoint_returns_indexed_execution_stats(self):
        self.insert_employee("EMP0601", "Explain")
        self.insert_log("EMP0601", "2026-07-06", late=20, hours=8.0)
        cases = (
            ("attendance_list", ""),
            ("employee_monthly", "?emp_code=EMP0601&month=2026-07"),
            ("department_summary", "?month=2026-07"),
            ("late_leaderboard", "?month=2026-07"),
            ("department_trend", "?department=Explain&from=2026-07-06&to=2026-07-07"),
        )
        for endpoint, query in cases:
            with self.subTest(endpoint=endpoint):
                status, result = self.request("GET", f"/admin/explain/{endpoint}{query}")
                self.assertEqual(status, 200, result)
                self.assertEqual(result["endpoint"], endpoint)
                stages = []
                execution_stats = []

                def collect(value):
                    if isinstance(value, dict):
                        if "executionStats" in value:
                            execution_stats.append(value["executionStats"])
                        if isinstance(value.get("stage"), str):
                            stages.append(value["stage"])
                        for nested in value.values():
                            collect(nested)
                    elif isinstance(value, list):
                        for nested in value:
                            collect(nested)

                collect(result["explain"])
                self.assertTrue(execution_stats, "executionStats missing from explain output")
                self.assertTrue(any("IXSCAN" in stage for stage in stages), stages)
                self.assertNotIn("COLLSCAN", stages)

    def test_attendance_explain_preserves_empty_employee_filter(self):
        status, result = self.request("GET", "/admin/explain/attendance_list?emp_code=")
        self.assertEqual(status, 200, result)
        parsed_queries = []

        def collect(value):
            if isinstance(value, dict):
                if isinstance(value.get("parsedQuery"), dict):
                    parsed_queries.append(value["parsedQuery"])
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(result["explain"])
        self.assertTrue(
            any(query.get("emp_code") == {"$eq": ""} for query in parsed_queries),
            parsed_queries,
        )


if __name__ == "__main__":
    unittest.main()
