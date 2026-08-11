"""Cupo -- usage limits, feature gates and metering for AI products."""

from .client import Cupo
from .plans import CheckResult, Entitlement, PlanError, Plans
from .store import PostgresStore
from .windows import InvalidWindow, window_end, window_start

__version__ = "0.1.0.dev0"

__all__ = [
    "Cupo",
    "CheckResult",
    "Entitlement",
    "PlanError",
    "Plans",
    "PostgresStore",
    "InvalidWindow",
    "window_start",
    "window_end",
]
