"""
HROne Employee Attendance & Analytics API
Run: python -m uvicorn app.main:app --port 8000 --reload
"""
import os, re, calendar, json
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, NoReturn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util
from bson.decimal128 import Decimal128

load_dotenv()
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC)
db = client[os.getenv("MONGO_DB", "hrone_assignment")]
emp = db.employees
logs = db.attendance_logs
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

def invalid_request(detail: str) -> NoReturn:
    raise HTTPException(422, [{"type": "value_error", "loc": [], "msg": detail}])

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)

def from_ms(v):
    if isinstance(v, bool) or not isinstance(v, int) or not MIN_MS <= v <= MAX_MS:
        invalid_request("Timestamp must be epoch milliseconds in the supported range")
    try: return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=v)).replace(microsecond=0)
    except (ValueError, OverflowError, OSError): invalid_request("Invalid timestamp")

def to_ms(v):
    if v is None: return None
    if v.tzinfo is None: v = v.replace(tzinfo=UTC)
    delta = v.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000

def local(v):
    if v.tzinfo is None: v = v.replace(tzinfo=UTC)
    return v.astimezone(IST)

def parse_date(s, name="date"):
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s: raise ValueError()
        return d
    except (ValueError, TypeError): invalid_request(f"{name} must be YYYY-MM-DD")

def bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        invalid_request("month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    if y == 0:
        invalid_request("month must be YYYY-MM")
    return f"{y:04}-{m:02}-01", f"{y:04}-{m:02}-{calendar.monthrange(y,m)[1]:02}"

def half_up(x, n=2):
    return float(Decimal(str(x)).quantize(Decimal("1").scaleb(-n), rounding=ROUND_HALF_UP))

def mongo_half_up(value, n=2):
    scale = Decimal128(str(10 ** n))
    scaled = {"$multiply": [{"$toDecimal": value}, scale]}
    rounded = {"$divide": [
        {"$floor": {"$add": [scaled, Decimal128("0.5")]}},
        scale
    ]}
    return {"$toDouble": rounded}

def workdays(a, b):
    count = 0
    while a <= b:
        count += a.weekday() < 5
        a += timedelta(days=1)
    return count

def shift_bounds(day, start, end):
    d = parse_date(day)
    sh, sm = map(int, start.split(":")); eh, em = map(int, end.split(":"))
    s = datetime.combine(d, time(sh,sm), IST)
    e = datetime.combine(d, time(eh,em), IST)
    if e <= s: e += timedelta(days=1)
    return s.astimezone(UTC), e.astimezone(UTC)

def attendance_day(ts, start, end):
    t = local(ts)
    sh, sm = map(int, start.split(":")); eh, em = map(int, end.split(":"))
    if (eh,em) <= (sh,sm) and (t.hour,t.minute) < (eh,em):
        return (t.date()-timedelta(days=1)).isoformat()
    return t.date().isoformat()

def late_minutes(pin, day, shift_start):
    if pin is None: return 0
    start, _ = shift_bounds(day, shift_start, "23:59" if shift_start != "23:59" else "23:58")
    seconds = (pin-start).total_seconds()
    return int(seconds//60) if seconds > 600 else 0

def overtime_minutes(pout, day, shift_start, shift_end):
    if pout is None: return 0
    _, end = shift_bounds(day, shift_start, shift_end)
    mins = int((pout-end).total_seconds()//60)
    return mins if mins >= 30 else 0

def derived(status, pin, pout, employee, day):
    if status in ("ABSENT","LEAVE"):
        return {"late_minutes":0,"work_hours":None,"overtime_minutes":0,"half_day":False}
    late = late_minutes(pin, day, employee.get("shift_start","09:30"))
    if pin is None or pout is None:
        return {"late_minutes":late,"work_hours":None,"overtime_minutes":0,"half_day":False}
    if pout <= pin: invalid_request("punch_out must be after punch_in")
    hours = half_up(Decimal(str((pout-pin).total_seconds()))/Decimal(3600), 2)
    return {"late_minutes":late,"work_hours":hours,
            "overtime_minutes":overtime_minutes(pout,day,employee.get("shift_start","09:30"),employee.get("shift_end","18:30")),
            "half_day":hours < 4.5}

def out_record(d):
    r = {k:v for k,v in d.items() if k != "_id"}
    for f in ("punch_in","punch_out"): r[f] = to_ms(r.get(f))
    r.setdefault("work_hours", None)
    r.setdefault("late_minutes", 0)
    r.setdefault("overtime_minutes", 0)
    r.setdefault("half_day", False)
    history = []
    for entry in r.get("history", []):
        item = {
            **entry,
            "at": to_ms(entry["at"]) if isinstance(entry.get("at"), datetime) else int(entry["at"])
        }
        item["changes"] = {
            field: {
                key: to_ms(value) if field in ("punch_in", "punch_out") and isinstance(value, datetime)
                else int(value) if field in ("punch_in", "punch_out") and value is not None
                else value
                for key, value in change.items()
            }
            for field, change in entry.get("changes", {}).items()
        }
        history.append(item)
    r["history"] = history
    return r

def out_employee(d):
    r = {k:v for k,v in d.items() if k != "_id"}
    if isinstance(r.get("created_at"),datetime): r["created_at"] = to_ms(r["created_at"])
    return r

def create_indexes():
    emp.create_index([("emp_code",ASCENDING)], unique=True, name="uq_emp_code")
    emp.create_index([("department",ASCENDING),("joined_on",ASCENDING)], name="ix_emp_dept_joined")
    emp.create_index([("joined_on",ASCENDING)], name="ix_emp_joined")
    logs.create_index([("emp_code",ASCENDING),("date",ASCENDING)], unique=True, name="uq_log_emp_date")
    logs.create_index([("date",DESCENDING),("emp_code",ASCENDING)], name="ix_log_date_emp")
    logs.create_index([("date",ASCENDING),("status",ASCENDING)], name="ix_log_date_status")
    logs.create_index([("emp_code",ASCENDING),("punch_in",DESCENDING)], name="ix_log_emp_pin")
    logs.create_index([("date",ASCENDING),("late_minutes",DESCENDING)], name="ix_log_late")

@app.on_event("startup")
def startup():
    client.admin.command("ping")
    create_indexes()

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1,max_length=100)
    email: str = Field(max_length=120,pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1,max_length=50)
    shift_start: str = Field(default="09:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$",validate_default=True)
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls,v): parse_date(v,"joined_on"); return v
    @field_validator("shift_end")
    @classmethod
    def different_shift(cls,v,info):
        if v == info.data.get("shift_start","09:30"): raise ValueError("shift_start must differ from shift_end")
        return v

class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(default=None,strict=True,ge=MIN_MS,le=MAX_MS)
    status: Literal["PRESENT","WFH","ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self

class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(default=None,strict=True,ge=MIN_MS,le=MAX_MS)

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self

class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None,strict=True,ge=MIN_MS,le=MAX_MS)
    punch_out: Optional[int] = Field(default=None,strict=True,ge=MIN_MS,le=MAX_MS)
    reason: str = Field(min_length=5,max_length=200)
    regularized_by: str = Field(min_length=1,max_length=50)

    @model_validator(mode="after")
    def reject_null_updates(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self

@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except PyMongoError: raise HTTPException(503,"MongoDB unavailable")

@app.post("/employees",status_code=201)
def create_employee(body: EmployeeIn):
    doc=body.model_dump(); doc["created_at"]=now_utc()
    try: emp.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return out_employee(doc)

@app.get("/employees")
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={"department":department} if department is not None else {}
    total=emp.count_documents(q)
    items=list(emp.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size))
    return {"items":[out_employee(x) for x in items],"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    e=emp.find_one({"emp_code":body.emp_code})
    if not e: raise HTTPException(404,"Unknown employee")
    pin=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day=attendance_day(pin,e.get("shift_start","09:30"),e.get("shift_end","18:30"))
    d={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":pin,"punch_out":None,
       "work_hours":None,"late_minutes":late_minutes(pin,day,e.get("shift_start","09:30")),
       "overtime_minutes":0,"half_day":False,"history":[]}
    try: logs.insert_one(d)
    except DuplicateKeyError: raise HTTPException(409,"Already punched in for this date")
    return out_record(d)

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    e=emp.find_one({"emp_code":body.emp_code})
    if not e: raise HTTPException(404,"Unknown employee")
    pout=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    r=logs.find_one({"emp_code":body.emp_code,"punch_in":{"$lte":pout}},sort=[("punch_in",DESCENDING)])
    if not r: raise HTTPException(404,"No punch-in record found")
    if r.get("punch_out") is not None: raise HTTPException(409,"Already punched out")
    if pout <= r["punch_in"]: invalid_request("punched_at must be after punch_in")
    if pout > r["punch_in"]+timedelta(hours=24): invalid_request("Punch-out exceeds 24 hours")
    d=derived(r["status"],r["punch_in"],pout,e,r["date"])
    res=logs.update_one({"_id":r["_id"],"punch_out":None},{"$set":{"punch_out":pout,**d}})
    if res.modified_count != 1: raise HTTPException(409,"Attendance record changed")
    return out_record(logs.find_one({"_id":r["_id"]}))

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,
 status:Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]]=None,
 page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={}
    if emp_code is not None:q["emp_code"]=emp_code
    if date_from is not None:parse_date(date_from,"date_from");q.setdefault("date",{})["$gte"]=date_from
    if date_to is not None:parse_date(date_to,"date_to");q.setdefault("date",{})["$lte"]=date_to
    if date_from and date_to and date_from>date_to: invalid_request("date_from must be <= date_to")
    if status:q["status"]=status
    total=logs.count_documents(q)
    items=list(logs.find(q,{"_id":0}).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size))
    return {"items":[out_record(x) for x in items],"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:str,body:RegularizeRequest):
    parse_date(date)
    e=emp.find_one({"emp_code":emp_code})
    if not e:raise HTTPException(404,"Unknown employee")
    r=logs.find_one({"emp_code":emp_code,"date":date})
    if not r:raise HTTPException(404,"Attendance record not found")
    updates={}; changes={}
    for f in ("status","punch_in","punch_out"):
        if f not in body.model_fields_set:continue
        v=getattr(body,f)
        if f in ("punch_in","punch_out"):v=from_ms(v) if v is not None else None
        old=r.get(f)
        if old!=v:
            changes[f]={"from":old if f in ("punch_in","punch_out") else old,
                        "to":v if f in ("punch_in","punch_out") else v}
            updates[f]=v
    st=updates.get("status",r["status"]); pin=updates.get("punch_in",r.get("punch_in")); pout=updates.get("punch_out",r.get("punch_out"))
    if st in ("ABSENT","LEAVE"):
        if ("punch_in" in body.model_fields_set and body.punch_in is not None) or ("punch_out" in body.model_fields_set and body.punch_out is not None):
            invalid_request("ABSENT/LEAVE cannot be supplied with punch times")
        pin=pout=None;updates.update(punch_in=None,punch_out=None)
    elif pin is None: invalid_request("Presence status requires punch_in")
    if pin is not None and attendance_day(pin,e.get("shift_start","09:30"),e.get("shift_end","18:30"))!=date:
        invalid_request("punch_in must be on the attendance date")
    if pout is not None:
        if pin is None or pout<=pin: invalid_request("punch_out must be after punch_in")
        if pout>pin+timedelta(hours=24): invalid_request("Punch-out exceeds 24 hours")
    if st in ("ABSENT","LEAVE"):
        for field, old in (("punch_in", r.get("punch_in")), ("punch_out", r.get("punch_out"))):
            if old is not None:
                changes[field] = {"from": old, "to": None}
        updates.update(punch_in=None, punch_out=None)
    for f,v in derived(st,pin,pout,e,date).items():
        if r.get(f)!=v:changes[f]={"from":r.get(f),"to":v};updates[f]=v
    if not changes: invalid_request("Request changes no fields")
    history=r.get("history",[])
    updates["history"]=history+[{"at":now_utc(),"by":body.regularized_by,"reason":body.reason,"changes":changes}]
    history_filter = {"history": history} if "history" in r else {"history": {"$exists": False}}
    result=logs.update_one({"_id":r["_id"],**history_filter,"status":r.get("status"),"punch_in":r.get("punch_in"),"punch_out":r.get("punch_out")},{"$set":updates})
    if result.modified_count!=1:raise HTTPException(409,"Attendance record changed; retry")
    return out_record(logs.find_one({"_id":r["_id"]}))

def employee_monthly_pipeline(emp_code, start, end):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$set": {"_day": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", PRESENCE]},
                    {"$gt": [{"$dayOfWeek": "$_day"}, 1]},
                    {"$lt": [{"$dayOfWeek": "$_day"}, 7]}
                ]},
                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}
        }}
    ]

def department_summary_pipeline(start, end, department):
    match = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", start]}, {"$lte": ["$date", end]}
            ]}}}],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"_day": {"$cond": [
            {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
            {"$dateFromString": {"dateString": "$logs.date"}}, None
        ]}}},
        {"$group": {
            "_id": "$department",
            "employee_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", PRESENCE]},
                    {"$ne": ["$_day", None]},
                    {"$gt": [{"$dayOfWeek": "$_day"}, 1]},
                    {"$lt": [{"$dayOfWeek": "$_day"}, 7]}
                ]},
                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0
            ]}},
            "avg_hours": {"$avg": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", PRESENCE]},
                    {"$ne": ["$logs.work_hours", None]}
                ]}, "$logs.work_hours", None
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
        }},
        {"$project": {
            "_id": 0, "department": "$_id",
            "headcount": {"$size": "$employee_codes"},
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$eq": ["$avg_hours", None]}, None,
                mongo_half_up("$avg_hours", 2)
            ]},
            "late_count": 1, "total_late_minutes": 1,
            "leave_count": 1, "on_duty_count": 1
        }},
        {"$sort": {"department": 1}}
    ]

def late_leaderboard_pipeline(start, end, limit, department):
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}}
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1,
                      "total_late_minutes": 1, "late_count": 1}}
    ]
    return pipeline

def department_trend_pipeline(department, from_date, start, end):
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "_day": {"$dateFromString": {"dateString": from_date}}}},
        {"$densify": {"field": "_day", "range": {
            "step": 1, "unit": "day", "bounds": [
                datetime.combine(start, time.min, UTC),
                datetime.combine(end + timedelta(days=1), time.min, UTC)
            ]
        }}},
        {"$set": {"date": {"$dateToString": {"date": "$_day", "format": "%Y-%m-%d"}}}},
        {"$lookup": {
            "from": "employees", "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"}
            ], "as": "_hc"
        }},
        {"$lookup": {
            "from": "attendance_logs", "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "_e"}},
                {"$unwind": "$_e"},
                {"$match": {"_e.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", PRESENCE]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
                }}
            ], "as": "_s"
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$_hc.n", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$_s.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$_s.late_count", 0]}, 0]},
            "is_working_day": {"$and": [
                {"$gt": [{"$dayOfWeek": "$_day"}, 1]},
                {"$lt": [{"$dayOfWeek": "$_day"}, 7]}
            ]}
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mongo_half_up({"$divide": ["$present_count", "$headcount"]}, 4), None
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"_day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$moving_avg_7d", None]}, None, mongo_half_up("$moving_avg_7d", 4)
            ]}
        }},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = bounds(month)
    e = emp.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    if not e:
        raise HTTPException(404, "Unknown employee")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    first = max(first, date.fromisoformat(e["joined_on"]))
    wd = workdays(first, last) if first <= last else 0
    pipeline = employee_monthly_pipeline(emp_code, start, end)
    rows = list(logs.aggregate(pipeline))
    row = rows[0] if rows else {}
    present = float(row.get("present_days", 0))
    return {
        "emp_code": emp_code, "month": month, "working_days": wd,
        "present_days": present, "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(present / wd * 100, 4) if wd else None
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = bounds(month)
    pipeline = department_summary_pipeline(start, end, department)
    items = [x for x in emp.aggregate(pipeline) if x["headcount"] > 0]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = bounds(month)
    pipeline = late_leaderboard_pipeline(start, end, limit, department)
    return {"month": month, "items": list(logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start or (end - start).days + 1 > 92:
        invalid_request("Invalid date range: to must be >= from and range <= 92 days")
    if emp.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Unknown department")
    pipeline = department_trend_pipeline(department, from_date, start, end)
    return {"department": department, "items": list(emp.aggregate(pipeline))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code is not None: q["emp_code"] = emp_code
        if date_from is not None: parse_date(date_from, "date_from"); q.setdefault("date", {})["$gte"] = date_from
        if date_to is not None: parse_date(date_to, "date_to"); q.setdefault("date", {})["$lte"] = date_to
        if date_from and date_to and date_from > date_to: invalid_request("date_from must be <= date_to")
        if status: q["status"] = status
        explain = db.command({"explain": {"find": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size},
            "verbosity": "executionStats"})
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: invalid_request("emp_code and month are required")
        start, end = bounds(month)
        pipeline = employee_monthly_pipeline(emp_code, start, end)
        explain = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats"})
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month: invalid_request("month is required")
        start, end = bounds(month)
        pipeline = department_summary_pipeline(start, end, department)
        explain = db.command({"explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                              "verbosity": "executionStats"})
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month: invalid_request("month is required")
        start, end = bounds(month)
        pipeline = late_leaderboard_pipeline(start, end, limit, department)
        explain = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats"})
        collection = "attendance_logs"
    else:
        if not department or not from_date or not to_date: invalid_request("department, from and to are required")
        a, b = parse_date(from_date, "from"), parse_date(to_date, "to")
        if b < a or (b-a).days+1 > 92: invalid_request("Invalid trend date range")
        pipeline = department_trend_pipeline(department, from_date, a, b)
        explain = db.command({"explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                              "verbosity": "executionStats"})
        collection = "employees"
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(explain))}
