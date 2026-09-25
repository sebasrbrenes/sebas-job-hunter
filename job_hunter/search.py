"""Provider interface and resilient search orchestration."""

import json
import logging
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .config import Preferences, SearchConfig
from .database import Database
from .normalize import normalize_job
from .prefilter import prefilter
from .deduplicate import normalized_text

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    rows: list[dict]
    warnings: list[str] = field(default_factory=list)


class SearchProvider(Protocol):
    def search(self, source: str, query: str, config: SearchConfig) -> SearchResult: ...


def jobspy_arguments(source: str, query: str, config: SearchConfig) -> dict:
    return {
        "site_name": [source], "search_term": query,
        "google_search_term": f"{query} jobs in {config.location} in the last {config.days_old} days",
        "location": config.location, "country_indeed": config.country_indeed,
        "results_wanted": config.results_per_query, "hours_old": config.days_old * 24,
        "linkedin_fetch_description": config.linkedin_fetch_description,
        "description_format": "markdown", "verbose": 1,
    }


class JobSpyProvider:
    def search(self, source: str, query: str, config: SearchConfig) -> SearchResult:
        # A process boundary enforces a real timeout, including hung native HTTP
        # calls. subprocess.run kills and reaps the worker on timeout (Windows too).
        with tempfile.TemporaryDirectory(prefix="job-hunter-") as temp:
            request, response = Path(temp) / "request.json", Path(temp) / "response.json"
            request.write_text(json.dumps(jobspy_arguments(source, query, config)), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-m", "job_hunter.search_worker", str(request), str(response)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=config.timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode:
                raise RuntimeError((completed.stderr or completed.stdout)[-4000:].strip() or "JobSpy worker failed")
            rows = json.loads(response.read_text(encoding="utf-8"))
            diagnostics = "\n".join(line for line in completed.stderr.splitlines()
                                    if line.strip() and not re.search(r" - (INFO|DEBUG) - ", line))
            warnings = [diagnostics[-4000:]] if diagnostics else []
            return SearchResult(rows, warnings)


def infer_track(query: str, config: SearchConfig) -> str:
    text = normalized_text(query)
    for track in ("automation", "cybersecurity", "support", "qa"):
        terms = config.query_groups.get(track, []) + config.query_aliases.get(track, [])
        if any(re.search(r"\b" + re.escape(normalized_text(term)) + r"\b", text) for term in terms):
            return track
    if re.search(r"\b(sdet|automation|automatizacion)\b", text):
        return "automation"
    if re.search(r"\b(soc|security|cybersecurity|seguridad)\b", text):
        return "cybersecurity"
    if re.search(r"\b(support|soporte)\b", text):
        return "support"
    return "qa" if re.search(r"\b(qa|quality|tester|testing)\b", text) else "unknown"


def search_plan(config: SearchConfig, queries: list[str] | None = None,
                tracks: list[str] | None = None, quick: bool = False) -> list[dict]:
    if tracks and any(track not in config.query_groups for track in tracks):
        raise ValueError("Unknown career track; use qa, automation, cybersecurity or support")
    selected = tracks or list(config.query_groups)
    plan = []
    if queries:
        plan = [{"query": q, "track": infer_track(q, config), "location": config.location}
                for q in dict.fromkeys(queries)]
    else:
        for index, track in enumerate(selected):
            group = list(dict.fromkeys(config.query_groups[track]))
            if not group:
                continue
            plan.extend({"query": q, "track": track, "location": config.location} for q in (group[:1] if quick else group))
            if not quick:
                # One city probe per track, distributed across the configured
                # cities; do not multiply every synonym by every geography.
                if config.additional_locations:
                    plan.append({"query": group[0], "track": track,
                                 "location": config.additional_locations[index % len(config.additional_locations)]})
                if config.remote_probes:
                    plan.append({"query": f"{group[0]} remote", "track": track, "location": config.location})
    return list({(p["query"], p["location"], p["track"]): p for p in plan}.values())


def run_search(db: Database, preferences: Preferences, provider: SearchProvider | None = None,
               queries: list[str] | None = None, tracks: list[str] | None = None, quick: bool = False) -> dict:
    config = preferences.search
    official_sources = []
    if provider is None:
        from .official_sources import OfficialATSProvider, enabled_sources
        official_sources = enabled_sources(config)
        official_provider = OfficialATSProvider(official_sources)
        discovery_provider = JobSpyProvider()
    else:
        official_provider = discovery_provider = provider
    plan = search_plan(config, queries, tracks, quick)
    if not plan:
        raise ValueError("Configure at least one search query")
    run_id = db.start_run({**config.model_dump(), "effective_queries": sorted({p["query"] for p in plan}),
                           "plan": plan, "minimum_prefilter_score": preferences.prefilter.minimum_score})
    status, problems = "completed", False
    disabled = {}
    try:
        source_names = [*config.sources, *(source.source for source in official_sources)]
        combinations = [(p, s) for p in plan for s in dict.fromkeys(source_names)]
        for index, (planned, source) in enumerate(combinations):
            query, location, track = planned["query"], planned["location"], planned["track"]
            attempt = {"source": source, "query": query, "location": location, "track": track,
                       "scraped": 0, "valid": 0, "warnings": [], "error": None}
            if source in disabled:
                attempt.update(skipped=True, skip_reason=disabled[source])
                db.record_attempt(run_id, attempt)
                continue
            logger.info("Searching %s | %s | %s | %s", source, track, query, location)
            try:
                selected_provider = official_provider if ":" in source else discovery_provider
                result = selected_provider.search(source, query, config.model_copy(update={"location": location}))
                attempt["scraped"] = len(result.rows)
                attempt["warnings"] = result.warnings
                for index_row, row in enumerate(result.rows):
                    try:
                        normalized = normalize_job(row, source, query)
                        normalized.discovery_tracks = [track]
                        job, _ = db.upsert(normalized, run_id)
                        db.save(prefilter(job, preferences))
                        db.record_discovery(run_id, job.id, track, source, query, location)
                        attempt["valid"] += 1
                    except (ValueError, TypeError) as exc:
                        attempt["warnings"].append(f"Row {index_row}: {exc}")
                if not result.rows and not result.warnings:
                    attempt["warnings"].append("No jobs returned; may mean no matches or an upstream restriction")
                logger.info("%s | %s: %d scraped, %d valid", source, query, attempt["scraped"], attempt["valid"])
            except Exception as exc:
                attempt["error"] = f"{type(exc).__name__}: {exc}"
            diagnostics = " ".join(attempt["warnings"]).lower()
            if attempt["error"] or (not attempt["scraped"] and re.search(r"cursor|403|429|blocked|captcha|not available|error|denied", diagnostics)):
                attempt["provider_failed"] = True
                disabled[source] = attempt["error"] or diagnostics
                logger.warning("Disabling %s for the rest of this run; next run may try it again", source)
            if attempt["error"] or attempt["warnings"]:
                problems = True
                logger.warning("%s | %s: %s", source, query, attempt["error"] or "; ".join(attempt["warnings"]))
            db.record_attempt(run_id, attempt)
            if index < len(combinations) - 1:
                time.sleep(config.pause_seconds)
        status = "completed_with_warnings" if problems else "completed"
    except BaseException:
        status = "interrupted"
        raise
    finally:
        db.finish_run(run_id, status)
    return db.run_summary(run_id)
