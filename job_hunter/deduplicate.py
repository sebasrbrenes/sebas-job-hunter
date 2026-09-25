"""Conservative identity keys; never equate jobs on title alone."""

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Job, SourceReference

DISCOVERY_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.com", "google.com")


def normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def normalized_company(value: str) -> str:
    value = normalized_text(re.sub(r"\.(com|net|io|org)\s*$", "", value, flags=re.IGNORECASE))
    return re.sub(r"\s+(?:corporation|corp|inc|llc|ltd)$", "", value)


def normalized_location(value: str) -> str:
    loc = re.sub(r"\bcr\b", "costa rica", normalized_text(value))
    if "costa rica" in loc:
        # Indeed province abbreviations beside full city names (Heredia, H, CR).
        loc = re.sub(r"\b[a-z]\b", "", loc)
        if "san jose" in loc:
            loc = re.sub(r"\bsj\b", "", loc)
    return " ".join(sorted(set(loc.split())))


def clean_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        host = parts.netloc.lower()
        if parts.username or parts.password:
            return None
        path = parts.path.rstrip("/") or "/"
        if parts.hostname.lower() == "linkedin.com" or parts.hostname.lower().endswith(".linkedin.com"):
            match = re.search(r"/jobs/view/(?:.*-)?(\d+)$", path)
            if match:
                return f"https://www.linkedin.com/jobs/view/{match.group(1)}"
        tracking = {"trk", "trackingid", "ref", "refid", "source", "from", "fbclid", "gclid"}
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in tracking]
        return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))
    except ValueError:
        return None


def preferred_job_url(job: Job) -> str | None:
    """Prefer an employer/ATS vacancy URL while retaining board provenance."""
    urls = list(dict.fromkeys(filter(None, [job.job_url, *(ref.url for ref in job.source_urls)])))
    def rank(url: str):
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return 9
        is_board = any(host == domain or host.endswith("." + domain) for domain in DISCOVERY_HOSTS)
        return (1 if is_board else 0, 0 if url == job.job_url and job.source_kind == "official" else 1)
    return clean_url(min(urls, key=rank)) if urls else None


def identity_keys(job: Job) -> list[str]:
    keys = []
    refs = [SourceReference(source=job.source, source_job_id=job.source_job_id, url=job.job_url), *job.source_urls]
    for ref in refs:
        if ref.source_job_id:
            keys.append(f"source:{ref.source.casefold()}:{ref.source_job_id}")
        if url := clean_url(ref.url):
            parts = urlsplit(url)
            # A shared employer landing page is useful provenance, not a vacancy
            # identity. Query strings can carry a real requisition identifier.
            landing = parts.path.rstrip("/").lower() in {"", "/jobs", "/job", "/careers", "/career", "/opportunities", "/join-us"}
            if not landing or parts.query:
                keys.append(f"url:{url}")
    values = [normalized_company(job.company), normalized_text(job.title), normalized_text(job.location)]
    if all(value and not value.startswith("unknown") for value in values):
        values[2] = normalized_location(job.location)
        keys.append("natural:" + "|".join(values))
    return list(dict.fromkeys(keys))


def stable_id(job: Job) -> str:
    keys = identity_keys(job)
    key = next((k for k in keys if k.startswith("natural:")), keys[0] if keys else job.id)
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def content_hash(job: Job) -> str:
    fields = {key: value for key, value in job.model_dump(mode="json").items() if key in {
        "title", "company", "location", "description", "is_remote", "employment_type",
        "salary_min", "salary_max", "salary_currency", "salary_interval",
    }}
    fields["company"] = normalized_company(job.company)
    fields["location"] = normalized_location(job.location)
    fields["title"] = normalized_text(job.title)
    fields["description"] = " ".join(job.description.split())
    import json
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def description_similarity(a: Job, b: Job) -> float:
    left, right = normalized_text(a.description), normalized_text(b.description)
    if min(len(left), len(right)) < 250:
        return 0.0
    return SequenceMatcher(None, left.split(), right.split(), autojunk=False).ratio()


def distinct_openings(a: Job, b: Job) -> bool:
    """Evidence of distinct openings defeats a weak company/title match."""
    def requisition(job):
        return set(re.findall(r"(?:requisition|req(?:uisition)? id|job id)\s*[:#]\s*([\w-]+)", job.description.lower()))
    first, second = requisition(a), requisition(b)
    if first and second and first.isdisjoint(second):
        return True
    natural_a = (normalized_company(a.company), normalized_text(a.title), normalized_location(a.location))
    natural_b = (normalized_company(b.company), normalized_text(b.title), normalized_location(b.location))
    similarity = description_similarity(a, b)
    # Some boards append a per-result opaque token to otherwise identical text.
    # Exact copies stay conservative because they can represent distinct openings.
    if natural_a == natural_b and 0.995 <= similarity < 1.0 and (not a.date_posted or not b.date_posted or a.date_posted == b.date_posted):
        return False
    def source_ids(job):
        result = {}
        for ref in [SourceReference(source=job.source, source_job_id=job.source_job_id), *job.source_urls]:
            if ref.source_job_id:
                result.setdefault(ref.source, set()).add(ref.source_job_id)
        return result
    left, right = source_ids(a), source_ids(b)
    if any(left[source].isdisjoint(right[source]) for source in left.keys() & right.keys()):
        return True
    if not a.location.startswith("Unknown") and not b.location.startswith("Unknown"):
        if normalized_location(a.location) != normalized_location(b.location):
            return True
    if min(len(normalized_text(a.description)), len(normalized_text(b.description))) >= 250:
        return description_similarity(a, b) < 0.55
    return False


def strong_missing_location_match(a: Job, b: Job) -> bool:
    if a.source == b.source:
        return False
    if not (a.location.startswith("Unknown") or b.location.startswith("Unknown")):
        return False
    if normalized_company(a.company).startswith("unknown"):
        return False
    if (normalized_company(a.company), normalized_text(a.title)) != (normalized_company(b.company), normalized_text(b.title)):
        return False
    if a.date_posted and b.date_posted and abs((a.date_posted - b.date_posted).days) > 30:
        return False
    if distinct_openings(a, b):
        return False
    return description_similarity(a, b) >= 0.94


def merge_jobs(old: Job, incoming: Job) -> Job:
    merged = old.model_copy(deep=True)
    for field in ("title", "company", "location", "job_url", "date_posted", "salary_min",
                  "salary_max", "salary_currency", "salary_interval", "is_remote", "employment_type"):
        value = getattr(incoming, field)
        if value is not None and not (isinstance(value, str) and (not value or value.startswith("Unknown"))):
            setattr(merged, field, value)
    description_source = old.description_source or old.source
    # Official provenance survives later rediscovery on a secondary board.
    if old.source_kind == "official" or incoming.source_kind == "official":
        primary = incoming if incoming.source_kind == "official" else old
        merged.source_kind = "official"
        merged.source, merged.source_job_id = primary.source, primary.source_job_id
        merged.job_url = primary.job_url
        merged.work_arrangement = primary.work_arrangement
    if incoming.description and (incoming.source == description_source or len(incoming.description) > len(old.description)):
        merged.description = incoming.description
        merged.description_source = incoming.description_source or incoming.source
    refs = [*old.source_urls, *incoming.source_urls]
    merged.source_urls = list({(r.source, r.source_job_id, r.url): r for r in refs}.values())
    merged.search_queries = sorted(set(old.search_queries + incoming.search_queries))
    merged.discovery_tracks = sorted(set(old.discovery_tracks + incoming.discovery_tracks))
    merged.last_seen_at = max(old.last_seen_at, incoming.last_seen_at)
    if content_hash(merged) != content_hash(old) or clean_url(merged.job_url) != clean_url(old.job_url):
        merged.availability = "unknown"
        merged.availability_checked_at = None
        merged.availability_reason = "Posting content or URL changed; check again"
    if content_hash(merged) != content_hash(old):
        merged.codex_score = None
        merged.recommendation = None
        merged.category = None
        merged.strengths, merged.gaps, merged.dealbreakers = [], [], []
        merged.score_reasoning = None
        merged.score_context_hash = None
        merged.status = "discovered"
    return merged
