import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

import yaml

from .config import ROOT, Preferences, load_candidate, load_preferences
from .database import Database
from .report import generate_report
from .scoring import context_hash, export_scores, import_scores
from .search import run_search
from .availability import verify_top
from .models import ApplicationStatus


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(description="Local job discovery with interactive Codex scoring; no API key.")
    app.add_argument("--db", type=Path, default=ROOT / "data/jobs.sqlite3")
    app.add_argument("--candidate", type=Path, default=ROOT / "config/candidate.yaml")
    app.add_argument("--preferences", type=Path, default=ROOT / "config/preferences.yaml")
    commands = app.add_subparsers(dest="command", required=True)
    for name in ("search", "run-search"):
        command = commands.add_parser(name, help="Search + normalize + deduplicate + prefilter" + (" + export" if name == "run-search" else ""))
        command.add_argument("--source", action="append", help="Repeat for multiple boards; overrides configured sources")
        command.add_argument("--query", action="append", help="Repeat for multiple queries; overrides configured groups")
        command.add_argument("--location")
        command.add_argument("--track", action="append", choices=["qa", "automation", "cybersecurity", "support"])
        command.add_argument("--quick", action="store_true", help="One representative query per selected track; country only")
        command.add_argument("--days", type=positive)
        command.add_argument("--results", type=positive, help="Maximum requested per board/query")
        if name == "run-search":
            command.add_argument("--output", type=Path, default=ROOT / "data/to_score.json")
            command.add_argument("--limit", type=positive)
    command = commands.add_parser("export-score", help="Export unscored jobs for Codex")
    command.add_argument("--output", type=Path, default=ROOT / "data/to_score.json")
    command.add_argument("--limit", type=positive)
    command.add_argument("--all", action="store_true", help="Include historical jobs, not only latest search")
    command.add_argument("--include-filtered", action="store_true", help="Audit jobs below threshold or excluded")
    command = commands.add_parser("import-scores", help="Validate and atomically import Codex scores")
    command.add_argument("--input", type=Path, default=ROOT / "data/scored_jobs.json")
    command = commands.add_parser("report", help="Write today's ranked Markdown report")
    command.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    command.add_argument("--all", action="store_true")
    commands.add_parser("stats", help="Show persistent totals and latest search diagnostics")
    commands.add_parser("deduplicate", help="Conservatively reconcile stored duplicates after enrichment")
    command = commands.add_parser("verify", help="Conservative HTTP availability checks for highest-scored jobs")
    command.add_argument("--limit", type=positive, default=15)
    command = commands.add_parser("application-add", help="Track an application for an existing job")
    command.add_argument("job_id")
    command.add_argument("--date", required=True, dest="applied_date", help="Application date (YYYY-MM-DD)")
    command.add_argument("--status", choices=[item.value for item in ApplicationStatus], default="Applied")
    command.add_argument("--notes", default="")
    commands.add_parser("application-list", help="List tracked applications")
    command = commands.add_parser("application-update", help="Explicitly update a tracked application")
    command.add_argument("job_id")
    command.add_argument("--status", choices=[item.value for item in ApplicationStatus])
    command.add_argument("--notes")
    command = commands.add_parser("application-note", help="Append a note to a tracked application")
    command.add_argument("job_id")
    command.add_argument("notes")
    return app


def main(argv: list[str] | None = None) -> int:
    # Windows redirected streams otherwise use the legacy ANSI code page,
    # breaking UTF-8 consumers and paths containing names with accents
    # with accents. Keep CLI pipes consistent with our UTF-8 JSON files.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        preferences = load_preferences(args.preferences)
        candidate = load_candidate(args.candidate)
        with Database(args.db) as db:
            if args.command in {"search", "run-search"}:
                data = preferences.model_dump()
                for arg, field in [("source", "sources"), ("location", "location"), ("days", "days_old"), ("results", "results_per_query")]:
                    if getattr(args, arg) is not None:
                        data["search"][field] = getattr(args, arg)
                if args.source:
                    data["search"]["providers"] = {source: True for source in args.source}
                if args.location:
                    data["search"]["additional_locations"] = []
                preferences = Preferences.model_validate(data)
                result = run_search(db, preferences, queries=args.query, tracks=args.track, quick=args.quick)
                print(f"Search: {result['scraped']} scraped, {result['unique']} unique, {result['new']} new; {result['status']}")
                if args.command == "run-search":
                    batch = export_scores(db, candidate, preferences, args.output, args.limit)
                    print(f"Exported {len(batch['jobs'])} jobs to {args.output}. Codex semantic scoring is the next step.")
                if not result["unique"]:
                    print("No usable jobs returned. Inspect stats for board errors; try fewer sources or a broader age window.", file=sys.stderr)
                    return 2
            elif args.command == "export-score":
                batch = export_scores(db, candidate, preferences, args.output, args.limit, args.all, args.include_filtered)
                print(f"Exported {len(batch['jobs'])} jobs to {args.output}")
            elif args.command == "import-scores":
                count = import_scores(db, args.input, candidate, preferences)
                print(f"Imported {count} scores")
            elif args.command == "report":
                print(generate_report(db, args.output_dir, context_hash(candidate, preferences), args.all))
            elif args.command == "verify":
                print(json.dumps(verify_top(db, context_hash(candidate, preferences), args.limit), indent=2))
            elif args.command == "application-add":
                print(json.dumps(db.add_application(args.job_id, args.applied_date, args.status, args.notes), indent=2))
            elif args.command == "application-list":
                print(json.dumps(db.applications(), indent=2))
            elif args.command == "application-update":
                if args.status is None and args.notes is None:
                    raise ValueError("Provide --status and/or --notes")
                print(json.dumps(db.update_application(args.job_id, args.status, args.notes), indent=2))
            elif args.command == "application-note":
                print(json.dumps(db.update_application(args.job_id, notes=args.notes, append_notes=True), indent=2))
            elif args.command == "deduplicate":
                print(f"Merged {db.reconcile_duplicates()} duplicate pairs")
            elif args.command == "stats":
                jobs = db.jobs()
                context = context_hash(candidate, preferences)
                print(json.dumps({"total_unique": len(jobs), "new_latest_search": sum(j.is_new for j in jobs),
                                  "scored_current_profile": sum(j.codex_score is not None and j.score_context_hash == context for j in jobs),
                                  "latest_search": db.run_summary()}, indent=2, ensure_ascii=True))
        return 0
    except (ValueError, OSError, sqlite3.Error, yaml.YAMLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Search interrupted; completed results are saved.", file=sys.stderr)
        return 130
