"""Ground-truth isolation, the committed fixture, and webhook pathology.

The isolation tests here are the v2 counterpart of the Phase 3 ground-truth
guard: the answer key lives in the simulator, and the application must have no
path to it. Day 1 already proved `app/` never names a `truth_*` column; this
proves it never imports the generator that produces one either.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from simulator.benchmark_fixture import (
    BENCHMARK_SEED,
    build_snapshot,
    population_checksum,
    read_fixture,
)
from simulator.delivery import apply_delivery_plan
from simulator.potential_outcomes import DEFAULT_CASE_COUNT, generate
from simulator.rng import DeterministicRng
from simulator.segments import SEGMENTS, SegmentId
from simulator.webhooks import (
    CORRUPTIONS,
    MAX_DELAY_SECONDS,
    Corruption,
    build_delivery_plan,
    emit_malformed,
    malformed_from,
)

APP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "app"

#: Modules that exist only to hold the answer key.
GROUND_TRUTH_MODULES = ("potential_outcomes", "segments", "benchmark_fixture")


def app_files() -> list[pathlib.Path]:
    return sorted(APP_ROOT.rglob("*.py"))


class TestApplicationCannotReachTheAnswerKey:
    def test_app_imports_no_simulator_module(self) -> None:
        for path in app_files():
            source = path.read_text(encoding="utf-8")
            assert "import simulator" not in source, path
            assert "from simulator" not in source, path

    @pytest.mark.parametrize("module", GROUND_TRUTH_MODULES)
    def test_app_never_names_a_ground_truth_module(self, module: str) -> None:
        for path in app_files():
            assert module not in path.read_text(encoding="utf-8"), f"{path} names {module}"

    def test_the_scan_actually_covers_the_application(self) -> None:
        """A scan over zero files passes trivially."""
        assert len(app_files()) > 40

    def test_causal_and_engine_are_covered_when_they_exist(self) -> None:
        engine = APP_ROOT / "engine"
        assert engine.exists()
        for path in engine.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for module in GROUND_TRUTH_MODULES:
                assert module not in source, path


class TestTheDependencyDirectionHolds:
    def test_the_simulator_may_read_app_enums(self) -> None:
        """The permitted direction: simulator -> app, never the reverse."""
        from simulator import segments

        source = pathlib.Path(segments.__file__).read_text(encoding="utf-8")
        assert "from app.models.enums import" in source

    def test_the_simulator_imports_only_vocabulary_from_the_app(self) -> None:
        """Enums and value constants are fine — sharing them is what stops the
        two sides drifting. Services, repositories, the engine, the API and the
        database layer are not: importing one would make the simulator depend on
        behaviour it is supposed to be generating test data *for*.

        `app.models.mixins` is on the permitted side because what is taken from
        it is `DEFAULT_CURRENCY`, a string constant, not an ORM class.
        """
        forbidden_prefixes = (
            "app.services",
            "app.repositories",
            "app.engine",
            "app.api",
            "app.db",
            "app.experiments",
            "app.causal",
        )
        root = pathlib.Path(__file__).resolve().parents[3] / "simulator"

        for path in sorted(root.rglob("*.py")):
            modules: set[str] = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    modules.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.add(node.module)

            for module in modules:
                assert not module.startswith(forbidden_prefixes), f"{path}: {module}"

    def test_the_simulator_never_imports_an_orm_model(self) -> None:
        """Vocabulary, not tables. The simulator writes no rows."""
        root = pathlib.Path(__file__).resolve().parents[3] / "simulator"
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            assert "from app.models import" not in source, path
            assert "sqlalchemy" not in source, path


class TestCommittedFixture:
    def test_the_fixture_exists_and_records_its_provenance(self) -> None:
        fixture = read_fixture()
        assert fixture["seed"] == BENCHMARK_SEED
        assert fixture["case_count"] == DEFAULT_CASE_COUNT
        assert fixture["generator_version"] == "2.0.0"

    def test_regenerating_reproduces_the_committed_checksum(self) -> None:
        """The point of the fixture: the generator is reproducible, not a copy
        of its output."""
        population = generate(seed=BENCHMARK_SEED, case_count=DEFAULT_CASE_COUNT)
        assert population_checksum(population) == read_fixture()["checksum_sha256"]

    def test_the_snapshot_matches_the_committed_file(self) -> None:
        assert build_snapshot() == read_fixture()

    def test_the_fixture_records_every_planted_segment(self) -> None:
        planted = read_fixture()["planted_parameters"]
        assert set(planted) == {spec.id.value for spec in SEGMENTS}

    def test_the_fixture_records_ground_truth_for_every_segment(self) -> None:
        by_segment = read_fixture()["ground_truth"]["by_segment"]
        assert set(by_segment) == {spec.id.value for spec in SEGMENTS}

    def test_the_fixture_shows_the_sleeping_dog(self) -> None:
        dog = read_fixture()["ground_truth"]["by_segment"][
            SegmentId.LOW_ENGAGEMENT_MANDATE_HOLDER.value
        ]
        assert dog["true_harm_ate_bps"] >= 700
        assert dog["is_sleeping_dog"] is True

    def test_the_fixture_is_canonical_json(self) -> None:
        import json

        from simulator.benchmark_fixture import FIXTURE_PATH

        raw = FIXTURE_PATH.read_text(encoding="utf-8")
        assert raw == json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n"

    def test_every_fixture_rate_is_an_integer(self) -> None:
        for entry in read_fixture()["ground_truth"]["by_segment"].values():
            for key, value in entry.items():
                if key.endswith("_bps") or key == "n":
                    assert isinstance(value, int) and not isinstance(value, bool)


class TestDeliveryPathology:
    def _events(self, count: int = 200):  # noqa: ANN202
        from simulator import simulate

        result = simulate("S14", seed=42)
        events = [d.event for d in result.deliveries][:count]
        assert events
        return events

    def test_a_plan_is_reproducible(self) -> None:
        first = build_delivery_plan(DeterministicRng(5), 500)
        second = build_delivery_plan(DeterministicRng(5), 500)
        assert first.duplicates == second.duplicates
        assert first.delays == second.delays
        assert first.reorder == second.reorder

    def test_duplicates_land_near_the_stated_rate(self) -> None:
        plan = build_delivery_plan(DeterministicRng(11), 10_000)
        assert 300 <= len(plan.duplicates) <= 700  # ~5%

    def test_delays_are_bounded_by_thirty_minutes(self) -> None:
        plan = build_delivery_plan(DeterministicRng(11), 5_000)
        assert plan.delays
        assert all(1 <= seconds <= MAX_DELAY_SECONDS for seconds in plan.delays.values())

    def test_reordering_is_local_rather_than_a_shuffle(self) -> None:
        """Real webhook reordering is adjacent; a full shuffle would be an
        easier problem than production actually presents."""
        plan = build_delivery_plan(DeterministicRng(3), 2_000)
        assert plan.reorder is not None
        displacements = [abs(i - position) for position, i in enumerate(plan.reorder)]
        assert max(displacements) <= 1

    def test_a_plan_applies_cleanly_to_real_events(self) -> None:
        events = self._events()
        plan = build_delivery_plan(DeterministicRng(2), len(events))
        deliveries = apply_delivery_plan(events, plan)
        assert len(deliveries) >= len(events)

    def test_duplicates_and_out_of_order_are_actually_emitted(self) -> None:
        events = self._events()
        plan = build_delivery_plan(DeterministicRng(2), len(events))
        deliveries = apply_delivery_plan(events, plan)
        assert any(d.envelope.is_duplicate for d in deliveries)
        assert any(d.envelope.is_delayed for d in deliveries)

    def test_no_plan_for_an_empty_stream(self) -> None:
        plan = build_delivery_plan(DeterministicRng(1), 0)
        assert plan.duplicates == {}
        assert plan.reorder is None

    def test_a_negative_event_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            build_delivery_plan(DeterministicRng(1), -1)


class TestMalformedDeliveries:
    def _event(self):  # noqa: ANN202
        from simulator import simulate

        return simulate("S04", seed=42).deliveries[0].event

    def test_every_corruption_kind_is_producible(self) -> None:
        event = self._event()
        for corruption in CORRUPTIONS:
            malformed = malformed_from(event, corruption)
            assert malformed.corruption is corruption

    def test_a_missing_amount_is_actually_missing(self) -> None:
        malformed = malformed_from(self._event(), Corruption.MISSING_AMOUNT)
        assert "amount_minor" not in malformed.payload

    def test_a_string_amount_is_a_string(self) -> None:
        malformed = malformed_from(self._event(), Corruption.AMOUNT_AS_STRING)
        assert isinstance(malformed.payload["amount_minor"], str)

    def test_a_float_amount_is_a_float(self) -> None:
        """ADR 0001 forbids float money end to end; ingestion must reject it."""
        malformed = malformed_from(self._event(), Corruption.AMOUNT_AS_FLOAT)
        assert isinstance(malformed.payload["amount_minor"], float)

    def test_a_negative_amount_is_negative(self) -> None:
        malformed = malformed_from(self._event(), Corruption.NEGATIVE_AMOUNT)
        assert int(malformed.payload["amount_minor"]) < 0  # type: ignore[arg-type]

    def test_a_blank_identity_cannot_be_deduplicated(self) -> None:
        malformed = malformed_from(self._event(), Corruption.BLANK_EXTERNAL_ID)
        assert malformed.external_event_id == ""

    def test_an_unknown_event_type_is_outside_the_vocabulary(self) -> None:
        from app.models.enums import EventType

        malformed = malformed_from(self._event(), Corruption.UNKNOWN_EVENT_TYPE)
        assert malformed.event_type not in EventType.values()

    def test_emission_lands_near_one_percent(self) -> None:
        from simulator import simulate

        events = [d.event for d in simulate("S14", seed=42).deliveries]
        pool = events * 60  # ~5,400 events
        malformed = emit_malformed(DeterministicRng(4), pool)
        share = len(malformed) * 10_000 // len(pool)
        assert 40 <= share <= 200, share

    def test_emission_is_reproducible(self) -> None:
        from simulator import simulate

        events = [d.event for d in simulate("S04", seed=42).deliveries] * 40
        first = emit_malformed(DeterministicRng(8), events)
        second = emit_malformed(DeterministicRng(8), events)
        assert [m.corruption for m in first] == [m.corruption for m in second]

    def test_malformed_deliveries_are_returned_separately(self) -> None:
        """A rejected payload must not poison the timeline it arrived in, and
        that can only be asserted if the valid events are all still present."""
        from simulator import simulate

        events = [d.event for d in simulate("S04", seed=42).deliveries]
        malformed = emit_malformed(DeterministicRng(6), events, rate_bps=10_000)

        assert len(malformed) == len(events)
        assert all(hasattr(m, "corruption") for m in malformed)
        # The originals are untouched.
        assert [e.external_event_id for e in events] == [
            d.event.external_event_id for d in simulate("S04", seed=42).deliveries
        ]

    def test_a_zero_rate_emits_nothing(self) -> None:
        from simulator import simulate

        events = [d.event for d in simulate("S04", seed=42).deliveries]
        assert emit_malformed(DeterministicRng(1), events, rate_bps=0) == ()
