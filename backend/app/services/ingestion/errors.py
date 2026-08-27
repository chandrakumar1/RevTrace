"""Ingestion errors."""

from __future__ import annotations


class IngestionError(Exception):
    """Base class for ingestion failures."""


class UnknownEntityReferenceError(IngestionError):
    """An event references an entity that is neither in the batch nor stored.

    Raised before any write. `events.merchant_id` is NOT NULL with a foreign
    key, so this would otherwise surface as an opaque integrity error partway
    through a batch.
    """
