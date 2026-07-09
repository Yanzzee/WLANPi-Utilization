"""Shared data models for beacon and survey analysis."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BeaconRecord:
    timestamp: float
    ssid: Optional[str]
    bssid: str
    qbss_cu_raw: Optional[int]
    qbss_cu_percent: Optional[float]
    qbss_station_count: Optional[int]
    qbss_admission_capacity: Optional[int]


@dataclass(frozen=True)
class SurveySample:
    timestamp: float
    active_ms: Optional[int]
    busy_ms: Optional[int]
    receive_ms: Optional[int]
    transmit_ms: Optional[int]
    noise_dbm: Optional[int]


@dataclass(frozen=True)
class SecondStats:
    second: int
    unique_bssid_count: int
    qbss_station_count_sum: int
    qbss_cu_min_percent: Optional[float]
    qbss_cu_mean_percent: Optional[float]
    qbss_cu_max_percent: Optional[float]
    top_qbss_cu_ssid: Optional[str]
    top_qbss_cu_bssid: Optional[str]
    top_qbss_cu_percent: Optional[float]
    local_cu_percent: Optional[float]
