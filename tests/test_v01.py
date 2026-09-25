import json
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from job_hunter.availability import Page, assess_page, verify_job, public_url
from job_hunter.config import SearchConfig
from job_hunter.experience import experience_minima
from job_hunter.freshness import classify_freshness
from job_hunter.prefilter import prefilter
from job_hunter.scoring import export_scores
from job_hunter.search import SearchResult, search_plan, run_search


@pytest.mark.parametrize("text,expected", [
    ("1 año de experiencia", [1]), ("1-2 años", [1]), ("1 a 3 años", [1]),
    ("entre 1 y 3 años", [1]), ("mínimo 2 años", [2]), ("al menos 2 años", [2]),
    ("más de 5 años", [5]), ("5+ años", [5]), ("cinco años de experiencia", [5]),
    ("1-5 años de experiencia", [1]), ("1 - 5 años de experiencia", [1]),
    ("Empresa fundada hace cinco años", []), ("5 años de experiencia deseable", []),
    ("Mínimo cinco años de experiencia. Entre 1 y 3 años con Python.", [5, 1]),
])
def test_spanish_experience(text, expected):
    assert experience_minima(text) == expected


@pytest.mark.parametrize("age,expected", [(0, "today"), (1, "1_3_days"), (3, "1_3_days"), (4, "4_7_days"), (7, "4_7_days"), (8, "older"), (-1, "unknown")])
def test_freshness(age, expected):
    today = date(2026, 9, 9)
    assert classify_freshness(today - timedelta(days=age), today) == expected
    assert classify_freshness(None, today) == "unknown"


def test_freshness_does_not_dominate_fit(make_job, preferences):
    from job_hunter.freshness import local_today
    good = make_job(date_posted=local_today() - timedelta(days=5))
    bad = make_job(title="Senior Marketing Director", date_posted=local_today())
    assert prefilter(good, preferences).prefilter_score > prefilter(bad, preferences).prefilter_score + 30


def test_provider_configuration_and_legacy():
    assert SearchConfig(providers={"linkedin": True, "google": False}, query_groups={}).sources == ["linkedin"]
    assert SearchConfig(sources=["indeed"], query_groups={}).sources == ["indeed"]
    with pytest.raises(ValueError, match="at least one"):
        SearchConfig(providers={"google": False}, query_groups={})


def test_bounded_geography_and_alias_attribution(preferences):
    config = preferences.search
    plan = search_plan(config)
    assert len(plan) == sum(len(v) for v in config.query_groups.values()) + 8
    assert {p["location"] for p in plan} == {"Costa Rica", "San Jose, Costa Rica", "Heredia, Costa Rica"}
    quick = search_plan(config, quick=True)
    assert len(quick) == 4 and all(p["location"] == "Costa Rica" for p in quick)
    custom = search_plan(config, ["Manual QA", "Software Development Engineer in Test", "Junior Security Analyst", "Application Support Engineer"])
    assert [p["track"] for p in custom] == ["qa", "automation", "cybersecurity", "support"]


class TrackProvider:
    def __init__(self):
        self.calls = []

    def search(self, source, query, config):
        self.calls.append((source, query, config.location))
        if source == "google":
            return SearchResult([], ["initial cursor not found"])
        if source == "glassdoor":
            raise RuntimeError("Unsupported country")
        return SearchResult([{"id": "shared", "title": "QA Analyst", "company": "Track fixture",
                              "location": "Cartago, Costa Rica", "description": "Manual testing. Python useful.",
                              "job_url": "https://example.com/jobs/shared"}])


def test_failure_circuit_tracks_telemetry_and_repeats(db, candidate, preferences, tmp_path):
    preferences.search.sources = ["google", "glassdoor", "linkedin"]
    provider = TrackProvider()
    first = run_search(db, preferences, provider, quick=True)
    assert len(provider.calls) == 6  # Broken boards once, LinkedIn for every track.
    assert first["provider_failures"] == 2 and first["skipped_attempts"] == 6
    assert first["unique"] == 1 and first["duplicates_removed"] == 3
    job = db.jobs()[0]
    assert set(job.discovery_tracks) == {"qa", "automation", "cybersecurity", "support"}
    assert job.location == "Cartago, Costa Rica"  # Query region does not reject results.
    assert all(value["unique"] == 1 for value in first["by_track"].values())
    export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    assert db.run_summary()["exported"] == 1
    second = run_search(db, preferences, provider, quick=True)
    assert second["new"] == 0 and second["previously_known"] == 1
    assert len(provider.calls) == 12  # Next run retries enabled boards.
    assert db.run_summary(first["id"])["by_track"]["qa"]["new"] == 1


DESCRIPTION = """We are seeking a QA Automation Engineer to test authentication flows.
Maintain Cypress and Playwright browser automation. Review requirements, create
test plans, report bugs, perform regression tests and collaborate with developers.
Experience in JavaScript automation, functional testing and clear defect reporting
is required. English B2 or higher. Accessibility and CI/CD knowledge are preferred."""


def test_missing_location_cross_provider_duplicate(db, make_job):
    a = make_job(description=DESCRIPTION, location="Unknown location")
    b = make_job(description=DESCRIPTION.replace("\n", " "), site="indeed", id="in-42", job_url="https://indeed.com/viewjob?jk=42")
    first, _ = db.upsert(a)
    merged, created = db.upsert(b)
    assert not created and merged.id == first.id and len(db.jobs()) == 1
    assert len(merged.source_urls) == 2 and merged.location == b.location
    db.upsert(a)
    db.upsert(b)
    assert len(db.jobs()) == 1


@pytest.mark.parametrize("changes", [
    {"description": "Completely different short duties."},
    {"site": "linkedin", "id": "li-999"},
    {"title": "Senior QA Analyst"},
])
def test_weak_missing_location_evidence_does_not_merge(db, make_job, changes):
    db.upsert(make_job(description=DESCRIPTION, location="Unknown location"))
    b = make_job(site="indeed", id="in-42", job_url="https://indeed.com/viewjob?jk=42", description=DESCRIPTION)
    for key, value in changes.items():
        if key == "site":
            b.source = value
            b.source_urls[0].source = value
        elif key == "id":
            b.source_job_id = value
            b.source_urls[0].source_job_id = value
        else:
            setattr(b, key, value)
    db.upsert(b)
    assert len(db.jobs()) == 2


def test_same_company_title_location_distinct_requisitions(db, make_job):
    first = make_job(description=DESCRIPTION + " Requisition: A101")
    second = make_job(id="li-999", job_url="https://linkedin.com/jobs/view/999", description=DESCRIPTION + " Requisition: A102")
    db.upsert(first)
    db.upsert(second)
    db.upsert(second)
    assert len(db.jobs()) == 2


def test_ambiguous_cross_board_match_stays_separate(db, make_job):
    db.upsert(make_job(description=DESCRIPTION))
    db.upsert(make_job(id="li-999", job_url="https://linkedin.com/jobs/view/999", description=DESCRIPTION))
    db.upsert(make_job(site="indeed", id="in-42", job_url="https://indeed.com/viewjob?jk=42", description=DESCRIPTION))
    assert len(db.jobs()) == 3


def test_adjacent_support_title_with_technical_duties_survives(make_job, preferences):
    job = make_job(title="Data Operations Analyst", description="Triage data quality tickets and investigate system interfaces using runbooks. 1 year experience.")
    assert prefilter(job, preferences).prefilter_score >= preferences.prefilter.minimum_score


def test_reconcile_is_idempotent(db, make_job):
    # Represent an old stored missing-location pair before V0.1 enrichment.
    a = make_job(location="Unknown location", description=DESCRIPTION)
    b = make_job(site="indeed", id="in-42", job_url="https://indeed.com/viewjob?jk=42", description=DESCRIPTION)
    with db.connection:
        db._save(a)
        db._save(b)
    assert db.reconcile_duplicates() == 1
    assert db.reconcile_duplicates() == 0
    assert len(db.jobs()[0].source_urls) == 2


@pytest.mark.parametrize("status", [403, 429, 999, 500])
def test_availability_blocks_are_unknown(make_job, status):
    assert assess_page(make_job(), Page(status, "this job has expired", "https://example.com/jobs/1"))[0] == "unknown"


@pytest.mark.parametrize("status", [404, 410])
def test_availability_removed_is_only_possible(make_job, status):
    assert assess_page(make_job(), Page(status, "", "https://example.com/jobs/1"))[0] == "possibly_unavailable"


def test_availability_structured_job_and_expiration(make_job):
    job = make_job()
    def page(expiry):
        data = {"@type": "JobPosting", "title": job.title, "validThrough": expiry}
        return Page(200, '<script type="application/ld+json">' + json.dumps(data) + '</script>', job.job_url)
    assert assess_page(job, page("2099-01-01"))[0] == "available"
    assert assess_page(job, page("2020-01-01"))[0] == "possibly_unavailable"
    assert assess_page(job, Page(200, "Generic careers page", job.job_url))[0] == "unknown"
    checked = verify_job(job, fetch=Mock(side_effect=TimeoutError()))
    assert checked.availability == "unknown" and checked.availability_checked_at


def test_private_url_not_requested(monkeypatch):
    monkeypatch.setattr("job_hunter.availability.socket.getaddrinfo", lambda *args: [(None, None, None, None, ("127.0.0.1", 80))])
    with pytest.raises(ValueError, match="Private"):
        public_url("http://localhost/jobs/1")


def test_closed_notice_overrides_stale_structured_data(make_job):
    job = make_job()
    body = '<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "title": job.title}) + '</script>'
    page = Page(200, body + '<p>No longer accepting applications</p>', job.job_url)
    assert assess_page(job, page)[0] == "possibly_unavailable"


def test_grc_title_is_reviewed_without_claiming_candidate_experience(make_job, preferences):
    job = make_job(title="Analyst II Governance Risk and Compliance", description="2-4 years in information security and IT audit. Degree preferred.")
    result = prefilter(job, preferences)
    assert not result.prefilter_excluded
    assert result.prefilter_score >= preferences.prefilter.minimum_score


def test_report_fit_wins_over_freshness(db, make_job, candidate, preferences, tmp_path):
    from job_hunter.freshness import local_today
    from job_hunter.report import generate_report
    from job_hunter.scoring import context_hash, import_scores
    from tests.test_scoring import batch_file, score

    old, _ = db.upsert(make_job(company="Strong Five Days Old", date_posted=local_today() - timedelta(days=5)))
    recent, _ = db.upsert(make_job(company="Lower Fit Today", id="li-888", job_url="https://example.com/jobs/888", date_posted=local_today()))
    exported = export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    import_scores(db, batch_file(tmp_path, exported, [score(old.id, 88), score(recent.id, 80)]), candidate, preferences)
    report = generate_report(db, tmp_path, context_hash(candidate, preferences)).read_text(encoding="utf-8")
    assert report.index("Strong Five Days Old") < report.index("Lower Fit Today")


def test_rediscovered_changed_posting_requires_availability_recheck(db, make_job):
    old, _ = db.upsert(make_job())
    old.availability = "available"
    db.save(old)
    unchanged, _ = db.upsert(make_job())
    assert unchanged.availability == "available"
    changed, _ = db.upsert(make_job(description="Updated application support responsibilities"))
    assert changed.availability == "unknown" and changed.availability_checked_at is None


def test_strong_bridge_does_not_erase_distinct_requisitions(db, make_job):
    from job_hunter.models import SourceReference

    first, _ = db.upsert(make_job(description=DESCRIPTION + " Requisition: A101"))
    second, _ = db.upsert(make_job(id="li-999", job_url="https://linkedin.com/jobs/view/999", description=DESCRIPTION + " Requisition: A102"))
    bridge = make_job()
    bridge.source_urls.append(SourceReference(source="linkedin", source_job_id="li-999", url=second.job_url))
    with pytest.raises(ValueError, match="Conflicting"):
        db.upsert(bridge)
    assert {j.id for j in db.jobs()} == {first.id, second.id}
