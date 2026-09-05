"""DispatcherStatus lives in its own module rather than dispatcher.py purely
to avoid a circular import: projection.py needs this enum to build
TransportView.status, and (from Phase 12 onward) dispatcher.py needs
projection.py's pure ledger/read functions — if DispatcherStatus stayed in
dispatcher.py, those two modules would import each other. dispatcher.py
imports and re-exports this name, so every existing
`from app.dispatcher import DispatcherStatus` keeps working unchanged.
"""
from __future__ import annotations

from enum import Enum


class DispatcherStatus(str, Enum):
    STARTING = "STARTING"
    STABLE = "STABLE"
    PREPARING = "PREPARING"
    CUTOVER_SCHEDULED = "CUTOVER_SCHEDULED"
    NOT_READY = "NOT_READY"
    # Phase 13: every candidate in the queue declined (or there were none to
    # begin with) and there is no current destination to fall back to.
    NO_ACCEPTING_FACILITY = "NO_ACCEPTING_FACILITY"
    # Phase 15 (R18): a manual-policy transport was displaced by a capacity
    # rebalance and couldn't be auto-redirected — needs an operator's call.
    NEEDS_REPLAN = "NEEDS_REPLAN"
