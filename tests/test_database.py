from datetime import timedelta

import pytest

from job_hunter.database import Database
from job_hunter.models import Job


def test_second_run_dedup_history_and_persistence(db, make_job):
    run1 = db.start_run({})
    first, created = db.upsert(make_job(), run1)
    duplicate, created_twice = db.upsert(make_job(), run1)
    assert created and not created_twice and duplicate.is_new
    db.finish_run(run1, "completed")
    run2 = db.start_run({})
    later = make_job()
    later.last_seen_at = first.last_seen_at + timedelta(hours=2)
    second, created = db.upsert(later, run2)
    assert not created and not second.is_new
    assert second.first_seen_at == first.first_seen_at
    assert second.last_seen_at > first.last_seen_at
    assert len(db.jobs()) == 1
    assert db.run_summary(run1)["new"] == 1
    assert db.run_summary(run2)["new"] == 0
    path = db.connection.execute("PRAGMA database_list").fetchone()[2]
    with Database(path) as reopened:
        assert reopened.get(first.id).last_seen_at == second.last_seen_at


def test_cross_board_preserves_urls_queries_and_richer_description(db, make_job):
    first, _ = db.upsert(make_job())
    second = make_job(site="indeed", id="in-42", job_url="https://indeed.com/viewjob?jk=42", description=None)
    second.search_queries = ["Software Quality Assurance"]
    result, created = db.upsert(second)
    assert not created and result.id == first.id
    assert len(result.source_urls) == 2
    assert result.description == first.description
    assert len(result.search_queries) == 2


def test_same_source_id_with_changed_location_stays_one_job(db, make_job):
    first, _ = db.upsert(make_job())
    second, created = db.upsert(make_job(location="Heredia, Costa Rica"))
    assert not created and first.id == second.id and len(db.jobs()) == 1


def test_unidentifiable_row_rejected(db):
    with pytest.raises(ValueError, match="lacks"):
        db.upsert(Job())
    assert db.jobs() == []


def test_bridge_legacy_records_preserves_history(db, make_job):
    first = make_job(company="360training", location="Heredia, Heredia, Costa Rica")
    second = make_job(company="360training.com", location="Heredia, H, CR", site="indeed", id="in-42", job_url="https://indeed.com/42")
    run = db.start_run({})
    db.upsert(first, run)
    # Simulate records created before domain/province normalization was added.
    second.id = "legacy-record"
    with db.connection:
        db._save(second)
        db.connection.execute("INSERT INTO job_keys VALUES (?,?)", ("source:indeed:in-42", second.id))
        db.connection.execute("INSERT INTO run_jobs VALUES (?,?,?)", (run, second.id, 1))
    merged, created = db.upsert(second, run)
    assert not created and len(db.jobs()) == 1
    assert len(merged.source_urls) == 2
    assert db.run_summary(run)["unique"] == db.run_summary(run)["new"] == 1


def test_latest_run_when_windows_clock_timestamps_tie(db, monkeypatch):
    from job_hunter.models import utcnow
    timestamp = utcnow()
    monkeypatch.setattr("job_hunter.database.utcnow", lambda: timestamp)
    first = db.start_run({})
    second = db.start_run({})
    assert db.run_summary()["id"] == second != first
