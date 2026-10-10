
"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
"""
import json
from bson import json_util
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# -------------------- MongoDB indexes --------------------

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="idx_employee_department_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="idx_employee_department_joined",
    )
    db.employees.create_index(
    [("joined_on", ASCENDING)],
    name="idx_employee_joined_on",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="idx_attendance_date_status",
    )
    db.attendance_logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING),
            ("status", ASCENDING),
        ],
        name="idx_attendance_employee_date_status",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING)],
        name="idx_attendance_date_late",
    )


# -------------------- Helper functions --------------------

def utc(value: datetime) -> datetime:
    """Convert a datetime to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def trunc(value: datetime) -> datetime:
    """Remove fractional seconds."""
    return value.replace(microsecond=0)


def from_ms(value: Optional[int]) -> Optional[datetime]:
    """Convert Unix epoch milliseconds to a UTC datetime."""
    if value is None:
        return None
    return datetime.fromtimestamp(
        value / 1000,
        tz=UTC,
    ).replace(microsecond=0)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    """Convert a datetime to Unix epoch milliseconds."""
    if value is None:
        return None
    return int(utc(trunc(value)).timestamp() * 1000)


def round_half_up(value, places=2) -> float:
    """Round using the required ROUND_HALF_UP rule."""
    decimal_value = Decimal(str(value))
    quantum = Decimal("1").scaleb(-places)
    return float(
        decimal_value.quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def parse_date(value: str, label="date") -> date:
    """Validate a strict YYYY-MM-DD date."""
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail=f"{label} must be YYYY-MM-DD",
        )

    if parsed.isoformat() != value:
        raise HTTPException(
            status_code=422,
            detail=f"{label} must be YYYY-MM-DD",
        )

    return parsed


def month_bounds(month: str):
    """Return the first and last dates of a YYYY-MM month."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(
            status_code=422,
            detail="month must be YYYY-MM",
        )

    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]

    return (
        f"{month}-01",
        f"{month}-{last_day:02d}",
        date(year, month_number, 1),
        date(year, month_number, last_day),
    )


def workdays(start: date, end: date) -> int:
    """Count Monday-Friday days inclusively."""
    if start > end:
        return 0

    count = 0
    current = start

    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)

    return count


def overnight(emp) -> bool:
    """Determine whether an employee's shift crosses midnight."""
    return emp["shift_end"] <= emp["shift_start"]


def shift_end_dt(day_str: str, emp) -> datetime:
    """Get the shift end datetime in UTC for an attendance date."""
    day = date.fromisoformat(day_str)

    if overnight(emp):
        day += timedelta(days=1)

    hour, minute = map(int, emp["shift_end"].split(":"))

    local_end = datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        tzinfo=IST,
    )

    return local_end.astimezone(UTC)


def attendance_day(punch: datetime, emp) -> str:
    """Determine the attendance date using IST and the employee's shift."""
    local_time = utc(trunc(punch)).astimezone(IST)
    day = local_time.date()

    if (
        overnight(emp)
        and local_time.strftime("%H:%M") < emp["shift_end"]
    ):
        day -= timedelta(days=1)

    return day.isoformat()


def late_minutes(
    punch: datetime,
    shift_start: str,
    shift_end: Optional[str] = None,
) -> int:
    """Calculate late minutes; lateness must be strictly over 10 minutes."""
    local_time = utc(trunc(punch)).astimezone(IST)

    hour, minute = map(int, shift_start.split(":"))

    shift_start_dt = local_time.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )

    if (
        shift_end is not None
        and shift_end <= shift_start
        and local_time.strftime("%H:%M") < shift_end
    ):
        shift_start_dt -= timedelta(days=1)

    elapsed_seconds = (
        local_time - shift_start_dt
    ).total_seconds()

    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)


def work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    """Calculate work duration in hours, rounded HALF_UP to two decimals."""
    seconds = (
        utc(trunc(punch_out)) - utc(trunc(punch_in))
    ).total_seconds()

    hours = Decimal(str(seconds)) / Decimal(3600)
    return round_half_up(hours, 2)


def overtime_minutes(
    punch_out: datetime,
    day_str: str,
    emp,
) -> int:
    """Count overtime only when it reaches at least 30 minutes."""
    shift_end = shift_end_dt(day_str, emp)

    minutes = int(
        (
            utc(trunc(punch_out)) - shift_end
        ).total_seconds() // 60
    )

    return minutes if minutes >= 30 else 0


def record_out(doc):
    """Convert an attendance document into the API response format."""
    history = []

    for entry in doc.get("history", []) or []:
        changes = {}

        for key, pair in (entry.get("changes", {}) or {}).items():
            old_value = pair.get("from")
            new_value = pair.get("to")

            if key in ("punch_in", "punch_out"):
                old_value = to_ms(old_value)
                new_value = to_ms(new_value)

            changes[key] = {
                "from": old_value,
                "to": new_value,
            }

        history.append({
            "at": to_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def present_fraction(doc) -> float:
    """Return 1.0 for a full present day, 0.5 for half-day, else 0."""
    if doc.get("status") not in PRESENCE:
        return 0.0

    return 0.5 if doc.get("half_day", False) else 1.0


def date_range_filter(date_from, date_to):
    """Build a MongoDB date-range filter."""
    query = {}

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            parse_date(date_from, "date_from")
            query["date"]["$gte"] = date_from

        if date_to:
            parse_date(date_to, "date_to")
            query["date"]["$lte"] = date_to

        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must be <= date_to",
            )

    return query


# -------------------- Request models --------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError(
                "shift_start and shift_end must differ"
            )
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = Field(
        default=None,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = Field(
        default=None,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )


class RegularizeIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None
    punch_in: Optional[int] = Field(
        default=None,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )
    punch_out: Optional[int] = Field(
        default=None,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

# -------------------- Health endpoint --------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable",
        )

    return {"status": "ok"}


# -------------------- Employee endpoints --------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    result = body.model_dump()
    result["created_at"] = to_ms(doc["created_at"])
    return result


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        item["created_at"] = to_ms(item.get("created_at"))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# -------------------- Punch-in endpoint --------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="employee not found",
        )

    punched_at = (
        from_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )

    attendance_date = attendance_day(punched_at, emp)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(
            punched_at,
            emp["shift_start"],
            emp["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return record_out(doc)


# -------------------- Punch-out endpoint --------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="employee not found",
        )

    punched_at = (
        from_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )

    attendance_date = attendance_day(punched_at, emp)

    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": attendance_date,
    })

    # For an overnight shift, a punch-out before the shift end
    # belongs to the previous attendance date.
    if record is None and overnight(emp):
        local_time = punched_at.astimezone(IST)
        previous_date = (local_time.date() - timedelta(days=1)).isoformat()

        record = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "date": previous_date,
        })

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="attendance record not found; punch in first",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="already punched out",
        )

    punch_in_time = record.get("punch_in")

    if punch_in_time is None:
        raise HTTPException(
            status_code=422,
            detail="attendance record has no punch-in time",
        )

    elapsed = (
        utc(trunc(punched_at)) - utc(trunc(punch_in_time))
    ).total_seconds()

    if elapsed <= 0 or elapsed > 86400:
        raise HTTPException(
            status_code=422,
            detail="punch-out must be after punch-in and within 24 hours",
        )

    hours = work_hours(punch_in_time, punched_at)
    overtime = overtime_minutes(
        punched_at,
        record["date"],
        emp,
    )

    updated = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": hours,
                "overtime_minutes": overtime,
                "half_day": hours < 4.50,
            }
        },
    )

    if updated.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="attendance record changed; please retry",
        )

    record["punch_out"] = punched_at
    record["work_hours"] = hours
    record["overtime_minutes"] = overtime
    record["half_day"] = hours < 4.50

    return record_out(record)

# -------------------- Attendance list endpoint --------------------


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = date_range_filter(date_from, date_to)

    if emp_code is not None:
        query["emp_code"] = emp_code

    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    documents = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [record_out(doc) for doc in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# -------------------- Attendance regularization --------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    parse_date(date, "date")

    emp = db.employees.find_one({"emp_code": emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="employee not found",
        )

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="attendance record not found",
        )

    request_data = body.model_dump(exclude_unset=True)
    reason = request_data.pop("reason")
    regularized_by = request_data.pop("regularized_by")

    status = request_data.get("status", record["status"])

    if status in ("ABSENT", "LEAVE"):
        if (
            request_data.get("punch_in") is not None
            or request_data.get("punch_out") is not None
        ):
            raise HTTPException(
                status_code=422,
                detail="ABSENT/LEAVE cannot have punch times",
            )

        pin = None
        pout = None
    else:
        pin_ms = request_data.get(
            "punch_in",
            to_ms(record.get("punch_in")),
        )
        pout_ms = request_data.get(
            "punch_out",
            to_ms(record.get("punch_out")),
        )

        if pin_ms is None:
            raise HTTPException(
                status_code=422,
                detail="presence status requires punch_in",
            )

        pin = from_ms(pin_ms)
        pout = from_ms(pout_ms) if pout_ms is not None else None

        if attendance_day(pin, emp) != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to attendance date",
            )

        if pout is not None:
            elapsed = (
                utc(pout) - utc(pin)
            ).total_seconds()

            if elapsed <= 0 or elapsed > 86400:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

    new_values = {
        "status": status,
        "punch_in": pin,
        "punch_out": pout,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }

    if status in PRESENCE:
        new_values["late_minutes"] = late_minutes(
            pin,
            emp["shift_start"],
            emp["shift_end"],
        )

        if pout is not None:
            new_values["work_hours"] = work_hours(pin, pout)
            new_values["overtime_minutes"] = overtime_minutes(
                pout,
                date,
                emp,
            )
            new_values["half_day"] = (
                new_values["work_hours"] < 4.50
            )

    changes = {}
    history_changes = {}

    for key, new_value in new_values.items():
        old_value = record.get(key)

        if key == "late_minutes":
            old_value = old_value or 0
        elif key == "overtime_minutes":
            old_value = old_value or 0
        elif key == "half_day":
            old_value = bool(old_value)

        if old_value != new_value:
            changes[key] = {
                "from": old_value,
                "to": new_value,
            }

            old_history_value = old_value
            new_history_value = new_value

            if key in ("punch_in", "punch_out"):
                old_history_value = (
                    utc(old_value) if old_value is not None else None
                )
                new_history_value = (
                    utc(new_value) if new_value is not None else None
                )

            history_changes[key] = {
                "from": old_history_value,
                "to": new_history_value,
            }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="request makes no changes",
        )

    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": regularized_by,
        "reason": reason,
        "changes": history_changes,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
        },
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="concurrent regularization conflict",
        )

    record.update(new_values)
    record["history"] = (
        record.get("history", []) or []
    ) + [history_entry]

    return record_out(record)


# -------------------- Employee monthly analytics --------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
):
    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="employee not found",
        )

    start_string, end_string, start_day, end_day = month_bounds(month)

    joined_date = parse_date(employee["joined_on"], "joined_on")
    effective_start = max(start_day, joined_date)

    working_days = (
        workdays(effective_start, end_day)
        if effective_start <= end_day
        else 0
    )

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_string,
                    "$lte": end_string,
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE)]},
                                    {
                                        "$lt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            7,
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            1,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {"$ifNull": ["$half_day", False]},
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$late_minutes", 0]},
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    result = results[0] if results else {}

    present_days = round_half_up(
        result.get("present_days", 0),
        2,
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": (
            round_half_up(present_days / working_days * 100, 2)
            if working_days
            else None
        ),
    }


# -------------------- Department summary --------------------

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start_string, end_string, start_day, end_day = month_bounds(month)

    employee_match = {"joined_on": {"$lte": end_string}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "code": "$emp_code",
                    "joined": "$joined_on",
                },
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": start_string,
                                "$lte": end_string,
                            },
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", "$$joined"]},
                                ]
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE)]},
                                                {
                                                    "$lt": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        7,
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        1,
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE)]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE)]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                                }
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$stats.present_days", 0]},
                            0,
                        ]
                    }
                },
                "hours_sum": {
                    "$sum": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$stats.hours_sum", 0]},
                            0,
                        ]
                    }
                },
                "hours_count": {
                    "$sum": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$stats.hours_count", 0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$stats.late_count", 0]},
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$stats.total_late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$stats.leave_count", 0]},
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$stats.on_duty_count", 0]},
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$hours_count", 0]},
                        {"$divide": ["$hours_sum", "$hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))

    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)

    return {"month": month, "items": items}


# -------------------- Late leaderboard --------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start_string, end_string, _, _ = month_bounds(month)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_string, "$lte": end_string},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = list(
        db.attendance_logs.aggregate(pipeline, allowDiskUse=True)
    )

    return {"month": month, "items": items}


# -------------------- Department trend --------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")

    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="invalid trend date range (maximum 92 days)",
        )

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(
            status_code=404,
            detail="unknown department",
        )

    start_string = start.isoformat()
    end_string = end.isoformat()

    # Aggregate attendance records by day.
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_string, "$lte": end_string}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENCE)]},
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "day": {
                    "$dateFromString": {"dateString": "$_id"}
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime(start.year, start.month, start.day),
                        datetime(
                            (end + timedelta(days=1)).year,
                            (end + timedelta(days=1)).month,
                            (end + timedelta(days=1)).day,
                        ),
                    ],
                },
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day",
                    }
                },
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$and": [
                        {"$gt": [{"$dayOfWeek": "$day"}, 1]},
                        {"$lt": [{"$dayOfWeek": "$day"}, 7]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_string": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_string"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(
        db.attendance_logs.aggregate(pipeline, allowDiskUse=True)
    )

    # Round values returned by MongoDB.
    for item in items:
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = round_half_up(
                item["attendance_rate"], 4
            )

        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = round_half_up(
                item["moving_avg_7d"], 4
            )

    return {"department": department, "items": items}


# -------------------- Admin explain endpoint --------------------


def explain_find(collection, query, sort=None, skip=0, limit=None):
    command = {
        "find": collection.name,
        "filter": query,
    }

    if sort:
        if isinstance(sort, (list, tuple)):
            sort = dict(sort)
        command["sort"] = sort

    if skip:
        command["skip"] = skip

    if limit is not None:
        command["limit"] = limit

    result = collection.database.command({
    "explain": command,
    "verbosity": "executionStats",
    })

    return json.loads(json_util.dumps(result))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query = date_range_filter(date_from, date_to)

        if emp_code is not None:
            query["emp_code"] = emp_code

        if status is not None:
            query["status"] = status

        result = explain_find(
            db.attendance_logs,
            query,
            [("date", -1), ("emp_code", 1)],
            (page - 1) * page_size,
            page_size,
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": result,
        }


    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        employee = db.employees.find_one({"emp_code": emp_code})
        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="employee not found",
            )

        start_string, end_string, _, _ = month_bounds(month)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": start_string,
                        "$lte": end_string,
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", list(PRESENCE)]},
                                        {
                                            "$lt": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                7,
                                            ]
                                        },
                                        {
                                            "$ne": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$date"
                                                        }
                                                    }
                                                },
                                                1,
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                {"$ifNull": ["$half_day", False]},
                                                True,
                                            ]
                                        },
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]

        result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        result = json.loads(json_util.dumps(result))

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": result,
        }


    if endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        start_string, end_string, _, _ = month_bounds(month)

        employee_match = {"joined_on": {"$lte": end_string}}
        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "code": "$emp_code",
                        "joined": "$joined_on",
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "date": {
                                    "$gte": start_string,
                                    "$lte": end_string,
                                },
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", "$$joined"]},
                                    ]
                                },
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_days": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", list(PRESENCE)]},
                                                    {
                                                        "$lt": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$date"
                                                                    }
                                                                }
                                                            },
                                                            7,
                                                        ]
                                                    },
                                                    {
                                                        "$ne": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$date"
                                                                    }
                                                                }
                                                            },
                                                            1,
                                                        ]
                                                    },
                                                ]
                                            },
                                            {
                                                "$cond": [
                                                    {
                                                        "$eq": [
                                                            {"$ifNull": ["$half_day", False]},
                                                            True,
                                                        ]
                                                    },
                                                    0.5,
                                                    1,
                                                ]
                                            },
                                            0,
                                        ]
                                    }
                                },
                                "hours_sum": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", list(PRESENCE)]},
                                                    {"$ne": ["$work_hours", None]},
                                                ]
                                            },
                                            "$work_hours",
                                            0,
                                        ]
                                    }
                                },
                                "hours_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", list(PRESENCE)]},
                                                    {"$ne": ["$work_hours", None]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "total_late_minutes": {
                                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                                },
                                "leave_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$eq": ["$status", "LEAVE"]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "on_duty_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$eq": ["$status", "ON_DUTY"]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "stats",
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {
                        "$sum": {
                            "$ifNull": [
                                {"$arrayElemAt": ["$stats.present_days", 0]},
                                0,
                            ]
                        }
                    },
                    "hours_sum": {
                        "$sum": {
                            "$ifNull": [
                                {"$arrayElemAt": ["$stats.hours_sum", 0]},
                                0,
                            ]
                        }
                    },
                    "hours_count": {
                        "$sum": {
                            "$ifNull": [
                                {"$arrayElemAt": ["$stats.hours_count", 0]},
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$ifNull": [
                                {"$arrayElemAt": ["$stats.late_count", 0]},
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$ifNull": [
                                {"$arrayElemAt": ["$stats.total_late_minutes", 0]},
                                0,
                            ]
                        }
                    },
                    "leave_count": {
                        "$sum": {
                            "$ifNull": [
                                {"$arrayElemAt": ["$stats.leave_count", 0]},
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$ifNull": [
                                {"$arrayElemAt": ["$stats.on_duty_count", 0]},
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$hours_count", 0]},
                            {"$divide": ["$hours_sum", "$hours_count"]},
                            None,
                        ]
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]

        result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
                "allowDiskUse": True,
            },
            verbosity="executionStats",
        )
        result = json.loads(json_util.dumps(result))

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": result,
        }


    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        start_string, end_string, _, _ = month_bounds(month)

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": start_string,
                        "$lte": end_string,
                    },
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department is not None:
            pipeline.append(
                {"$match": {"employee.department": department}}
            )

        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {
                    "$project": {
                        "_id": 0,
                        "rank": 1,
                        "emp_code": "$_id",
                        "name": "$employee.name",
                        "department": "$employee.department",
                        "total_late_minutes": 1,
                        "late_count": 1,
                    }
                },
                {
                    "$sort": {
                        "total_late_minutes": -1,
                        "emp_code": 1,
                    }
                },
            ]
        )

        result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
                "allowDiskUse": True,
            },
            verbosity="executionStats",
        )
        result = json.loads(json_util.dumps(result))

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": result,
        }




    if endpoint == "department_trend":
        if not department or not date_from or not date_to:
            raise HTTPException(
                status_code=422,
                detail="department, date_from, and date_to are required",
            )

        start = parse_date(date_from, "date_from")
        end = parse_date(date_to, "date_to")

        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="invalid trend date range",
            )

        start_string = start.isoformat()
        end_string = end.isoformat()
        end_exclusive = (end + timedelta(days=1)).isoformat()

        trend_pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": start_string,
                        "$lte": end_string,
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$status",
                                        list(PRESENCE),
                                    ]
                                },
                                {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                {"$ifNull": ["$half_day", False]},
                                                True,
                                            ]
                                        },
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gt": [
                                        {"$ifNull": ["$late_minutes", 0]},
                                        0,
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": {
                        "$dateFromString": {
                            "dateString": "$_id",
                        }
                    },
                    "present_count": 1,
                    "late_count": 1,
                }
            },
         {
                "$densify": {
                    "field": "date",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [
                            datetime.combine(start,time.min),
                            datetime.combine(end + timedelta(days=1), time.min),
                        ]
                    },
                }
            },
            {
                "$set": {
                    "date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$date",
                            "timezone": "+05:30",
                        }
                    },
                    "present_count": {"$ifNull": ["$present_count", 0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                    "is_working_day": {
                        "$and": [
                            {
                                "$ne": [
                                    {"$dayOfWeek": "$date"},
                                    1,
                                ]
                            },
                            {
                                "$ne": [
                                    {"$dayOfWeek": "$date"},
                                    7,
                                ]
                            },
                        ]
                    },
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"trend_date": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "department": department,
                                "$expr": {
                                    "$lte": [
                                        "$joined_on",
                                        "$$trend_date",
                                    ]
                                },
                            }
                        },
                        {"$count": "headcount"},
                    ],
                    "as": "headcount_result",
                }
            },
            {
                "$set": {
                    "headcount": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$headcount_result.headcount",
                                    0,
                                ]
                            },
                            0,
                        ]
                    }
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {
                                "$multiply": [
                                    {
                                        "$divide": [
                                            "$present_count",
                                            "$headcount",
                                        ]
                                    },
                                    100,
                                ]
                            },
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, 0],
                            },
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
            {"$sort": {"date": 1}},
        ]

        result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": trend_pipeline,
                "cursor": {},
                "allowDiskUse": True,
            },
            verbosity="executionStats",
        )
        result = json.loads(json_util.dumps(result))

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": result,
        }
