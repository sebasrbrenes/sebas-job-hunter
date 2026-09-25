import html
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .database import Database
from .deduplicate import clean_url, preferred_job_url
from .models import Recommendation
from .freshness import classify_freshness, freshness_bonus

COSTA_RICA = timezone(timedelta(hours=-6))
TRACK_LABELS = {"qa": "QA", "automation": "QA Automation", "cybersecurity": "Cybersecurity", "support": "Technical Support"}


def safe_text(value) -> str:
    value = html.escape(str(value if value is not None else "Unknown"), quote=False)
    value = " ".join(value.split())
    return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", value)


def bullets(items: list[str]) -> str:
    return "; ".join(safe_text(item) for item in items) or "None recorded"


def job_link(url: str | None) -> str:
    url = clean_url(url)
    return f"[Open job]({quote(url, safe=':/?=&%#@+;,~')})" if url else "URL unavailable"


def generate_report(db: Database, directory: Path, context: str, all_jobs: bool = False) -> Path:
    now = datetime.now(COSTA_RICA)
    summary = db.run_summary()
    ids = db.run_job_ids(summary["id"]) if summary and not all_jobs else None
    jobs = [j for j in db.jobs() if (ids is None or j.id in ids)]
    scored = [j for j in jobs if j.codex_score is not None and j.score_context_hash == context]
    modality_rank = {"remote": 3, "hybrid": 2, "onsite": 1, "unknown": 0}
    career_rank = {"qa": 4, "qa_automation": 3, "cybersecurity": 2, "technical_support": 1, "other": 0}
    ranked = sorted(scored, key=lambda j: (-j.codex_score, -freshness_bonus(j.date_posted),
                    -modality_rank.get(j.work_arrangement, 0), -career_rank.get(str(j.category), 0),
                    -j.prefilter_score, j.title.casefold(), j.id))
    applications = db.applications()
    applications_by_job = {item["job_id"]: item for item in applications}
    lines = ["# Sebas Job Hunter", "", f"Generated: {now.isoformat(timespec='seconds')} (Costa Rica)",
             f"Search date: {summary['started_at'] if summary else 'No search run yet'}",
             f"Search status: {summary['status'] if summary else 'Not run'}", "",
             f"- Number scraped (latest run, before deduplication): {summary['scraped'] if summary else 0}",
             f"- Number unique (latest run): {summary['unique'] if summary else 0}",
             f"- Number new (latest run): {summary['new'] if summary else 0}",
             f"- Number historical in latest run: {(summary['previously_known'] if summary else 0)}",
             f"- Number semantically scored (report scope, current profile): {len(scored)}",
             f"- Applications tracked: {len(applications)}",
             f"- Report scope: {'all stored jobs; old listings may be closed' if all_jobs else 'latest search run'}", "",
             "Ordering uses semantic fit first, then freshness, work arrangement and career category. Scores reflect candidate fit, not confirmed eligibility or an application. Verify the posting before applying.", ""]
    lines.extend(["## JOB HUNT SUMMARY", "",
                  f"{summary['scraped'] if summary else 0} scraped · {summary['unique'] if summary else len(jobs)} unique · "
                  f"{summary['new'] if summary else 0} new · {len(scored)} analyzed", "",
                  "Recommended: " + " · ".join(f"{value.value}: {sum(j.recommendation == value for j in scored)}" for value in Recommendation if value != Recommendation.SKIP), ""])
    if summary:
        lines.extend([f"Queries executed (provider/query/location calls): {summary['queries_executed']}; providers: {', '.join(summary['providers_queried'])}",
                      f"Provider failures: {summary['provider_failures']}; skipped attempts: {summary['skipped_attempts']}",
                      f"Previously known: {summary['previously_known']}; duplicates removed: {summary['duplicates_removed']}; invalid rows: {summary['invalid_rows']}",
                      f"Passed prefilter: {summary['prefiltered']}; exported for Codex during run: {summary['exported']}", "",
                      "By discovery track (a job may occur in several tracks):", "",
                      "| Track | Raw | Unique | New | Exported |", "| --- | ---: | ---: | ---: | ---: |"])
        track_order = list(TRACK_LABELS)
        for track in sorted(summary["by_track"], key=lambda t: track_order.index(t) if t in track_order else 99):
            data = summary["by_track"][track]
            lines.append(f"| {safe_text(TRACK_LABELS.get(track, track))} | {data['raw']} | {data['unique']} | {data['new']} | {data['exported']} |")
        lines.extend(["", "By provider:", "", "| Provider | Calls | Raw | Unique | Failures |", "| --- | ---: | ---: | ---: | ---: |"])
        for provider, data in summary["by_provider"].items():
            lines.append(f"| {safe_text(provider)} | {data['attempts']} | {data['raw']} | {data['unique']} | {data['failures']} |")
        lines.append("")
    for recommendation, heading in [(Recommendation.EXCEPTIONAL, "🔥 Exceptional"),
                                    (Recommendation.STRONG_APPLY, "🟢 Strong Apply"),
                                    (Recommendation.APPLY, "✅ Apply"),
                                    (Recommendation.CONSIDER, "🟡 Consider")]:
        lines.extend([f"## {heading}", ""])
        group = [j for j in ranked if j.recommendation == recommendation]
        if not group:
            lines.extend(["No jobs in this category yet.", ""])
        for job in group:
            sources = sorted({job.source, *(ref.source for ref in job.source_urls)})
            application = applications_by_job.get(job.id)
            lines.extend([f"### {safe_text(job.title)} — {safe_text(job.company)}", "",
                          f"- Score: **{job.codex_score}/100**",
                          f"- Location: {safe_text(job.location)}",
                          f"- Primary source: **{safe_text(job.source_kind)}** ({safe_text(job.source)})",
                          f"- All sources: {safe_text(', '.join(sources))}",
                          f"- Work arrangement: {safe_text(job.work_arrangement)}",
                          f"- Geographic eligibility: **{safe_text(job.geographic_eligibility)}** — {safe_text(job.geographic_reason)}",
                          f"- Date posted: {safe_text(job.date_posted)}",
                          f"- Freshness: {classify_freshness(job.date_posted)}; last seen: {job.last_seen_at.isoformat()}",
                          f"- Availability: **{job.availability}** (checked {safe_text(job.availability_checked_at)})",
                          f"- Availability evidence: {safe_text(job.availability_reason or 'Not checked')}",
                          f"- Discovery tracks: {safe_text(', '.join(job.discovery_tracks) or 'unknown')}",
                          f"- Job URL: {job_link(preferred_job_url(job))}",
                          f"- Category: {safe_text(job.category)}",
                          f"- Why it matches: {bullets(job.strengths)}",
                          f"- Important gaps: {bullets(job.gaps)}",
                          f"- Dealbreakers: {bullets(job.dealbreakers)}",
                          f"- Recommendation: {safe_text(job.recommendation)}",
                          f"- Reasoning: {safe_text(job.score_reasoning)}",
                          f"- Discovery: {'new this search' if job.is_new else 'previously seen'}; first seen {job.first_seen_at.date()}", ""])
            if application:
                lines.extend([f"- Application: **{safe_text(application['status'])}**; applied {safe_text(application['applied_date'])}",
                              f"- Application notes: {safe_text(application['notes'] or 'None')}", ""])
    lines.extend(["## Applications", ""])
    if not applications:
        lines.extend(["No applications tracked.", ""])
    else:
        lines.extend(["| Applied | Status | Company | Title | Fit | Notes |", "| --- | --- | --- | --- | ---: | --- |"])
        for item in applications:
            lines.append(f"| {safe_text(item['applied_date'])} | {safe_text(item['status'])} | {safe_text(item['company'])} | {safe_text(item['title'])} | {safe_text(item['fit_score'])} | {safe_text(item['notes'] or '')} |")
        lines.append("")
    if summary:
        warnings = [a for a in summary["attempts"] if a["error"] or a["warnings"]]
        if warnings:
            lines.extend(["## Search issues", ""])
            for attempt in warnings:
                details = attempt["error"] or "; ".join(attempt["warnings"])
                lines.append(f"- {safe_text(attempt['source'])} / {safe_text(attempt['query'])}: {safe_text(details[-1200:])}")
            lines.append("")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now.date()}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
