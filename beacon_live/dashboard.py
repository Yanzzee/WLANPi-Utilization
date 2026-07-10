"""Terminal-only rendering for rolling live beacon statistics."""

from __future__ import annotations

import sys
from datetime import datetime
from datetime import timezone
from typing import Iterable, Optional, TextIO

from beacon_live.models import SecondStats

_CLEAR_SCREEN = "\x1b[2J\x1b[H"


class RollingStatsWindow:
    """Keep stats whose timestamps fall within the latest time window."""

    def __init__(self, max_seconds: int = 60) -> None:
        if max_seconds <= 0:
            raise ValueError("max_seconds must be greater than zero")
        self.max_seconds = max_seconds
        self._stats_by_second: dict[int, SecondStats] = {}

    def add(self, stats: SecondStats) -> None:
        self._stats_by_second[stats.second] = stats
        latest_second = max(self._stats_by_second)
        earliest_second = latest_second - self.max_seconds + 1
        self._stats_by_second = {
            second: row
            for second, row in self._stats_by_second.items()
            if second >= earliest_second
        }

    def extend(self, stats_rows: Iterable[SecondStats]) -> None:
        for stats in stats_rows:
            self.add(stats)

    @property
    def rows(self) -> tuple[SecondStats, ...]:
        return tuple(
            self._stats_by_second[second]
            for second in sorted(self._stats_by_second)
        )


class TerminalDashboard:
    """Render a compact rolling window of existing per-second stats."""

    def __init__(
        self,
        *,
        max_seconds: int = 60,
        include_local_cu: bool = False,
    ) -> None:
        self.window = RollingStatsWindow(max_seconds=max_seconds)
        self.include_local_cu = include_local_cu

    def update(self, stats_rows: Iterable[SecondStats]) -> None:
        self.window.extend(stats_rows)

    def render(self) -> str:
        title = (
            "WLANPi Beacon Live | "
            f"rolling {self.window.max_seconds}s | rows={len(self.window.rows)}"
        )
        header = (
            f"{'UTC':8} {'BSSID COUNT':>11} {'QBSS STA SUM':>12} "
            f"{'QBSS CU MIN/MEAN/MAX':>26}  "
            f"{'TOP QBSS SSID/BSSID':<38}"
        )
        if self.include_local_cu:
            header = f"{header}  {'LOCAL SURVEY CU':>16}"

        lines = [title, header]
        lines.extend(
            _format_stats_row(stats, include_local_cu=self.include_local_cu)
            for stats in self.window.rows
        )
        return "\n".join(lines)

    def refresh(
        self,
        stats_rows: Iterable[SecondStats] = (),
        *,
        stream: Optional[TextIO] = None,
    ) -> None:
        self.update(stats_rows)
        output = sys.stdout if stream is None else stream
        output.write(f"{_CLEAR_SCREEN}{self.render()}\n")
        output.flush()


def _format_stats_row(stats: SecondStats, *, include_local_cu: bool) -> str:
    line = (
        f"{_format_second_time(stats.second):8} "
        f"{stats.unique_bssid_count:>11} "
        f"{stats.qbss_station_count_sum:>12} "
        f"{_format_cu_range(stats):>26}  "
        f"{_format_top_ap(stats):<38}"
    )
    if include_local_cu:
        return f"{line}  {_format_local_cu(stats.local_cu_percent):>16}"
    return line


def _format_second_time(second: int) -> str:
    return datetime.fromtimestamp(second, timezone.utc).strftime("%H:%M:%S")


def _format_cu_range(stats: SecondStats) -> str:
    values = (
        stats.qbss_cu_min_percent,
        stats.qbss_cu_mean_percent,
        stats.qbss_cu_max_percent,
    )
    return "/".join(_format_optional_percent(value) for value in values)


def _format_top_ap(stats: SecondStats) -> str:
    if stats.top_qbss_cu_bssid is None:
        return "--"
    ssid = stats.top_qbss_cu_ssid or "<hidden>"
    label = f"{ssid}/{stats.top_qbss_cu_bssid}"
    return _truncate(label, 38)


def _format_local_cu(value: Optional[float]) -> str:
    return "unavailable" if value is None else f"{value:.2f}%"


def _format_optional_percent(value: Optional[float]) -> str:
    return "--" if value is None else f"{value:.2f}%"


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return f"{value[: width - 1]}…"
