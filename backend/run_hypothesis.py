"""Generate one AI hypothesis about an experiment and record the verdict.

A deliberate operator run, not an endpoint. The API has no authentication of any
kind, and an unauthenticated route that triggers an outbound model call is a
spend primitive exposed to whoever can reach the process. This is also the shape
`run_acceptance.py` already uses for expensive operations.

Usage::

    TEST_DATABASE_URL=postgresql+psycopg://user@localhost:5432/revtrace_hypothesis_test \\
        python run_hypothesis.py <experiment_id> [--live] [--commit]

**Read `revtrace_hypothesis_test`, not `revtrace_test`.** The latter is the
shared pytest database and is contractually ephemeral — every test
materialises, asserts and rolls back. A population committed there breaks
that invariant for the whole suite, which is exactly what happened once:
eleven modules failed, some on the benchmark bridge's seed-collision guard
and some simply because they count rows and expect none. The persistent
canonical population lives in its own database, which satisfies the same
`_test` marker rule and is never written by pytest.

**Dry by default, three times over.** Without `--live` the run uses no provider
at all and refuses to start; without `--commit` the transaction is rolled back
whatever happened. A live run additionally passes the billing guard below.

**This script owns the transaction.** The service never commits — it flushes so
the row exists to inspect, and this file decides whether it survives.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.agents.contracts import HypothesisRequest
from app.agents.hypothesis_agent import (
    FREE_MODEL_SUFFIX,
    OPENROUTER_MODEL,
    FallbackProvider,
    OpenAICompatibleProvider,
)
from app.core.config import get_settings
from app.services.hypothesis.loader import load_hypothesis_request
from app.services.hypothesis.service import MAX_RETRIES, generate_and_record, live_provider

#: Never run against the development database by accident.
FORBIDDEN_DATABASE = "revtrace_dev"

#: The one model this script may call. Checked against the configuration rather
#: than assumed, so a config edit cannot quietly redirect a live run.
EXPECTED_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

DEFAULT_ALPHA_BPS = 500
DEFAULT_MDE_BPS = 1_000


def resolve_dsn() -> str:
    """The DSN this run will use. Requires an explicit choice."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        raise SystemExit(
            "TEST_DATABASE_URL is not set. This script will not fall back to "
            "DATABASE_URL: the database a hypothesis is written to must be a "
            "deliberate choice, not an ambient one."
        )
    name = dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    if name == FORBIDDEN_DATABASE:
        raise SystemExit(f"refusing to run against {FORBIDDEN_DATABASE}")
    return dsn


def billing_guard(request: HypothesisRequest) -> FallbackProvider:
    """Build the live chain and prove what it will send, before any socket.

    Every check is against the object that will issue the request, not against a
    constant in this file: a guard that checked its own literals would pass
    while the chain called something else.
    """
    settings = get_settings()
    chain = live_provider(settings)
    info = chain.info

    if info.model != EXPECTED_MODEL or OPENROUTER_MODEL != EXPECTED_MODEL:
        raise SystemExit(f"REFUSING — model is {info.model!r}, expected {EXPECTED_MODEL!r}")
    if not info.model.endswith(FREE_MODEL_SUFFIX):
        raise SystemExit(f"REFUSING — {info.model!r} is not a free variant")
    if not getattr(chain, "free_only", False) or not getattr(chain, "is_free", False):
        raise SystemExit("REFUSING — the chain is not free-only")
    if len(chain._providers) != 1:  # noqa: SLF001
        raise SystemExit(f"REFUSING — chain has {len(chain._providers)} providers")  # noqa: SLF001

    live = chain._providers[0]  # noqa: SLF001
    if not isinstance(live, OpenAICompatibleProvider):
        raise SystemExit(f"REFUSING — the live member is a {type(live).__name__}, not a client")
    if live._client.max_retries != MAX_RETRIES:  # noqa: SLF001
        raise SystemExit("REFUSING — retries are not pinned to zero")

    # The body that will actually be sent, checked before the socket opens: a
    # guard that trusted the provider's own claims about itself is not a guard.
    body = live._request_kwargs(request)  # noqa: SLF001
    for forbidden in ("tools", "tool_choice", "functions", "function_call"):
        if forbidden in body:
            raise SystemExit(f"REFUSING — request body carries {forbidden!r}")
    schema = body.get("response_format")
    if not isinstance(schema, dict) or schema.get("type") != "json_schema":
        raise SystemExit("REFUSING — structured output is not json_schema")
    if not schema["json_schema"].get("strict"):
        raise SystemExit("REFUSING — the JSON schema is not strict")
    if body["model"] != EXPECTED_MODEL:
        raise SystemExit(f"REFUSING — body model is {body['model']!r}")

    print(f"billing guard passed: {info.provider} / {info.model}, free-only, max_retries=0")
    print("body guard passed: strict json_schema, no tools")
    return chain


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id", type=uuid.UUID)
    parser.add_argument(
        "--live",
        action="store_true",
        help="issue one real OpenRouter request on the pinned free model",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="keep the audit row; without it the transaction is rolled back",
    )
    parser.add_argument("--alpha-bps", type=int, default=DEFAULT_ALPHA_BPS)
    parser.add_argument("--mde-bps", type=int, default=DEFAULT_MDE_BPS)
    args = parser.parse_args()

    if not args.live:
        raise SystemExit(
            "No provider selected. This script issues a real model request and "
            "will not invent one: pass --live to proceed, or use RecordedProposals "
            "from a test or a notebook for an offline replay."
        )

    dsn = resolve_dsn()
    engine = create_engine(dsn, future=True)
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()

    try:
        name = session.execute(text("SELECT current_database()")).scalar_one()
        print(f"database: {name}")
        if name == FORBIDDEN_DATABASE:
            raise SystemExit(f"refusing to run against {FORBIDDEN_DATABASE}")

        # Assembled before the provider exists, so the guard below inspects
        # the exact body this run will send rather than a stand-in.
        request = load_hypothesis_request(
            session, args.experiment_id, alpha_bps=args.alpha_bps, mde_bps=args.mde_bps
        )
        print(f"payload: {len(request.cells)} cells, ATE {request.population.ate_bps} bps")

        provider = billing_guard(request)

        started = datetime.now(UTC)
        outcome = generate_and_record(
            session,
            args.experiment_id,
            provider=provider,
            as_of=started,
            alpha_bps=args.alpha_bps,
            mde_bps=args.mde_bps,
            request=request,
            live=True,
        )
        elapsed = (datetime.now(UTC) - started).total_seconds()

        print(f"\nresponse and verdict in {elapsed:.1f}s")
        print(f"  cell      {outcome.hypothesis.cell_key} ({outcome.hypothesis.ladder_level})")
        print(f"  claim     {outcome.hypothesis.claim.value}")
        print(f"  status    {outcome.result.status.value}   rule: {outcome.result.rule}")
        print(f"  audit id  {outcome.audit_event.id if outcome.audit_event else None}")
        print()
        print(json.dumps(outcome.result.as_dict(), indent=2, sort_keys=True))

        if args.commit:
            transaction.commit()
            print("\ncommitted: the audit row is persisted")
        else:
            transaction.rollback()
            print("\nrolled back: nothing persisted (pass --commit to keep it)")
    except Exception:
        transaction.rollback()
        print("rolled back: the run failed, nothing persisted", file=sys.stderr)
        raise
    finally:
        session.close()
        connection.close()
        engine.dispose()


if __name__ == "__main__":
    main()
