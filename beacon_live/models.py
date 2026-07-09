"""Shared data models for beacon and survey analysis."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BeaconRecord:
    timestamp: float
    ssid: str | None
    bssid: str
    qbss_cu_raw: int | None
    qbss_cu_percent: float | None
    qbss_station_count: int | None
    qbss_admission_capacity: int | None


@dataclass(frozen=True, slots=True)
class SurveySample:
    timestamp: float
    active_ms: int | None
    busy_ms: int | None
    receive_ms: int | None
    transmit_ms: int | None
    noise_dbm: int | None


@dataclass(frozen=True, slots=True)
class SecondStats:
    second: int
    unique_bssid_count: int
    qbss_station_count_sum: int
    qbss_cu_min_percent: float | None
    qbss_cu_mean_percent: float | None
    qbss_cu_max_percent: float | None
    top_qbss_cu_ssid: str | None
    top_qbss_cu_bssid: str | None
    top_qbss_cu_percent: float | None
    local_cu_percent: float | None
