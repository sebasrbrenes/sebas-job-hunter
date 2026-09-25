"""Posting age is separate from rediscovery time and semantic fit."""
from datetime import date, datetime, timedelta, timezone


def local_today() -> date:
    return datetime.now(timezone(timedelta(hours=-6))).date()


def classify_freshness(posted: date | None, today: date | None = None) -> str:
    if posted is None:
        return "unknown"
    age = ((today or local_today()) - posted).days
    if age < 0:
        return "unknown"
    return "today" if age == 0 else "1_3_days" if age <= 3 else "4_7_days" if age <= 7 else "older"


def freshness_bonus(posted: date | None) -> int:
    return {"today": 3, "1_3_days": 2, "4_7_days": 1}.get(classify_freshness(posted), 0)
