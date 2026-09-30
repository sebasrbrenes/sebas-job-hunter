import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import requests

from job_hunter.ats_discovery import FeedClient, import_urls, parse_url, queries, read_input
from job_hunter.database import Database
from job_hunter.freshness import classify_freshness
from job_hunter.geography import eligibility
from job_hunter.models import Job
from job_hunter.normalize import normalize_job
from job_hunter.official_sources import normalize_ashby, normalize_greenhouse, normalize_lever
from job_hunter.prefilter import prefilter
from job_hunter.scoring import export_scores, import_scores
from job_hunter.report import generate_report


@pytest.mark.parametrize("url", ["http://jobs.lever.co/acme/123", "https://jobs.lever.co.evil.test/acme/123",
    "https://user:pass@jobs.lever.co/acme/123", "https://jobs.lever.co:443/acme/123",
    "https://jobs.lever.co/acme", "https://jobs.ashbyhq.com/acme/../123",
    "https://boards.greenhouse.io/acme/jobs/foo", "https://jobs.ashbyhq.com/%2facme/123"])
def test_reject_unsafe_or_general_url(url):
    with pytest.raises(ValueError):
        parse_url(url)


def test_queries_bounded_by_tracks(preferences):
    result = queries(preferences)
    assert len(result) == len(preferences.search.query_groups) * 4
    assert {row["track"] for row in result} == set(preferences.search.query_groups)
    assert all("site:jobs.ashbyhq.com" in row["query"] for row in result)


def test_updated_and_republished_are_not_original_dates():
    gh = normalize_job(normalize_greenhouse({"id": 1, "updated_at": "2026-09-30T10:00:00Z"}, "Acme"), "greenhouse:acme", "")
    ashby = normalize_job(normalize_ashby({"jobUrl": "https://jobs.ashbyhq.com/acme/1", "publishedAt": "2026-09-30T10:00:00Z"}, "Acme"), "ashby:acme", "")
    assert gh.date_posted is None and ashby.date_posted is None
    assert gh.date_evidence[0]["type"] == "ats_updated"
    assert ashby.date_evidence[0]["type"] == "ats_last_published"
    assert "additional" in normalize_lever({"lists": [], "additional": "additional"}, "Acme")["description"]


@pytest.mark.parametrize("hours,expected", [(20, "today"), (24, "today"), (60, "1_3_days"),
    (72, "1_3_days"), (120, "4_7_days"), (168, "4_7_days"), (169, "older"), (-1, "unknown")])
def test_timestamp_bands(hours, expected):
    now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    assert classify_freshness(now - timedelta(hours=hours), now) == expected


@pytest.mark.parametrize("title,excluded", [("Senior QA Manager", True), ("QA Analyst reporting to senior manager", False), ("QA Analyst", False)])
def test_seniority(preferences, title, excluded):
    job = Job(title=title, location="Costa Rica")
    assert prefilter(job, preferences).prefilter_excluded == excluded


def test_us_restriction_precedes_local_location():
    assert eligibility("Costa Rica", "Remote, US only")[0] == "ineligible"
    assert eligibility("Remote", "Must be authorized to work in the United States")[0] == "ineligible"
    assert eligibility("Remote", "Distributed team")[0] == "unknown"


def test_three_source_dedup_preserves_identity(db, preferences, tmp_path):
    linkedin = normalize_job({"id": "li-1", "company": "Acme", "title": "QA Analyst", "location": "Costa Rica",
                              "description": "Testing applications", "job_url": "https://linkedin.com/jobs/view/123"}, "linkedin", "QA")
    existing, _ = db.upsert(linkedin)
    result = import_urls(db, preferences, input_file(tmp_path, ["https://boards.greenhouse.io/acme/jobs/1"]), client=FakeClient())
    assert result["new"] == 0 and len(db.jobs()) == 1
    job = db.get(existing.id)
    assert {r.source for r in job.source_urls} == {"linkedin", "greenhouse:acme", "google_manual"}
    assert job.source_kind == "official" and job.job_url == "https://boards.greenhouse.io/acme/jobs/1"


def test_ashby_adapter_contract_and_secondary_locations():
    from job_hunter.official_sources import OfficialATSProvider, CompanySource
    from job_hunter.config import SearchConfig
    from unittest.mock import patch
    source = CompanySource("acme", "Acme", "ashby", True, "integrated", "https://jobs.ashbyhq.com/acme", {"board": "acme"})
    item = {"title": "QA Analyst", "jobUrl": "https://jobs.ashbyhq.com/acme/1", "descriptionPlain": "Test applications",
            "location": "Remote LATAM", "isRemote": True, "workplaceType": "Remote", "publishedAt": "2026-09-30T12:00:00Z"}
    with patch('job_hunter.official_sources._json', return_value={"apiVersion": "1", "jobs": [item]}):
        result = OfficialATSProvider([source]).search(source.source, "QA Analyst", SearchConfig(query_groups={"qa": ["QA"]}))
    assert len(result.rows) == 1 and result.rows[0]["company"] == "Acme"
    assert result.rows[0]["date_posted"] is None


def test_timeout_continues(db, preferences, tmp_path):
    class Client(FakeClient):
        def get(self, url):
            if "jobs/2" in url:
                raise requests.Timeout("sensitive URL omitted")
            return super().get(url)
    result = import_urls(db, preferences, input_file(tmp_path, ["https://boards.greenhouse.io/acme/jobs/2", "https://boards.greenhouse.io/acme/jobs/1"]), client=Client())
    assert result["unknown"] == result["resolved"] == 1
    assert db.run_summary()["attempts"][0]["error"] == "Timeout"


class FakeClient:
    def get(self, url):
        if "jobs/404" in url:
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError(response=response)
        return {"id": 1, "company_name": "Acme", "title": "QA Analyst", "location": {"name": "Costa Rica"},
                "content": "<p>Testing applications</p>", "updated_at": "2026-09-30T12:00:00Z",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"}


def input_file(tmp_path, urls):
    path = tmp_path / "urls.json"
    path.write_text(json.dumps([{"url": url} for url in urls]), encoding="utf-8")
    return path


def test_import_reimport_scores_and_report(db, preferences, tmp_path):
    path = input_file(tmp_path, ["https://boards.greenhouse.io/acme/jobs/1?utm_source=google"])
    first = import_urls(db, preferences, path, client=FakeClient())
    assert first["new"] == first["unique"] == 1
    job = db.jobs()[0]
    first_seen = job.first_seen_at
    exported = export_scores(db, {}, preferences, tmp_path / "export.json")
    score_file = tmp_path / "scores.json"
    score_file.write_text(json.dumps({"schema_version": 1, "export_id": exported["export_id"], "scores": [{
        "job_id": job.id, "score": 72, "recommendation": "apply", "category": "qa", "strengths": [],
        "gaps": [], "dealbreakers": [], "reasoning": "Fixture assessment"}]}), encoding="utf-8")
    assert import_scores(db, score_file, {}, preferences) == 1
    second = import_urls(db, preferences, path, client=FakeClient())
    assert second["new"] == 0 and len(db.jobs()) == 1
    saved = db.jobs()[0]
    assert saved.first_seen_at == first_seen and saved.codex_score == 72
    assert {r.source for r in saved.source_urls} == {"greenhouse:acme", "google_manual"}
    report = generate_report(db, tmp_path / "reports", saved.score_context_hash).read_text(encoding="utf-8")
    assert "google" in report and "ats" in report and "unknown" in report


def test_failures_continue_without_snippet_jobs(db, preferences, tmp_path):
    path = input_file(tmp_path, ["https://boards.greenhouse.io/acme/jobs/404", "https://boards.greenhouse.io/acme/jobs/1"])
    result = import_urls(db, preferences, path, client=FakeClient())
    assert result["unknown"] == 1 and result["resolved"] == 1
    assert len(db.jobs()) == 1 and db.run_summary()["status"] == "completed_with_warnings"
    review = db.connection.execute("SELECT status,reason FROM ats_candidates WHERE url LIKE '%404'").fetchone()
    assert tuple(review) == ("review", "HTTP 404")


def test_unmapped_ashby_stays_review(db, preferences, tmp_path):
    class Client:
        def get(self, url):
            return {"jobs": [{"title": "QA Analyst", "jobUrl": "https://jobs.ashbyhq.com/acme/1", "descriptionPlain": "Test"}]}
    result = import_urls(db, preferences, input_file(tmp_path, ["https://jobs.ashbyhq.com/acme/1"]), client=Client())
    assert result["unknown"] == 1 and not db.jobs()


def test_migration_backup_and_idempotency(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    from datetime import date
    with Database(path) as db:
        db.save(Job(id="legacy", source="greenhouse:acme", date_posted=date(2026, 9, 29), codex_score=72))
        result = db.migrate_ats_dates(path)
        assert result["reclassified"] == 1
        assert db.get("legacy").date_posted is None and db.get("legacy").codex_score == 72
        with sqlite3.connect(result["backup"]) as backup:
            assert '2026-09-29' in backup.execute("SELECT payload FROM jobs").fetchone()[0]
        assert db.migrate_ats_dates(path) == {"reclassified": 0}


def test_input_validation_before_mutation(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text('[{"url":"https://jobs.lever.co/a/b","discovered_at":"2026-09-30"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="timezone"):
        read_input(path, 40)


def test_cli_csv_and_query_commands(tmp_path, monkeypatch, capsys):
    from job_hunter.cli import main
    from job_hunter.config import ROOT
    monkeypatch.setattr(FeedClient, "get", FakeClient().get)
    path = tmp_path / "urls.csv"
    path.write_text('url,query,discovered_at,google_window,notes\nhttps://boards.greenhouse.io/acme/jobs/1,QA,,week,\n', encoding="utf-8")
    prefix = ["--db", str(tmp_path / "db.sqlite3"), "--candidate", str(ROOT / "config/candidate.example.yaml")]
    assert main([*prefix, "ats-queries"]) == 0
    assert main([*prefix, "import-ats-urls", "--input", str(path), "--output", str(tmp_path / "export.json")]) == 0
    exported = json.loads((tmp_path / "export.json").read_text(encoding="utf-8"))
    assert len(exported["jobs"]) == 1 and exported["jobs"][0]["date_posted"] is None
    assert '"coverage"' in capsys.readouterr().out


def test_insufficient_description_not_shortlisted(db, preferences, tmp_path):
    class Client(FakeClient):
        def get(self, url):
            item = super().get(url)
            item["content"] = ""
            return item
    result = import_urls(db, preferences, input_file(tmp_path, ["https://boards.greenhouse.io/acme/jobs/1"]), client=Client())
    assert result["unknown"] == 1 and not db.jobs()


@pytest.mark.parametrize("status", [403, 404, 429, 500, 302])
def test_http_failures_bounded_no_redirects(monkeypatch, status):
    calls = []
    class Response:
        status_code = status
        headers = {"Retry-After": "0"}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError("fixture")
    def get(url, **kwargs):
        calls.append(kwargs)
        return Response()
    monkeypatch.setattr(requests, "get", get)
    with pytest.raises((ValueError, requests.HTTPError)):
        FeedClient(pause=0).get("https://api.lever.co/v0/postings/acme/1?mode=json")
    assert len(calls) == (2 if status in {429, 500} else 1)
    assert all(call["allow_redirects"] is False for call in calls)
