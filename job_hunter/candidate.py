"""Candidate evidence validation only; no scoring or resume generation."""
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(ProfileModel):
    # "Verified" means confirmed by the candidate, not externally certified.
    status: Literal["verified_professional", "verified_non_professional", "learning", "unknown"]
    basis: Literal["professional", "project", "academic", "fundamental", "learning", "personal", "unknown"]
    source: Text | None = None

    @model_validator(mode="after")
    def consistent_classification(self):
        allowed = {
            "verified_professional": {"professional"},
            "verified_non_professional": {"project", "academic", "fundamental", "personal"},
            "learning": {"learning"},
            "unknown": {"unknown"},
        }
        if self.basis not in allowed[self.status]:
            raise ValueError("Evidence status and basis are inconsistent")
        if self.status != "unknown" and self.source is None:
            raise ValueError("Confirmed evidence and learning require a source")
        return self


class Fact(ProfileModel):
    id: Text
    statement: Text
    evidence: Evidence
    limitations: list[Text] = Field(default_factory=list)


class Capability(Fact):
    evidence_refs: list[Text] = Field(default_factory=list)


class Identity(ProfileModel):
    name: Text
    country: Text
    evidence: Evidence


class Education(ProfileModel):
    id: Text
    institution: Text
    program: Text
    status: Literal["completed", "not_completed", "in_progress", "unknown"]
    degree_awarded: bool | None = Field(strict=True)
    approximate_years_studied: float | None = Field(default=None, gt=0)
    evidence: Evidence
    limitations: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def completion_and_evidence(self):
        if self.status != "completed" and self.degree_awarded is True:
            raise ValueError("An incomplete or unknown program cannot award a degree")
        if self.evidence.status != "verified_non_professional" or self.evidence.basis != "academic":
            raise ValueError("Education requires confirmed academic evidence")
        return self


class ProfessionalExperience(ProfileModel):
    id: Text
    employer: Text
    environment: Text
    title: Text
    started: date
    ended: date | None = None
    evidence: Evidence
    facts: list[Fact] = Field(min_length=1)

    @model_validator(mode="after")
    def professional_evidence(self):
        if self.ended is not None and self.ended < self.started:
            raise ValueError("Employment end cannot precede start")
        if any(e.status != "verified_professional" for e in [self.evidence, *(f.evidence for f in self.facts)]):
            raise ValueError("Professional history requires verified professional evidence")
        return self


class Project(ProfileModel):
    id: Text
    name: Text
    evidence: Evidence
    facts: list[Fact] = Field(min_length=1)

    @model_validator(mode="after")
    def non_professional_projects(self):
        if any(e.status != "verified_non_professional" or e.basis != "project"
               for e in [self.evidence, *(f.evidence for f in self.facts)]):
            raise ValueError("Personal projects require non-professional project evidence")
        return self


class Language(ProfileModel):
    proficiency: Text
    evidence: Evidence
    certification_status: Literal["unknown", "confirmed", "none"]
    certification_details: Text | None = None

    @model_validator(mode="after")
    def certification_is_separate(self):
        if (self.certification_status == "confirmed") != (self.certification_details is not None):
            raise ValueError("Only a confirmed certification may have certification details")
        return self


class TransferableCapability(ProfileModel):
    id: Text
    kind: Literal["interpretation"] = "interpretation"
    statement: Text
    evidence_refs: list[Text] = Field(min_length=1)
    limitation: Text


class CareerPreferences(ProfileModel):
    kind: Literal["preference_not_experience"]
    primary: list[Text]
    next_step: list[Text]
    parallel_interest: list[Text]
    fallback: list[Text]


class CandidateProfile(ProfileModel):
    schema_version: Literal[2]
    identity: Identity
    education: list[Education]
    professional_experience: list[ProfessionalExperience]
    technical_skills: list[Capability]
    languages: dict[Text, Language]
    projects: list[Project]
    learning: list[Capability]
    transferable_capabilities: list[TransferableCapability]
    career_preferences: CareerPreferences
    unknown_information: dict[Text, None]
    evidence_rules: list[Text] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_references(self):
        facts = [f for group in [*self.professional_experience, *self.projects] for f in group.facts]
        records = [*self.education, *self.professional_experience, *self.projects, *facts,
                   *self.technical_skills, *self.learning, *self.transferable_capabilities]
        ids = [r.id for r in records]
        if len(ids) != len(set(ids)):
            raise ValueError("Profile record IDs must be unique")
        # Capabilities must point to original evidence, never to another inference.
        sources = {r.id: r.evidence for r in [*self.education, *facts]}
        for capability in [*self.technical_skills, *self.learning]:
            if capability.evidence.basis in {"professional", "project", "academic"} and not capability.evidence_refs:
                raise ValueError("Professional/project/academic capabilities require evidence_refs")
            for ref in capability.evidence_refs:
                evidence = sources.get(ref)
                if evidence is None or (evidence.status, evidence.basis) != (capability.evidence.status, capability.evidence.basis):
                    raise ValueError("Capability reference must have the same evidence classification")
        if any(c.evidence.status != "learning" for c in self.learning):
            raise ValueError("Learning entries must remain classified as learning")
        for capability in self.transferable_capabilities:
            for ref in capability.evidence_refs:
                evidence = sources.get(ref)
                if evidence is None or not evidence.status.startswith("verified_"):
                    raise ValueError("Transferable interpretations must reference confirmed facts")
        return self


class LegacyRole(ProfileModel):
    title: Text
    employer: Text
    environment: Text
    started: Text


class LegacyCandidate(ProfileModel):
    """Accept the original V0 YAML without promoting or migrating its claims."""
    name: Text
    country: Text
    languages: dict[Text, Text]
    current_role: LegacyRole
    professional_experience: list[Text]
    technical_knowledge: list[Text]
    currently_learning: list[Text]
    evidence_rules: list[Text]
