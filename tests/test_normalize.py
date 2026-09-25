from datetime import date

import pandas as pd
import pytest

from job_hunter.normalize import normalize_job
from job_hunter.deduplicate import preferred_job_url


def test_nan_na_and_missing_fields():
    job = normalize_job({"id": pd.NA, "title": float("nan"), "description": pd.NA,
                         "date_posted": pd.NaT, "min_amount": float("nan"), "is_remote": pd.NA}, "indeed", "QA")
    assert job.title == "Unknown title"
    assert job.description == ""
    assert job.salary_min is None and job.date_posted is None and job.is_remote is None
    assert job.source_job_id is None


def test_normalizes_salary_date_html_location():
    job = normalize_job({"title": " QA Analyst ", "company": "Acme", "location": {"city": "Heredia", "country": "Costa Rica"},
                         "description": "<p>Testing &amp; QA</p><p>Python</p>", "date_posted": "2026-09-01",
                         "min_amount": "1200", "max_amount": "2000", "currency": "USD", "interval": "monthly",
                         "is_remote": "false"}, "indeed", "QA")
    assert job.title == "QA Analyst"
    assert job.location == "Heredia, Costa Rica"
    assert "Testing & QA\nPython" == job.description
    assert job.date_posted == date(2026, 9, 1)
    assert job.salary_min == 1200 and job.salary_interval == "monthly"
    assert job.is_remote is False


@pytest.mark.parametrize("value", ["nonsense", "", None, float("nan")])
def test_bad_optional_date(value):
    assert normalize_job({"date_posted": value}, "google", "QA").date_posted is None


def test_keep_direct_url(make_job):
    job = make_job(job_url_direct="https://careers.example.com/jobs/123")
    assert len(job.source_urls) == 2
    assert preferred_job_url(job) == "https://careers.example.com/jobs/123"
