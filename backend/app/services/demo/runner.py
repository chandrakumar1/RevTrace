"""The demo, as a structured result a browser can render.

`run_demo.py` walks the recovery path and prints it. This walks the *same* path
and returns it, because a judge watching a screen should not have to read a
terminal. It is a second **presentation**, never a second implementation: every
step below calls the function the CLI calls — `build_demo_population`,
`build_request`, `create_payment_link`, `demo_scenario`, `verify_signature`,
`map_event`, `apply_webhook` — and adds no payment-link logic, no signature
check, no merchant resolution, no idempotency rule and no status transition of
its own. If the production path changed, this would change with it or break.

**There is no HTTP equivalent of `--commit`.** The CLI can be told to keep its
rows; this cannot, and the ability is not merely defaulted off — `execute`
takes no such parameter and rolls back in a `finally`. An endpoint reachable
from a browser with no authentication must not be able to write a row that
survives, and the surest way to guarantee that is to give it no way to ask.

**Protected databases are refused by name.** `revtrace_dev` holds development
data and `revtrace_hypothesis_test` holds the canonical benchmark population;
neither may receive a demo row, rollback or not.

**Nothing here touches Razorpay.** The provider is `DemoPaymentLinkClient`,
which implements the SDK's own surface, so the adapter's real mapping and
validation run against synthetic bytes. No credential is read and no socket is
opened. Every figure this returns describes synthetic data, and `PROVENANCE`
travels with it so a screenshot cannot be mistaken for a capture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.integrations.razorpay.demo import (
    DEMO_EVENTS,
    DEMO_WEBHOOK_SECRET,
    PROVENANCE,
    DemoPaymentLinkClient,
    demo_scenario,
)
from app.integrations.razorpay.mapper import map_event
from app.integrations.razorpay.payment_links import build_request, create_payment_link
from app.integrations.razorpay.webhooks import WebhookVerificationError, verify_signature
from app.models.event import Event
from app.services.verification.demo_scenario import (
    DEMO_AMOUNT_MINOR,
    DemoPopulation,
    build_demo_population,
)
from app.services.verification.service import VerificationError, apply_webhook

#: Databases a demo may never be pointed at, whatever configuration says.
#: `revtrace_dev` is development data; `revtrace_hypothesis_test` is the
#: canonical benchmark population every measured claim rests on.
FORBIDDEN_DATABASES: frozenset[str] = frozenset({"revtrace_dev", "revtrace_hypothesis_test"})

#: What the run ends with. The demo is only honest if it says this.
FINAL_STATUS = "Rolled back — nothing persisted."

#: A fact's presentation weight. `refused` marks a security control that did its
#: job — a rejection is the *successful* outcome there, and rendering it as an
#: application error would invert what the demo is showing.
Tone = Literal["plain", "verified", "refused"]


class DemoUnavailable(RuntimeError):
    """The demo cannot run. Carries a message safe to show a stranger."""


@dataclass(frozen=True, slots=True)
class DemoFact:
    """One line inside a step. A label, a value, and why it matters.

    `minor` carries money as an integer count of minor units when the fact is
    about an amount, and the browser formats it. Sending a formatted string
    instead would put a second money renderer in this codebase, and sending only
    `value` would make the page invent one — both are how a float eventually
    reaches a money path.
    """

    label: str
    value: str
    note: str | None = None
    tone: Tone = "plain"
    minor: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "value": self.value,
            "note": self.note,
            "tone": self.tone,
            "minor": self.minor,
        }


@dataclass(frozen=True, slots=True)
class DemoStep:
    """One numbered step of the recovery story."""

    number: int
    title: str
    subtitle: str
    facts: list[DemoFact] = field(default_factory=list)
    tone: Tone = "plain"

    def as_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "title": self.title,
            "subtitle": self.subtitle,
            "tone": self.tone,
            "facts": [fact.as_dict() for fact in self.facts],
        }


@dataclass(frozen=True, slots=True)
class DemoRun:
    """A complete run. `committed` is a constant `False` and is sent anyway.

    Stating it on the wire means the browser can display the guarantee rather
    than assert it, and a change that broke the guarantee would show up in the
    response instead of only in this file.
    """

    provenance: str
    database: str
    steps: list[DemoStep]
    final_status: str = FINAL_STATUS
    committed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "provenance": self.provenance,
            "database": self.database,
            "committed": self.committed,
            "final_status": self.final_status,
            "steps": [step.as_dict() for step in self.steps],
        }


def database_name(dsn: str) -> str:
    """The database a DSN names, without its credentials.

    Only the last path segment is taken, so nothing this returns can carry a
    user or a password into a message, a log or an HTTP response.
    """
    return dsn.rsplit("/", 1)[-1].split("?", 1)[0]


def resolve_demo_dsn(settings: Settings) -> str:
    """The demo's DSN, or a refusal explaining why there is none.

    Every refusal message here is written to be shown to whoever called the
    endpoint: it names the setting and the refused database, and never the DSN
    itself.
    """
    dsn = settings.demo_database_url.strip()
    if not dsn:
        raise DemoUnavailable(
            "the demo is not enabled: DEMO_DATABASE_URL is not set. It is "
            "deliberately empty by default, so a deployment that has not chosen "
            "a demo database cannot run a demo against any other one."
        )
    if not dsn.startswith("postgresql"):
        raise DemoUnavailable(
            "DEMO_DATABASE_URL must be a PostgreSQL DSN; RevTrace requires PostgreSQL."
        )

    name = database_name(dsn)
    if name in FORBIDDEN_DATABASES:
        raise DemoUnavailable(
            f"refusing to run the demo against {name}: it is a protected "
            "database and must never receive demo rows."
        )
    return dsn


def _link_step(reference_id: str) -> tuple[DemoStep, str]:
    """Step 2. The production adapter, an offline provider."""
    request = build_request(
        amount_minor=DEMO_AMOUNT_MINOR,
        reference_id=reference_id,
        description="RevTrace demo recovery link (synthetic)",
    )
    client = DemoPaymentLinkClient(reference_id=reference_id)
    link = create_payment_link(client, request)

    notify = request["notify"]
    step = DemoStep(
        number=2,
        title="Payment-link recovery",
        subtitle=(
            "The recovery action is built and mapped by the Razorpay adapter interface — "
            "production code. The response comes from a deterministic offline demo "
            "provider implementing the same interface. Razorpay was not contacted."
        ),
        facts=[
            DemoFact(
                "Adapter",
                "Razorpay adapter interface",
                "payment_links.build_request and create_payment_link — production code path",
                "verified",
            ),
            DemoFact(
                "Provider",
                "Deterministic offline demo provider",
                "Synthetic provider data. No credential is read and no socket is opened.",
            ),
            DemoFact("Link id", link.provider_link_id, "Marked DEMO, so it cannot be mistaken"),
            DemoFact("Status", link.status),
            DemoFact(
                "Reference id",
                link.reference_id or "—",
                "How a later webhook is matched back without trusting arrival order",
            ),
            DemoFact(
                "Customer notification",
                "disabled" if notify == {"sms": False, "email": False} else str(notify),
                "RevTrace decides when a customer is contacted; the policy engine counts it",
            ),
        ],
    )
    return step, link.provider_link_id


def _webhook_step(scenario: dict[str, tuple[bytes, str]]) -> DemoStep:
    """Step 3. Synthetic bytes, a genuine signature over exactly those bytes."""
    facts = []
    for name in DEMO_EVENTS:
        raw, signature = scenario[name]
        facts.append(
            DemoFact(
                name,
                f"{name} → {map_event(name).value}",
                f"{len(raw)} bytes · HMAC-SHA256 {signature[:12]}…",
            )
        )
    return DemoStep(
        number=3,
        title="Signed synthetic webhooks",
        subtitle=(
            "Three synthetic webhooks, each signed with a genuine HMAC-SHA256 over the "
            "exact bytes that will be delivered — not over a re-serialisation of them. "
            "The secret is a synthetic demo secret held in the demo module; the "
            "cryptography is the same primitive the verifier uses. Note that "
            "payment_link.paid maps to order.paid: the leak graph never depends on "
            "one provider's naming."
        ),
        facts=facts,
    )


def _apply_step(
    session: Session,
    scenario: dict[str, tuple[bytes, str]],
    population: DemoPopulation,
) -> DemoStep:
    """Step 4. Verify, attribute, persist — every line the production path."""
    facts = []
    for name in DEMO_EVENTS:
        raw, signature = scenario[name]
        # The boundary. Raw bytes, verified before anything parses them.
        verify_signature(raw, signature, DEMO_WEBHOOK_SECRET)
        payload = json.loads(raw)
        outcome = apply_webhook(session, payload, received_at=datetime.now(UTC))

        row = session.execute(
            select(Event).where(Event.external_event_id == outcome.external_event_id)
        ).scalar_one()
        attributed = row.merchant_id == population.merchant.id

        facts.append(
            DemoFact(
                name,
                "HMAC verified · merchant verified · persisted"
                if attributed and outcome.persisted
                else "HMAC verified · merchant mismatch",
                (
                    "Merchant derived from the signed payment id through "
                    "payment_attempts → orders → merchant_id, never from the caller"
                ),
                "verified" if attributed and outcome.persisted else "refused",
            )
        )

    # Flush before refresh: `refresh` re-reads from the database, so without it
    # the pending status change would be discarded and the step would report the
    # old value. Flushing also makes this stronger than an in-memory assertion —
    # the status it reads has reached the database.
    session.flush()
    session.refresh(population.attempt)
    facts.append(
        DemoFact(
            "Payment attempt",
            f"failed → {population.attempt.status}",
            "Advanced by the webhook, and only forward: a delayed payment.authorized "
            "cannot undo a payment.captured that already arrived",
            "verified",
        )
    )

    return DemoStep(
        number=4,
        title="Verify → Attribute → Persist",
        subtitle=(
            "The production verification code path: signature checked over the raw "
            "bytes, the owning merchant derived from the signed payment id, the event "
            "stored, the payment attempt advanced."
        ),
        facts=facts,
        tone="verified",
    )


def _replay_step(session: Session, scenario: dict[str, tuple[bytes, str]]) -> DemoStep:
    """Step 5. The same three deliveries again, with no second effect."""
    before = session.execute(select(func.count()).select_from(Event)).scalar_one()
    facts = []
    for name in DEMO_EVENTS:
        raw, signature = scenario[name]
        verify_signature(raw, signature, DEMO_WEBHOOK_SECRET)
        outcome = apply_webhook(session, json.loads(raw), received_at=datetime.now(UTC))
        facts.append(
            DemoFact(
                name,
                "duplicate detected · nothing written"
                if outcome.duplicate
                else "written a second time",
                None,
                "verified" if outcome.duplicate else "refused",
            )
        )
    after = session.execute(select(func.count()).select_from(Event)).scalar_one()
    facts.append(
        DemoFact(
            "Event rows",
            f"{before} before · {after} after · unchanged={before == after}",
            "Idempotency is carried by UNIQUE(merchant_id, external_event_id), "
            "so a repeat is an insert that writes nothing rather than a case to detect",
            "verified" if before == after else "refused",
        )
    )
    return DemoStep(
        number=5,
        title="Replay protection",
        subtitle=(
            "Razorpay never promises exactly-once delivery. The same three synthetic "
            "webhooks are delivered again, and the database declines the repeats."
        ),
        facts=facts,
        tone="verified",
    )


def _security_step(session: Session, scenario: dict[str, tuple[bytes, str]]) -> DemoStep:
    """Step 6. Two attacks, both refused. Refusal is the success here."""
    facts = []

    raw, signature = scenario["payment.captured"]
    tampered = raw.replace(b"50000", b"99999")
    try:
        verify_signature(tampered, signature, DEMO_WEBHOOK_SECRET)
        facts.append(
            DemoFact(
                "Tampered body",
                "ACCEPTED — this should be impossible",
                "The amount was altered after signing and the signature still passed",
                "plain",
            )
        )
    except WebhookVerificationError:
        facts.append(
            DemoFact(
                "Tampered body",
                "REFUSED",
                "The amount was altered from 50000 to 99999 after signing. The HMAC "
                "covers the exact octets, so the change is detected.",
                "refused",
            )
        )

    other = build_demo_population(session, label="B")
    try:
        apply_webhook(
            session,
            json.loads(raw),
            received_at=datetime.now(UTC),
            asserted_merchant_id=other.merchant.id,
        )
        facts.append(
            DemoFact(
                "Foreign merchant",
                "ACCEPTED — this should be impossible",
                "A validly-signed webhook was filed under a merchant that does not own it",
                "plain",
            )
        )
    except VerificationError as exc:
        facts.append(
            DemoFact(
                "Foreign merchant",
                "REFUSED",
                f"{exc} A second merchant claimed a validly-signed webhook about the "
                "first merchant's payment. Tenancy comes from the signed data, so the "
                "claim is rejected rather than silently ignored.",
                "refused",
            )
        )

    return DemoStep(
        number=6,
        title="Security controls",
        subtitle=(
            "Two attacks against the verified path. Both are refused, and the refusals "
            "are the result being demonstrated — these are controls working, not "
            "failures. The second was a real defect, found and fixed: a validly-signed "
            "webhook about one merchant's payment could be filed under another."
        ),
        facts=facts,
        tone="refused",
    )


def run_demo(session: Session) -> DemoRun:
    """Walk the recovery path and return it. **Does not commit.**

    The caller owns the transaction, as everywhere else in this project — and
    the only caller that matters, `execute` below, rolls it back.
    """
    database = session.execute(text("SELECT current_database()")).scalar_one()

    population = build_demo_population(session, label="A")
    first = DemoStep(
        number=1,
        title="Synthetic failed payment",
        subtitle=(
            "A synthetic customer's payment fails. That failure is the recovery "
            "opportunity everything below acts on. No real customer, order or payment "
            "is involved, and every identifier is marked DEMO."
        ),
        facts=[
            DemoFact("Merchant", population.merchant.name),
            DemoFact("Order", population.order.external_order_id or "—"),
            DemoFact("Payment", population.payment_id),
            DemoFact(
                "Amount",
                f"{DEMO_AMOUNT_MINOR} paise",
                "Carried as an integer count of minor units and formatted by the "
                "browser. No float touches a money path, on either side of the wire.",
                minor=DEMO_AMOUNT_MINOR,
            ),
            DemoFact("Status", population.attempt.status, "The recovery opportunity"),
            DemoFact("Failure code", population.attempt.failure_code or "—"),
        ],
    )

    link_step, link_id = _link_step(population.reference_id)
    scenario = demo_scenario(population.payment_id, link_id)

    steps = [
        first,
        link_step,
        _webhook_step(scenario),
        _apply_step(session, scenario, population),
        _replay_step(session, scenario),
        _security_step(session, scenario),
    ]
    return DemoRun(provenance=PROVENANCE, database=database, steps=steps)


def execute(settings: Settings) -> DemoRun:
    """Run the demo against the configured demo database and **roll back**.

    There is no `commit` parameter, deliberately. The rollback is in a `finally`
    and runs whether the demo succeeded or raised, so no path through this
    function leaves a row behind.

    The engine is built here rather than taken from `db.session`, so the
    application's own connection — which points at the development database —
    is never involved in a demo.
    """
    dsn = resolve_demo_dsn(settings)

    engine = create_engine(dsn, future=True, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()
    try:
        return run_demo(session)
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


__all__ = [
    "FINAL_STATUS",
    "FORBIDDEN_DATABASES",
    "DemoFact",
    "DemoRun",
    "DemoStep",
    "DemoUnavailable",
    "database_name",
    "execute",
    "resolve_demo_dsn",
    "run_demo",
]
