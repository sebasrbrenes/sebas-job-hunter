"""SQLite persistence. Canonical Pydantic records are stored as JSON payloads.

Identity keys and run membership are relational and indexed. Transactions keep
payloads and identities consistent; no ORM or external database is needed.
"""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from .deduplicate import identity_keys, merge_jobs, distinct_openings, preferred_job_url, strong_missing_location_match
from .models import ApplicationStatus, Job, utcnow


class Database:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload))
            );
            CREATE TABLE IF NOT EXISTS job_keys (
                key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS job_keys_job ON job_keys(job_id);
            CREATE TABLE IF NOT EXISTS search_runs (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, scraped INTEGER NOT NULL DEFAULT 0,
                config TEXT NOT NULL, attempts TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS run_jobs (
                run_id TEXT NOT NULL REFERENCES search_runs(id),
                job_id TEXT NOT NULL REFERENCES jobs(id), is_new INTEGER NOT NULL,
                PRIMARY KEY(run_id, job_id)
            );
            CREATE TABLE IF NOT EXISTS score_exports (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discoveries (
                run_id TEXT NOT NULL REFERENCES search_runs(id),
                job_id TEXT NOT NULL REFERENCES jobs(id), track TEXT NOT NULL,
                provider TEXT NOT NULL, query TEXT NOT NULL, location TEXT NOT NULL,
                PRIMARY KEY(run_id,job_id,track,provider,query,location)
            );
            CREATE TABLE IF NOT EXISTS run_exports (
                run_id TEXT NOT NULL REFERENCES search_runs(id),
                export_id TEXT NOT NULL REFERENCES score_exports(id),
                job_id TEXT NOT NULL REFERENCES jobs(id),
                PRIMARY KEY(run_id,export_id,job_id)
            );
            CREATE TABLE IF NOT EXISTS applications (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
                company TEXT NOT NULL, title TEXT NOT NULL, direct_url TEXT,
                applied_date TEXT NOT NULL, status TEXT NOT NULL,
                fit_score INTEGER, notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                CHECK(status IN ('Applied','Interview','Rejected','Ghosted','Offer','Withdrawn')),
                CHECK(fit_score IS NULL OR (fit_score BETWEEN 0 AND 100))
            );
            CREATE INDEX IF NOT EXISTS applications_status ON applications(status);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def jobs(self) -> list[Job]:
        return [Job.model_validate_json(r[0]) for r in self.connection.execute("SELECT payload FROM jobs")]

    def get(self, job_id: str) -> Job | None:
        row = self.connection.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def applications(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM applications ORDER BY applied_date DESC,created_at DESC")]

    def application_for(self, job_id: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def add_application(self, job_id: str, applied_date: str, status: str = "Applied", notes: str = "") -> dict:
        job = self.get(job_id)
        if not job:
            raise ValueError(f"Unknown job_id: {job_id}")
        status = ApplicationStatus(status).value
        try:
            from datetime import date
            date.fromisoformat(applied_date)
        except ValueError as exc:
            raise ValueError("applied_date must use YYYY-MM-DD") from exc
        now = utcnow().isoformat()
        with self.connection:
            try:
                self.connection.execute(
                    "INSERT INTO applications VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid4().hex, job.id, job.company, job.title, preferred_job_url(job), applied_date, status,
                     job.codex_score, notes.strip(), now, now))
            except sqlite3.IntegrityError as exc:
                if self.application_for(job_id):
                    raise ValueError("An application already exists for this job; use application-update") from exc
                raise
        return self.application_for(job_id)

    def update_application(self, job_id: str, status: str | None = None, notes: str | None = None,
                           append_notes: bool = False) -> dict:
        current = self.application_for(job_id)
        if not current:
            raise ValueError(f"No application exists for job_id: {job_id}")
        new_status = ApplicationStatus(status).value if status else current["status"]
        new_notes = current["notes"]
        if notes is not None:
            new_notes = (new_notes + ("\n" if new_notes and append_notes else "") + notes.strip()) if append_notes else notes.strip()
        with self.connection:
            self.connection.execute("UPDATE applications SET status=?,notes=?,updated_at=? WHERE job_id=?",
                                    (new_status, new_notes, utcnow().isoformat(), job_id))
        return self.application_for(job_id)

    def _save(self, job: Job):
        self.connection.execute("INSERT INTO jobs(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (job.id, job.model_dump_json()))

    def save(self, job: Job):
        with self.connection:
            self._save(job)

    def start_run(self, config: dict) -> str:
        run_id = uuid4().hex
        with self.connection:
            # is_new means discovered during the latest search, not forever.
            for job in self.jobs():
                if job.is_new:
                    job.is_new = False
                    self._save(job)
            self.connection.execute("INSERT INTO search_runs(id,started_at,status,config) VALUES (?,?,?,?)",
                                    (run_id, utcnow().isoformat(), "running", json.dumps(config)))
        return run_id

    def upsert(self, incoming: Job, run_id: str | None = None) -> tuple[Job, bool]:
        keys = identity_keys(incoming)
        if not keys:
            raise ValueError("Job lacks a source ID, usable URL, or complete company/title/location identity")
        with self.connection:
            found = set()
            for key in keys:
                row = self.connection.execute("SELECT job_id FROM job_keys WHERE key=?", (key,)).fetchone()
                if row:
                    if not key.startswith("natural:"):
                        found.add(row[0])
            # A missing region must not prevent a well-evidenced cross-board
            # match. Ambiguous multiple candidates deliberately stay separate.
            natural_keys = {key for key in keys if key.startswith("natural:")}
            matches = [j for j in self.jobs() if j.id not in found and
                       (strong_missing_location_match(j, incoming) or
                        (natural_keys.intersection(identity_keys(j)) and not distinct_openings(j, incoming)))]
            if len(matches) == 1:
                found.add(matches[0].id)
            if len(found) > 1:
                old = self._consolidate(found)
            else:
                old = self.get(next(iter(found))) if found else None
            if old is None and self.get(incoming.id):
                # Same natural identity can describe distinct requisitions.
                # Keep their strong aliases; never overwrite the other payload.
                incoming = incoming.model_copy(update={"id": uuid4().hex})
            created = old is None
            job = merge_jobs(old, incoming) if old else incoming.model_copy(deep=True)
            if old:
                job.is_new = False
                if run_id:
                    membership = self.connection.execute("SELECT is_new FROM run_jobs WHERE run_id=? AND job_id=?", (run_id, job.id)).fetchone()
                    job.is_new = bool(membership and membership[0])
            self._save(job)
            for key in set(keys + identity_keys(job)):
                self.connection.execute("INSERT OR IGNORE INTO job_keys(key,job_id) VALUES (?,?)", (key, job.id))
            if run_id:
                self.connection.execute("INSERT OR IGNORE INTO run_jobs VALUES (?,?,?)", (run_id, job.id, int(created)))
        return job, created

    def _consolidate(self, ids: set[str]) -> Job:
        """A new posting may bridge old records after identity enrichment.

        Require compatible natural identities or strong missing-location evidence.
        Keep aliases and historical run membership; stale losing score IDs fail
        import validation and must be re-exported.
        """
        jobs = sorted((self.get(job_id) for job_id in ids), key=lambda j: (j.first_seen_at, j.id))
        if any(distinct_openings(a, b) for index, a in enumerate(jobs) for b in jobs[index + 1:]):
            raise ValueError("Conflicting existing job identities; manual review needed")
        natural = [{k for k in identity_keys(j) if k.startswith("natural:")} for j in jobs]
        if not set.intersection(*natural) and not all(strong_missing_location_match(jobs[0], other) for other in jobs[1:]):
            raise ValueError("Conflicting existing job identities; manual review needed")
        winner = jobs[0]
        for loser in jobs[1:]:
            winner = merge_jobs(winner, loser)
            self._save(winner)
            self.connection.execute("UPDATE job_keys SET job_id=? WHERE job_id=?", (winner.id, loser.id))
            for table, columns in [("discoveries", "run_id,track,provider,query,location"), ("run_exports", "run_id,export_id")]:
                for row in self.connection.execute(f"SELECT {columns} FROM {table} WHERE job_id=?", (loser.id,)).fetchall():
                    placeholders = ",".join("?" for _ in range(len(row) + 1))
                    self.connection.execute(f"INSERT OR IGNORE INTO {table} ({columns},job_id) VALUES ({placeholders})", (*row, winner.id))
                self.connection.execute(f"DELETE FROM {table} WHERE job_id=?", (loser.id,))
            for row in self.connection.execute("SELECT run_id,is_new FROM run_jobs WHERE job_id=?", (loser.id,)).fetchall():
                self.connection.execute("INSERT OR IGNORE INTO run_jobs VALUES (?,?,?)", (row[0], winner.id, row[1]))
            self.connection.execute("DELETE FROM run_jobs WHERE job_id=?", (loser.id,))
            losing_application = self.application_for(loser.id)
            if losing_application:
                winning_application = self.application_for(winner.id)
                if winning_application:
                    combined = "\n".join(filter(None, [winning_application["notes"], losing_application["notes"]]))
                    self.connection.execute("UPDATE applications SET notes=?,updated_at=? WHERE job_id=?",
                                            (combined, utcnow().isoformat(), winner.id))
                    self.connection.execute("DELETE FROM applications WHERE job_id=?", (loser.id,))
                else:
                    self.connection.execute("UPDATE applications SET job_id=?,company=?,title=?,direct_url=?,updated_at=? WHERE job_id=?",
                                            (winner.id, winner.company, winner.title, preferred_job_url(winner), utcnow().isoformat(), loser.id))
            self.connection.execute("DELETE FROM jobs WHERE id=?", (loser.id,))
        earliest = self.connection.execute("SELECT r.id FROM search_runs r JOIN run_jobs j ON r.id=j.run_id WHERE j.job_id=? ORDER BY r.started_at,r.rowid LIMIT 1", (winner.id,)).fetchone()
        if earliest:
            self.connection.execute("UPDATE run_jobs SET is_new=(run_id=?) WHERE job_id=?", (earliest[0], winner.id))
        return winner

    def record_attempt(self, run_id: str, attempt: dict):
        with self.connection:
            row = self.connection.execute("SELECT attempts FROM search_runs WHERE id=?", (run_id,)).fetchone()
            attempts = json.loads(row[0]) + [attempt]
            self.connection.execute("UPDATE search_runs SET attempts=?,scraped=scraped+? WHERE id=?",
                                    (json.dumps(attempts), attempt.get("scraped", 0), run_id))

    def finish_run(self, run_id: str, status: str):
        with self.connection:
            self.connection.execute("UPDATE search_runs SET status=?,finished_at=? WHERE id=?", (status, utcnow().isoformat(), run_id))

    def run_summary(self, run_id: str | None = None) -> dict | None:
        row = self.connection.execute("SELECT * FROM search_runs WHERE id=?", (run_id,)).fetchone() if run_id else self.connection.execute("SELECT * FROM search_runs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        if not row:
            return None
        result = dict(row)
        result["attempts"] = json.loads(result["attempts"])
        result["config"] = json.loads(result["config"])
        members = self.connection.execute("SELECT job_id,is_new FROM run_jobs WHERE run_id=?", (result["id"],)).fetchall()
        result["unique"] = len(members)
        result["new"] = sum(r[1] for r in members)
        result["scored"] = sum(self.get(r[0]).codex_score is not None for r in members)
        result.update(self.telemetry(result, members))
        return result

    def record_discovery(self, run_id: str, job_id: str, track: str, provider: str, query: str, location: str):
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO discoveries VALUES (?,?,?,?,?,?)", (run_id, job_id, track, provider, query, location))

    def reconcile_duplicates(self) -> int:
        """Recheck stored pairs after enrichment, only when mutually unambiguous."""
        merged = 0
        with self.connection:
            jobs = self.jobs()
            def matches(a, b):
                natural = {k for k in identity_keys(a) if k.startswith("natural:")}
                return strong_missing_location_match(a, b) or bool(natural.intersection(identity_keys(b)) and not distinct_openings(a, b))
            candidates = {a.id: [b.id for b in jobs if a.id != b.id and matches(a, b)] for a in jobs}
            for first, others in candidates.items():
                if len(others) == 1 and candidates[others[0]] == [first] and self.get(first) and self.get(others[0]):
                    job = self._consolidate({first, others[0]})
                    for key in identity_keys(job):
                        self.connection.execute("INSERT OR IGNORE INTO job_keys VALUES (?,?)", (key, job.id))
                    merged += 1
        return merged

    def telemetry(self, run: dict, members) -> dict:
        attempts = [a for a in run["attempts"] if not a.get("skipped")]
        ids = {r[0] for r in members}
        new_flags = {r[0]: r[1] for r in members}
        jobs = {j.id: j for j in self.jobs() if j.id in ids}
        exported = {r[0] for r in self.connection.execute("SELECT job_id FROM run_exports WHERE run_id=?", (run["id"],))}
        rows = self.connection.execute("SELECT * FROM discoveries WHERE run_id=?", (run["id"],)).fetchall()
        threshold = run["config"].get("minimum_prefilter_score", 25)
        def breakdown(field):
            names = {a.get(field, "unknown") for a in attempts}
            result = {}
            for name in sorted(names):
                column = "provider" if field == "source" else field
                selected = {r["job_id"] for r in rows if r[column] == name}
                subset = [a for a in attempts if a.get(field, "unknown") == name]
                result[name] = {"attempts": len(subset), "raw": sum(a["scraped"] for a in subset),
                                "unique": len(selected), "new": sum(new_flags[i] for i in selected),
                                "exported": len(selected & exported),
                                "failures": sum(bool(a.get("error") or a.get("provider_failed")) for a in subset)}
            return result
        valid = sum(a.get("valid", 0) for a in attempts)
        return {"queries_executed": len(attempts), "distinct_queries": sorted({a["query"] for a in attempts}),
                "providers_queried": sorted({a["source"] for a in attempts}),
                "provider_failures": sum(bool(a.get("error") or a.get("provider_failed")) for a in attempts),
                "skipped_attempts": sum(bool(a.get("skipped")) for a in run["attempts"]),
                "previously_known": run["unique"] - run["new"],
                "duplicates_removed": max(0, valid - len(ids)), "invalid_rows": run["scraped"] - valid,
                "prefiltered": sum(not j.prefilter_excluded and j.prefilter_score >= threshold for j in jobs.values()),
                "exported": len(exported), "by_track": breakdown("track"), "by_provider": breakdown("source")}

    def run_job_ids(self, run_id: str) -> set[str]:
        return {r[0] for r in self.connection.execute("SELECT job_id FROM run_jobs WHERE run_id=?", (run_id,))}
