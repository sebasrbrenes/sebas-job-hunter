"""Explicit manual discovery, bounded public feeds, no search-engine scraping."""
import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, quote

import requests

from .deduplicate import clean_url
from .models import SourceReference, utcnow
from .normalize import normalize_job
from .official_sources import load_catalog, normalize_greenhouse, normalize_lever, normalize_ashby
from .prefilter import prefilter

HOSTS = {"boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse",
         "jobs.lever.co": "lever", "jobs.ashbyhq.com": "ashby"}
FIELDS = {"url", "query", "discovered_at", "google_window", "notes", "google_displayed_date"}


def parse_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid URL length/type")
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname not in HOSTS or parts.username or parts.password or parts.port:
        raise ValueError("HTTPS ATS host required; credentials and ports forbidden")
    path = parts.path.strip("/").split("/")
    provider = HOSTS[parts.hostname]
    if provider == "greenhouse":
        if len(path) != 3 or path[1] != "jobs" or not path[2].isdigit():
            raise ValueError("Specific Greenhouse posting required")
        board, posting = path[0], path[2]
    else:
        if len(path) != 2:
            raise ValueError("Specific ATS posting required")
        board, posting = path
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", board) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", posting):
        raise ValueError("Invalid board or posting identifier")
    # Only posting identity crosses the HTTP boundary, never arbitrary queries.
    return provider, board, posting, f"https://{parts.hostname}/{'/'.join(path)}"


def read_input(path: Path, maximum: int):
    if path.stat().st_size > 1_000_000:
        raise ValueError("Input exceeds 1 MB")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = json.load(stream) if path.suffix.lower() == ".json" else list(csv.DictReader(stream)) if path.suffix.lower() == ".csv" else None
    if not isinstance(rows, list) or len(rows) > maximum:
        raise ValueError(f"Expected CSV/JSON rows, maximum {maximum}")
    for row in rows:
        if not isinstance(row, dict) or set(row) - FIELDS or not row.get("url"):
            raise ValueError("Expected discovery fields and required url")
        if any(not isinstance(value, str) or len(value) > 4096 for value in row.values()):
            raise ValueError("Discovery values must be bounded strings")
        if row.get("discovered_at"):
            stamp = datetime.fromisoformat(row["discovered_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("discovered_at requires timezone")
    return rows


class FeedClient:
    def __init__(self, timeout=15, pause=1, budget=20):
        self.timeout, self.pause, self.budget = timeout, pause, budget
        self.counts, self.cache = {}, {}

    def get(self, url):
        if url in self.cache:
            return self.cache[url]
        host = urlsplit(url).hostname
        for attempt in range(2):
            if self.counts.get(host, 0) >= self.budget:
                raise ValueError("Host request budget exhausted")
            self.counts[host] = self.counts.get(host, 0) + 1
            time.sleep(self.pause)
            # No redirects: an unexpected destination is never requested.
            with requests.get(url, timeout=self.timeout, allow_redirects=False, stream=True,
                              headers={"Accept": "application/json", "User-Agent": "SebasJobHunter/0.3"}) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 0:
                        delay = response.headers.get("Retry-After", "1")
                        if not delay.isdigit() or int(delay) > 10:
                            raise ValueError("Retry-After exceeds bounded wait; review later")
                        time.sleep(int(delay))
                        continue
                if 300 <= response.status_code < 400:
                    raise ValueError("Redirect refused; review manually")
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 8_000_000:
                        raise ValueError("Feed exceeds 8 MB response budget")
                    chunks.append(chunk)
                data = json.loads(b"".join(chunks))
                self.cache[url] = data
                return data


def resolve(provider, board, posting, client, catalog):
    matches = [c for c in catalog if c.provider == provider and
               (c.config.get("token") or c.config.get("site") or c.config.get("board")) == board]
    company = matches[0].company if len(matches) == 1 else None
    if provider == "greenhouse":
        item = client.get(f"https://boards-api.greenhouse.io/v1/boards/{quote(board, safe='')}/jobs/{posting}")
        if str(item.get("id")) != posting:
            raise ValueError("Posting ID mismatch")
        company = item.get("company_name") or company
        normalizer = normalize_greenhouse
    elif provider == "lever":
        item = client.get(f"https://api.lever.co/v0/postings/{quote(board, safe='')}/{posting}?mode=json")
        if str(item.get("id")) != posting:
            raise ValueError("Posting ID mismatch")
        normalizer = normalize_lever
    else:
        feed = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{quote(board, safe='')}")
        items = [item for item in feed.get("jobs", []) if clean_url(item.get("jobUrl")) == f"https://jobs.ashbyhq.com/{board}/{posting}"]
        if len(items) != 1:
            raise ValueError("Posting absent or ambiguous in official feed")
        item, normalizer = items[0], normalize_ashby
    # A board slug is not evidence of the employer's identity.
    if not company:
        raise ValueError("Company identity unconfirmed; review catalog mapping manually")
    row = normalizer(item, company)
    if not row.get("title") or not row.get("job_url") or not row.get("description"):
        raise ValueError("Insufficient official posting data")
    resolved, resolved_board, resolved_id, _ = parse_url(row["job_url"])
    if (resolved, resolved_board, resolved_id) != (provider, board, posting):
        raise ValueError("Unexpected official posting URL; review manually")
    return row, matches[0].source if len(matches) == 1 else f"{provider}:{board}"


def import_urls(db, preferences, path, maximum=40, client=None):
    if not 1 <= maximum <= 200:
        raise ValueError("maximum must be between 1 and 200")
    rows = read_input(path, maximum)
    catalog = load_catalog(preferences.search.official_catalog)
    client = client or FeedClient(pause=preferences.search.pause_seconds)
    db.connection.execute("""CREATE TABLE IF NOT EXISTS ats_candidates (
        url TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        status TEXT NOT NULL, reason TEXT, provenance TEXT NOT NULL, job_id TEXT)""")
    latest = db.run_summary()
    run_id = latest["id"] if latest else db.start_run({"mode": "google_manual", "minimum_prefilter_score": preferences.prefilter.minimum_score})
    counts = dict(input=len(rows), valid=0, resolved=0, unique=0, new=0, filtered=0, unknown=0)
    ids = set()
    for entry in rows:
        now = utcnow().isoformat()
        started = time.monotonic()
        provenance = {**entry, "schema_version": 1, "discovered_at": entry.get("discovered_at") or now, "method": "google_manual"}
        reason, job_id, provider, canonical = None, None, "unknown", None
        try:
            provider, board, posting, canonical = parse_url(entry["url"])
            counts["valid"] += 1
            row, source = resolve(provider, board, posting, client, catalog)
            job = normalize_job(row, source, entry.get("query", ""))
            job.resolved_provider, job.verification_status = provider, "official_feed"
            job.discovery_provenance = [provenance]
            for evidence in job.date_evidence:
                evidence["observed_at"] = now
            if entry.get("google_displayed_date"):
                job.date_evidence.append({"type": "google_displayed", "value": entry["google_displayed_date"],
                                          "url": canonical, "observed_at": now, "precision": "unknown"})
            job.source_urls.append(SourceReference(source="google_manual", url=canonical))
            job = prefilter(job, preferences)
            job, created = db.upsert(job, run_id)
            db.record_discovery(run_id, job.id, "manual", source, entry.get("query", ""), job.location)
            job_id = job.id
            ids.add(job_id)
            counts["resolved"] += 1
            counts["new"] += int(created)
            counts["filtered"] += int(job.prefilter_excluded or job.prefilter_score < preferences.prefilter.minimum_score)
        except (ValueError, KeyError, TypeError, requests.RequestException) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            reason = f"HTTP {status}" if status else str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            # HTTP error messages may contain URLs/query strings; never log them.
            counts["unknown"] += 1
        if canonical:
            with db.connection:
                old = db.connection.execute("SELECT provenance FROM ats_candidates WHERE url=?", (canonical,)).fetchone()
                history = json.loads(old[0]) if old else []
                if provenance not in history:
                    history.append(provenance)
                db.connection.execute("""INSERT INTO ats_candidates VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(url) DO UPDATE SET last_seen_at=excluded.last_seen_at,status=excluded.status,
                    reason=excluded.reason,provenance=excluded.provenance,job_id=excluded.job_id""",
                    (canonical, now, now, "resolved" if job_id else "review", reason, json.dumps(history), job_id))
        db.record_attempt(run_id, {"source": provider if not job_id else source, "track": "manual", "query": "manual URL import",
                                  "location": "", "scraped": 1, "valid": int(job_id is not None),
                                  "error": reason, "warnings": [], "seconds": round(time.monotonic() - started, 3),
                                  "stage": "official_resolution", "host": urlsplit(canonical).hostname if canonical else None})
    counts["unique"] = len(ids)
    previous_warnings = bool(latest and any(a.get("error") or a.get("warnings") for a in latest["attempts"]))
    db.finish_run(run_id, "completed_with_warnings" if counts["unknown"] or previous_warnings else "completed")
    return {"run_id": run_id, **counts, "coverage": "partial; manually supplied URLs only"}


def queries(preferences):
    domains = "(" + " OR ".join(f"site:{host}" for host in HOSTS) + ")"
    result = []
    for track, titles in preferences.search.query_groups.items():
        roles = "(" + " OR ".join('"' + title.replace('"', '') + '"' for title in titles) + ")"
        for region in ("Costa Rica", "LATAM", "Latin America", "Americas"):
            result.append({"track": track, "query": f'{domains} {roles} "Remote" "{region}"'})
    return result
