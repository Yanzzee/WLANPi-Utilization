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
    rssi_dbm: Optional[int] = None


@dataclass(frozen=True)
class SurveySample:
    timestamp: float
    active_ms: Optional[int]
    busy_ms: Optional[int]
    receive_ms: Optional[int]
    transmit_ms: Optional[int]
    noise_dbm: Optional[int]
    frequency_mhz: Optional[int] = None
    in_use: bool = False


@dataclass(frozen=True)
class SecondStats:
    second: int
    unique_bssid_count: int
    qbss_station_count_sum: int
    selected_qbss_cu_percent: Optional[float]
    selected_qbss_ssid: Optional[str]
    selected_qbss_bssid: Optional[str]
    selected_qbss_rssi_dbm: Optional[int]
    local_cu_percent: Optional[float]
    selected_qbss_cu_raw: Optional[int] = None
    selected_qbss_station_count: Optional[int] = None
    selected_qbss_strongest_rssi_dbm: Optional[int] = None
