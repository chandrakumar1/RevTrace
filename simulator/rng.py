"""Deterministic randomness.

Every random draw in the simulator comes from here. Nothing calls the `random`
module at module level, and nothing calls `uuid.uuid4()` — that uses
`os.urandom` and is not reproducible.

Sub-stream derivation is the important part. Each concern (entities, timing,
amounts, delivery) gets its own derived generator, seeded from
`sha256(parent_seed, label)`. Without that isolation, adding a single draw in
one concern would silently shift every value in every other concern, and every
recorded checksum would move for no real reason.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

#: Bytes of the derivation digest used to seed a sub-stream.
_DERIVE_SEED_BYTES = 8


def _derive_seed(parent_seed: int, label: str) -> int:
    """Derive a child seed deterministically from a parent seed and a label."""
    digest = hashlib.sha256(f"{parent_seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:_DERIVE_SEED_BYTES], "big")


class DeterministicRng:
    """A seeded random source with named, isolated sub-streams.

    Instances are independent: deriving or drawing from one never perturbs
    another.
    """

    __slots__ = ("_label", "_random", "_seed")

    def __init__(self, seed: int, label: str = "root") -> None:
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise TypeError(f"seed must be an int, got {type(seed).__name__}")
        if seed < 0:
            raise ValueError(f"seed must be non-negative, got {seed}")

        self._seed = seed
        self._label = label
        self._random = random.Random(seed)

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def label(self) -> str:
        return self._label

    def derive(self, label: str) -> DeterministicRng:
        """Return an independent sub-stream for one concern."""
        if not label:
            raise ValueError("sub-stream label must not be empty")
        return DeterministicRng(_derive_seed(self._seed, label), f"{self._label}.{label}")

    # -- draws ------------------------------------------------------------

    def randbytes(self, n: int) -> bytes:
        return self._random.randbytes(n)

    def randrange(self, start: int, stop: int, step: int = 1) -> int:
        return self._random.randrange(start, stop, step)

    def randint(self, a: int, b: int) -> int:
        return self._random.randint(a, b)

    def choice(self, seq: Sequence[T]) -> T:
        if not seq:
            raise ValueError("cannot choose from an empty sequence")
        return seq[self._random.randrange(0, len(seq))]

    def shuffled(self, seq: Sequence[T]) -> list[T]:
        """Return a shuffled copy. Never mutates the input."""
        items = list(seq)
        self._random.shuffle(items)
        return items

    def uuid(self) -> uuid.UUID:
        """A reproducible UUID with correct version-4 shape.

        `uuid.uuid4()` is deliberately not used: it draws from `os.urandom` and
        would make every run produce different identifiers.
        """
        return uuid.UUID(bytes=self.randbytes(16), version=4)

    def __repr__(self) -> str:
        return f"DeterministicRng(seed={self._seed}, label={self._label!r})"
