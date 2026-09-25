"""One bounded JobSpy invocation; never used to invoke Codex."""

import json
import sys
from pathlib import Path


def main():
    try:
        from jobspy import scrape_jobs
        arguments = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        jobs = scrape_jobs(**arguments)
        # pandas serializes NaN/NaT as JSON null.
        Path(sys.argv[2]).write_text(jobs.to_json(orient="records", date_format="iso"), encoding="utf-8")
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
