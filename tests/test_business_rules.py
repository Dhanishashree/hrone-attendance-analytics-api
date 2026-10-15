import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi import HTTPException

from app.main import (
    IST,
    UTC,
    app,
    attendance_day,
    bounds,
    derived,
    from_ms,
    half_up,
    late_minutes,
    overtime_minutes,
    parse_date,
    shift_bounds,
    to_ms,
    workdays,
)


class BusinessRuleTests(unittest.TestCase):
    def test_epoch_millisecond_round_trip_truncates_subseconds(self):
        instant = datetime(2026, 7, 6, 4, 10, 0, 900_000, tzinfo=UTC)
        millis = to_ms(instant)
        result = from_ms(millis)
        self.assertEqual(result.microsecond, 0)
        self.assertEqual(to_ms(result), millis // 1000 * 1000)

    def test_attendance_date_for_normal_and_overnight_shifts(self):
        after_midnight = datetime(2026, 7, 7, 3, 0, tzinfo=IST).astimezone(UTC)
        self.assertEqual(attendance_day(after_midnight, "22:00", "06:00"), "2026-07-06")
        self.assertEqual(attendance_day(after_midnight, "09:30", "18:30"), "2026-07-07")

    def test_shift_bounds_roll_overnight_end_into_next_day(self):
        start, end = shift_bounds("2026-07-06", "22:00", "06:00")
        self.assertEqual(end - start, timedelta(hours=8))
        self.assertEqual(end.astimezone(IST).date().isoformat(), "2026-07-07")

    def test_late_grace_period_is_strict_and_minutes_are_floored(self):
        shift_start = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
        self.assertEqual(late_minutes(shift_start + timedelta(minutes=10), "2026-07-06", "09:30"), 0)
        self.assertEqual(late_minutes(shift_start + timedelta(minutes=10, seconds=1), "2026-07-06", "09:30"), 10)
        self.assertEqual(late_minutes(shift_start + timedelta(minutes=45, seconds=59), "2026-07-06", "09:30"), 45)

    def test_overtime_threshold_is_thirty_whole_minutes(self):
        shift_end = datetime(2026, 7, 6, 18, 30, tzinfo=IST)
        self.assertEqual(overtime_minutes(shift_end + timedelta(minutes=29, seconds=59), "2026-07-06", "09:30", "18:30"), 0)
        self.assertEqual(overtime_minutes(shift_end + timedelta(minutes=30), "2026-07-06", "09:30", "18:30"), 30)

    def test_work_hours_half_day_uses_rounded_half_up_value(self):
        employee = {"shift_start": "09:30", "shift_end": "18:30"}
        punch_in = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
        exactly_half = derived("PRESENT", punch_in, punch_in + timedelta(seconds=16_182), employee, "2026-07-06")
        just_below = derived("PRESENT", punch_in, punch_in + timedelta(seconds=16_181), employee, "2026-07-06")
        self.assertEqual(exactly_half["work_hours"], 4.50)
        self.assertFalse(exactly_half["half_day"])
        self.assertEqual(just_below["work_hours"], 4.49)
        self.assertTrue(just_below["half_day"])

    def test_absent_and_leave_have_no_derived_attendance(self):
        result = derived("LEAVE", None, None, {}, "2026-07-06")
        self.assertEqual(result, {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        })

    def test_half_up_and_working_days(self):
        self.assertEqual(half_up(1.005, 2), 1.01)
        self.assertEqual(workdays(parse_date("2026-07-15"), parse_date("2026-07-31")), 13)

    def test_invalid_month_and_timestamp_are_rejected(self):
        with self.assertRaises(HTTPException):
            bounds("0000-01")
        for bad in (True, 1_783_312_500, 1_783_312_500_000.0, "1783312500000"):
            with self.subTest(value=bad), self.assertRaises(HTTPException):
                from_ms(bad)


class OpenApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "openapi.yaml").read_text(encoding="utf-8")
        )

    def test_every_contract_path_and_method_is_registered(self):
        contract = {
            (path, method.upper())
            for path, operations in self.contract["paths"].items()
            for method in operations
        }
        registered = {
            (route.path, method)
            for route in app.routes
            if getattr(route, "methods", None)
            for method in route.methods
            if method not in {"HEAD", "OPTIONS"}
            and not route.path.startswith(("/docs", "/redoc", "/openapi"))
        }
        self.assertEqual(registered, contract)

    def test_path_and_query_parameters_match_contract(self):
        route_map = {
            (route.path, method): route
            for route in app.routes
            if getattr(route, "methods", None)
            for method in route.methods
        }
        for path, operations in self.contract["paths"].items():
            for method, operation in operations.items():
                with self.subTest(path=path, method=method):
                    route = route_map[(path, method.upper())]
                    actual = {
                        (parameter_name, parameter_location)
                        for parameters, parameter_location in (
                            (route.dependant.path_params, "path"),
                            (route.dependant.query_params, "query"),
                        )
                        for parameter in parameters
                        for parameter_name in (parameter.alias,)
                    }
                    expected = {
                        (parameter["name"], parameter["in"])
                        for parameter in operation.get("parameters", [])
                        if "$ref" not in parameter
                    }
                    for parameter in operation.get("parameters", []):
                        if "$ref" in parameter:
                            name = parameter["$ref"].rsplit("/", 1)[-1]
                            schema_parameter = self.contract["components"]["parameters"][name]
                            expected.add((schema_parameter["name"], schema_parameter["in"]))
                    self.assertEqual(actual, expected)

    def test_api_declares_contract_success_status_codes(self):
        route_map = {
            (route.path, method): route
            for route in app.routes
            if getattr(route, "methods", None)
            for method in route.methods
        }
        for path, operations in self.contract["paths"].items():
            for method, operation in operations.items():
                route = route_map[(path, method.upper())]
                expected_success = {
                    code for code in operation["responses"] if code.startswith("2")
                }
                actual_success = {str(route.status_code or 200)}
                self.assertTrue(
                    expected_success & actual_success,
                    f"{method.upper()} {path}: expected success in {expected_success}, got {actual_success}",
                )


if __name__ == "__main__":
    unittest.main()
