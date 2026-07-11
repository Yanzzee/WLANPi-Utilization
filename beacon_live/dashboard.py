"""Terminal-only rendering for rolling live beacon statistics."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from datetime import tzinfo
from typing import Iterable, Optional, TextIO, Tuple

from beacon_live.models import SecondStats

_CLEAR_SCREEN = "\x1b[2J\x1b[H"
DEFAULT_WINDOW_SECONDS = 120
DEFAULT_WARMUP_CYCLES = 2
_BAR_LEVELS = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True)
class RollingCuSummary:
    """Summary of eligible per-second maximum QBSS CU values."""

    sample_count: int
    min_percent: Optional[float]
    mean_percent: Optional[float]
    max_percent: Optional[float]


class RollingStatsWindow:
    """Keep stats whose timestamps fall within the latest time window."""

    def __init__(self, max_seconds: int = DEFAULT_WINDOW_SECONDS) -> None:
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
        max_seconds: int = DEFAULT_WINDOW_SECONDS,
        include_local_cu: bool = False,
        warmup_cycles: int = DEFAULT_WARMUP_CYCLES,
        local_timezone: Optional[tzinfo] = None,
    ) -> None:
        if warmup_cycles < 0:
            raise ValueError("warmup_cycles must not be negative")
        self.window = RollingStatsWindow(max_seconds=max_seconds)
        self.include_local_cu = include_local_cu
        self.warmup_cycles = warmup_cycles
        self.local_timezone = local_timezone
        self.cycles_seen = 0
        self._warmup_seconds: set[int] = set()

    def update(self, stats_rows: Iterable[SecondStats]) -> None:
        rows = list(stats_rows)
        self.cycles_seen += 1
        if self.cycles_seen <= self.warmup_cycles:
            self._warmup_seconds.update(stats.second for stats in rows)
        self.window.extend(rows)
        visible_seconds = {stats.second for stats in self.window.rows}
        self._warmup_seconds.intersection_update(visible_seconds)

    @property
    def graph_data(self) -> Tuple[Tuple[int, Optional[float]], ...]:
        """Return the max-only QBSS CU series, excluding warm-up cycles."""
        return tuple(
            (stats.second, stats.qbss_cu_max_percent)
            for stats in self.window.rows
            if stats.second not in self._warmup_seconds
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
        title = (
            "WLANPi Beacon Live | "
            f"rolling {self.window.max_seconds}s | rows={len(self.window.rows)}"
        )
        header = (
            f"{'LOCAL TIME':10} {'BSSID COUNT':>11} {'QBSS STA SUM':>12} "
            f"{'QBSS CU MIN/MEAN/MAX':>26}  "
            f"{'TOP QBSS SSID/BSSID':<38}"
        )
        if self.include_local_cu:
            header = f"{header}  {'LOCAL SURVEY CU':>16}"

        lines = [
            title,
            self._format_warmup_status(),
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
            for stats in self.window.rows
        )
        return "\n".join(lines)

    def _format_warmup_status(self) -> str:
        if self.warmup_cycles == 0:
            return "Warm-up: disabled"
        if self.cycles_seen <= self.warmup_cycles:
            cycle = min(self.cycles_seen, self.warmup_cycles)
            return (
                f"Warm-up: cycle {cycle}/{self.warmup_cycles}; "
                "excluded from graph and rolling summary"
            )
        return f"Warm-up: complete; first {self.warmup_cycles} cycles excluded"

    def _format_rolling_summary(self) -> str:
        summary = self.rolling_summary
        return (
            "Rolling per-second MAX QBSS CU: "
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
        return f"MAX QBSS CU graph (0–100%, one bar/second): {bars or '--'}"

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
        f"{_format_cu_range(stats):>26}  "
        f"{_format_top_ap(stats):<38}"
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


def _format_summary_percent(value: Optional[float]) -> str:
    return "unavailable" if value is None else f"{value:.2f}%"


def _bar_for_percent(value: float) -> str:
    bounded = min(100.0, max(0.0, value))
    index = min(len(_BAR_LEVELS) - 1, int(bounded / 100 * len(_BAR_LEVELS)))
    return _BAR_LEVELS[index]


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return f"{value[: width - 1]}…"
