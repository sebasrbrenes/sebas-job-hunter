from job_hunter.models import recommendation_for
from job_hunter.report import generate_report
from job_hunter.scoring import context_hash, export_scores, import_scores
from tests.test_scoring import batch_file, score


def test_report_order_excludes_skip(db, make_job, candidate, preferences, tmp_path):
    jobs = []
    for index in range(5):
        job, _ = db.upsert(make_job(id=f"li-{index+700}", job_url=f"https://example.com/{index}", company=f"Company {index}"))
        jobs.append(job)
    exported = export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    scores = [{**score(j.id, value), "recommendation": recommendation_for(value)} for j, value in zip(jobs, [80, 96, 88, 65, 40])]
    import_scores(db, batch_file(tmp_path, exported, scores), candidate, preferences)
    report = generate_report(db, tmp_path, context_hash(candidate, preferences)).read_text(encoding="utf-8")
    assert report.index("Company 1") < report.index("Company 2") < report.index("Company 0") < report.index("Company 3")
    assert "Company 4" not in report
    assert "Number semantically scored (report scope, current profile): 5" in report


def test_empty_report(db, candidate, preferences, tmp_path):
    report = generate_report(db, tmp_path, context_hash(candidate, preferences)).read_text(encoding="utf-8")
    assert "No search run yet" in report
