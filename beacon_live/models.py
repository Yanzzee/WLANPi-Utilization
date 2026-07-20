"""Shared data models for beacon, survey, and rolling analysis."""

from dataclasses import dataclass
from dataclasses import field
from typing import Optional


# Available Admission Capacity uses 32-microsecond units: 31,250 is 100%.
QBSS_ADMISSION_CAPACITY_MAX = 31_250


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
    beacon_interval_tu: Optional[int] = None
    ap_name: Optional[str] = None
    vendor: Optional[str] = None


@dataclass(frozen=True)
class FrameRecord:
    """Minimal normalized 802.11 frame used by the shared live analyzer."""

    timestamp: float
    bssid: Optional[str] = None
    ssid: Optional[str] = None
    rssi_dbm: Optional[int] = None
    frame_type: Optional[int] = None
    frame_subtype: Optional[int] = None
    retry_flag: Optional[bool] = None
    frame_length: Optional[int] = None
    beacon_interval_tu: Optional[int] = None
    qbss_cu_raw: Optional[int] = None
    qbss_cu_percent: Optional[float] = None
    qbss_station_count: Optional[int] = None
    qbss_admission_capacity: Optional[int] = None
    transmitter_address: Optional[str] = None
    receiver_address: Optional[str] = None
    source_address: Optional[str] = None
    destination_address: Optional[str] = None
    ap_name: Optional[str] = None
    vendor: Optional[str] = None

    @property
    def is_management_frame(self) -> bool:
        return self.frame_type == 0

    @property
    def is_beacon(self) -> bool:
        return self.is_management_frame and self.frame_subtype == 8

    @property
    def retry_eligible(self) -> bool:
        """Whether this received MPDU can contribute to a retry ratio."""
        return self.retry_exclusion_reason is None

    @property
    def retry_exclusion_reason(self) -> Optional[str]:
        """Return why the frame is excluded from retry calculations."""
        if any(
            _is_group_address(address)
            for address in (
                self.receiver_address,
                self.destination_address,
            )
            if address is not None
        ):
            return "group_address"
        if self.retry_flag is None:
            return "missing_retry_bit"
        if self.frame_type == 0:
            # Unicast management exchanges can be retried. Probe requests,
            # beacons, Action No Ack, and reserved subtypes cannot.
            if self.frame_subtype not in {0, 1, 2, 3, 5, 9, 10, 11, 12, 13}:
                return "non_retryable_frame_type"
        elif self.frame_type != 2:
            # Control and extension frames do not use the retry semantics
            # measured by this screen.
            return "non_retryable_frame_type"
        return None

    @property
    def mac_addresses(self) -> tuple[str, ...]:
        """Return every available BSSID/TA/RA/SA/DA address once."""
        addresses: list[str] = []
        for address in (
            self.bssid,
            self.transmitter_address,
            self.receiver_address,
            self.source_address,
            self.destination_address,
        ):
            if address is not None and address not in addresses:
                addresses.append(address)
        return tuple(addresses)

    def beacon_record(self) -> Optional[BeaconRecord]:
        """Return the beacon projection used by existing Phase 1/2 metrics."""
        if not self.is_beacon or self.bssid is None:
            return None
        return BeaconRecord(
            timestamp=self.timestamp,
            ssid=self.ssid,
            bssid=self.bssid,
            qbss_cu_raw=self.qbss_cu_raw,
            qbss_cu_percent=self.qbss_cu_percent,
            qbss_station_count=self.qbss_station_count,
            qbss_admission_capacity=self.qbss_admission_capacity,
            rssi_dbm=self.rssi_dbm,
            beacon_interval_tu=self.beacon_interval_tu,
            ap_name=self.ap_name,
            vendor=self.vendor,
        )


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
    received_frame_count: int = 0
    retry_observed_frame_count: int = 0
    retry_eligible_frame_count: int = 0
    retry_frame_count: int = 0
    retry_percent: Optional[float] = None
    selected_beacon_rate_percent: Optional[float] = None
    top_retry_bssid: Optional[str] = None


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
    latest_ap_name: Optional[str] = None
    latest_vendor: Optional[str] = None
    window_frame_count: int = 0
    window_retry_observed_frame_count: int = 0
    window_retry_frame_count: int = 0
    window_retry_percent: Optional[float] = None
    window_beacon_rate_percent: Optional[float] = None

    @property
    def qbss_present(self) -> bool:
        """Whether the authoritative latest beacon contains QBSS CU."""
        return self.latest_qbss_cu_percent is not None

    @property
    def advertises_qbss(self) -> bool:
        """Whether the latest beacon contains any decoded QBSS field."""
        return any(
            value is not None
            for value in (
                self.latest_qbss_cu_raw,
                self.latest_station_count,
                self.latest_admission_capacity,
            )
        )

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
class CompositionSnapshot:
    """Analyzer-derived fields required by the text-only Composition screen."""

    bssid_count: int
    qbss_bssid_count: int
    estimated_radio_count: int
    strongest_radio_bssid_count: int
    strongest_radio_ap_name: Optional[str]
    strongest_radio_vendor: Optional[str]
    displayed_ssid: Optional[str]
    displayed_bssid: Optional[str]
    displayed_rssi_dbm: Optional[int]

    @classmethod
    def empty(cls) -> "CompositionSnapshot":
        return cls(
            bssid_count=0,
            qbss_bssid_count=0,
            estimated_radio_count=0,
            strongest_radio_bssid_count=0,
            strongest_radio_ap_name=None,
            strongest_radio_vendor=None,
            displayed_ssid=None,
            displayed_bssid=None,
            displayed_rssi_dbm=None,
        )


@dataclass(frozen=True)
class RetryBssidState:
    """One-second retry metrics for one BSSID from the shared frame stream."""

    bssid: str
    ssid: Optional[str]
    rssi_dbm: Optional[int]
    window_frame_count: int
    window_retry_observed_frame_count: int
    retry_eligible_frame_count: int
    window_retry_frame_count: int
    window_retry_percent: Optional[float]


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
    top_retry_bssid: Optional[str] = None
    retry_bssids: tuple[RetryBssidState, ...] = ()
    composition: CompositionSnapshot = field(
        default_factory=CompositionSnapshot.empty
    )

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
            top_retry_bssid=None,
            retry_bssids=(),
            composition=CompositionSnapshot.empty(),
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

    def retry_state_for(self, bssid: str) -> Optional[RetryBssidState]:
        return next(
            (state for state in self.retry_bssids if state.bssid == bssid),
            None,
        )


def _is_group_address(address: str) -> bool:
    try:
        first_octet = int(address.split(":", 1)[0], 16)
    except ValueError:
        return False
    return bool(first_octet & 1)
