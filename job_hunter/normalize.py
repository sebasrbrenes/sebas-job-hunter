"""Convert JobSpy records (including pandas NA/NaN) into canonical jobs."""

import math
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any

from .deduplicate import clean_url, stable_id
from .models import Job, SourceReference, utcnow


def clean_scalar(value: Any) -> Any:
    if value is None:
        return None
    # pandas is already a required transitive dependency of JobSpy.
    import pandas as pd
    if not isinstance(value, (dict, list, tuple)) and pd.isna(value):
        return None
    return value


def text_value(value: Any, default: str = "") -> str:
    value = clean_scalar(value)
    return str(value).strip() if value is not None else default


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def description_text(value: Any) -> str:
    value = text_value(value)
    if any(tag in value.lower() for tag in ("<p", "<div", "<br", "<li", "<span")):
        parser = PlainText()
        parser.feed(value)
        return unescape("".join(parser.parts)).strip()
    return unescape(value)


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def posted_date(value: Any) -> date | None:
    value = clean_scalar(value)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def normalize_job(row: dict, source: str, query: str) -> Job:
    actual_source = text_value(row.get("site"), source).lower()
    sid = text_value(row.get("id")) or text_value(row.get("source_job_id")) or None
    url = clean_url(text_value(row.get("job_url")))
    direct_url = clean_url(text_value(row.get("job_url_direct")))
    remote = text_value(row.get("is_remote")).lower()
    arrangement = text_value(row.get("work_arrangement")).lower()
    if arrangement not in {"remote", "hybrid", "onsite", "unknown"}:
        arrangement = "unknown"
    if arrangement == "unknown" and remote in {"true", "1", "1.0", "yes"}:
        arrangement = "remote"
    location = row.get("location")
    if isinstance(location, dict):
        location = ", ".join(text_value(location.get(k)) for k in ("city", "state", "country") if clean_scalar(location.get(k)))
    now = utcnow()
    job = Job(
        source=actual_source, source_job_id=sid,
        source_kind=row.get("source_kind", "discovery"),
        work_arrangement=arrangement or "unknown",
        title=text_value(row.get("title")) or "Unknown title",
        company=text_value(row.get("company")) or "Unknown company",
        location=text_value(location) or "Unknown location",
        job_url=url or direct_url, description=description_text(row.get("description")), description_source=actual_source,
        date_posted=posted_date(row.get("date_posted")),
        salary_min=number(row.get("min_amount")), salary_max=number(row.get("max_amount")),
        salary_currency=text_value(row.get("currency")) or None,
        salary_interval=text_value(row.get("interval")) or None,
        is_remote=True if remote in {"true", "1", "1.0", "yes"} else False if remote in {"false", "0", "0.0", "no"} else None,
        employment_type=text_value(row.get("job_type")) or None,
        search_query=query, search_queries=[query], first_seen_at=now, last_seen_at=now,
        source_urls=[SourceReference(source=actual_source, source_job_id=sid, url=url or direct_url)],
    )
    if direct_url and direct_url != url:
        job.source_urls.append(SourceReference(source=actual_source, url=direct_url))
    job.id = stable_id(job)
    return job
