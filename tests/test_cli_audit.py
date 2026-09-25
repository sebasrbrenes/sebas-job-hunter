"""Offline audit through public CLI commands, with only the search board mocked."""
import json
import re
import subprocess
import sys
from datetime import timedelta

import pytest

from job_hunter.cli import main
from job_hunter.database import Database
from job_hunter.models import utcnow
from job_hunter.prefilter import prefilter
from job_hunter.search import SearchResult


def test_cli_sample_roundtrip_and_malformed_json(tmp_path, monkeypatch):
    database = tmp_path / "sample.sqlite3"
    exported = tmp_path / "to_score.json"
    scored = tmp_path / "scored_jobs.json"
    timestamp = utcnow()

    def fake(self, source, query, config):
        return SearchResult([{"id": f"sample-{i}", "title": "Junior QA Analyst", "company": f"Audit Sample {i}",
                              "location": "Costa Rica", "description": "Manual testing; Python useful.",
                              "job_url": f"https://example.com/jobs/{i}"} for i in [1, 1, 1, 2]])

    monkeypatch.setattr("job_hunter.search.JobSpyProvider.search", fake)
    monkeypatch.setattr("job_hunter.normalize.utcnow", lambda: timestamp)
    args = ["--db", str(database), "run-search", "--source", "linkedin", "--query", "QA Analyst", "--output", str(exported)]
    assert main(args) == 0
    with Database(database) as db:
        before = {j.id: j for j in db.jobs()}
        assert len(before) == 2 and all(j.is_new for j in before.values())
        assert db.run_summary()["scraped"] == 4
    monkeypatch.setattr("job_hunter.normalize.utcnow", lambda: timestamp + timedelta(hours=1))
    assert main(args) == 0
    with Database(database) as db:
        assert len(db.jobs()) == 2 and db.run_summary()["new"] == 0
        for job in db.jobs():
            assert job.first_seen_at == before[job.id].first_seen_at
            assert job.last_seen_at == timestamp + timedelta(hours=1)
            assert not job.is_new
    payload = json.loads(exported.read_text(encoding="utf-8"))
    batch = {"export_id": payload["export_id"], "scores": [
        {"job_id": j["id"], "score": value, "recommendation": recommendation, "category": "qa",
         "strengths": ["SYNTHETIC AUDIT FIXTURE"], "gaps": [], "dealbreakers": [],
         "reasoning": "Synthetic score solely to verify import and report ordering."}
        for j, value, recommendation in zip(payload["jobs"], [72, 91], ["apply", "exceptional"])]}
    scored.write_text(json.dumps(batch), encoding="utf-8")

    def cli(*arguments):
        return subprocess.run([sys.executable, "-m", "job_hunter", "--db", str(database), *arguments],
                              capture_output=True, text=True, encoding="utf-8")

    imported = cli("import-scores", "--input", str(scored))
    assert imported.returncode == 0 and "Imported 2 scores" in imported.stdout
    with Database(database) as db:
        saved = {j.id: j.model_dump_json() for j in db.jobs()}
    for malformed in ["{broken json", json.dumps({**batch, "scores": [batch["scores"][0], {**batch["scores"][1], "score": 999}]})]:
        scored.write_text(malformed, encoding="utf-8")
        result = cli("import-scores", "--input", str(scored))
        assert result.returncode == 1 and "Error:" in result.stderr
        with Database(database) as db:
            assert {j.id: j.model_dump_json() for j in db.jobs()} == saved
    report_result = cli("report", "--output-dir", str(tmp_path / "reports"))
    assert report_result.returncode == 0 and str(tmp_path) in report_result.stdout
    report = next((tmp_path / "reports").glob("*.md"))
    values = [int(n) for n in re.findall(r"Score: \*\*(\d+)/100", report.read_text(encoding="utf-8"))]
    assert values == [91, 72]
    print(f"Sample report: {report}; duplicate rows 4 -> 2; repeated run new=0; report scores={values}; malformed imports left DB unchanged")


@pytest.mark.parametrize("seniority", ["Senior", "Staff", "Principal", "Manager", "Director"])
def test_senior_titles_receive_strong_penalty(seniority, make_job, preferences):
    baseline = prefilter(make_job(title="QA Engineer"), preferences)
    senior = prefilter(make_job(title=f"{seniority} QA Engineer"), preferences)
    assert baseline.prefilter_score - senior.prefilter_score == 35
