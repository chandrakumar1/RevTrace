"""Health check response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "revtrace-backend"
    environment: str
    timestamp: datetime


class DbHealthResponse(BaseModel):
    """Database connectivity.

    `razorpay_configured` and `gemini_configured` report only whether
    credentials are present — never any part of their value.
    """

    status: Literal["ok", "degraded"]
    database_connected: bool
    environment: str
    timestamp: datetime
    razorpay_configured: bool = Field(
        description="Whether Razorpay TEST-mode credentials are present. Never the value."
    )
    gemini_configured: bool = Field(
        description="Whether a Gemini API key is present. Never the value."
    )
