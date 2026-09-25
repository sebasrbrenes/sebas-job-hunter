import subprocess
from unittest.mock import patch

import pytest

from job_hunter.search import JobSpyProvider, SearchResult, jobspy_arguments, run_search


class FakeProvider:
    def search(self, source, query, config):
        if source == "glassdoor":
            raise RuntimeError("Mock board blocked")
        return SearchResult([
            {"id": f"{source}-42", "site": source, "title": "Junior QA Analyst", "company": "Fixture Company",
             "location": "Costa Rica", "job_url": f"https://{source}.example/42", "description": "Manual testing. Python useful."},
            {},  # One unusable row must not discard valid rows.
        ])


def test_failure_isolation_and_repeated_mock_pipeline(db, candidate, preferences, tmp_path):
    from job_hunter.scoring import export_scores, import_scores, context_hash
    from job_hunter.report import generate_report
    from tests.test_scoring import batch_file, score
    preferences.search.sources = ["linkedin", "glassdoor", "indeed"]
    first = run_search(db, preferences, FakeProvider(), ["QA Analyst", "QA Engineer"])
    second = run_search(db, preferences, FakeProvider(), ["QA Analyst", "QA Engineer"])
    assert first["unique"] == second["unique"] == len(db.jobs()) == 1
    assert first["new"] == 1 and second["new"] == 0
    assert first["scraped"] == 8
    assert any(a["error"] for a in first["attempts"])
    exported = export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    import_scores(db, batch_file(tmp_path, exported, [score(db.jobs()[0].id)]), candidate, preferences)
    report = generate_report(db, tmp_path / "reports", context_hash(candidate, preferences))
    assert "Fixture Company" in report.read_text(encoding="utf-8")
    assert "Mock board blocked" in report.read_text(encoding="utf-8")


def test_documented_jobspy_arguments(preferences):
    args = jobspy_arguments("google", "QA Analyst", preferences.search)
    assert args["site_name"] == ["google"]
    assert args["hours_old"] == 168 and args["country_indeed"] == "Costa Rica"
    assert "Costa Rica" in args["google_search_term"] and "7 days" in args["google_search_term"]
    assert "is_remote" not in args  # Conflicts with Indeed hours_old filter


def test_process_timeout_is_propagated(preferences):
    with patch("job_hunter.search.subprocess.run", side_effect=subprocess.TimeoutExpired("jobspy", 1)):
        with pytest.raises(subprocess.TimeoutExpired):
            JobSpyProvider().search("linkedin", "QA", preferences.search)
