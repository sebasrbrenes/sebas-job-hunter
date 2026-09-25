"""Read-only, bounded HTTP checks. Blocking is never evidence of expiration."""
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .database import Database
from .deduplicate import clean_url, normalized_text
from .models import Job, utcnow


@dataclass
class Page:
    status: int
    body: str
    url: str


def public_url(url: str):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Only public HTTP(S) URLs can be checked")
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Private/local addresses are not checked")


class PublicRedirects(HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(url: str) -> Page:
    public_url(url)
    request = Request(url, headers={"User-Agent": "SebasJobHunter/0.1 (vacancy availability check)"})
    try:
        with build_opener(PublicRedirects()).open(request, timeout=8) as response:
            body = response.read(1_000_000).decode("utf-8", errors="replace")
            return Page(response.status, body, response.url)
    except HTTPError as exc:
        return Page(exc.code, "", url)


def assess_page(job: Job, page: Page) -> tuple[str, str]:
    if page.status in {404, 410}:
        return "possibly_unavailable", f"HTTP {page.status}; vacancy URL may have been removed"
    text = page.body.lower()
    if page.status != 200 or re.search(r"captcha|access denied|verify you are human|sign in to continue", text):
        return "unknown", f"HTTP {page.status} or access challenge; not evidence of expiration"
    title_words = set(normalized_text(job.title).split())
    def matches(value):
        return bool(title_words) and len(title_words & set(normalized_text(value).split())) / len(title_words) >= 0.8
    closed = re.search(r"no longer accepting applications|this job (?:has expired|is no longer available)|this position has been filled|esta (?:oferta|vacante) (?:ha expirado|ya no esta disponible)", normalized_text(page.body))
    if closed and matches(page.body):
        return "possibly_unavailable", "Posting contains a job-specific closed/expired notice"
    def objects(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from objects(child)
    for raw in re.findall(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", page.body, flags=re.I | re.S):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for obj in objects(data):
            if "JobPosting" not in str(obj.get("@type", "")) or not matches(str(obj.get("title", ""))):
                continue
            expiry = obj.get("validThrough")
            if expiry:
                try:
                    when = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                    when = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when
                    if when < utcnow():
                        return "possibly_unavailable", f"Matching JobPosting validThrough has passed: {expiry}"
                except ValueError:
                    return "unknown", "Matching JobPosting has an unreadable expiration date"
            return "available", "Matching JobPosting structured data is present; no elapsed expiration found"
    return "unknown", "No reliable active or expired vacancy evidence; HTTP 200 alone is insufficient"


def verify_job(job: Job, fetch=fetch_page) -> Job:
    urls = list(dict.fromkeys(url for url in [*(ref.url for ref in job.source_urls), job.job_url] if clean_url(url)))
    boards = ("linkedin.com", "indeed.com", "glassdoor.com", "google.com")
    urls.sort(key=lambda url: any((urlsplit(url).hostname or "").endswith(domain) for domain in boards))
    findings = []
    for url in urls[:2]:
        try:
            result, reason = assess_page(job, fetch(url))
        except (OSError, ValueError) as exc:
            result, reason = "unknown", f"Check could not complete: {type(exc).__name__}"
        findings.append((result, f"{url}: {reason}"))
        if result == "available":
            break
    if any(status == "available" for status, _ in findings):
        job.availability = "available"
    elif any(status == "possibly_unavailable" for status, _ in findings):
        job.availability = "possibly_unavailable"
    else:
        job.availability = "unknown"
    job.availability_checked_at = utcnow()
    job.availability_reason = "; ".join(reason for _, reason in findings) or "No usable public URL"
    return job


def verify_top(db: Database, context: str, limit: int = 15, fetch=fetch_page) -> dict:
    latest = db.run_summary()
    ids = db.run_job_ids(latest["id"]) if latest else {j.id for j in db.jobs()}
    jobs = sorted((j for j in db.jobs() if j.id in ids and j.codex_score is not None
                   and j.codex_score >= 60 and j.score_context_hash == context), key=lambda j: -j.codex_score)[:limit]
    totals = {"available": 0, "possibly_unavailable": 0, "unknown": 0}
    for job in jobs:
        checked = verify_job(job, fetch)
        db.save(checked)
        totals[checked.availability] += 1
    return totals
