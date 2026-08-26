"""Secret hygiene helpers.

Phase 1 scope is deliberately narrow: this module exists so that no secret can
reach a log line, an audit snapshot, or an API response. Authentication and
authorisation are not part of Phase 1.

Razorpay signature verification lives in app/integrations/razorpay/webhooks.py
(Phase 9), not here — this module must stay provider-agnostic.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

REDACTED = "***REDACTED***"

#: Substrings that mark a mapping key as secret-bearing. Matched case-insensitively.
SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "signature",
    "private_key",
    "credential",
    "key_secret",
)


def is_sensitive_key(key: str) -> bool:
    """Return True if a mapping key looks like it holds a secret."""
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def mask(value: str, keep_last: int = 4) -> str:
    """Mask a value, optionally revealing a short suffix for correlation.

    Short values are masked entirely rather than partially revealed.
    """
    if not value:
        return ""
    if len(value) <= keep_last or keep_last <= 0:
        return REDACTED
    return f"{REDACTED[:-3]}{value[-keep_last:]}"


def redact(data: Any, _depth: int = 0) -> Any:
    """Recursively redact secret-bearing values in a structure.

    Used before writing payloads to logs or to audit snapshots. Unknown types
    are returned unchanged; SecretStr never yields its value.
    """
    if _depth > 12:  # defensive: refuse to walk pathological nesting
        return "***TRUNCATED***"

    if isinstance(data, SecretStr):
        return REDACTED

    if isinstance(data, dict):
        return {
            k: (REDACTED if is_sensitive_key(str(k)) else redact(v, _depth + 1))
            for k, v in data.items()
        }

    if isinstance(data, (list, tuple)):
        redacted = [redact(item, _depth + 1) for item in data]
        return type(data)(redacted) if isinstance(data, tuple) else redacted

    return data
