import pytest

from job_hunter.config import ROOT, load_candidate, load_preferences
from job_hunter.database import Database
from job_hunter.normalize import normalize_job


@pytest.fixture
def preferences():
    result = load_preferences(ROOT / "config/preferences.yaml")
    result.search.pause_seconds = 0
    return result


@pytest.fixture
def candidate():
    return load_candidate(ROOT / "config/candidate.example.yaml")


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "test.sqlite3") as database:
        yield database


@pytest.fixture
def make_job():
    def make(**changes):
        row = {"id": "li-123", "site": "linkedin", "title": "Junior QA Analyst", "company": "Example Tech",
               "location": "San José, Costa Rica", "description": "Manual testing, Python useful. 1-3 years experience.",
               "job_url": "https://www.linkedin.com/jobs/view/123?trk=search", "is_remote": True}
        row.update(changes)
        return normalize_job(row, "linkedin", "QA Analyst")
    return make
