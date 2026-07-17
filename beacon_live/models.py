"""Shared data models for beacon, survey, and rolling analysis."""

from dataclasses import dataclass
from typing import Optional


QBSS_ADMISSION_CAPACITY_MAX = 65535


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
    selected_qbss_admission_capacity: Optional[int] = None


@dataclass(frozen=True)
class BssidState:
    """Immutable rolling state for one BSSID."""

    bssid: str
    ssid: Optional[str]
    last_seen_ts: float
    latest_beacon_ts: float
    latest_beacon_record: BeaconRecord
    window_beacon_count: int
    latest_station_count: Optional[int]
    latest_qbss_cu_raw: Optional[int]
    latest_qbss_cu_percent: Optional[float]
    latest_admission_capacity: Optional[int]
    latest_rssi_dbm: Optional[int]
    peak_rssi_dbm: Optional[int]

    @property
    def qbss_present(self) -> bool:
        """Whether the authoritative latest beacon contains QBSS CU."""
        return self.latest_qbss_cu_percent is not None

    @property
    def latest_beacon(self) -> BeaconRecord:
        """Concise alias for the authoritative latest beacon record."""
        return self.latest_beacon_record

    @property
    def window_beacons(self) -> int:
        return self.window_beacon_count

    @property
    def window_station_count_latest(self) -> Optional[int]:
        return self.latest_station_count

    @property
    def window_qbss_present(self) -> bool:
        return self.qbss_present

    @property
    def window_cu_latest(self) -> Optional[float]:
        return self.latest_qbss_cu_percent

    @property
    def window_admission_capacity_latest(self) -> Optional[int]:
        return self.latest_admission_capacity

    @property
    def window_rssi_latest(self) -> Optional[int]:
        return self.latest_rssi_dbm

    @property
    def window_rssi_peak(self) -> Optional[int]:
        return self.peak_rssi_dbm


@dataclass(frozen=True)
class MetricsSnapshot:
    """Read-only analyzer state shared by every display renderer."""

    generated_at: Optional[float]
    window_seconds: int
    bssids: tuple[BssidState, ...]
    selected_bssid: Optional[str]
    current: SecondStats
    history: tuple[SecondStats, ...]
    top_station_bssid: Optional[str] = None

    @classmethod
    def empty(cls, *, window_seconds: int = 120) -> "MetricsSnapshot":
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than zero")
        return cls(
            generated_at=None,
            window_seconds=window_seconds,
            bssids=(),
            selected_bssid=None,
            current=SecondStats(
                second=0,
                unique_bssid_count=0,
                qbss_station_count_sum=0,
                selected_qbss_cu_percent=None,
                selected_qbss_ssid=None,
                selected_qbss_bssid=None,
                selected_qbss_rssi_dbm=None,
                local_cu_percent=None,
            ),
            history=(),
            top_station_bssid=None,
        )

    @property
    def selected(self) -> Optional[BssidState]:
        """Return the selected BSSID state, if a QBSS source is available."""
        if self.selected_bssid is None:
            return None
        return next(
            (
                state
                for state in self.bssids
                if state.bssid == self.selected_bssid
            ),
            None,
        )

    @property
    def bssid_states(self) -> tuple[BssidState, ...]:
        """Explicit alias used by consumers that need all per-BSSID states."""
        return self.bssids

    def state_for(self, bssid: str) -> Optional[BssidState]:
        """Look up one immutable BSSID state without exposing mutable storage."""
        return next((state for state in self.bssids if state.bssid == bssid), None)
