"""Public ATS clients configured by an editable employer catalog."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
import yaml

from .config import ROOT, SearchConfig
from .normalize import description_text


@dataclass(frozen=True)
class CompanySource:
    slug: str
    company: str
    provider: str
    enabled: bool
    status: str
    careers_url: str
    config: dict

    @property
    def source(self) -> str:
        return f"{self.provider}:{self.slug}"


@dataclass
class ATSResult:
    rows: list[dict]
    warnings: list[str]


def load_catalog(path: str | Path | None) -> list[CompanySource]:
    if not path:
        return []
    catalog_path = Path(path)
    if not catalog_path.is_absolute():
        catalog_path = ROOT / catalog_path
    data = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    entries = data.get("companies", [])
    if not isinstance(entries, list):
        raise ValueError(f"{catalog_path}: companies must be a list")
    sources = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError(f"{catalog_path}: every company must be a mapping")
        required = {"slug", "company", "provider", "enabled", "status", "careers_url"}
        missing = required - item.keys()
        if missing:
            raise ValueError(f"{catalog_path}: company entry missing {sorted(missing)}")
        config = {key: value for key, value in item.items() if key not in required | {"notes"}}
        sources.append(CompanySource(item["slug"], item["company"], item["provider"], bool(item["enabled"]),
                                     item["status"], item["careers_url"], config))
    return sources


def enabled_sources(config: SearchConfig) -> list[CompanySource]:
    enabled_providers = config.providers
    return [source for source in load_catalog(config.official_catalog)
            if source.enabled and source.status == "integrated" and
            (enabled_providers is None or enabled_providers.get(source.provider, False))]


def _json(url: str, timeout: float, payload: dict | None = None) -> dict | list:
    headers = {"Accept": "application/json", "User-Agent": "SebasJobHunter/0.2"}
    response = requests.post(url, json=payload, headers=headers, timeout=timeout) if payload is not None else requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _contains_query(row: dict, query: str) -> bool:
    words = [word for word in re.findall(r"[a-z0-9]+", query.casefold()) if len(word) > 1]
    haystack = " ".join(str(row.get(key, "")) for key in ("title", "description")).casefold()
    return not words or any(word in haystack for word in words)


def normalize_smartrecruiters(summary: dict, detail: dict, company: str) -> dict:
    location = detail.get("location") or summary.get("location") or {}
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    description = "\n\n".join(description_text(section.get("text", "")) for section in sections.values()
                                if isinstance(section, dict) and section.get("text"))
    identifier = str(detail.get("id") or summary.get("id") or "")
    remote, hybrid = bool(location.get("remote")), bool(location.get("hybrid"))
    return {"id": identifier, "title": detail.get("name") or summary.get("name"), "company": company,
            "location": location.get("fullLocation") or ", ".join(str(location.get(k, "")) for k in ("city", "country") if location.get(k)),
            "description": description, "date_posted": detail.get("releasedDate") or summary.get("releasedDate"),
            "job_url": f"https://jobs.smartrecruiters.com/{quote((detail.get('company') or {}).get('identifier') or company)}/{quote(identifier)}",
            "is_remote": remote, "work_arrangement": "remote" if remote else "hybrid" if hybrid else "onsite",
            "job_type": ((detail.get("typeOfEmployment") or {}).get("label")), "source_kind": "official"}


def normalize_workday(summary: dict, detail: dict, source: CompanySource) -> dict:
    info = detail.get("jobPostingInfo") or detail
    path = summary.get("externalPath") or info.get("externalPath") or ""
    locale = source.config.get("locale", "en-US")
    base = f"https://{source.config['domain']}/{locale}/{source.config['site']}"
    posted = info.get("startDate") or info.get("postedOn")
    if not posted:
        match = re.search(r"(\d+)\s+Days?", str(summary.get("postedOn", "")), flags=re.I)
        posted = (date.today() - timedelta(days=int(match.group(1)))).isoformat() if match else None
    arrangement = "remote" if "remote" in str(summary.get("locationsText", "")).casefold() else "unknown"
    return {"id": (summary.get("bulletFields") or [info.get("jobReqId") or path])[-1],
            "title": info.get("title") or summary.get("title"), "company": source.company,
            "location": info.get("location") or summary.get("locationsText"),
            "description": description_text(info.get("jobDescription", "")), "date_posted": posted,
            "job_url": base + path, "is_remote": arrangement == "remote", "work_arrangement": arrangement,
            "job_type": summary.get("timeType"), "source_kind": "official"}


def normalize_greenhouse(item: dict, company: str) -> dict:
    locations = item.get("location") or {}
    return {"id": str(item.get("id", "")), "title": item.get("title"), "company": company,
            "location": locations.get("name") if isinstance(locations, dict) else locations,
            "description": description_text(item.get("content", "")), "date_posted": item.get("updated_at"),
            "job_url": item.get("absolute_url"), "source_kind": "official",
            "is_remote": "remote" in str(locations).casefold(),
            "work_arrangement": "remote" if "remote" in str(locations).casefold() else "unknown"}


def normalize_lever(item: dict, company: str) -> dict:
    categories = item.get("categories") or {}
    location = categories.get("location") or item.get("workplaceType") or ""
    description = "\n\n".join(filter(None, [description_text(item.get("description", "")),
                                                description_text(item.get("additional", ""))]))
    return {"id": str(item.get("id", "")), "title": item.get("text"), "company": company,
            "location": location, "description": description, "job_url": item.get("hostedUrl"),
            "job_type": categories.get("commitment"), "source_kind": "official",
            "is_remote": "remote" in str(location).casefold(),
            "work_arrangement": "remote" if "remote" in str(location).casefold() else "unknown"}


class OfficialATSProvider:
    def __init__(self, sources: list[CompanySource]):
        self.sources = {source.source: source for source in sources}

    def search(self, source_name: str, query: str, config: SearchConfig) -> ATSResult:
        source = self.sources[source_name]
        try:
            method = getattr(self, f"_{source.provider}")
        except AttributeError as exc:
            raise ValueError(f"Unsupported official provider: {source.provider}") from exc
        rows = method(source, query, config)
        return ATSResult(rows[:config.results_per_query], [])

    def _smartrecruiters(self, source: CompanySource, query: str, config: SearchConfig) -> list[dict]:
        identifier = source.config["identifier"]
        params = urlencode({"q": query, "limit": min(config.results_per_query, 100)})
        listing = _json(f"https://api.smartrecruiters.com/v1/companies/{quote(identifier)}/postings?{params}", config.timeout_seconds)
        rows = []
        for summary in listing.get("content", []):
            detail = _json(summary.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{quote(identifier)}/postings/{summary['id']}", config.timeout_seconds)
            rows.append(normalize_smartrecruiters(summary, detail, source.company))
        return rows

    def _workday(self, source: CompanySource, query: str, config: SearchConfig) -> list[dict]:
        api = f"https://{source.config['domain']}/wday/cxs/{source.config['tenant']}/{source.config['site']}"
        listing = _json(api + "/jobs", config.timeout_seconds,
                        {"appliedFacets": {}, "limit": min(config.results_per_query, 20), "offset": 0, "searchText": query})
        rows = []
        for summary in listing.get("jobPostings", []):
            detail = _json(api + summary["externalPath"], config.timeout_seconds)
            rows.append(normalize_workday(summary, detail, source))
        return rows

    def _greenhouse(self, source: CompanySource, query: str, config: SearchConfig) -> list[dict]:
        data = _json(f"https://boards-api.greenhouse.io/v1/boards/{quote(source.config['token'])}/jobs?content=true", config.timeout_seconds)
        return [row for item in data.get("jobs", []) if _contains_query((row := normalize_greenhouse(item, source.company)), query)]

    def _lever(self, source: CompanySource, query: str, config: SearchConfig) -> list[dict]:
        data = _json(f"https://api.lever.co/v0/postings/{quote(source.config['site'])}?mode=json", config.timeout_seconds)
        return [row for item in data if _contains_query((row := normalize_lever(item, source.company)), query)]
