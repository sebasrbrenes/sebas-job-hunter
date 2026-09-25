import os
import sqlite3

import pytest

from job_hunter.database import Database
from job_hunter.cli import main
from job_hunter.deduplicate import merge_jobs
from job_hunter.geography import eligibility
from job_hunter.models import Category, Job, Recommendation
from job_hunter.official_sources import (CompanySource, OfficialATSProvider,
    normalize_smartrecruiters, normalize_workday)
from job_hunter.report import generate_report
from job_hunter.search import SearchResult, run_search


def test_smartrecruiters_normalization():
    summary = {"id": "123", "name": "QA Analyst", "releasedDate": "2026-09-19T10:00:00Z",
               "company": {"identifier": "Acme"}, "location": {"fullLocation": "Heredia, Costa Rica", "hybrid": True}}
    detail = {**summary, "jobAd": {"sections": {"jobDescription": {"text": "<p>Test releases</p>"},
                                                   "qualifications": {"text": "API testing"}}},
              "typeOfEmployment": {"label": "Full-time"}}
    row = normalize_smartrecruiters(summary, detail, "Acme")
    assert row["id"] == "123"
    assert row["source_kind"] == "official"
    assert row["work_arrangement"] == "hybrid"
    assert "Test releases" in row["description"] and "API testing" in row["description"]
    assert row["job_url"].startswith("https://jobs.smartrecruiters.com/Acme/123")


def test_workday_normalization():
    source = CompanySource("acme", "Acme", "workday", True, "integrated", "https://example.com",
                           {"domain": "acme.wd.test", "tenant": "acme", "site": "External", "locale": "en-US"})
    summary = {"title": "SOC Analyst I", "externalPath": "/job/San-Jose/SOC-Analyst_R1",
               "locationsText": "San Jose, Costa Rica", "postedOn": "Posted 2 Days Ago", "bulletFields": ["R1"]}
    detail = {"jobPostingInfo": {"title": "SOC Analyst I", "location": "San Jose, Costa Rica",
                                  "jobDescription": "<p>Monitor security alerts.</p>"}}
    row = normalize_workday(summary, detail, source)
    assert row["id"] == "R1"
    assert row["job_url"] == "https://acme.wd.test/en-US/External/job/San-Jose/SOC-Analyst_R1"
    assert row["description"] == "Monitor security alerts."
    assert row["source_kind"] == "official"


def test_provider_error_does_not_stop_pipeline(db, preferences):
    preferences.search.providers = {"linkedin": True, "indeed": True}
    preferences.search.sources = ["linkedin", "indeed"]
    class PartialProvider:
        def search(self, source, query, config):
            if source == "linkedin":
                raise RuntimeError("blocked")
            return SearchResult([{"id": "ok", "title": "QA Analyst", "company": "Acme",
                                  "location": "Costa Rica", "job_url": "https://example.com/jobs/ok"}])
    result = run_search(db, preferences, PartialProvider(), queries=["QA Analyst"])
    assert result["status"] == "completed_with_warnings"
    assert result["unique"] == 1 and result["provider_failures"] == 1


def test_deduplication_prefers_official_primary_url(make_job):
    discovery = make_job(company="Acme", job_url="https://linkedin.com/jobs/view/123", source_kind="discovery")
    official = make_job(site="greenhouse:acme", company="Acme", id="gh-1",
                        job_url="https://boards.greenhouse.io/acme/jobs/1", source_kind="official")
    merged = merge_jobs(discovery, official)
    assert merged.source_kind == "official"
    assert merged.source == "greenhouse:acme"
    assert merged.job_url == "https://boards.greenhouse.io/acme/jobs/1"


@pytest.mark.parametrize(("location", "description", "expected"), [
    ("Heredia, Costa Rica", "", "eligible"),
    ("Remote LATAM", "Candidates across Latin America", "eligible"),
    ("Remote", "Must reside in the United States", "ineligible"),
    ("Remote", "Distributed team", "unknown"),
])
def test_geographic_eligibility(location, description, expected):
    assert eligibility(location, description)[0] == expected


def test_tracker_duplicate_update_notes_and_report(tmp_path):
    path = tmp_path / "existing.sqlite3"
    # An old database with only the original jobs table must migrate in place.
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload)))")
    job = Job(id="job-1", title="QA Analyst", company="Acme", location="Costa Rica",
              job_url="https://example.com/jobs/1", codex_score=75, score_context_hash="ctx",
              recommendation=Recommendation.APPLY, category=Category.QA)
    connection.execute("INSERT INTO jobs VALUES (?,?)", (job.id, job.model_dump_json()))
    connection.commit()
    connection.close()
    with Database(path) as db:
        application = db.add_application("job-1", "2026-09-19", notes="Submitted on careers site")
        assert application["status"] == "Applied" and application["fit_score"] == 75
        with pytest.raises(ValueError, match="already exists"):
            db.add_application("job-1", "2026-09-19")
        db.update_application("job-1", "Interview")
        updated = db.update_application("job-1", notes="Screen scheduled", append_notes=True)
        assert updated["status"] == "Interview" and "Screen scheduled" in updated["notes"]
        report = generate_report(db, tmp_path / "reports", "ctx", all_jobs=True).read_text(encoding="utf-8")
        assert "## Applications" in report and "Interview" in report and "Geographic eligibility" in report


def test_tracker_cli_roundtrip(tmp_path, capsys):
    path = tmp_path / "tracker.sqlite3"
    with Database(path) as db:
        db.save(Job(id="job-cli", title="QA Analyst", company="Acme", location="Costa Rica",
                    job_url="https://example.com/jobs/cli", codex_score=72))
    prefix = ["--db", str(path)]
    assert main([*prefix, "application-add", "job-cli", "--date", "2026-09-22"]) == 0
    assert main([*prefix, "application-update", "job-cli", "--status", "Interview"]) == 0
    assert main([*prefix, "application-note", "job-cli", "Screen scheduled"]) == 0
    assert main([*prefix, "application-list"]) == 0
    output = capsys.readouterr().out
    assert '"status": "Interview"' in output and "Screen scheduled" in output


@pytest.mark.smoke
@pytest.mark.skipif(os.getenv("JOB_HUNTER_LIVE_TESTS") != "1", reason="set JOB_HUNTER_LIVE_TESTS=1")
def test_live_smartrecruiters_public_source():
    source = CompanySource("experian", "Experian", "smartrecruiters", True, "integrated",
                           "https://jobs.experian.com/jobs", {"identifier": "Experian"})
    from job_hunter.config import SearchConfig
    config = SearchConfig(sources=["linkedin"], query_groups={"qa": ["QA"]}, results_per_query=1)
    result = OfficialATSProvider([source]).search(source.source, "QA", config)
    assert result.rows and result.rows[0]["source_kind"] == "official"
