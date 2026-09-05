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

from app.models import Patient


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
    DECLINED = "DECLINED"  # Phase 12+: a facility refusing a PREPARE (see acceptance.py)


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
    # Phase 12+: carried on a PREPARE targeting a capacity-aware transport so
    # the facility's own accept() (Phase 14's F1) can run the transport-time
    # window check (acceptance.py step 5) — None for every plain command.
    eta_minutes: Optional[float] = Field(default=None, ge=0)
    # Phase 14: carried on the same PREPARE so the facility can run
    # accept() without needing an external, out-of-band patient registry —
    # a message envelope should be self-contained. None for every plain
    # command; a Facility remembers it locally (see facility.py's
    # _known_patients) the first time it sees a given transport_id, since
    # later commands for that transport (ACTIVATE_AT, WITHDRAW_AT, ...)
    # don't repeat it.
    patient: Optional[Patient] = Field(default=None)
    # Phase 12+: a per-transport counter stamped on every REDIRECT_NOTICE, so
    # the ambulance can tell a notice issued *before* it was stood down from
    # one issued after. The epoch fence can't do this on its own: R15 walks a
    # whole candidate list under a single epoch, so a notice for a candidate
    # that has since declined carries the same epoch as its replacement.
    # Without it, a notice already on the wire when the dispatcher gives up
    # lands afterwards and sends the ambulance back to the hospital that
    # just refused it (I3 / E33). None on every plain, non-capacity command.
    notice_seq: Optional[int] = Field(default=None, ge=1)

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
    # Phase 12+: only ever set alongside ack_type=DECLINED (acceptance.py's
    # Decision.reason, e.g. "no_bed:ICU").
    reason: Optional[str] = Field(default=None)
