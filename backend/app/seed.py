"""Static demo data: the three hospitals, the one sample transport, and the
default network parameters simulation.py (Phase 6) builds its Bus from."""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class HospitalSeed:
    facility_id: str
    beds_available: int
    distance_km: float
    specialities: tuple[str, ...]


HOSPITALS: tuple[HospitalSeed, ...] = (
    HospitalSeed("Hospital_A", beds_available=6, distance_km=4.2, specialities=("trauma", "cardiac")),
    HospitalSeed("Hospital_B", beds_available=3, distance_km=6.8, specialities=("stroke", "trauma")),
    HospitalSeed("Hospital_C", beds_available=9, distance_km=9.5, specialities=("burn", "pediatric")),
)


@dataclass(frozen=True)
class PatientSeed:
    patient_id: str
    acuity: int
    condition: str


@dataclass(frozen=True)
class TransportSeed:
    transport_id: str
    patient: PatientSeed
    initial_destination: str


TRANSPORT = TransportSeed(
    transport_id="AMB-101",
    patient=PatientSeed(patient_id="PT-01", acuity=3, condition="chest pain"),
    initial_destination="Hospital_A",
)


@dataclass(frozen=True)
class NetworkSeed:
    min_delay_ms: int
    max_delay_ms: int
    duplicate_rate: float


NETWORK = NetworkSeed(min_delay_ms=50, max_delay_ms=settings.d_max_ms, duplicate_rate=0.05)
