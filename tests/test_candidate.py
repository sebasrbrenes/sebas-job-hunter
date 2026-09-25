"""Offline evidence-boundary and compatibility tests for candidate profiles."""
import copy
import json

import pytest
import yaml
from pydantic import ValidationError

from job_hunter.candidate import CandidateProfile, Evidence
from job_hunter.cli import main
from job_hunter.config import ROOT, load_candidate, read_yaml
from job_hunter.scoring import context_hash, export_scores, import_scores
from tests.test_scoring import batch_file, score


def test_example_profile_preserves_confirmed_boundaries(candidate):
    education = candidate["education"][0]
    assert education["institution"] == "Example University"
    assert education["status"] == "not_completed"
    assert education["degree_awarded"] is False
    assert education["approximate_years_studied"] == 2
    role = candidate["professional_experience"][0]
    assert role["employer"] == "Example Employer"
    assert role["started"] == "2025-01-15"
    assert len(role["facts"]) == 1
    assert all(f["evidence"]["status"] == "verified_professional" for f in role["facts"])
    assert all(s["evidence"]["status"] == "verified_non_professional" and
               s["evidence"]["basis"] == "fundamental" for s in candidate["technical_skills"])
    assert candidate["languages"]["English"]["certification_status"] == "unknown"
    assert candidate["projects"] == []
    assert all(v is None for v in candidate["unknown_information"].values())
    assert candidate["career_preferences"]["kind"] == "preference_not_experience"
    assert all(c["kind"] == "interpretation" for c in candidate["transferable_capabilities"])


@pytest.mark.parametrize("status,basis", [
    ("verified_professional", "fundamental"),
    ("verified_professional", "project"),
    ("verified_non_professional", "professional"),
    ("learning", "professional"),
    ("unknown", "academic"),
    ("invented", "unknown"),
])
def test_conflicting_evidence_classifications_rejected(status, basis):
    with pytest.raises(ValidationError):
        Evidence(status=status, basis=basis, source="Synthetic fixture")


def test_verified_evidence_requires_source():
    with pytest.raises(ValidationError, match="require a source"):
        Evidence(status="verified_professional", basis="professional")
    assert Evidence(status="unknown", basis="unknown").source is None


@pytest.mark.parametrize("field", ["unrecognized_field", "technical_knowledge"])
def test_structured_profile_rejects_extra_fields(candidate, field):
    candidate[field] = []
    with pytest.raises(ValidationError, match="Extra inputs"):
        CandidateProfile.model_validate(candidate)


@pytest.mark.parametrize("change", ["professional", "project", "academic"])
def test_skill_cannot_be_promoted_without_supporting_reference(candidate, change):
    skill = candidate["technical_skills"][0]
    skill["evidence"].update(basis=change, status=(
        "verified_professional" if change == "professional" else "verified_non_professional"))
    with pytest.raises(ValidationError, match="require evidence_refs"):
        CandidateProfile.model_validate(candidate)


def test_academic_reference_cannot_support_professional_claim(candidate):
    skill = candidate["technical_skills"][0]
    skill["evidence"].update(status="verified_professional", basis="professional")
    skill["evidence_refs"] = [candidate["education"][0]["id"]]
    with pytest.raises(ValidationError, match="same evidence classification"):
        CandidateProfile.model_validate(candidate)


def test_project_evidence_is_supported_without_becoming_employment(candidate):
    # This example exists only in an isolated in-memory test, never in real data.
    evidence = {"status": "verified_non_professional", "basis": "project", "source": "Synthetic fixture"}
    candidate["projects"] = [{"id": "fixture_project", "name": "Fixture", "evidence": evidence,
        "facts": [{"id": "fixture_project_fact", "statement": "Used a fixture tool in a personal project.", "evidence": evidence}]}]
    candidate["technical_skills"].append({"id": "fixture_skill", "statement": "Fixture project tool use.",
        "evidence": evidence, "evidence_refs": ["fixture_project_fact"]})
    validated = CandidateProfile.model_validate(candidate)
    assert validated.technical_skills[-1].evidence.status == "verified_non_professional"
    assert len(validated.professional_experience) == 1
    candidate["projects"][0]["evidence"] = {
        "status": "verified_professional", "basis": "professional", "source": "Synthetic fixture"}
    with pytest.raises(ValidationError, match="non-professional project evidence"):
        CandidateProfile.model_validate(candidate)


def test_transferable_interpretation_requires_real_fact_reference(candidate):
    candidate["transferable_capabilities"][0]["evidence_refs"] = ["invented_fact"]
    with pytest.raises(ValidationError, match="reference confirmed facts"):
        CandidateProfile.model_validate(candidate)


def test_duplicate_fact_ids_rejected(candidate):
    candidate["professional_experience"][0]["facts"].append(
        copy.deepcopy(candidate["professional_experience"][0]["facts"][0]))
    with pytest.raises(ValidationError, match="IDs must be unique"):
        CandidateProfile.model_validate(candidate)


def test_incomplete_program_cannot_claim_awarded_degree(candidate):
    candidate["education"][0]["degree_awarded"] = True
    with pytest.raises(ValidationError, match="cannot award a degree"):
        CandidateProfile.model_validate(candidate)


def test_proficiency_does_not_create_certification(candidate):
    candidate["languages"]["English"]["certification_details"] = "Invented credential"
    with pytest.raises(ValidationError, match="confirmed certification"):
        CandidateProfile.model_validate(candidate)


def test_unknown_topics_cannot_contain_guessed_answers(candidate):
    candidate["unknown_information"]["professional_python_usage"] = True
    with pytest.raises(ValidationError):
        CandidateProfile.model_validate(candidate)


def test_learning_cannot_silently_become_verified_knowledge(candidate):
    candidate["learning"][0]["evidence"].update(status="verified_non_professional", basis="fundamental")
    with pytest.raises(ValidationError, match="remain classified as learning"):
        CandidateProfile.model_validate(candidate)


def test_legacy_profile_load_preserves_data_and_hash(preferences, tmp_path):
    legacy = {"name": "Fixture candidate", "country": "Costa Rica", "languages": {"English": "advanced"},
        "current_role": {"title": "QA Agent", "employer": "Fixture employer", "environment": "Operations", "started": "2025-12"},
        "professional_experience": ["Operational QA"], "technical_knowledge": ["Python"],
        "currently_learning": ["Software development"], "evidence_rules": ["Do not invent experience."]}
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump(legacy), encoding="utf-8")
    loaded = load_candidate(path)
    assert loaded == legacy
    assert context_hash(loaded, preferences) == context_hash(legacy, preferences)


def test_invalid_profile_stops_cli_before_database_creation(candidate, tmp_path, capsys):
    candidate["schema_version"] = 999
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(candidate), encoding="utf-8")
    database = tmp_path / "must_not_exist.sqlite3"
    assert main(["--candidate", str(path), "--db", str(database), "stats"]) == 1
    assert "Error:" in capsys.readouterr().err
    assert not database.exists()


def test_exchange_preserves_evidence_and_rejects_stale_education(db, make_job, candidate, preferences, tmp_path):
    job, _ = db.upsert(make_job())
    exported = export_scores(db, candidate, preferences, tmp_path / "to_score.json")
    payload = json.loads((tmp_path / "to_score.json").read_text(encoding="utf-8"))
    assert payload["candidate"] == candidate
    assert "profile_questions" not in payload["candidate"]
    path = batch_file(tmp_path, exported, [score(job.id)])
    assert import_scores(db, path, candidate, preferences) == 1
    candidate["education"][0]["approximate_years_studied"] = 3  # Synthetic revision.
    with pytest.raises(ValueError, match="Candidate/preferences changed"):
        import_scores(db, path, candidate, preferences)
    assert len(export_scores(db, candidate, preferences, tmp_path / "new.json")["jobs"]) == 1


def test_question_backlog_is_prioritized_unique_and_unanswered():
    backlog = read_yaml(ROOT / "config/profile_questions.example.yaml")
    priorities = backlog["priority_order"]
    ranks = [priorities.index(group["priority"]) for group in backlog["groups"]]
    assert ranks == sorted(ranks)
    questions = [question for group in backlog["groups"] for question in group["questions"]]
    assert len({q["id"] for q in questions}) == len(questions)
    assert all(set(q) == {"id", "question"} and q["question"].strip() for q in questions)
