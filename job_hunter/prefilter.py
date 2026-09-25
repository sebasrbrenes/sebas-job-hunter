"""Ranking aid, not a semantic hiring decision. Explain every signal."""

import re

from .config import Preferences
from .deduplicate import normalized_text
from .models import Job
from .experience import experience_minima
from .freshness import classify_freshness, freshness_bonus
from .geography import eligibility


def prefilter(job: Job, preferences: Preferences) -> Job:
    title = normalized_text(job.title)
    description = normalized_text(job.description)
    if re.search(r"\b(remote|remoto|remota|fully remote|100 remote)\b", description):
        job.work_arrangement = "remote"
    elif re.search(r"\b(hybrid|hibrido|hibrida)\b", description):
        job.work_arrangement = "hybrid"
    score, reasons = 20, []

    def signal(points: int, reason: str):
        nonlocal score
        score += points
        reasons.append(f"{points:+d}: {reason}")

    matched = False
    for tier, roles in preferences.role_tiers.items():
        if any(re.search(r"\b" + re.escape(normalized_text(role)) + r"\b", title) for role in roles):
            signal({"tier_1": 35, "tier_2": 30, "tier_3": 25, "tier_4": 20}.get(tier, 20), f"Desired title ({tier})")
            matched = True
            break
    relevant = re.search(r"\b(qa|quality assurance|quality engineer|test(?:ing)?|tester|sdet|soc|cybersecurity|security operations|security analyst|governance risk and compliance|technical support|application support|product support|support engineer|systems? support|production support)\b", title)
    if relevant and not matched:
        signal(25, "Relevant title keywords")
    elif not relevant and not matched:
        signal(-15, "Title is outside preferred role families; review description")
        evidence = [r"data quality|data integrity", r"application (?:support|maintenance)|soporte de aplicaciones",
                    r"triag|troubleshoot|incident", r"regression testing|pruebas de regresion",
                    r"security (?:operations|controls)|information security"]
        if sum(bool(re.search(pattern, description)) for pattern in evidence) >= 2:
            signal(20, "Multiple technical QA/support/security duties despite a different title")
    if re.search(r"\b(junior|jr|associate|entry level)\b", title):
        signal(10, "Junior/associate title")
    if re.search(r"\b(senior|sr|staff|principal|manager|director)\b", title):
        signal(-35, "Senior/management title")
    if re.search(r"\blead\b", title) and re.search(r"(manag\w*|lead\w*) (a |the )?team|leadership experience", description):
        signal(-30, "Lead title with leadership requirements")
    # Only inspect requirement-like sentences: mentoring senior colleagues is not seniority.
    required_years = any(years >= 5 for years in experience_minima(job.description))
    if required_years:
        signal(-30, "5+ years of experience appears required")
    if re.search(r"\b(python|testing|automation|networking|cybersecurity)\b", description):
        signal(8, "Relevant technical content")
    if "costa rica" in normalized_text(job.location) or re.search(r"\b(cr|heredia|san jose|cartago|alajuela)\b", normalized_text(job.location)):
        signal(10, "Costa Rica location")
    if job.is_remote or "remote" in normalized_text(job.location):
        signal(5, "Remote (eligibility still needs semantic review)")
    unrelated = re.search(r"\b(manufacturing|civil|mechanical|chemical|food|pharmaceutical)\b", title)
    if unrelated and not re.search(r"\b(software|application|technical support)\b", title):
        signal(-40, "Quality work in an unrelated discipline")
    excluded = False
    job.geographic_eligibility, job.geographic_reason = eligibility(job.location, job.description)
    if job.geographic_eligibility == "ineligible":
        signal(-100, job.geographic_reason)
        excluded = True
    if re.search(r"\bunpaid\b", title) or re.search(r"(?:this|the) (?:position|role|internship) is unpaid", job.description.lower()):
        signal(-80, "Explicitly unpaid role")
        excluded = True
    if not preferences.prefilter.allow_internships and (re.search(r"\b(intern|internship)\b", title) or job.employment_type == "internship"):
        signal(-70, "Internship role (disabled in preferences)")
        excluded = True
    job.prefilter_score = max(0, min(100, score))
    job.freshness = classify_freshness(job.date_posted)
    # A maximum three-point bonus cannot outweigh large fit/seniority penalties.
    if freshness_bonus(job.date_posted):
        signal(freshness_bonus(job.date_posted), "Recent posting")
        job.prefilter_score = max(0, min(100, score))
    job.prefilter_reasons = reasons
    job.prefilter_excluded = excluded
    return job
