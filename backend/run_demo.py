"""RevTrace end-to-end demo. **Offline, synthetic, no credentials.**

Walks the whole recovery path in one process:

    synthetic failed payment
      -> demo payment link (through the real Razorpay adapter)
      -> synthetic signed webhooks
      -> HMAC verification over raw bytes
      -> merchant derived from the signed payment id
      -> event persistence, idempotent
      -> payment status advancement

**Nothing here touches Razorpay.** The provider is `DemoPaymentLinkClient`,
which implements the SDK's own `payment_link.create` / `.fetch` surface, so the
adapter's real mapping and validation run unchanged — only the bytes are
synthetic. No key is read, no socket is opened, and the run works with an empty
`.env`.

**The security path is not simulated.** The webhooks are signed with a genuine
HMAC-SHA256 over the exact bytes delivered, and verified by the production
verifier. Merchant derivation, idempotency and ordering are the real code. Only
the secret is synthetic, and it is a literal in `integrations/razorpay/demo.py`.

**Nothing is kept.** The transaction is rolled back unless `--commit` is passed,
so `revtrace_test` stays the ephemeral database the suite depends on.

Usage::

    TEST_DATABASE_URL=postgresql+psycopg://user@localhost:5432/revtrace_test \\
        python run_demo.py

Every figure it prints describes synthetic data. It is not evidence about
Razorpay, about a real customer, or about money.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.integrations.razorpay.demo import (
    DEMO_EVENTS,
    DEMO_WEBHOOK_SECRET,
    PROVENANCE,
    DemoPaymentLinkClient,
    demo_scenario,
)
from app.integrations.razorpay.mapper import map_event
from app.integrations.razorpay.payment_links import build_request, create_payment_link
from app.integrations.razorpay.webhooks import verify_signature
from app.models.event import Event
from app.services.verification.demo_scenario import DEMO_AMOUNT_MINOR, build_demo_population
from app.services.verification.service import apply_webhook

FORBIDDEN_DATABASE = "revtrace_dev"

#: The persistent canonical population lives here and must not be written by a
#: demo. Named so the refusal says why.
HYPOTHESIS_DATABASE = "revtrace_hypothesis_test"

BANNER = "=" * 72


def resolve_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        raise SystemExit(
            "TEST_DATABASE_URL is not set.\n\n"
            "    TEST_DATABASE_URL=postgresql+psycopg://USER@localhost:5432/revtrace_test \\\n"
            "        python run_demo.py"
        )
    name = dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    if name == FORBIDDEN_DATABASE:
        raise SystemExit(f"refusing to run the demo against {FORBIDDEN_DATABASE}")
    if name == HYPOTHESIS_DATABASE:
        raise SystemExit(
            f"refusing to write demo rows into {HYPOTHESIS_DATABASE}: it holds the "
            "canonical benchmark population and is read-only for everything else"
        )
    return dsn


def step(number: int, title: str) -> None:
    print(f"\n{BANNER}\n  STEP {number}  {title}\n{BANNER}")


def main() -> None:
    parser = argparse.ArgumentParser(description="RevTrace offline demo.")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="keep the demo rows; without it the transaction is rolled back",
    )
    args = parser.parse_args()

    dsn = resolve_dsn()
    engine = create_engine(dsn, future=True)
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()

    print(BANNER)
    print("  RevTrace — END-TO-END RECOVERY DEMO")
    print(f"  {PROVENANCE}")
    print("  No Razorpay call. No credentials. No network.")
    print(BANNER)

    try:
        database = session.execute(text("SELECT current_database()")).scalar_one()
        print(f"\ndatabase   : {database}")
        print(f"mode       : {'COMMIT' if args.commit else 'dry run (rolls back)'}")

        # -- 1 ------------------------------------------------------------
        step(1, "A synthetic payment fails — the recovery opportunity")
        population = build_demo_population(session, label="A")
        print(f"  merchant       {population.merchant.name}")
        print(f"  order          {population.order.external_order_id}")
        print(f"  payment        {population.payment_id}")
        print(f"  amount         {DEMO_AMOUNT_MINOR:,} paise (integer minor units)")
        print(f"  attempt status {population.attempt.status!r}  <- the opportunity")

        # -- 2 ------------------------------------------------------------
        step(2, "RevTrace creates a payment link — through the real adapter")
        request = build_request(
            amount_minor=DEMO_AMOUNT_MINOR,
            reference_id=population.reference_id,
            description="RevTrace demo recovery link (synthetic)",
        )
        client = DemoPaymentLinkClient(reference_id=population.reference_id)
        link = create_payment_link(client, request)
        print("  adapter        payment_links.create_payment_link (production code)")
        print("  provider       DemoPaymentLinkClient (synthetic, offline)")
        print(f"  link id        {link.provider_link_id}")
        print(f"  status         {link.status}")
        print(f"  reference      {link.reference_id}  <- how a webhook is matched back")
        print(f"  notifications  disabled: {request['notify']}  <- the gate counts contacts")

        # -- 3 ------------------------------------------------------------
        step(3, "Three synthetic webhooks arrive, signed with a real HMAC")
        scenario = demo_scenario(population.payment_id, link.provider_link_id)
        for name in DEMO_EVENTS:
            raw, signature = scenario[name]
            print(
                f"  {name:20} {len(raw):>4} bytes  sig {signature[:8]}…  -> {map_event(name).value}"
            )

        # -- 4 ------------------------------------------------------------
        step(4, "Verification, attribution and persistence")
        for name in DEMO_EVENTS:
            raw, signature = scenario[name]
            # Raw bytes, verified before anything parses them.
            verify_signature(raw, signature, DEMO_WEBHOOK_SECRET)
            payload = json.loads(raw)
            outcome = apply_webhook(session, payload, received_at=datetime.now(UTC))
            row = session.execute(
                select(Event).where(Event.external_event_id == outcome.external_event_id)
            ).scalar_one()
            attributed = row.merchant_id == population.merchant.id
            print(
                f"  {name:20} verified · persisted={outcome.persisted} · "
                f"merchant_ok={attributed} · attempt={outcome.new_status or '—'}"
            )

        # Flush before refresh: `refresh` re-reads from the database, so
        # without it the pending change would be discarded and the demo
        # would narrate the old value.
        session.flush()
        session.refresh(population.attempt)
        print(f"\n  payment attempt advanced to {population.attempt.status!r} (was 'failed')")

        # -- 5 ------------------------------------------------------------
        step(5, "Replay — duplicate delivery must have no second effect")
        before = session.execute(select(func.count()).select_from(Event)).scalar_one()
        for name in DEMO_EVENTS:
            raw, signature = scenario[name]
            verify_signature(raw, signature, DEMO_WEBHOOK_SECRET)
            outcome = apply_webhook(session, json.loads(raw), received_at=datetime.now(UTC))
            print(f"  {name:20} duplicate={outcome.duplicate} · persisted={outcome.persisted}")
        after = session.execute(select(func.count()).select_from(Event)).scalar_one()
        print(
            f"\n  event rows before replay {before} · after {after} · unchanged={before == after}"
        )

        # -- 6 ------------------------------------------------------------
        step(6, "Tampering and cross-tenant attribution are refused")
        raw, signature = scenario["payment.captured"]
        tampered = raw.replace(b"50000", b"99999")
        try:
            verify_signature(tampered, signature, DEMO_WEBHOOK_SECRET)
            print("  !! tampered body ACCEPTED — this should be impossible")
        except Exception as exc:  # noqa: BLE001 - the demo reports, it does not handle
            print(f"  tampered body      refused: {type(exc).__name__}")

        other = build_demo_population(session, label="B")
        from app.services.verification.service import VerificationError

        try:
            apply_webhook(
                session,
                json.loads(raw),
                received_at=datetime.now(UTC),
                asserted_merchant_id=other.merchant.id,
            )
            print("  !! cross-tenant ACCEPTED — this should be impossible")
        except VerificationError as exc:
            print(f"  foreign merchant   refused: {exc}")

        print(f"\n{BANNER}")
        print("  DEMO COMPLETE — every figure above is SYNTHETIC")
        print("  No Razorpay request was made and no credential was read.")
        print(BANNER)

        if args.commit:
            transaction.commit()
            print("\ncommitted: demo rows kept")
        else:
            transaction.rollback()
            print("\nrolled back: nothing persisted (pass --commit to keep it)")
    except Exception:
        transaction.rollback()
        print("rolled back: the demo failed, nothing persisted", file=sys.stderr)
        raise
    finally:
        session.close()
        connection.close()
        engine.dispose()


if __name__ == "__main__":
    main()
