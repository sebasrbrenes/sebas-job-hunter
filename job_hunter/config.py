from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .candidate import CandidateProfile, LegacyCandidate

ROOT = Path(__file__).resolve().parents[1]


class SearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[str] = Field(default_factory=lambda: ["linkedin", "indeed", "glassdoor", "google"], min_length=1)
    providers: dict[str, bool] | None = None
    official_catalog: str | None = None
    location: str = "Costa Rica"
    additional_locations: list[str] = Field(default_factory=list)
    remote_probes: bool = False
    query_aliases: dict[str, list[str]] = Field(default_factory=dict)
    country_indeed: str = "Costa Rica"
    days_old: int = Field(default=7, ge=1, le=365)
    results_per_query: int = Field(default=15, ge=1, le=1000)
    timeout_seconds: float = Field(default=90, ge=1, le=600)
    pause_seconds: float = Field(default=2, ge=0, le=60)
    linkedin_fetch_description: bool = True
    query_groups: dict[str, list[str]]

    @model_validator(mode="after")
    def provider_selection(self):
        if self.providers is not None:
            discovery = {"linkedin", "indeed", "glassdoor", "google"}
            self.sources = [name for name, enabled in self.providers.items() if enabled and name in discovery]
        if not self.sources and not self.official_catalog:
            raise ValueError("Enable at least one search provider")
        return self


class FilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    export_limit: int = Field(default=40, ge=1, le=500)
    minimum_score: int = Field(default=25, ge=0, le=100)
    allow_internships: bool = False


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_tiers: dict[str, list[str]]
    locations: list[str]
    prefer: list[str]
    allowed_gaps: list[str]
    strong_negative_signals: list[str]
    search: SearchConfig
    prefilter: FilterConfig


def read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def load_preferences(path: Path) -> Preferences:
    return Preferences.model_validate(read_yaml(path))


def load_candidate(path: Path) -> dict[str, Any]:
    data = read_yaml(path)
    if "schema_version" not in data:
        LegacyCandidate.model_validate(data)
        return data  # Preserve legacy scoring hashes and unclassified evidence.
    return CandidateProfile.model_validate(data).model_dump(mode="json")
