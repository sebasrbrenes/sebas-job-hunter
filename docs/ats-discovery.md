# Manual ATS discovery

Google discovery is opt-in via explicit URL import. It does not search Google,
scrape snippets, activate companies, apply to jobs, or change candidate evidence.

```powershell
.\.venv\Scripts\python.exe -m job_hunter ats-queries
.\.venv\Scripts\python.exe -m job_hunter import-ats-urls --input data/ats-urls.csv
.\.venv\Scripts\python.exe -m job_hunter migrate-ats-dates
```

Copy `docs/ats-urls.example.csv` to `data/ats-urls.csv`, then add specific HTTPS
posting URLs. JSON arrays of the same fields are supported. Only `url` is required.
Optional fields: `query`, timezone-aware `discovered_at`, `google_window`, `notes`,
`google_displayed_date`. Keep personal information out of discovery notes.

The query command generates four geographic variants per configured track,
with alternative titles grouped using OR: Remote Costa Rica, LATAM, Latin America,
Americas. Defaults produce 16 queries, not a Cartesian query/location/provider
expansion. Run manually in Google with last 24 hours, last week, and no time filter.
Split domains if coverage is poor. These windows measure indexing signals, not
original publication or complete ATS coverage. No negative senior keyword is used.

Import adds resolved postings to the latest run (creates a run if none exists),
records attempts, applies the existing filter and exports up to the configured
limit for Codex. Complete the normal scoring, import-scores, verify and report
workflow. Search `--source` and `--track` behavior remains unchanged.

Greenhouse specific-posting responses can identify employers outside the catalog.
Lever and Ashby require a reviewed catalog entry to identify the employer; slugs
are not company names. A catalog entry need not be enabled for explicit manual
import. Enabling it for routine search remains a separate manual choice. For Ashby,
use `provider: ashby`, `board: <verified board name>` and the normal catalog fields;
enable `search.providers.ashby` only when wanted. The adapter is available without
activating any company by default. Custom careers URLs, ambiguous URLs, missing
employer identity and insufficient descriptions remain manual review. There is
no generic HTML fallback: these implementations use only documented public feeds.

Unresolved allowed posting URLs live in SQLite `ats_candidates`, never in the Job
shortlist. Invalid URLs are rejected and counted in run diagnostics. A 404 during
resolution is review evidence; existing HTTP verification handles
`possibly_unavailable`. Blocks and timeouts mean unknown. Provider failures do not
stop other rows. Coverage is always partial and limited to supplied URLs.

Limits: 40 input URLs by default (`--maximum`), 1 MB input, 8 MB per response,
hard maximum 200 input URLs,
15-second request timeout, 20 actual requests per API host, configured pauses,
one retry for 429/5xx, Retry-After at most 10 seconds (longer/date-form values defer
to manual review). Redirects are refused before requesting another host. Repeated
feed URLs are cached per import. Imported URL queries never reach the HTTP client.

Original dates and timestamps are separate from ATS updates, last republication,
Google displayed dates and local observations. Greenhouse `first_published` is
used when supplied; `updated_at` never becomes `date_posted`. Ashby `publishedAt`
means last published, so original freshness remains unknown. Timestamp freshness
uses exact <=24/72/168-hour bounds; date-only records use existing day bands.
Unknown dates do not exclude a job or add semantic fit points. Reports prioritize
semantic score, then freshness. Senior titles and mandatory 5+ years are stored
but excluded from ordinary export; `--include-filtered` allows audit.

Run `migrate-ats-dates` once to reclassify legacy Greenhouse dates: it creates a
SQLite backup, stores original payloads in `date_migrations`, clears unreliable
posting dates, and retains jobs and semantic scores. Repeating it changes nothing.
No destructive migration runs automatically. New JSON fields have defaults for
old records. Reimport preserves first observation and valid semantic scores;
actual content changes use existing score invalidation.

Public API contracts checked 2026-09-30:
- [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html)
- [Lever Postings API](https://github.com/lever/postings-api)
- [Ashby public postings API](https://developers.ashbyhq.com/docs/public-job-posting-api)

Future programmatic Google/search-provider integration, custom HTML/JSON-LD
resolution, company identity discovery, scheduled monitoring and robots/ToS
handling for new page adapters remain outside this MVP. No paid service or new
credential is needed. Public feed access is not a guarantee of open applications,
country eligibility, original publication date or complete provider coverage.
