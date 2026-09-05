"""Phase 12 smoke check (build-only phase — no tests here; see the phase
report). Run with:

    python scripts/phase12_smoke_check.py

Prints rank() for the three sample patients against the six-hospital seed,
with the exclusion reason for every declined hospital, then demonstrates the
bed ledger's free()/load() derivation from a hand-built event list.
"""
from __future__ import annotations

from app.events import Event, EventType
from app.models import BedType
from app.projection import empty_ledger_view, free, load, project_ledger
from app.scoring import rank
from app.seed import INITIAL_HOSPITAL_STATUSES, MULTI_HOSPITALS, SAMPLE_PATIENTS

_POSITION = (20.0, 20.0)  # roughly the centre of the 40x40 grid
_SPEED_KM_PER_MIN = 1.0
_LOAD_WEIGHT = 10.0
_SATURATION_LIMIT = 0.9


def print_rankings() -> None:
    empty_ledger = {hospital.id: empty_ledger_view(hospital.id) for hospital in MULTI_HOSPITALS}
    for patient in SAMPLE_PATIENTS:
        print(f"\n=== {patient.id}: acuity={patient.acuity} {patient.condition.value} {patient.age_group.value} ===")
        ranked = rank(
            patient, _POSITION, list(MULTI_HOSPITALS), INITIAL_HOSPITAL_STATUSES, empty_ledger,
            _SPEED_KM_PER_MIN, _LOAD_WEIGHT, _SATURATION_LIMIT,
        )
        for candidate in ranked:
            if candidate.score is not None:
                print(f"  ACCEPT  {candidate.hospital_id:12s} score={candidate.score:.3f}")
            else:
                print(f"  DECLINE {candidate.hospital_id:12s} reason={candidate.reason}")


def print_ledger_demo() -> None:
    print("\n=== bed ledger: free()/load() from a hand-built event list ===")
    hospital = next(h for h in MULTI_HOSPITALS if h.id == "Hospital_2")  # ICU:5, no paediatric
    status = INITIAL_HOSPITAL_STATUSES[hospital.id]

    events = [
        Event(transport_id="AMB-1", epoch=1, ts_ms=0, type=EventType.BED_RESERVED, facility_id=None,
              payload={"hospital_id": hospital.id, "transport_id": "AMB-1", "bed_type": BedType.ICU.value}),
        Event(transport_id="AMB-2", epoch=1, ts_ms=10, type=EventType.BED_RESERVED, facility_id=None,
              payload={"hospital_id": hospital.id, "transport_id": "AMB-2", "bed_type": BedType.ICU.value}),
        Event(transport_id="AMB-3", epoch=1, ts_ms=20, type=EventType.BED_RESERVED, facility_id=None,
              payload={"hospital_id": hospital.id, "transport_id": "AMB-3", "bed_type": BedType.GENERAL.value}),
    ]
    ledger = project_ledger(events, patients={})
    view = ledger[hospital.id]
    print(f"After 2 ICU + 1 GENERAL reservation: free(ICU)={free(view, status, BedType.ICU)} "
          f"free(GENERAL)={free(view, status, BedType.GENERAL)} load={load(view, status):.3f}")

    events.append(
        Event(transport_id="AMB-1", epoch=1, ts_ms=30, type=EventType.BED_OCCUPIED, facility_id=None,
              payload={"hospital_id": hospital.id, "transport_id": "AMB-1", "bed_type": BedType.ICU.value})
    )
    events.append(
        Event(transport_id="AMB-2", epoch=1, ts_ms=40, type=EventType.BED_RELEASED, facility_id=None,
              payload={"hospital_id": hospital.id, "transport_id": "AMB-2", "bed_type": BedType.ICU.value})
    )
    ledger = project_ledger(events, patients={})
    view = ledger[hospital.id]
    print(f"After AMB-1 arrives (reserved->occupied) and AMB-2's reservation releases: "
          f"free(ICU)={free(view, status, BedType.ICU)} load={load(view, status):.3f}")


if __name__ == "__main__":
    print_rankings()
    print_ledger_demo()
