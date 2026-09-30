# ATS implementation validation — 2026-09-30

Offline suite: **184 passed, 1 skipped** with warnings treated as errors.
The skipped test is the existing opt-in network test. Dependency check and
Git whitespace check passed.

Coverage includes unsafe/general URLs, bounded manual queries, three-source
deduplication, stable IDs and first observation, CSV CLI import, score export and
import, report generation, unresolved/missing-description candidates, Ashby
contract fixtures, 403/404/429/5xx/redirect/timeout failures, geographic exclusions,
seniority context, exact timestamp freshness bands, backup and date migration.

Bounded public validation used isolated local SQLite files under ignored `data/`:
- Konrad Greenhouse listing: 93 postings returned. Its custom careers URL was
  rejected by the ATS-only importer and remained outside the shortlist.
- Grafana Greenhouse listing: 120 postings returned. One supplied posting resolved
  into one Job; repeating the import created zero new jobs. It was excluded by
  the prefilter and therefore exported only with `--include-filtered` for audit.
- Existing HTTP verification ran on that real posting and returned **unknown**.
  Feed discovery and HTTP reachability do not prove applications remain open.
- Ashby's own `Ashby` board: 67 postings returned, with the documented public
  response fields. Two attempted SimSpace board names returned HTTP errors; no
  SimSpace integration was activated.

The local database's 18 legacy Greenhouse dates were reclassified with a SQLite
backup and per-record original payload audit. Semantic scores were retained.
No synthetic records were inserted into the real job database. Public listings
were used for implementation checks, not a new candidate-scored job hunt.

Limits: no automatic Google discovery, no generic page/JSON-LD import, no inferred
employer identity, no automatic catalog enrollment. Lever and Ashby imports need
a reviewed company mapping. Greenhouse can use official `company_name`. Ashby
last-publication timestamps do not establish original posting freshness.
This is partial provider validation; live Lever import and a mapped Ashby import
were covered offline, not exercised against a candidate-target employer live.
