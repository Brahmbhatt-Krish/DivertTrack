"""The two envelopes that cross the bus. Every field is typed and validated
at construction (Pydantic) — nothing downstream re-checks that a command has
a real action or that an epoch is non-negative, because it cannot exist
otherwise.

FacilityState lives here, not in facility.py, because Ack.applied_state is
part of the envelope's contract: facility.py depends on messages.py, never
the other way around.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActionType(str, Enum):
    PREPARE = "PREPARE"
    ACTIVATE_AT = "ACTIVATE_AT"
    WITHDRAW_AT = "WITHDRAW_AT"
    WITHDRAW = "WITHDRAW"
    REMAIN = "REMAIN"
    REDIRECT_NOTICE = "REDIRECT_NOTICE"


class AckType(str, Enum):
    READY = "READY"
    RECEIVED = "RECEIVED"
    APPLIED = "APPLIED"


class FacilityState(str, Enum):
    IDLE = "IDLE"
    ARMED = "ARMED"
    ACTIVE = "ACTIVE"
    WITHDRAWN = "WITHDRAWN"


class Command(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: str = Field(min_length=1)
    transport_id: str = Field(min_length=1)
    target_facility: str = Field(min_length=1)
    epoch: int = Field(ge=1)
    action: ActionType
    effective_at_ms: Optional[int] = Field(default=None, ge=0)
    sent_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _effective_at_matches_action(self) -> "Command":
        # ACTIVATE_AT/WITHDRAW_AT are scheduled commands and must carry the
        # time they take effect; every other action is immediate and must
        # not — a stray effective_at_ms would silently be ignored downstream
        # otherwise, which is exactly the kind of bug we want at the boundary.
        needs_effective_at = self.action in (ActionType.ACTIVATE_AT, ActionType.WITHDRAW_AT)
        if needs_effective_at and self.effective_at_ms is None:
            raise ValueError(f"{self.action.value} requires effective_at_ms, received None")
        if not needs_effective_at and self.effective_at_ms is not None:
            raise ValueError(
                f"{self.action.value} must not carry effective_at_ms, received {self.effective_at_ms}"
            )
        return self


class Ack(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: str = Field(min_length=1)
    transport_id: str = Field(min_length=1)
    facility_id: str = Field(min_length=1)
    epoch: int = Field(ge=1)
    ack_type: AckType
    applied_state: FacilityState
    sent_at_ms: int = Field(ge=0)
