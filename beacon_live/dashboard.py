"""Terminal-only rendering for rolling live beacon statistics."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from datetime import tzinfo
from typing import Optional, TextIO, Tuple

from beacon_live.models import MetricsSnapshot
from beacon_live.models import SecondStats
from beacon_live.screen_manager import ScreenManager
from beacon_live.screens import CU_SCREEN_ID
from beacon_live.screens import DisplayIdentity

_CLEAR_SCREEN = "\x1b[2J\x1b[H"
DEFAULT_WINDOW_SECONDS = 120
_BAR_LEVELS = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True)
class RollingCuSummary:
    """Summary of displayed per-second QBSS CU values."""

    sample_count: int
    min_percent: Optional[float]
    mean_percent: Optional[float]
    max_percent: Optional[float]


class TerminalDashboard:
    """Render the CU view from the analyzer's shared immutable snapshot."""

    def __init__(
        self,
        *,
        max_seconds: int = DEFAULT_WINDOW_SECONDS,
        include_local_cu: bool = False,
        local_timezone: Optional[tzinfo] = None,
    ) -> None:
        self.screen_manager = ScreenManager(
            snapshot=MetricsSnapshot.empty(window_seconds=max_seconds)
        )
        self.include_local_cu = include_local_cu
        self.local_timezone = local_timezone

    @property
    def snapshot(self) -> MetricsSnapshot:
        return self.screen_manager.snapshot

    def update(self, snapshot: MetricsSnapshot) -> None:
        self.screen_manager.update(snapshot)

    @property
    def active_screen_id(self) -> str:
        return self.screen_manager.active_screen_id

    def navigate_up(self, *, now: Optional[float] = None) -> bool:
        return self.screen_manager.navigate_up(now=now)

    def navigate_down(self, *, now: Optional[float] = None) -> bool:
        return self.screen_manager.navigate_down(now=now)

    @property
    def graph_data(self) -> Tuple[Tuple[int, Optional[float]], ...]:
        """Return active-screen values on the shared per-second time base."""
        return tuple(
            (point.second, point.display_value)
            for point in self.screen_manager.view.graph_points
        )

    @property
    def rolling_summary(self) -> RollingCuSummary:
        values = [value for _, value in self.graph_data if value is not None]
        if not values:
            return RollingCuSummary(0, None, None, None)
        return RollingCuSummary(
            sample_count=len(values),
            min_percent=min(values),
            mean_percent=sum(values) / len(values),
            max_percent=max(values),
        )

    def render(self) -> str:
        if self.active_screen_id != CU_SCREEN_ID:
            return self._render_metric_screen()

        title = (
            "WLANPi Beacon Live | "
            f"rolling {self.snapshot.window_seconds}s | "
            f"rows={len(self.snapshot.history)}"
        )
        header = (
            f"{'LOCAL TIME':10} {'BSSID COUNT':>11} {'QBSS STA SUM':>12} "
            f"{'QBSS CU':>9}  "
            f"{'QBSS SOURCE SSID/BSSID (RSSI)':<48}"
        )
        if self.include_local_cu:
            header = f"{header}  {'LOCAL SURVEY CU':>16}"

        lines = [
            title,
            self._format_rolling_summary(),
            self._format_bar_graph(),
            header,
        ]
        lines.extend(
            _format_stats_row(
                stats,
                include_local_cu=self.include_local_cu,
                local_timezone=self.local_timezone,
            )
            for stats in self.snapshot.history
        )
        return "\n".join(lines)

    def _render_metric_screen(self) -> str:
        view = self.screen_manager.view
        title = (
            "WLANPi Beacon Live | "
            f"{view.title} | rolling {self.snapshot.window_seconds}s | "
            f"rows={len(self.snapshot.history)}"
        )
        graph = "".join(
            "·"
            if point.value is None
            else _bar_for_value(point.value, view.graph_maximum)
            for point in view.graph_points
        )
        return "\n".join(
            [
                title,
                view.summary,
                f"{view.graph_label} graph: {graph or '--'}",
                f"Source: {_format_identity(view.identity)}",
            ]
        )

    def _format_rolling_summary(self) -> str:
        summary = self.rolling_summary
        return (
            "Rolling selected QBSS CU: "
            f"min={_format_summary_percent(summary.min_percent)} "
            f"mean={_format_summary_percent(summary.mean_percent)} "
            f"max={_format_summary_percent(summary.max_percent)} "
            f"samples={summary.sample_count}"
        )

    def _format_bar_graph(self) -> str:
        bars = "".join(
            "·" if value is None else _bar_for_percent(value)
            for _, value in self.graph_data
        )
        return f"Selected QBSS CU graph (0–100%, one bar/second): {bars or '--'}"

    def refresh(
        self,
        snapshot: Optional[MetricsSnapshot] = None,
        *,
        stream: Optional[TextIO] = None,
    ) -> None:
        if snapshot is not None:
            self.update(snapshot)
        output = sys.stdout if stream is None else stream
        output.write(f"{_CLEAR_SCREEN}{self.render()}\n")
        output.flush()


def _format_stats_row(
    stats: SecondStats,
    *,
    include_local_cu: bool,
    local_timezone: Optional[tzinfo],
) -> str:
    line = (
        f"{_format_second_time(stats.second, local_timezone):10} "
        f"{stats.unique_bssid_count:>11} "
        f"{stats.qbss_station_count_sum:>12} "
        f"{_format_optional_percent(stats.selected_qbss_cu_percent):>9}  "
        f"{_format_qbss_source(stats):<48}"
    )
    if include_local_cu:
        return f"{line}  {_format_local_cu(stats.local_cu_percent):>16}"
    return line


def _format_second_time(second: int, local_timezone: Optional[tzinfo]) -> str:
    if local_timezone is None:
        timestamp = datetime.fromtimestamp(second).astimezone()
    else:
        timestamp = datetime.fromtimestamp(second, local_timezone)
    return timestamp.strftime("%H:%M:%S")


def _format_qbss_source(stats: SecondStats) -> str:
    if stats.selected_qbss_bssid is None:
        return "--"
    ssid = stats.selected_qbss_ssid or "<hidden>"
    rssi = (
        "RSSI unavailable"
        if stats.selected_qbss_rssi_dbm is None
        else f"{stats.selected_qbss_rssi_dbm} dBm"
    )
    label = f"{ssid}/{stats.selected_qbss_bssid} ({rssi})"
    return _truncate(label, 48)


def _format_local_cu(value: Optional[float]) -> str:
    return "unavailable" if value is None else f"{value:.2f}%"


def _format_optional_percent(value: Optional[float]) -> str:
    return "--" if value is None else f"{value:.2f}%"


def _format_summary_percent(value: Optional[float]) -> str:
    return "unavailable" if value is None else f"{value:.2f}%"


def _bar_for_percent(value: float) -> str:
    bounded = min(100.0, max(0.0, value))
    index = min(len(_BAR_LEVELS) - 1, int(bounded / 100 * len(_BAR_LEVELS)))
    return _BAR_LEVELS[index]


def _bar_for_value(value: float, maximum: float) -> str:
    if maximum <= 0:
        return _BAR_LEVELS[0]
    return _bar_for_percent(value / maximum * 100)


def _format_identity(identity: DisplayIdentity) -> str:
    if identity.bssid is None:
        return identity.unavailable_text
    ssid = identity.ssid or "<hidden>"
    rssi = (
        "RSSI unavailable"
        if identity.rssi_dbm is None
        else f"{identity.rssi_dbm} dBm"
    )
    return _truncate(f"{ssid}/{identity.bssid} ({rssi})", 72)


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return f"{value[: width - 1]}…"
