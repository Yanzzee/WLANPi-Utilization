"""Pure display-screen definitions over the shared metrics snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from beacon_live.models import BssidState
from beacon_live.models import MetricsSnapshot

CU_SCREEN_ID = "cu"
ADMISSION_CAPACITY_SCREEN_ID = "admission_capacity"
TOTAL_STATION_COUNT_SCREEN_ID = "total_station_count"


@dataclass(frozen=True)
class DisplayIdentity:
    ssid: Optional[str]
    bssid: Optional[str]
    rssi_dbm: Optional[int]
    unavailable_text: str


@dataclass(frozen=True)
class GraphPoint:
    second: int
    value: Optional[int]
    display_value: Optional[float]


@dataclass(frozen=True)
class ScreenView:
    screen_id: str
    title: str
    metadata_tokens: tuple[str, ...]
    summary: str
    graph_label: str
    graph_points: tuple[GraphPoint, ...]
    graph_maximum: int
    identity: DisplayIdentity


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
            metadata_tokens=_selected_station_metadata(snapshot),
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
        selected = snapshot.selected
        admission_capacity = (
            selected.latest_admission_capacity if selected is not None else None
        )
        current_cu = snapshot.current.selected_qbss_cu_percent
        return ScreenView(
            screen_id=self.screen_id,
            title="Admission Capacity",
            metadata_tokens=_selected_station_metadata(snapshot),
            summary=(
                f"ADC {_optional_int(admission_capacity)} "
                f"CU {_whole_percent(current_cu)}%"
            ),
            graph_label="Selected QBSS CU",
            graph_points=_cu_graph_points(snapshot),
            graph_maximum=255,
            identity=_selected_identity(snapshot),
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
        values = tuple(
            point.value for point in graph_points if point.value is not None
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
            metadata_tokens=(
                "BSS",
                str(snapshot.current.unique_bssid_count),
                "TOP",
                _station_count(top_station_count),
            ),
            summary=(
                f"SUM {_station_count(current_sum)} "
                f"AVG {_station_count(_rounded(_mean(values)))} "
                f"MAX {_station_count(max(values) if values else None)}"
            ),
            graph_label="Total QBSS station count",
            graph_points=graph_points,
            graph_maximum=max(1, max(values) if values else 1),
            identity=_identity_from_state(
                top_station_state,
                unavailable_text="<No Station Counts>",
            ),
        )


DEFAULT_SCREENS: tuple[ScreenDefinition, ...] = (
    CuScreen(),
    AdmissionCapacityScreen(),
    TotalStationCountScreen(),
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


def _selected_station_metadata(snapshot: MetricsSnapshot) -> tuple[str, ...]:
    selected_station_count = snapshot.current.selected_qbss_station_count
    station_sum = (
        snapshot.current.qbss_station_count_sum
        if snapshot.generated_at is not None
        else None
    )
    return (
        "STA",
        _station_count(selected_station_count),
        "SUM",
        _station_count(station_sum),
    )


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


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _optional_int(value: Optional[int]) -> str:
    return "--" if value is None else str(value)


def _station_count(value: Optional[int]) -> str:
    if value is None:
        return "--"
    return "∞" if value > 999 else str(value)


def _whole_percent(value: Optional[float]) -> str:
    return "--" if value is None else str(round(value))


def _rounded(value: Optional[float]) -> Optional[int]:
    return None if value is None else round(value)
