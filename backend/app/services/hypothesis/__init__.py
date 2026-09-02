"""The seam where the hypothesis agent meets the database.

Two modules, deliberately split:

* `loader` turns stored experiment data into a `HypothesisRequest` — the only
  thing a model is ever shown. It reads `app.causal` and nothing else.
* `service` runs the fixed order (propose, validate, falsify, record) and
  persists only when every step succeeds.

Neither holds decision logic. Proposing lives in `app.agents`, validation and
falsification in `app.agents.hypothesis_agent` and `app.engine.falsification`,
and persistence in `app.repositories.audit_repository` — each testable without
the others, and none of them reachable by the model.
"""

from __future__ import annotations

from app.services.hypothesis.loader import (
    FEATURE_VOCABULARY,
    LoaderError,
    load_hypothesis_request,
)
from app.services.hypothesis.service import (
    HypothesisOutcome,
    HypothesisServiceError,
    generate_and_record,
)

__all__ = [
    "FEATURE_VOCABULARY",
    "HypothesisOutcome",
    "HypothesisServiceError",
    "LoaderError",
    "generate_and_record",
    "load_hypothesis_request",
]
