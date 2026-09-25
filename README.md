# Sebas Job Hunter

A working local V0.2 job-search assistant for a candidate in Costa Rica. It finds
jobs through discovery boards and public employer ATS feeds, stores discoveries in SQLite, and prepares a
shortlist for **the current Codex agent** to evaluate. Ranked results are saved as
Markdown. **V0 does not automatically apply to jobs.**

No OpenAI API, API key, browser automation, resume tailoring, or recursive Codex
process is used. Job descriptions are data, never instructions for the agent.

## Setup on Windows 11

Requires **Python 3.12**. JobSpy's pinned NumPy dependency is why this V0 explicitly
targets 3.12 rather than using whichever system Python happens to be installed.
Open PowerShell in this repository:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config\candidate.example.yaml config\candidate.yaml
Copy-Item config\profile_questions.example.yaml config\profile_questions.yaml
Copy-Item AGENTS.example.md AGENTS.md
.\.venv\Scripts\python.exe -m pytest -q
```

The supplied environment has already been installed and tested. The commands
above also reproduce it on a new machine. For normal use, `pip install -e .`
omits pytest. Activation is optional:

```powershell
.\.venv\Scripts\Activate.ps1
python -m job_hunter --help
```

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` directly for
every command below. There is no need to change your execution policy.

## First real hunt through Codex

Open this repository in Codex and say:

> Run job hunt. Read my profile and preferences, search the configured boards,
> score the exported jobs using AGENTS.md, import the scores, and generate the
> report. Show the best opportunities and any search errors.

The local-only `AGENTS.md` instructs Codex to actually execute the full workflow;
start from the tracked `AGENTS.example.md`. Check `config/candidate.yaml` for
factual accuracy before relying on recommendations. The professional evidence is
deliberately distinct from technical knowledge.

For a faster initial search, say:

> Run job hunt using LinkedIn and Indeed, query QA Analyst, at most 5 results per
> board. Complete scoring and reporting too.

## Commands

These examples assume the virtual environment is active:

```powershell
# Search + normalize + deduplicate + deterministic prefilter
python -m job_hunter search

# Same pipeline, then export the best unscored jobs for Codex
python -m job_hunter run-search

# Small live check (no applications are submitted)
python -m job_hunter run-search --query "QA Analyst" --source linkedin --source indeed --results 5

# Small validation across all four tracks (8 provider calls by default)
python -m job_hunter run-search --quick --results 4
python -m job_hunter run-search --track cybersecurity --quick --results 4

# Export latest search's jobs needing semantic scores (default limit 40)
python -m job_hunter export-score --limit 40

# Codex reads the export and writes data/scored_jobs.json, then:
python -m job_hunter import-scores
python -m job_hunter verify --limit 15
python -m job_hunter report
python -m job_hunter stats

# Application tracking (records only; never submits anything)
python -m job_hunter application-add JOB_ID --date 2026-09-22 --notes "Submitted on official site"
python -m job_hunter application-list
python -m job_hunter application-update JOB_ID --status Interview
python -m job_hunter application-note JOB_ID "Technical screen scheduled"

# Optional historical backlog and filter audit
python -m job_hunter export-score --all --include-filtered --limit 50
python -m job_hunter report --all
python -m job_hunter deduplicate

# Wider age window or narrower location
python -m job_hunter search --days 14 --location "Heredia, Costa Rica"
```

`--query` and `--source` can be repeated and replace configured defaults for that
invocation. `--results` is per board/query, not a total. `--days` defaults to seven.
CLI overrides do not modify YAML. Global options go **before** the command:

```powershell
python -m job_hunter --db data/another.sqlite3 --preferences config/preferences.yaml stats
python -m job_hunter export-score --output data/custom_export.json
python -m job_hunter import-scores --input data/custom_scores.json
python -m job_hunter report --output-dir reports
```

Default paths resolve to this repository even when invoked from another directory
after editable installation. Explicit relative paths resolve from your current
directory. Exit codes: `0` completed (possibly partial board failures), `1`
configuration/import/storage error, `2` search returned no usable jobs, `130`
interrupted. Read warnings and `stats`, even when exit code is zero.

## Configuration

`candidate.yaml` contains the candidate's private evidence and is intentionally
ignored by Git. Start from `config/candidate.example.yaml` and keep the completed
file local. `preferences.yaml` contains
all requested role tiers, eligible/preferred regions, career priorities, tolerated
gaps, grouped search queries and conservative prefilter controls.

`config/companies.yaml` is the editable target-company catalog. Each entry has an
`enabled` switch, official careers URL, integration status and only the identifiers
required by its verified ATS. Integrated sources currently include Experian through
SmartRecruiters, TransUnion and World Fuel Services through Workday CXS, and Konrad
through Greenhouse. Manual-review entries remain disabled until a stable supported
endpoint is verified. Provider-family switches live under `search.providers` in
`preferences.yaml`; disabling a family or company requires no code change.

Candidate schema version 2 is validated by `load_candidate` before CLI operations.
Each evidence item has a status (`verified_professional`,
`verified_non_professional`, `learning`, or `unknown`), a basis and a source.
"Verified" means confirmed by the candidate, not externally certified. Professional,
project and academic skill claims require references to supporting records;
transferable interpretations also reference facts and retain explicit limitations.
Validation checks structure and classification consistency, not the truth of prose.
Unversioned V0 profiles remain supported without upgrading their claims.

Education completion, language certification, ongoing learning and career
direction are separate fields. Null unknowns and empty project lists mean evidence
has not been supplied, not that experience or credentials do not exist.
`config/profile_questions.yaml` contains private prioritized unanswered questions;
it is ignored by Git, is not candidate evidence, and is not included in score
exports. Start from `config/profile_questions.example.yaml`. Add answers to the
profile only after the candidate confirms them. Updating the profile makes previous
scores stale for the current context; use a fresh export and semantic assessment.
This schema does not implement V1, applications or resume generation.

Defaults enable LinkedIn, Indeed and the verified official catalog. Glassdoor and Google remain available but
disabled following Costa Rica validation. Set `search.providers.<name>: true`
to re-enable either, or override with repeated `--source` flags. A failed provider
is skipped for the rest of that run and may be attempted again in a later run.
An ordinary empty result alone does not disable a provider.

The normal plan has three representative queries per career track, one city
probe per track distributed across San Jose/Heredia, and one remote-keyword probe
per track: 20 searches across two providers (40 bounded calls). Synonyms in
`query_aliases` improve attribution without all becoming separate searches.
`--quick` executes one country-wide query per track, with no extra probes.
An explicit `--query` also avoids geographic multiplication. Each call requests
at most 15 rows by default, has a 90-second timeout, and a two-second pause between
attempts. Use `--results 4` for validation; a normal hunt can take several minutes.

`search.location` defaults to Costa Rica; `additional_locations` and
`remote_probes` control the limited extra probes. Cartago, Alajuela and other
Costa Rica locations remain eligible regardless of query city. Remote probes add
the word remote while retaining Costa Rica; they cannot guarantee coverage of all
LATAM/Americas vacancies. Verify geographic eligibility during semantic scoring.
`is_remote` is not passed alongside the Indeed age filter because JobSpy documents
that combination as unsupported.

Location screening records `eligible`, `ineligible`, or `unknown`. Costa Rica,
San José, Heredia, Alajuela, Cartago and GAM locations are accepted. Remote
LATAM/Americas is accepted only when the text does not clearly exclude Costa Rica;
plain `Remote` stays unknown. Explicit mandatory residence outside Costa Rica is
excluded by the deterministic prefilter. This is location screening, not proof of
citizenship, work authorization or employer willingness to hire.

`prefilter.minimum_score` defaults to 25 and `export_limit` to 40. Degree and
isolated tooling gaps do not exclude jobs. Senior title/required experience and
unrelated discipline reduce rank; explicit unpaid roles and internship roles
(unless enabled) are excluded. Borderline jobs should reach Codex. Use the audit
flag to inspect misses. All identifiable jobs remain stored, including rejects.
Spanish and English experience ranges use their lower bound: `1-5 años` is one,
not five. Multiple technical duties can rescue relevant fallback titles such as
Data Operations Analyst. Freshness adds at most three prefilter points; final
ranking always prioritizes Codex fit score and uses freshness only as a tie-breaker.

## Codex scoring exchange

1. `run-search` or `export-score` writes `data/to_score.json`: a versioned export
   with an export ID, candidate/preferences, and full normalized job descriptions.
2. Codex reads it and the six-dimension 100-point rubric in `AGENTS.md`, and writes
   `data/scored_jobs.json`. `data/score_schema.json` documents the exact schema.
3. `import-scores` validates **all records before writing any**. The envelope is
   `{ "schema_version": 1, "export_id": "...", "scores": [...] }`. Each record
   requires `job_id`, integer `score`, `recommendation`, `category`, `strengths`,
   `gaps`, `dealbreakers`, and `reasoning`. See the complete example in `AGENTS.md`.
4. `report` ranks current-profile scores into Exceptional, Strong Apply, Apply,
   and Consider. Skip jobs stay in SQLite but are omitted from the report.

The rubric assigns role relevance 25 points, professional experience 20,
technical alignment 20, seniority feasibility 15, location 10, and career value
10. Recommendations map exactly to 90/80/70/60 thresholds. Scores cannot be
generated by just running Python: the interactive Codex evaluation is intentional.

Exports default to the latest search and omit already-scored, unchanged jobs.
The importer verifies the original export, job content, and candidate/preferences
snapshot. Changed content requires reevaluation. Partial batches are allowed;
reimport is idempotent. Running another export does not silently change an older
snapshot. Use the current export ID when completing the current hunt.

## Architecture and persistence

```text
JobSpy boards + public ATS feeds → normalize → SQLite upsert/deduplicate → prefilter
                                                        ↓
                       data/to_score.json → current Codex agent
                                                        ↓
                      reports/YYYY-MM-DD.md ← import scored_jobs.json
```

`search.py` exposes a small provider protocol; replace or add a provider without
changing the model, database or scoring flow. `search_worker.py` isolates each
JobSpy call in a bounded subprocess and uses its documented `scrape_jobs` API.
Failed calls and upstream diagnostics are recorded per source/query in SQLite.
`official_sources.py` implements bounded HTTP clients for SmartRecruiters Posting
API, Workday CXS, Greenhouse Job Board API and Lever Postings API. The last two are
available whenever a verified company token/site is added to the catalog. A failed
company source is disabled only for that run and does not stop other providers.

`jobs` stores complete validated canonical records as JSON payloads. `job_keys`
stores indexed source IDs, cleaned URLs and normalized company/title/location
keys. `search_runs` and `run_jobs` retain per-run diagnostics and discovery
counts; `discoveries` records query/track/provider/location attribution and
`run_exports` records exported jobs. Track/provider counts can overlap: a vacancy
found in two tracks is one global unique job. Exported counts are distinct jobs
exported during the run; already-current scores are reused, not exported again.
`score_exports` retains the content/context hashes used during import.
Use `json_extract(payload, '$.title')` to inspect fields directly with SQLite.

Company/title/location keys handle accents, punctuation, repeated location words,
CR/Costa Rica, single-letter province codes, and company domain suffixes such as
`.com`. Equivalent company/location formatting does not invalidate scores.
Missing identity fields never collapse unrelated jobs into one
unknown record. A source ID or URL can still identify partially populated rows.
Cross-provider missing-location matches require matching company/title, long
highly similar descriptions, compatible dates and no conflicting requisition
evidence. Conflicting same-provider IDs, explicit requisitions, locations or
substantially different descriptions protect distinct openings. Ambiguous
multiple matches stay separate. `deduplicate` reconciles mutually unambiguous
stored pairs after enrichment and preserves discovery history and source links.
All useful source links and search queries are retained. Rich descriptions survive
empty duplicate postings. The selected description's source is remembered, so
alternating shorter summaries from another board do not erase valid scores.
Updates from that selected source can still replace changed descriptions. Bare
shared careers-page URLs are retained as links but are not vacancy identity keys.
When the same vacancy is found through a discovery board and a verified ATS, the
official source and official vacancy URL become primary while every source URL is
kept as provenance.

`applications` is an additive SQLite table linked to canonical jobs. It snapshots
company, title, official/direct URL and fit score at the time of tracking. One row
per job prevents accidental duplicates; `application-update` and `application-note`
are the explicit mutation paths. Allowed states are Applied, Interview, Rejected,
Ghosted, Offer and Withdrawn. Tracking never opens or submits an application.

`first_seen_at` remains stable; `last_seen_at` advances on rediscovery. `is_new`
means discovered in the latest search run, including duplicates within that run.
Earlier discoveries are not new again. Historical run counts remain available.
Reports use Costa Rica's UTC−6 date and overwrite the same day's Markdown file.
The default report includes the latest search; `--all` includes historical scores.

Runtime databases, exports, logs, reports, caches and environment/secrets are
ignored by Git. **Keep `data/jobs.sqlite3` to preserve memory.** Close running
commands before backing up the database; when SQLite WAL files exist, include
them too or use SQLite's backup facility. No credentials are required.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q -W error
.\.venv\Scripts\python.exe -m pip check

# Explicit network check; excluded from the normal offline suite
$env:JOB_HUNTER_LIVE_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -q -m smoke tests/test_official_sources.py
```

Normal tests use temporary databases and mocked providers, never live boards. They cover
normalization, identity keys, duplicate upserts and timestamps, prefilter context,
provider failures/timeouts, scoring validation/atomicity/staleness, export scope,
official-source priority, geography, application tracking, migration compatibility,
and report order. The opt-in smoke test performs a bounded read of Experian's public
SmartRecruiters feed. An integrated fixture runs search twice, exports, imports a
test assessment and renders a report without polluting the real database.

## Troubleshooting and limitations

- **Glassdoor / Costa Rica:** JobSpy documents Costa Rica for Indeed but does
  not list Glassdoor support for that country. It is disabled by default but can
  be re-enabled for a future validation. Failure is logged; other boards continue.
- **403/429, CAPTCHA, empty Google results:** upstream restrictions and changing
  board markup are common. Wait and narrow the run or use another supported
  board. Zero rows can mean no matches or a provider issue; it is reported as a
  diagnostic rather than proof of an empty market. No bypass automation is built.
- **Employer catalog coverage:** Equifax, Moody's, Amazon, IBM, Encora, Gorilla
  Logic and SimSpace have verified official careers pages but no activated endpoint
  in this release. SimSpace currently points to Ashby; Encora exposes Greenhouse
  job IDs without a verified stable public board token. These entries stay visible
  as `manual_review` and disabled instead of guessing endpoints.
- **Public ATS stability:** SmartRecruiters, Workday CXS, Greenhouse and Lever can
  change formats, rate-limit or block requests. Diagnostics show partial coverage;
  an empty or failed feed does not prove the employer has no jobs.
- **Missing descriptions/dates:** tolerated and clearly exposed to Codex. Some
  providers ignore or loosely interpret date/location filters. Missing/unknown
  fields are not invented. LinkedIn description fetching adds requests.
- **Validation errors:** the error identifies the field or job. Repair malformed
  scores and use the exported schema. Re-export/re-score if job/profile changed.
  No partial score writes occur when any record fails validation.
- **Old jobs/newness:** repeat runs update history. Skipped and filtered jobs are
  intentionally retained. `verify` checks up to 15 latest-run jobs scoring 60+,
  at most two public URLs each, using bounded HTTP requests. Matching JobPosting
  structured data is evidence of availability, not proof an application can be
  completed. 404/410, matching expiration dates or closed notices produce
  `possibly_unavailable`; blocks, login walls and ambiguous pages stay `unknown`.
  Nothing is deleted. A changed posting can require a new score while
  remaining an old discovery.
- **Deduplication limits:** different title wording, employer aliases or sparse
  descriptions may leave duplicates. Undisclosed distinct requisitions with
  identical content can still be indistinguishable. Prefer reviewing suspected
  duplicates to broad fuzzy merging; no automatic employer-alias inference occurs.
- **Single local operator:** run one hunt at a time. SQLite prevents partial
  transactions, but concurrent searches are not an intended V0 workflow.
- **Dependency updates:** JobSpy is pinned because provider behavior changes.
  Upgrade intentionally and rerun offline tests plus a small live check. Its
  transitive dependencies include pandas, NumPy, HTTP and parsing libraries.

JobSpy API and board constraints: [upstream documentation](https://github.com/speedyapply/JobSpy).

## Next roadmap

- Broaden verified employer coverage, including Ashby and custom official feeds.
- Add scoring history and incremental exports.
- Add salary normalization and better configuration-driven filter rules.
- Consider reviewed resume tailoring and assisted applications only as separately
  authorized future features; V0 never submits an application.
