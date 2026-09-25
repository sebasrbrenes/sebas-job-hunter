from job_hunter.prefilter import prefilter
from job_hunter.scoring import export_scores, import_scores
from tests.test_scoring import batch_file, score


def test_shared_careers_page_does_not_merge_distinct_vacancies(db, make_job):
    first = make_job(job_url_direct="https://careers.example.com/jobs")
    second = make_job(id="li-999", title="SOC Analyst I", job_url="https://www.linkedin.com/jobs/view/999",
                      job_url_direct="https://careers.example.com/jobs")
    db.upsert(first)
    db.upsert(second)
    assert len(db.jobs()) == 2


def test_repeated_cross_board_short_description_preserves_score(db, make_job, candidate, preferences, tmp_path):
    short = make_job(description="QA testing.")
    rich = make_job(site="indeed", id="in-42", job_url="https://indeed.com/viewjob?jk=42",
                    description="QA testing. Full requirements include functional testing and error reporting.")
    job, _ = db.upsert(short)
    db.upsert(rich)
    batch = export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    import_scores(db, batch_file(tmp_path, batch, [score(job.id)]), candidate, preferences)
    db.upsert(short)
    db.upsert(rich)
    assert db.get(job.id).codex_score == 80


def test_spaced_experience_range_uses_lower_bound(make_job, preferences):
    job = prefilter(make_job(description="1 - 5 years of experience required."), preferences)
    assert not any("5+ years" in r for r in job.prefilter_reasons)
