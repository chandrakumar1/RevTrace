"""Deterministic randomness.

If these fail, nothing built on top of the RNG is trustworthy.
"""

from __future__ import annotations

import uuid

import pytest
from simulator.rng import DeterministicRng


class TestSeeding:
    def test_same_seed_same_sequence(self) -> None:
        a = DeterministicRng(42)
        b = DeterministicRng(42)
        assert [a.randint(0, 10_000) for _ in range(50)] == [
            b.randint(0, 10_000) for _ in range(50)
        ]

    def test_different_seed_different_sequence(self) -> None:
        a = DeterministicRng(42)
        b = DeterministicRng(43)
        assert [a.randint(0, 10_000) for _ in range(50)] != [
            b.randint(0, 10_000) for _ in range(50)
        ]

    def test_rejects_non_integer_seed(self) -> None:
        with pytest.raises(TypeError):
            DeterministicRng("42")  # type: ignore[arg-type]

    def test_rejects_bool_seed(self) -> None:
        with pytest.raises(TypeError):
            DeterministicRng(True)  # type: ignore[arg-type]

    def test_rejects_negative_seed(self) -> None:
        with pytest.raises(ValueError):
            DeterministicRng(-1)


class TestSubStreamIsolation:
    """The property that lets scenarios evolve without churning every checksum."""

    def test_derive_is_reproducible(self) -> None:
        a = DeterministicRng(7).derive("amounts")
        b = DeterministicRng(7).derive("amounts")
        assert [a.randint(0, 999) for _ in range(20)] == [b.randint(0, 999) for _ in range(20)]

    def test_different_labels_diverge(self) -> None:
        root = DeterministicRng(7)
        amounts = root.derive("amounts")
        timing = DeterministicRng(7).derive("timing")
        assert [amounts.randint(0, 10**6) for _ in range(20)] != [
            timing.randint(0, 10**6) for _ in range(20)
        ]

    def test_drawing_from_one_stream_does_not_perturb_another(self) -> None:
        """Adding draws in one concern must not shift values in another."""
        root_a = DeterministicRng(7)
        amounts_a = root_a.derive("amounts")
        timing_a = root_a.derive("timing")
        baseline = [timing_a.randint(0, 10**6) for _ in range(10)]

        root_b = DeterministicRng(7)
        amounts_b = root_b.derive("amounts")
        for _ in range(500):  # heavy extra use of an unrelated stream
            amounts_b.randint(0, 10**6)
        timing_b = root_b.derive("timing")

        assert [timing_b.randint(0, 10**6) for _ in range(10)] == baseline
        del amounts_a

    def test_empty_label_rejected(self) -> None:
        with pytest.raises(ValueError):
            DeterministicRng(1).derive("")

    def test_nested_derivation(self) -> None:
        a = DeterministicRng(3).derive("x").derive("y")
        b = DeterministicRng(3).derive("x").derive("y")
        assert a.randint(0, 10**9) == b.randint(0, 10**9)


class TestUuidGeneration:
    def test_is_reproducible(self) -> None:
        assert DeterministicRng(11).uuid() == DeterministicRng(11).uuid()

    def test_has_version_4_shape(self) -> None:
        assert DeterministicRng(11).uuid().version == 4

    def test_is_a_real_uuid(self) -> None:
        value = DeterministicRng(11).uuid()
        assert isinstance(value, uuid.UUID)
        assert uuid.UUID(str(value)) == value

    def test_no_collisions_across_a_large_sample(self) -> None:
        rng = DeterministicRng(5)
        values = {rng.uuid() for _ in range(2_000)}
        assert len(values) == 2_000

    def test_different_seeds_give_different_uuids(self) -> None:
        assert DeterministicRng(1).uuid() != DeterministicRng(2).uuid()


class TestDraws:
    def test_choice_is_reproducible(self) -> None:
        options = ("a", "b", "c", "d", "e")
        a = DeterministicRng(9)
        b = DeterministicRng(9)
        assert [a.choice(options) for _ in range(20)] == [b.choice(options) for _ in range(20)]

    def test_choice_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            DeterministicRng(1).choice([])

    def test_shuffled_does_not_mutate_input(self) -> None:
        original = [1, 2, 3, 4, 5]
        DeterministicRng(1).shuffled(original)
        assert original == [1, 2, 3, 4, 5]

    def test_shuffled_is_reproducible(self) -> None:
        items = list(range(20))
        assert DeterministicRng(4).shuffled(items) == DeterministicRng(4).shuffled(items)

    def test_randrange_respects_step(self) -> None:
        rng = DeterministicRng(2)
        for _ in range(100):
            assert rng.randrange(1_000, 10_000, 100) % 100 == 0
