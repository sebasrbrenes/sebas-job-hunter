"""Validated canonical data and the public scoring contract."""

from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Recommendation(StrEnum):
    EXCEPTIONAL = "exceptional"
    STRONG_APPLY = "strong_apply"
    APPLY = "apply"
    CONSIDER = "consider"
    SKIP = "skip"


class Category(StrEnum):
    QA = "qa"
    AUTOMATION = "qa_automation"
    SECURITY = "cybersecurity"
    SUPPORT = "technical_support"
    OTHER = "other"


class ApplicationStatus(StrEnum):
    APPLIED = "Applied"
    INTERVIEW = "Interview"
    REJECTED = "Rejected"
    GHOSTED = "Ghosted"
    OFFER = "Offer"
    WITHDRAWN = "Withdrawn"


def recommendation_for(score: int) -> Recommendation:
    for threshold, value in [(90, Recommendation.EXCEPTIONAL), (80, Recommendation.STRONG_APPLY),
                             (70, Recommendation.APPLY), (60, Recommendation.CONSIDER)]:
        if score >= threshold:
            return value
    return Recommendation.SKIP


class SourceReference(BaseModel):
    source: str
    source_job_id: str | None = None
    url: str | None = None


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    source: str = "unknown"
    source_job_id: str | None = None
    title: str = "Unknown title"
    source_kind: Literal["official", "discovery"] = "discovery"
    geographic_eligibility: Literal["eligible", "ineligible", "unknown"] = "unknown"
    geographic_reason: str = "Not assessed"
    work_arrangement: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    company: str = "Unknown company"
    location: str = "Unknown location"
    job_url: str | None = None
    description: str = ""
    description_source: str | None = None
    date_posted: date | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_interval: str | None = None
    is_remote: bool | None = None
    employment_type: str | None = None
    search_query: str = ""
    search_queries: list[str] = Field(default_factory=list)
    discovery_tracks: list[str] = Field(default_factory=list)
    source_urls: list[SourceReference] = Field(default_factory=list)
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    is_new: bool = True
    prefilter_score: int = Field(default=0, ge=0, le=100)
    prefilter_reasons: list[str] = Field(default_factory=list)
    prefilter_excluded: bool = False
    codex_score: int | None = Field(default=None, ge=0, le=100)
    recommendation: Recommendation | None = None
    category: Category | None = None
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)
    score_reasoning: str | None = None
    score_context_hash: str | None = None
    freshness: Literal["today", "1_3_days", "4_7_days", "older", "unknown"] = "unknown"
    availability: Literal["available", "possibly_unavailable", "unknown"] = "unknown"
    availability_checked_at: datetime | None = None
    availability_reason: str | None = None
    status: str = "discovered"


class ScoreRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    job_id: str = Field(min_length=1)
    score: int = Field(strict=True, ge=0, le=100)
    recommendation: Recommendation
    category: Category
    strengths: list[str]
    gaps: list[str]
    dealbreakers: list[str]
    reasoning: str = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_recommendation(self):
        expected = recommendation_for(self.score)
        if self.recommendation != expected:
            raise ValueError(f"Score {self.score} requires recommendation '{expected}'")
        for items in (self.strengths, self.gaps, self.dealbreakers):
            if any(not item.strip() for item in items):
                raise ValueError("Evidence lists must not contain empty strings")
        return self


class ScoreBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, strict=True, ge=1, le=1)
    export_id: str = Field(min_length=1)
    scores: list[ScoreRecord]

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [score.job_id for score in self.scores]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate job_id in score batch")
        return self
