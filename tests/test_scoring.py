import json

import pytest
from pydantic import ValidationError

from job_hunter.models import ScoreBatch, ScoreRecord
from job_hunter.scoring import export_scores, import_scores


def score(job_id, score=80):
    return {"job_id": job_id, "score": score, "recommendation": "strong_apply", "category": "qa",
            "strengths": ["Operational QA experience"], "gaps": ["Software QA tooling not confirmed"],
            "dealbreakers": [], "reasoning": "Fixture assessment for pipeline testing only."}


def batch_file(tmp_path, exported, scores):
    path = tmp_path / "scored_jobs.json"
    path.write_text(json.dumps({"schema_version": 1, "export_id": exported["export_id"], "scores": scores}), encoding="utf-8")
    return path


@pytest.mark.parametrize("changes", [{"score": 101}, {"score": -1}, {"score": "80"}, {"score": True},
    {"score": 80.5}, {"recommendation": "maybe"}, {"recommendation": "apply"}, {"category": "made_up"},
    {"reasoning": " "}, {"strengths": "QA"}, {"strengths": [""]}, {"extra": 1}])
def test_malformed_scores_rejected(changes):
    with pytest.raises(ValidationError):
        ScoreRecord.model_validate({**score("abc"), **changes})


def test_export_import_and_no_repeat_export(db, make_job, candidate, preferences, tmp_path):
    job, _ = db.upsert(make_job())
    exported = export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    assert len(exported["jobs"]) == 1
    path = batch_file(tmp_path, exported, [score(job.id)])
    assert import_scores(db, path, candidate, preferences) == 1
    assert db.get(job.id).codex_score == 80
    assert import_scores(db, path, candidate, preferences) == 1  # Idempotent
    assert export_scores(db, candidate, preferences, tmp_path / "next.json")["jobs"] == []
    db.upsert(make_job())
    assert db.get(job.id).codex_score == 80


def test_import_atomic_on_unknown_id(db, make_job, candidate, preferences, tmp_path):
    job, _ = db.upsert(make_job())
    exported = export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    path = batch_file(tmp_path, exported, [score(job.id), score("unknown")])
    with pytest.raises(ValueError, match="Unknown job_id"):
        import_scores(db, path, candidate, preferences)
    assert db.get(job.id).codex_score is None


def test_changed_job_rejects_stale_scores(db, make_job, candidate, preferences, tmp_path):
    job, _ = db.upsert(make_job())
    exported = export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    path = batch_file(tmp_path, exported, [score(job.id)])
    import_scores(db, path, candidate, preferences)
    db.upsert(make_job(description="Now requires a very different mandatory skill set."))
    assert db.get(job.id).codex_score is None
    with pytest.raises(ValueError, match="changed since export"):
        import_scores(db, path, candidate, preferences)


def test_profile_change_requires_new_scores(db, make_job, candidate, preferences, tmp_path):
    job, _ = db.upsert(make_job())
    exported = export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    path = batch_file(tmp_path, exported, [score(job.id)])
    import_scores(db, path, candidate, preferences)
    candidate["technical_skills"][0]["statement"] = "Test-only changed skill evidence"
    with pytest.raises(ValueError, match="Candidate/preferences changed"):
        import_scores(db, path, candidate, preferences)
    assert len(export_scores(db, candidate, preferences, tmp_path / "next.json")["jobs"]) == 1


def test_duplicate_score_ids_rejected():
    with pytest.raises(ValidationError, match="Duplicate job_id"):
        ScoreBatch.model_validate({"export_id": "test", "scores": [score("abc"), score("abc")]})


def test_export_limit_and_latest_scope(db, make_job, candidate, preferences, tmp_path):
    run = db.start_run({})
    old, _ = db.upsert(make_job(), run)
    run = db.start_run({})
    for index in range(3):
        db.upsert(make_job(id=f"li-{index+500}", job_url=f"https://example.com/{index}", company=f"Company {index}"), run)
    exported = export_scores(db, candidate, preferences, tmp_path / "to_score.json", limit=2)
    assert len(exported["jobs"]) == 2
    assert old.id not in {j["id"] for j in exported["jobs"]}
    assert len(export_scores(db, candidate, preferences, tmp_path / "all.json", all_jobs=True)["jobs"]) == 4


def test_board_format_variations_do_not_erase_scores(db, make_job, candidate, preferences, tmp_path):
    first = make_job(company="360training", location="Heredia, Heredia, Costa Rica")
    second = make_job(company="360training.com", location="Heredia, H, CR", site="indeed", id="in-42", job_url="https://indeed.com/42")
    job, _ = db.upsert(first)
    db.upsert(second)
    exported = export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    import_scores(db, batch_file(tmp_path, exported, [score(job.id)]), candidate, preferences)
    db.upsert(first)
    db.upsert(second)
    assert db.get(job.id).codex_score == 80
