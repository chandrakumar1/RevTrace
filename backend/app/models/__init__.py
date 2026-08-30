"""ORM model package.

Every model must be imported here. Alembic autogenerate only sees tables that
are registered on Base.metadata at import time; a model missing from this list
is silently omitted from migrations.
"""

from app.db.base import Base
from app.models.audit_event import AuditEvent
from app.models.case_assignment import CaseAssignment
from app.models.case_outcome import CaseOutcome
from app.models.customer import Customer
from app.models.event import Event
from app.models.experiment import Experiment
from app.models.experiment_result import ExperimentResult
from app.models.intervention import Intervention
from app.models.merchant import Merchant
from app.models.order import Order
from app.models.payment_attempt import PaymentAttempt
from app.models.recovery_action import RecoveryAction
from app.models.recovery_case import RecoveryCase
from app.models.revenue_risk import RevenueRisk
from app.models.uplift_score import UpliftScore

__all__ = [
    "AuditEvent",
    "Base",
    "CaseAssignment",
    "CaseOutcome",
    "Customer",
    "Event",
    "Experiment",
    "ExperimentResult",
    "Intervention",
    "Merchant",
    "Order",
    "PaymentAttempt",
    "RecoveryAction",
    "RecoveryCase",
    "RevenueRisk",
    "UpliftScore",
]
