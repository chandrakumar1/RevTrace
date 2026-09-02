"""Capture one demo run as a committed JSON artifact. **Offline, rolled back.**

The landing page needs the six demo steps as *static content*: it must render
them with the backend asleep, unreachable, or not yet deployed. This writes that
artifact by running the same `services.demo.runner.run_demo` the HTTP endpoint
runs — so the page shows a real captured run rather than prose someone wrote
about one.

**Nothing is kept.** The transaction is rolled back in a `finally`, exactly as
the endpoint does. This script has no `--commit`.

**Nothing external is called.** The provider is the offline demo client; no
Razorpay, OpenRouter or Gemini request is made, and no credential is read.

**Never the hosted database.** It requires `TEST_DATABASE_URL` explicitly and
refuses the protected databases by name, reusing the runner's own guard.

The captured identifiers come from one run and are frozen by being committed —
they are seeded per run so concurrent runs cannot collide on a unique
constraint, so re-running produces different ids and the same structure. That
is why the artifact is committed rather than regenerated at build time.

Usage::

    TEST_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \\
        python run_capture_demo.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import normalise_postgres_dsn
from app.services.demo.runner import FORBIDDEN_DATABASES, database_name, run_demo

#: Written beside the other generated evidence, and mirrored into the frontend
#: the way `docs/evaluation.json` is mirrored as `report.10k.json`.
REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "docs" / "demo_run.json"
FRONTEND_COPY = REPO_ROOT / "frontend" / "src" / "fixtures" / "demo_run.json"


def resolve_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        raise SystemExit(
            "TEST_DATABASE_URL is not set.\n\n"
            "    TEST_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \\\n"
            "        python run_capture_demo.py"
        )
    dsn = normalise_postgres_dsn(dsn, setting="TEST_DATABASE_URL")
    name = database_name(dsn)
    if name in FORBIDDEN_DATABASES:
        raise SystemExit(f"refusing to capture a demo against {name}: it is a protected database")
    return dsn


def main() -> None:
    dsn = resolve_dsn()
    engine = create_engine(dsn, future=True)
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()

    try:
        run = run_demo(session)
        payload = run.as_dict()
        # Provenance travels with the artifact so a reader never has to ask
        # where it came from or whether it describes a real payment.
        payload["captured_at"] = datetime.now(UTC).isoformat()
        payload["source"] = "app.services.demo.runner.run_demo"
        payload["note"] = (
            "One captured offline demo run. Synthetic throughout; no Razorpay request "
            "was made and no credential was read. The run was rolled back, so none of "
            "these rows exists in any database."
        )

        rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        ARTIFACT.write_text(rendered, encoding="utf-8")
        FRONTEND_COPY.write_text(rendered, encoding="utf-8")

        print(f"wrote {ARTIFACT.relative_to(REPO_ROOT)}")
        print(f"wrote {FRONTEND_COPY.relative_to(REPO_ROOT)}")
        print(f"steps: {len(run.steps)} · committed: {run.committed} · {run.final_status}")
    except Exception:
        transaction.rollback()
        print("rolled back: the capture failed, nothing persisted", file=sys.stderr)
        raise
    finally:
        # Always. There is no path through this script that keeps a row.
        transaction.rollback()
        session.close()
        connection.close()
        engine.dispose()


if __name__ == "__main__":
    main()
