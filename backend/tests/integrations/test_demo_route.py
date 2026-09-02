"""The browser-facing demo endpoint.

Three things are worth proving here and are proved separately, because they can
fail independently:

**It is off unless enabled**, and its refusal names a setting rather than a DSN.
**It cannot commit** — not by default, but at all: there is no parameter, and
the transaction is rolled back in a `finally`.
**It reaches no network.** Proved twice, because either proof alone is weak: a
structural assertion over the source catches an import on a path no test
exercises, and a behavioural one — every outbound connection blocked, the demo
run anyway — catches a call the structural check cannot see.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.customer import Customer
from app.models.event import Event
from app.models.merchant import Merchant
from app.models.order import Order
from app.models.payment_attempt import PaymentAttempt
from app.services.demo.runner import (
    FINAL_STATUS,
    FORBIDDEN_DATABASES,
    DemoUnavailable,
    database_name,
    resolve_demo_dsn,
    run_demo,
)
from tests.conftest_db import resolve_test_dsn

RUNNER_SOURCE = pathlib.Path("app/services/demo/runner.py")
ROUTE_SOURCE = pathlib.Path("app/api/routes/demo.py")


def _settings(demo_dsn: str) -> Settings:
    """A Settings whose every credential is empty.

    Explicitly empty rather than omitted: omitting them would let an ambient
    `.env` supply a real one, which is how a hermetic test quietly stops being
    hermetic.
    """
    return Settings(
        database_url="postgresql+psycopg://unused@localhost:5432/unused",
        demo_database_url=demo_dsn,
        razorpay_key_id=SecretStr(""),
        razorpay_key_secret=SecretStr(""),
        razorpay_webhook_secret=SecretStr(""),
        gemini_api_key=SecretStr(""),
        openrouter_api_key=SecretStr(""),
        featherless_api_key=SecretStr(""),
    )


@pytest.fixture
def demo_client(db_engine: Engine, _schema_is_current: None) -> TestClient:
    """The app, with the demo pointed at revtrace_test.

    The runner opens its own connection and rolls it back, so this leaves the
    database exactly as it found it.
    """
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: _settings(resolve_test_dsn())
    return TestClient(app)


@pytest.fixture
def disabled_client() -> TestClient:
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: _settings("")
    return TestClient(app)


class TestTheDemoIsOffUnlessEnabled:
    def test_status_reports_disabled_without_a_demo_database(
        self, disabled_client: TestClient
    ) -> None:
        response = disabled_client.get("/api/v1/demo/status")

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert "DEMO_DATABASE_URL" in body["reason"]

    def test_running_without_a_demo_database_is_503(self, disabled_client: TestClient) -> None:
        response = disabled_client.post("/api/v1/demo/run")

        assert response.status_code == 503
        assert "DEMO_DATABASE_URL" in response.json()["detail"]

    def test_the_refusal_carries_no_dsn(self, disabled_client: TestClient) -> None:
        """A 503 a stranger can read must not describe the deployment."""
        detail = disabled_client.post("/api/v1/demo/run").json()["detail"]

        assert "postgresql" not in detail
        assert "://" not in detail
        assert "@" not in detail

    @pytest.mark.parametrize("name", sorted(FORBIDDEN_DATABASES))
    def test_protected_databases_are_refused_by_name(self, name: str) -> None:
        settings = _settings(f"postgresql+psycopg://user@localhost:5432/{name}")

        with pytest.raises(DemoUnavailable, match=name):
            resolve_demo_dsn(settings)

    def test_the_protected_set_is_exactly_the_two_databases(self) -> None:
        assert FORBIDDEN_DATABASES == {"revtrace_dev", "revtrace_hypothesis_test"}

    def test_a_non_postgres_dsn_is_refused(self) -> None:
        with pytest.raises(DemoUnavailable, match="PostgreSQL"):
            resolve_demo_dsn(_settings("sqlite:///demo.db"))

    def test_the_database_name_is_read_without_its_credentials(self) -> None:
        assert database_name("postgresql+psycopg://u:p@host:5432/revtrace_dev") == "revtrace_dev"
        assert database_name("postgresql://h/revtrace_dev?sslmode=require") == "revtrace_dev"


class TestTheDemoRuns:
    def test_the_endpoint_returns_six_steps_in_order(self, demo_client: TestClient) -> None:
        body = demo_client.post("/api/v1/demo/run").json()

        assert [step["number"] for step in body["steps"]] == [1, 2, 3, 4, 5, 6]

    def test_the_run_is_labelled_synthetic_and_not_committed(self, demo_client: TestClient) -> None:
        body = demo_client.post("/api/v1/demo/run").json()

        assert "SYNTHETIC" in body["provenance"]
        assert body["committed"] is False
        assert body["final_status"] == FINAL_STATUS

    def test_the_payment_attempt_advances_to_captured(self, demo_client: TestClient) -> None:
        body = demo_client.post("/api/v1/demo/run").json()
        step = body["steps"][3]

        advanced = [f for f in step["facts"] if f["label"] == "Payment attempt"]
        assert advanced and advanced[0]["value"] == "failed → captured"

    def test_the_event_names_are_mapped_to_revtrace_vocabulary(
        self, demo_client: TestClient
    ) -> None:
        """`payment_link.paid` must show as `order.paid`, not as itself."""
        values = [
            f["value"] for f in demo_client.post("/api/v1/demo/run").json()["steps"][2]["facts"]
        ]

        assert "payment.failed → payment.failed" in values
        assert "payment.captured → payment.captured" in values
        assert "payment_link.paid → order.paid" in values

    def test_money_crosses_the_wire_as_an_integer(self, demo_client: TestClient) -> None:
        """The browser formats it. The backend must not send a rupee string."""
        facts = demo_client.post("/api/v1/demo/run").json()["steps"][0]["facts"]

        amount = next(f for f in facts if f["label"] == "Amount")
        assert amount["minor"] == 50_000
        assert isinstance(amount["minor"], int)
        assert "₹" not in amount["value"]
        assert all(f["minor"] is None for f in facts if f["label"] != "Amount")

    def test_the_replay_leaves_the_row_count_unchanged(self, demo_client: TestClient) -> None:
        step = demo_client.post("/api/v1/demo/run").json()["steps"][4]

        counts = [f for f in step["facts"] if f["label"] == "Event rows"]
        assert counts and "unchanged=True" in counts[0]["value"]

    def test_both_attacks_are_refused(self, demo_client: TestClient) -> None:
        step = demo_client.post("/api/v1/demo/run").json()["steps"][5]

        assert {f["label"]: f["value"] for f in step["facts"]} == {
            "Tampered body": "REFUSED",
            "Foreign merchant": "REFUSED",
        }

    def test_a_refusal_is_toned_as_a_control_not_a_failure(self, demo_client: TestClient) -> None:
        """The demo must not present a working security control as an error."""
        step = demo_client.post("/api/v1/demo/run").json()["steps"][5]

        assert step["tone"] == "refused"
        assert all(fact["tone"] == "refused" for fact in step["facts"])

    def test_two_runs_agree_on_every_step_and_outcome(self, demo_client: TestClient) -> None:
        """Deterministic in structure and outcome.

        Identifiers deliberately differ between runs — they are seeded with a
        fresh uuid so two concurrent runs cannot collide on a unique constraint
        — so determinism is asserted over everything except those.
        """
        first = demo_client.post("/api/v1/demo/run").json()
        second = demo_client.post("/api/v1/demo/run").json()

        def shape(body: dict) -> list[tuple]:
            return [
                (
                    step["number"],
                    step["title"],
                    step["tone"],
                    tuple(f["label"] for f in step["facts"]),
                )
                for step in body["steps"]
            ]

        assert shape(first) == shape(second)
        assert first["final_status"] == second["final_status"]


class TestTheDemoPersistsNothing:
    def test_no_row_survives_the_endpoint(
        self, demo_client: TestClient, db_session: Session
    ) -> None:
        """The strongest claim the demo makes, checked from a separate session."""
        before = {
            model: db_session.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (Merchant, Customer, Order, PaymentAttempt, Event)
        }

        assert demo_client.post("/api/v1/demo/run").status_code == 200

        db_session.expire_all()
        after = {
            model: db_session.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (Merchant, Customer, Order, PaymentAttempt, Event)
        }
        assert after == before

    def test_run_demo_does_not_commit(self, db_session: Session) -> None:
        """The service leaves the transaction to its caller, as everything does."""
        run = run_demo(db_session)

        assert run.committed is False
        assert db_session.in_transaction()

    def test_the_runner_offers_no_way_to_commit(self) -> None:
        """No `--commit` equivalent — not defaulted off, absent."""
        tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
        functions = {
            node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }

        execute_args = functions["execute"].args
        names = [a.arg for a in execute_args.args + execute_args.kwonlyargs]
        assert names == ["settings"]

        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "commit" not in calls
        assert "rollback" in calls

    def test_the_route_offers_no_way_to_commit(self) -> None:
        tree = ast.parse(ROUTE_SOURCE.read_text(encoding="utf-8"))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "commit" not in calls

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_demo_endpoint":
                args = node.args
                assert [a.arg for a in args.args + args.kwonlyargs] == ["settings"]


class TestTheDemoPathCannotOpenASocket:
    def test_the_runner_constructs_no_razorpay_client(self) -> None:
        tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        assert "build_client" not in called
        assert "Client" not in called

    def test_the_runner_imports_no_network_capable_module(self) -> None:
        """`razorpay`, `httpx`, `requests`, `socket`, `urllib` — none of them.

        Structural rather than behavioural: an import that is never reached by a
        passing test is exactly the import that opens a socket in the demo.
        """
        tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])

        forbidden = {"razorpay", "httpx", "requests", "socket", "urllib", "http", "openai"}
        assert not (modules & forbidden)

    def test_the_route_imports_no_network_capable_module(self) -> None:
        tree = ast.parse(ROUTE_SOURCE.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])

        forbidden = {"razorpay", "httpx", "requests", "socket", "urllib", "http", "openai"}
        assert not (modules & forbidden)

    def test_the_demo_runs_with_every_credential_empty(self, demo_client: TestClient) -> None:
        """The settings this client injects hold no credential at all."""
        settings = _settings(resolve_test_dsn())
        assert settings.razorpay_configured is False
        assert settings.razorpay_webhook_configured is False
        assert settings.openrouter_configured is False

        assert demo_client.post("/api/v1/demo/run").status_code == 200

    def test_the_demo_completes_with_every_outbound_connection_blocked(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The behavioural proof, and the reason it uses `run_demo` not `execute`.

        Blocking every new socket would also block PostgreSQL, and a test that
        could not tell those apart would prove nothing. So the database
        connection is established *first* — `db_session` is already open — and
        the block is installed after. Every remaining query travels the existing
        connection, so any socket this forbids is one the demo itself opened.
        """
        import socket

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("the demo attempted an outbound connection")

        # The connection exists before anything is blocked.
        db_session.execute(select(func.count()).select_from(Event)).scalar_one()

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket.socket, "connect_ex", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)

        # The block is real: prove it before relying on it.
        with pytest.raises(AssertionError, match="outbound connection"):
            socket.create_connection(("127.0.0.1", 1))

        run = run_demo(db_session)

        assert run.committed is False
        assert len(run.steps) == 6
