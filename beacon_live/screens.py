"""Pure display-screen definitions over the shared metrics snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from beacon_live.models import BssidState
from beacon_live.models import MetricsSnapshot
from beacon_live.models import QBSS_ADMISSION_CAPACITY_MAX
from beacon_live.models import RetryBssidState

CU_SCREEN_ID = "cu"
ADMISSION_CAPACITY_SCREEN_ID = "admission_capacity"
BEACONS_SCREEN_ID = "beacons"
COMPOSITION_SCREEN_ID = "composition"
TOTAL_STATION_COUNT_SCREEN_ID = "total_station_count"
RETRY_SCREEN_ID = "retry"
STATION_GRAPH_MAXIMUM = 64


@dataclass(frozen=True)
class DisplayIdentity:
    ssid: Optional[str]
    bssid: Optional[str]
    rssi_dbm: Optional[int]
    unavailable_text: str


@dataclass(frozen=True)
class GraphPoint:
    second: int
    value: Optional[float]
    display_value: Optional[float]


@dataclass(frozen=True)
class ScreenView:
    screen_id: str
    title: str
    metadata_tokens: tuple[str, ...]
    summary: str
    graph_label: str
    graph_points: tuple[GraphPoint, ...]
    graph_maximum: float
    identity: DisplayIdentity
    metadata_metric_token_count: int = 1
    summary_metric_token_count: int = 2
    text_only: bool = False
    detail_lines: tuple[str, ...] = ()
    secondary_graph_label: str = ""
    secondary_graph_points: tuple[GraphPoint, ...] = ()
    summary_secondary_metric_token_start: Optional[int] = None
    summary_secondary_metric_token_count: int = 0


class ScreenDefinition(Protocol):
    screen_id: str

    def render(self, snapshot: MetricsSnapshot) -> ScreenView:
        ...


@dataclass(frozen=True)
class CuScreen:
    screen_id: str = CU_SCREEN_ID

    def render(self, snapshot: MetricsSnapshot) -> ScreenView:
        graph_points = _cu_graph_points(snapshot)
        values = tuple(
            point.display_value
            for point in graph_points
            if point.display_value is not None
        )
        current = snapshot.current.selected_qbss_cu_percent
        summary = (
            f"CU {_whole_percent(current)}% "
            f"AVG {_whole_percent(_mean(values))}% "
            f"MAX {_whole_percent(max(values) if values else None)}%"
        )
        return ScreenView(
            screen_id=self.screen_id,
            title="Channel Utilization",
            metadata_tokens=("Utilization",),
            summary=summary,
            graph_label="Selected QBSS CU",
            graph_points=graph_points,
            graph_maximum=255,
            identity=_selected_identity(snapshot),
        )


@dataclass(frozen=True)
class AdmissionCapacityScreen:
    screen_id: str = ADMISSION_CAPACITY_SCREEN_ID

    def render(self, snapshot: MetricsSnapshot) -> ScreenView:
        graph_points = _admission_capacity_graph_points(snapshot)
        values = tuple(
            point.display_value
            for point in graph_points
            if point.display_value is not None
        )
        current = _admission_capacity_percent(
            snapshot.current.selected_qbss_admission_capacity
        )
        return ScreenView(
            screen_id=self.screen_id,
            title="Admission Capacity",
            metadata_tokens=("Admission",),
            summary=(
                f"ADC {_whole_percent(current)}% "
                f"AVG {_whole_percent(_mean(values))}% "
                f"MIN {_whole_percent(min(values) if values else None)}%"
            ),
            graph_label="Selected ADC",
            graph_points=graph_points,
            graph_maximum=100,
            identity=_selected_identity(snapshot),
        )


@dataclass(frozen=True)
class CompositionScreen:
    screen_id: str = COMPOSITION_SCREEN_ID

    def render(self, snapshot: MetricsSnapshot) -> ScreenView:
        composition = snapshot.composition
        return ScreenView(
            screen_id=self.screen_id,
            title="Composition",
            metadata_tokens=("Composition",),
            summary=(
                f"BSSIDs {composition.bssid_count} "
                f"QBSS {composition.qbss_bssid_count}"
            ),
            graph_label="",
            graph_points=(),
            graph_maximum=1,
            identity=DisplayIdentity(
                ssid=composition.displayed_ssid,
                bssid=composition.displayed_bssid,
                rssi_dbm=composition.displayed_rssi_dbm,
                unavailable_text="<No Beacons>",
            ),
            metadata_metric_token_count=0,
            summary_metric_token_count=0,
            text_only=True,
            detail_lines=(
                f"Est Radios {composition.estimated_radio_count}",
                (
                    "Radio BSSIDs "
                    f"{composition.strongest_radio_bssid_count}"
                ),
                (
                    "AP Name "
                    f"{composition.strongest_radio_ap_name or '<no AP name>'}"
                ),
                (
                    "Vendor "
                    f"{composition.strongest_radio_vendor or '<unknown>'}"
                ),
            ),
        )


@dataclass(frozen=True)
class TotalStationCountScreen:
    screen_id: str = TOTAL_STATION_COUNT_SCREEN_ID

    def render(self, snapshot: MetricsSnapshot) -> ScreenView:
        graph_points = tuple(
            GraphPoint(
                second=stats.second,
                value=stats.qbss_station_count_sum,
                display_value=float(stats.qbss_station_count_sum),
            )
            for stats in snapshot.history
        )
        mac_graph_points = tuple(
            GraphPoint(
                second=stats.second,
                value=stats.unique_client_mac_count,
                display_value=float(stats.unique_client_mac_count),
            )
            for stats in snapshot.history
        )
        current_sum = snapshot.current.qbss_station_count_sum
        top_station_state = (
            snapshot.state_for(snapshot.top_station_bssid)
            if snapshot.top_station_bssid is not None
            else None
        )
        top_station_count = (
            top_station_state.latest_station_count
            if top_station_state is not None
            else None
        )
        return ScreenView(
            screen_id=self.screen_id,
            title="Total Station Count",
            metadata_tokens=("Stations",),
            summary=(
                f"SUM {_station_count(current_sum)} "
                f"MAC {_station_count(snapshot.window_unique_client_mac_count)} "
                f"TOP {_station_count(top_station_count)}"
            ),
            graph_label="Total QBSS station count per second",
            graph_points=graph_points,
            graph_maximum=STATION_GRAPH_MAXIMUM,
            identity=_identity_from_state(
                top_station_state,
                unavailable_text="<No Station Counts>",
            ),
            secondary_graph_label="Unique client MACs per second",
            secondary_graph_points=mac_graph_points,
            summary_secondary_metric_token_start=2,
            summary_secondary_metric_token_count=2,
        )


@dataclass(frozen=True)
class RetryScreen:
    screen_id: str = RETRY_SCREEN_ID

    def render(self, snapshot: MetricsSnapshot) -> ScreenView:
        graph_points = tuple(
            GraphPoint(
                second=stats.second,
                value=stats.retry_percent,
                display_value=stats.retry_percent,
            )
            for stats in snapshot.history
        )
        values = tuple(
            point.display_value
            for point in graph_points
            if point.display_value is not None
        )
        top_retry_state = (
            snapshot.retry_state_for(snapshot.top_retry_bssid)
            if snapshot.top_retry_bssid is not None
            else None
        )
        return ScreenView(
            screen_id=self.screen_id,
            title="Retry Percentage",
            metadata_tokens=("Retries",),
            summary=(
                f"RET {_retry_percent(snapshot.current.retry_percent)}% "
                f"AVG {_retry_percent(_mean(values))}% "
                f"MAX {_retry_percent(max(values) if values else None)}%"
            ),
            graph_label="One-second retry percentage",
            graph_points=graph_points,
            graph_maximum=100,
            identity=_identity_from_retry_state(
                top_retry_state,
                unavailable_text="<No Retry Data>",
            ),
        )


@dataclass(frozen=True)
class BeaconsScreen:
    screen_id: str = BEACONS_SCREEN_ID

    def render(self, snapshot: MetricsSnapshot) -> ScreenView:
        graph_points = tuple(
            GraphPoint(
                second=stats.second,
                value=stats.beacon_received_percent,
                display_value=stats.beacon_received_percent,
            )
            for stats in snapshot.history
        )
        beacons = snapshot.beacons
        return ScreenView(
            screen_id=self.screen_id,
            title="Beacons",
            metadata_tokens=("Beacons",),
            summary=(
                f"BC {_whole_percent(beacons.received_percent)}% "
                f"REC {beacons.received_count} "
                f"EXP {beacons.expected_count}"
            ),
            graph_label="Strongest-radio beacon reception",
            graph_points=graph_points,
            graph_maximum=100,
            identity=DisplayIdentity(
                ssid=beacons.displayed_ssid,
                bssid=beacons.displayed_bssid,
                rssi_dbm=beacons.displayed_rssi_dbm,
                unavailable_text="<No Beacons>",
            ),
        )


DEFAULT_SCREENS: tuple[ScreenDefinition, ...] = (
    CuScreen(),
    AdmissionCapacityScreen(),
    TotalStationCountScreen(),
    RetryScreen(),
    BeaconsScreen(),
    CompositionScreen(),
)


def _cu_graph_points(snapshot: MetricsSnapshot) -> tuple[GraphPoint, ...]:
    points: list[GraphPoint] = []
    for stats in snapshot.history:
        raw = stats.selected_qbss_cu_raw
        if raw is None and stats.selected_qbss_cu_percent is not None:
            raw = round(stats.selected_qbss_cu_percent * 255 / 100)
        points.append(
            GraphPoint(
                second=stats.second,
                value=raw,
                display_value=stats.selected_qbss_cu_percent,
            )
        )
    return tuple(points)


def _admission_capacity_graph_points(
    snapshot: MetricsSnapshot,
) -> tuple[GraphPoint, ...]:
    points: list[GraphPoint] = []
    for stats in snapshot.history:
        percent = _admission_capacity_percent(
            stats.selected_qbss_admission_capacity
        )
        points.append(
            GraphPoint(
                second=stats.second,
                value=percent,
                display_value=percent,
            )
        )
    return tuple(points)


def _admission_capacity_percent(value: Optional[int]) -> Optional[float]:
    if value is None:
        return None
    return value / QBSS_ADMISSION_CAPACITY_MAX * 100


def _selected_identity(snapshot: MetricsSnapshot) -> DisplayIdentity:
    selected = snapshot.selected
    if selected is not None:
        return _identity_from_state(
            selected,
            unavailable_text="<No QBSS Beacons>",
        )
    current = snapshot.current
    if current.selected_qbss_bssid is not None:
        return DisplayIdentity(
            ssid=current.selected_qbss_ssid,
            bssid=current.selected_qbss_bssid,
            rssi_dbm=(
                current.selected_qbss_strongest_rssi_dbm
                if current.selected_qbss_strongest_rssi_dbm is not None
                else current.selected_qbss_rssi_dbm
            ),
            unavailable_text="<No QBSS Beacons>",
        )
    return DisplayIdentity(None, None, None, "<No QBSS Beacons>")


def _identity_from_state(
    state: Optional[BssidState],
    *,
    unavailable_text: str,
) -> DisplayIdentity:
    if state is None:
        return DisplayIdentity(None, None, None, unavailable_text)
    return DisplayIdentity(
        ssid=state.ssid,
        bssid=state.bssid,
        rssi_dbm=(
            state.peak_rssi_dbm
            if state.peak_rssi_dbm is not None
            else state.latest_rssi_dbm
        ),
        unavailable_text=unavailable_text,
    )


def _identity_from_retry_state(
    state: Optional[RetryBssidState],
    *,
    unavailable_text: str,
) -> DisplayIdentity:
    if state is None:
        return DisplayIdentity(None, None, None, unavailable_text)
    return DisplayIdentity(
        ssid=state.ssid,
        bssid=state.bssid,
        rssi_dbm=state.rssi_dbm,
        unavailable_text=unavailable_text,
    )


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _station_count(value: Optional[int]) -> str:
    if value is None:
        return "--"
    return "∞" if value > 999 else str(value)


def _whole_percent(value: Optional[float]) -> str:
    return "--" if value is None else str(round(value))


def _retry_percent(value: Optional[float]) -> str:
    if value is None:
        return "--"
    if 0 < value < 1:
        return "<1"
    return str(round(value))
