"""Full-screen curses dashboard over the shared analyzer snapshot."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from datetime import tzinfo
from typing import Any, Callable, Optional, Sequence, TextIO

from beacon_live.models import MetricsSnapshot
from beacon_live.models import QBSS_ADMISSION_CAPACITY_MAX
from beacon_live.models import SecondStats
from beacon_live.screens import ADMISSION_CAPACITY_SCREEN_ID
from beacon_live.screens import BEACONS_SCREEN_ID
from beacon_live.screens import CU_SCREEN_ID
from beacon_live.screens import DEFAULT_SCREENS
from beacon_live.screens import DisplayIdentity
from beacon_live.screens import RETRY_SCREEN_ID
from beacon_live.screens import ScreenView
from beacon_live.screens import TOTAL_STATION_COUNT_SCREEN_ID

try:
    import curses as _curses
except ImportError:  # pragma: no cover - depends on the Python build
    _curses = None


MIN_TERMINAL_HEIGHT = 12
MIN_TERMINAL_WIDTH = 24
HORIZONTAL_SCROLL_STEP = 8
_BAR_LEVELS = "▁▂▃▄▅▆▇█"


@dataclass(frozen=True)
class DashboardLayout:
    """Calculated rows for one terminal size."""

    height: int
    width: int
    too_small: bool
    side_by_side: bool
    compact_graphs: bool
    title_row: int
    identity_start: int
    identity_rows: int
    composition_start: int
    composition_rows: int
    graph_start: int
    graph_rows: int
    table_header_row: int
    table_start: int
    table_rows: int
    footer_row: int


@dataclass
class TableViewport:
    """Scrollable table state independent of curses window objects."""

    top_row: int = 0
    horizontal_offset: int = 0
    follow_newest: bool = True

    def sync(
        self,
        *,
        row_count: int,
        page_rows: int,
        table_width: int,
        visible_width: int,
    ) -> None:
        maximum_top = max(0, row_count - max(0, page_rows))
        if self.follow_newest:
            self.top_row = maximum_top
        else:
            self.top_row = min(max(0, self.top_row), maximum_top)
        maximum_horizontal = max(0, table_width - max(1, visible_width))
        self.horizontal_offset = min(
            max(0, self.horizontal_offset),
            maximum_horizontal,
        )

    def move_vertical(
        self,
        delta: int,
        *,
        row_count: int,
        page_rows: int,
    ) -> None:
        maximum_top = max(0, row_count - max(0, page_rows))
        new_top = min(max(0, self.top_row + delta), maximum_top)
        self.top_row = new_top
        self.follow_newest = new_top >= maximum_top

    def home(self) -> None:
        self.top_row = 0
        self.follow_newest = False

    def end(self, *, row_count: int, page_rows: int) -> None:
        self.top_row = max(0, row_count - max(0, page_rows))
        self.follow_newest = True

    def move_horizontal(
        self,
        delta: int,
        *,
        table_width: int,
        visible_width: int,
    ) -> None:
        maximum = max(0, table_width - max(1, visible_width))
        self.horizontal_offset = min(
            max(0, self.horizontal_offset + delta),
            maximum,
        )


def calculate_layout(
    height: int,
    width: int,
    *,
    radio_bssid_rows: int = 1,
) -> DashboardLayout:
    """Return a responsive layout while reserving at least one table row."""
    height = max(0, height)
    width = max(0, width)
    radio_bssid_rows = max(1, radio_bssid_rows)
    side_by_side = width >= 112
    identity_rows = 0
    composition_rows = 4 + radio_bssid_rows
    if not side_by_side:
        composition_rows += 3
    compact_graphs = height < 20
    graph_rows = 3 if compact_graphs else 6

    def fixed_rows() -> int:
        # Title, composition/identities, graphs, table header, and footer.
        return 1 + composition_rows + graph_rows + 1 + 1

    if height < fixed_rows() + 1 and graph_rows == 6:
        compact_graphs = True
        graph_rows = 3
    too_small = width < MIN_TERMINAL_WIDTH or height < fixed_rows() + 1
    title_row = 0
    identity_start = title_row + 1
    composition_start = identity_start + identity_rows
    graph_start = composition_start + composition_rows
    table_header_row = graph_start + graph_rows
    table_start = table_header_row + 1
    footer_row = max(0, height - 1)
    table_rows = max(0, footer_row - table_start)
    return DashboardLayout(
        height=height,
        width=width,
        too_small=too_small,
        side_by_side=side_by_side,
        compact_graphs=compact_graphs,
        title_row=title_row,
        identity_start=identity_start,
        identity_rows=identity_rows,
        composition_start=composition_start,
        composition_rows=composition_rows,
        graph_start=graph_start,
        graph_rows=graph_rows,
        table_header_row=table_header_row,
        table_start=table_start,
        table_rows=table_rows,
        footer_row=footer_row,
    )


def compress_history(
    values: Sequence[Optional[float]],
    width: int,
) -> tuple[Optional[float], ...]:
    """Average adjacent samples into columns without discarding either edge."""
    if width <= 0 or not values:
        return ()
    if len(values) <= width:
        return tuple(values)

    compressed: list[Optional[float]] = []
    for column in range(width):
        start = column * len(values) // width
        end = (column + 1) * len(values) // width
        bucket = [value for value in values[start:end] if value is not None]
        compressed.append(sum(bucket) / len(bucket) if bucket else None)
    return tuple(compressed)


def should_use_curses(
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
) -> bool:
    """Whether an interactive curses terminal is available for live output."""
    if _curses is None or os.environ.get("TERM", "").lower() == "dumb":
        return False
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    try:
        return input_stream.isatty() and output_stream.isatty()
    except (AttributeError, OSError):
        return False


class CursesDashboard:
    """Render all live metrics and history in one nonblocking curses screen."""

    poll_interval_seconds = 0.1

    def __init__(
        self,
        *,
        max_seconds: int = 120,
        include_local_cu: bool = False,
        local_timezone: Optional[tzinfo] = None,
        band: Optional[str] = None,
        channel: Optional[str] = None,
        frequency_mhz: Optional[int] = None,
        curses_module: Optional[Any] = None,
    ) -> None:
        self.snapshot = MetricsSnapshot.empty(window_seconds=max_seconds)
        self.include_local_cu = include_local_cu
        self.local_timezone = local_timezone
        self.band = band
        self.channel = channel
        self.frequency_mhz = frequency_mhz
        self.viewport = TableViewport()
        self.exit_requested = False
        self.last_layout: Optional[DashboardLayout] = None
        self._window: Optional[Any] = None
        self._curses = _curses if curses_module is None else curses_module

    def run(self, callback: Callable[[], int]) -> int:
        """Run a capture callback inside curses.wrapper cleanup semantics."""
        if self._curses is None:
            raise RuntimeError("curses is unavailable")
        return self._curses.wrapper(
            lambda window: self._run_in_window(window, callback)
        )

    def _run_in_window(
        self,
        window: Any,
        callback: Callable[[], int],
    ) -> int:
        self._window = window
        try:
            window.nodelay(True)
            window.keypad(True)
            try:
                self._curses.curs_set(0)
            except self._curses.error:
                pass
            self.draw()
            return callback()
        finally:
            self._window = None

    def refresh(self, snapshot: Optional[MetricsSnapshot] = None) -> None:
        if snapshot is not None:
            self.snapshot = snapshot
        if self._window is not None:
            self.draw()

    def poll_input(self) -> bool:
        """Consume pending keys without waiting and redraw interaction changes."""
        if self._window is None:
            return self.exit_requested
        changed = False
        while True:
            try:
                key = self._window.getch()
            except KeyboardInterrupt:
                self.exit_requested = True
                break
            if key == -1:
                break
            changed = self.handle_key(key) or changed
            if self.exit_requested:
                break
        if changed and not self.exit_requested:
            self.draw()
        return self.exit_requested

    def handle_key(self, key: int) -> bool:
        """Apply one curses key code and return whether the display changed."""
        if key in (ord("q"), ord("Q"), 3):
            self.exit_requested = True
            return True
        if self._window is None:
            return False

        height, width = self._window.getmaxyx()
        layout = calculate_layout(
            height,
            width,
            radio_bssid_rows=len(self._strongest_radio_identities()),
        )
        row_count = len(self.snapshot.history)
        page_rows = layout.table_rows
        table_width = len(_format_table_header(self.include_local_cu))
        key_up = getattr(self._curses, "KEY_UP", 259)
        key_down = getattr(self._curses, "KEY_DOWN", 258)
        key_left = getattr(self._curses, "KEY_LEFT", 260)
        key_right = getattr(self._curses, "KEY_RIGHT", 261)
        key_page_up = getattr(self._curses, "KEY_PPAGE", 339)
        key_page_down = getattr(self._curses, "KEY_NPAGE", 338)
        key_home = getattr(self._curses, "KEY_HOME", 262)
        key_end = getattr(self._curses, "KEY_END", 360)
        key_resize = getattr(self._curses, "KEY_RESIZE", 410)

        if key == key_up:
            self.viewport.move_vertical(
                -1,
                row_count=row_count,
                page_rows=page_rows,
            )
        elif key == key_down:
            self.viewport.move_vertical(
                1,
                row_count=row_count,
                page_rows=page_rows,
            )
        elif key == key_page_up:
            self.viewport.move_vertical(
                -max(1, page_rows),
                row_count=row_count,
                page_rows=page_rows,
            )
        elif key == key_page_down:
            self.viewport.move_vertical(
                max(1, page_rows),
                row_count=row_count,
                page_rows=page_rows,
            )
        elif key == key_home:
            self.viewport.home()
        elif key == key_end:
            self.viewport.end(row_count=row_count, page_rows=page_rows)
        elif key == key_left:
            self.viewport.move_horizontal(
                -HORIZONTAL_SCROLL_STEP,
                table_width=table_width,
                visible_width=width,
            )
        elif key == key_right:
            self.viewport.move_horizontal(
                HORIZONTAL_SCROLL_STEP,
                table_width=table_width,
                visible_width=width,
            )
        elif key == key_resize:
            return True
        else:
            return False
        return True

    def draw(self) -> None:
        if self._window is None:
            return
        height, width = self._window.getmaxyx()
        radio_identities = self._strongest_radio_identities()
        layout = calculate_layout(
            height,
            width,
            radio_bssid_rows=len(radio_identities),
        )
        self.last_layout = layout
        self._window.erase()

        if layout.too_small:
            self._draw_too_small(layout)
            self._window.refresh()
            return

        views = {
            screen.screen_id: screen.render(self.snapshot)
            for screen in DEFAULT_SCREENS
        }
        title = (
            f" {_format_tuning(self.frequency_mhz, self.band, self.channel)} "
            "| WLANPi Beacon Live "
            f"| {self.snapshot.window_seconds}s history "
            f"| rows {len(self.snapshot.history)} "
            f"| {'FOLLOW' if self.viewport.follow_newest else 'SCROLLED'} "
        )
        self._add(layout.title_row, 0, title, width, self._attr("A_REVERSE"))
        self._draw_composition(
            layout,
            radio_identities,
        )
        self._draw_graphs(layout, views)
        self._draw_table(layout)
        footer = (
            " ↑/↓ rows  PgUp/PgDn page  Home/End oldest/newest  "
            "←/→ columns  q quit "
        )
        self._add(layout.footer_row, 0, footer, width, self._attr("A_REVERSE"))
        self._window.refresh()

    def _draw_too_small(self, layout: DashboardLayout) -> None:
        self._add(0, 0, "WLANPi Beacon Live", layout.width, self._attr("A_BOLD"))
        if layout.height > 1:
            message = (
                f"Terminal too small: {layout.width}x{layout.height}; "
                f"minimum {MIN_TERMINAL_WIDTH}x{MIN_TERMINAL_HEIGHT}"
            )
            self._add(1, 0, message, layout.width)
        if layout.height > 2:
            self._add(
                layout.footer_row,
                0,
                "Resize terminal or press q to quit",
                layout.width,
            )

    def _draw_composition(
        self,
        layout: DashboardLayout,
        radio_identities: tuple[DisplayIdentity, ...],
    ) -> None:
        composition = self.snapshot.composition
        left_width = layout.width
        right_start: Optional[int] = None
        if layout.side_by_side:
            left_width = max(1, (layout.width - 1) // 2)
            right_start = left_width + 1

        row = layout.composition_start
        counts = (
            f"BSSIDs {composition.bssid_count}  "
            f"QBSS {composition.qbss_bssid_count}  "
            f"Radios {composition.estimated_radio_count}  "
            f"Radio BSSIDs {composition.strongest_radio_bssid_count}"
        )
        self._add(row, 0, counts, left_width, self._attr("A_BOLD"))
        strongest_ap = composition.strongest_radio_ap_name or "<no AP name>"
        vendor = composition.strongest_radio_vendor or "<unknown>"
        self._add(
            row + 1,
            0,
            f"Strongest AP: {strongest_ap}  Vendor: {vendor}",
            left_width,
        )
        self._add(
            row + 2,
            0,
            _format_identity_header(left_width),
            left_width,
            self._attr("A_REVERSE"),
        )
        for index, identity in enumerate(radio_identities, start=1):
            self._draw_radio_identity(
                row + 2 + index,
                identity,
                index=index,
                width=left_width,
            )
        metrics_row = row + 3 + len(radio_identities)
        self._add(
            metrics_row,
            0,
            _format_selected_qbss_metrics(self.snapshot),
            left_width,
            self._attr("A_BOLD"),
        )

        selected_start = row if right_start is not None else metrics_row + 1
        selected_width = (
            layout.width - right_start
            if right_start is not None
            else layout.width
        )
        selected_column = 0 if right_start is None else right_start
        self._draw_selected_bssids(
            selected_start,
            selected_column,
            selected_width,
            include_header=right_start is not None,
        )

    def _draw_radio_identity(
        self,
        row: int,
        identity: DisplayIdentity,
        *,
        index: int,
        width: int,
    ) -> None:
        selected = identity.bssid == self.snapshot.selected_bssid
        marker = "*" if selected else " "
        prefix = f"{marker}{index:>2} "
        bssid = identity.bssid or "--"
        bssid_field = f"{bssid:<17}"[:17]
        rssi = _format_identity_rssi(identity.rssi_dbm)
        middle = f" {rssi:>7} "
        ssid = identity.ssid or ("--" if identity.bssid else identity.unavailable_text)
        bold = self._attr("A_BOLD") if selected else 0
        column = 0
        self._add(row, column, prefix, width - column)
        column += len(prefix)
        self._add(row, column, bssid_field, width - column, bold)
        column += len(bssid_field)
        self._add(row, column, middle, width - column)
        column += len(middle)
        self._add(row, column, ssid, width - column, bold)

    def _draw_selected_bssids(
        self,
        row: int,
        column: int,
        width: int,
        *,
        include_header: bool,
    ) -> None:
        self._add(row, column, "Selected BSSIDs", width, self._attr("A_BOLD"))
        data_row = row + 1
        if include_header:
            self._add(
                data_row,
                column,
                _format_selected_header(width),
                width,
                self._attr("A_REVERSE"),
            )
            data_row += 1
        station_identity, station_value = _highest_station_identity(self.snapshot)
        retry_identity, retry_value = _highest_retry_identity(self.snapshot)
        self._add(
            data_row,
            column,
            _format_selected_identity(
                "STA max",
                station_value,
                station_identity,
                width,
            ),
            width,
        )
        self._add(
            data_row + 1,
            column,
            _format_selected_identity(
                "RET max",
                retry_value,
                retry_identity,
                width,
            ),
            width,
        )

    def _strongest_radio_identities(self) -> tuple[DisplayIdentity, ...]:
        identities: list[DisplayIdentity] = []
        for bssid in self.snapshot.composition.strongest_radio_bssids:
            state = self.snapshot.state_for(bssid)
            if state is None:
                identities.append(
                    DisplayIdentity(None, bssid, None, "<Unknown BSSID>")
                )
                continue
            identities.append(
                DisplayIdentity(
                    ssid=state.ssid,
                    bssid=state.bssid,
                    rssi_dbm=(
                        state.peak_rssi_dbm
                        if state.peak_rssi_dbm is not None
                        else state.latest_rssi_dbm
                    ),
                    unavailable_text="<Unknown BSSID>",
                )
            )
        if identities:
            return tuple(identities)
        return (DisplayIdentity(None, None, None, "<No radio BSSIDs>"),)

    def _draw_graphs(
        self,
        layout: DashboardLayout,
        views: dict[str, ScreenView],
    ) -> None:
        station_view = views[TOTAL_STATION_COUNT_SCREEN_ID]
        graphs = (
            ("CU", views[CU_SCREEN_ID].graph_points, views[CU_SCREEN_ID]),
            (
                "ADC",
                views[ADMISSION_CAPACITY_SCREEN_ID].graph_points,
                views[ADMISSION_CAPACITY_SCREEN_ID],
            ),
            ("MAC", station_view.secondary_graph_points, station_view),
            ("LOSS", views[BEACONS_SCREEN_ID].graph_points, views[BEACONS_SCREEN_ID]),
            ("STA", station_view.graph_points, station_view),
            ("RET", views[RETRY_SCREEN_ID].graph_points, views[RETRY_SCREEN_ID]),
        )
        if layout.compact_graphs:
            left_width = max(1, (layout.width - 1) // 2)
            right_width = max(1, layout.width - left_width - 1)
            for row_index in range(3):
                left = _format_compact_graph(*graphs[row_index * 2], left_width)
                right = _format_compact_graph(
                    *graphs[row_index * 2 + 1],
                    right_width,
                )
                self._add(
                    layout.graph_start + row_index,
                    0,
                    f"{left} {right}",
                    layout.width,
                )
            return

        for row_index, (label, points, view) in enumerate(graphs):
            line = _format_full_graph(label, points, view, layout.width)
            self._add(layout.graph_start + row_index, 0, line, layout.width)

    def _draw_table(self, layout: DashboardLayout) -> None:
        header = _format_table_header(self.include_local_cu)
        table_width = len(header)
        self.viewport.sync(
            row_count=len(self.snapshot.history),
            page_rows=layout.table_rows,
            table_width=table_width,
            visible_width=layout.width,
        )
        horizontal = self.viewport.horizontal_offset
        visible_header = header[horizontal : horizontal + layout.width]
        self._add(
            layout.table_header_row,
            0,
            visible_header,
            layout.width,
            self._attr("A_REVERSE"),
        )
        rows = self.snapshot.history[
            self.viewport.top_row : self.viewport.top_row + layout.table_rows
        ]
        for index, stats in enumerate(rows):
            line = _format_table_row(
                stats,
                include_local_cu=self.include_local_cu,
                local_timezone=self.local_timezone,
            )
            visible_line = line[horizontal : horizontal + layout.width]
            self._add(layout.table_start + index, 0, visible_line, layout.width)

    def _add(
        self,
        row: int,
        column: int,
        text: str,
        width: int,
        attribute: int = 0,
    ) -> None:
        if self._window is None or width <= 0:
            return
        clean = _clean_text(text)
        try:
            self._window.addnstr(row, column, clean, width, attribute)
        except self._curses.error:
            # Some curses builds reject a write to the bottom-right cell.
            pass

    def _attr(self, name: str) -> int:
        return int(getattr(self._curses, name, 0))


def _format_compact_graph(
    label: str,
    points: Sequence[Any],
    view: ScreenView,
    width: int,
) -> str:
    label_text = f"{label:<4}"
    graph_width = max(1, width - len(label_text))
    graph = _sparkline(
        [point.value for point in points],
        graph_width,
        view.graph_maximum,
    )
    return f"{label_text}{graph}"[:width].ljust(width)


def _format_full_graph(
    label: str,
    points: Sequence[Any],
    view: ScreenView,
    width: int,
) -> str:
    prefix = f"{label:<5} "
    summary = _format_graph_summary(view.summary)
    remaining = max(1, width - len(prefix))
    summary_width = len(summary) if remaining >= len(summary) + 10 else 0
    graph_width = max(1, remaining - summary_width - (1 if summary_width else 0))
    graph = _sparkline(
        [point.value for point in points],
        graph_width,
        view.graph_maximum,
    )
    if summary_width:
        return f"{prefix}{graph} {summary}"
    return f"{prefix}{graph}"


def _sparkline(
    values: Sequence[Optional[float]],
    width: int,
    maximum: float,
) -> str:
    compressed = compress_history(values, width)
    if not compressed:
        return "-" * max(1, width)
    result = []
    for value in compressed:
        if value is None:
            result.append("·")
            continue
        if maximum <= 0:
            result.append(_BAR_LEVELS[0])
            continue
        bounded = min(maximum, max(0.0, value))
        index = min(
            len(_BAR_LEVELS) - 1,
            int(bounded / maximum * len(_BAR_LEVELS)),
        )
        result.append(_BAR_LEVELS[index])
    return "".join(result).ljust(width)


def _format_graph_summary(summary: str) -> str:
    tokens = summary.split()
    pairs = list(zip(tokens[::2], tokens[1::2]))[:3]
    while len(pairs) < 3:
        pairs.append(("", ""))
    return "  ".join(
        f"{label:<4}{value:>5}"
        for label, value in pairs
    )


def _format_tuning(
    frequency_mhz: Optional[int],
    band: Optional[str],
    channel: Optional[str],
) -> str:
    frequency = "-- MHz" if frequency_mhz is None else f"{frequency_mhz} MHz"
    band_text = "--" if band is None else f"{band} GHz"
    channel_text = "--" if channel is None else channel
    return f"{frequency} | Band {band_text} | Channel {channel_text}"


def _format_identity_header(width: int) -> str:
    return f"{'#':>3} {'BSSID':<17} {'RSSI':>7} {'SSID'}"[:width]


def _format_selected_header(width: int) -> str:
    return (
        f"{'Metric':<7} {'Value':>6} {'BSSID':<17} {'RSSI':>7} {'SSID'}"
    )[:width]


def _format_selected_identity(
    label: str,
    value: str,
    identity: DisplayIdentity,
    width: int,
) -> str:
    bssid = identity.bssid or "--"
    rssi = _format_identity_rssi(identity.rssi_dbm)
    ssid = identity.ssid or (
        "<hidden>" if identity.bssid is not None else identity.unavailable_text
    )
    return (
        f"{label:<7} {value:>6} {bssid:<17} {rssi:>7} {ssid}"
    )[:width]


def _format_identity_rssi(value: Optional[int]) -> str:
    return "--" if value is None else f"{value}dBm"


def _highest_station_identity(
    snapshot: MetricsSnapshot,
) -> tuple[DisplayIdentity, str]:
    state = (
        snapshot.state_for(snapshot.top_station_bssid)
        if snapshot.top_station_bssid is not None
        else None
    )
    if state is None:
        return DisplayIdentity(None, None, None, "<No Station Counts>"), "--"
    identity = DisplayIdentity(
        ssid=state.ssid,
        bssid=state.bssid,
        rssi_dbm=(
            state.peak_rssi_dbm
            if state.peak_rssi_dbm is not None
            else state.latest_rssi_dbm
        ),
        unavailable_text="<No Station Counts>",
    )
    value = (
        "--"
        if state.latest_station_count is None
        else str(state.latest_station_count)
    )
    return identity, value


def _highest_retry_identity(
    snapshot: MetricsSnapshot,
) -> tuple[DisplayIdentity, str]:
    state = (
        snapshot.retry_state_for(snapshot.top_retry_bssid)
        if snapshot.top_retry_bssid is not None
        else None
    )
    if state is None:
        return DisplayIdentity(None, None, None, "<No Retry Data>"), "--"
    return (
        DisplayIdentity(
            ssid=state.ssid,
            bssid=state.bssid,
            rssi_dbm=state.rssi_dbm,
            unavailable_text="<No Retry Data>",
        ),
        _short_percent(state.window_retry_percent),
    )


def _format_selected_qbss_metrics(snapshot: MetricsSnapshot) -> str:
    selected = snapshot.selected
    current = snapshot.current
    selected_bssid = snapshot.selected_bssid or current.selected_qbss_bssid
    cu_percent = (
        selected.latest_qbss_cu_percent
        if selected is not None
        else current.selected_qbss_cu_percent
    )
    admission = (
        selected.latest_admission_capacity
        if selected is not None
        else current.selected_qbss_admission_capacity
    )
    adc_percent = (
        None
        if admission is None
        else admission / QBSS_ADMISSION_CAPACITY_MAX * 100
    )
    station_count = (
        selected.latest_station_count
        if selected is not None
        else current.selected_qbss_station_count
    )
    retry_state = (
        snapshot.retry_state_for(selected_bssid)
        if selected_bssid is not None
        else None
    )
    retry_percent = (
        retry_state.window_retry_percent
        if retry_state is not None
        else selected.window_retry_percent
        if selected is not None
        else None
    )
    beacon_loss = next(
        (
            reception.loss_percent
            for reception in snapshot.beacons.bssids
            if reception.bssid == selected_bssid
        ),
        None,
    )
    station = "--" if station_count is None else str(station_count)
    return (
        f"QBSS CU {_short_percent(cu_percent):>4}  "
        f"ADC {_short_percent(adc_percent):>4}  "
        f"STA {station:>3}  "
        f"RET {_short_percent(retry_percent):>4}  "
        f"LOSS {_short_percent(beacon_loss):>4}"
    )


def _short_percent(value: Optional[float]) -> str:
    if value is None:
        return "--"
    if 0 < value < 1:
        return "<1%"
    return f"{round(value)}%"


def _format_table_header(include_local_cu: bool) -> str:
    cells = [
        ("TIME", 8),
        ("BSSIDS", 6),
        ("STA SUM", 7),
        ("CLIENT", 6),
        ("CU%", 6),
        ("CU RAW", 6),
        ("SEL STA", 7),
        ("ADC%", 6),
    ]
    if include_local_cu:
        cells.append(("LOCAL%", 7))
    cells.extend(
        (
            ("FRAMES", 7),
            ("RET OBS", 7),
            ("RET ELIG", 8),
            ("RETRIES", 7),
            ("RETRY%", 7),
            ("BCN RATE", 8),
            ("BCN RX", 6),
            ("BCN EXP", 7),
            ("LOSS%", 7),
            ("SSID", 20),
            ("QBSS BSSID", 17),
            ("RSSI", 5),
            ("PEAK", 5),
            ("TOP RETRY BSSID", 17),
        )
    )
    return " ".join(f"{label:<{width}}"[:width] for label, width in cells)


def _format_table_row(
    stats: SecondStats,
    *,
    include_local_cu: bool,
    local_timezone: Optional[tzinfo],
) -> str:
    adc_percent = (
        None
        if stats.selected_qbss_admission_capacity is None
        else stats.selected_qbss_admission_capacity
        / QBSS_ADMISSION_CAPACITY_MAX
        * 100
    )
    values = [
        (_format_second(stats.second, local_timezone), 8),
        (str(stats.unique_bssid_count), 6),
        (str(stats.qbss_station_count_sum), 7),
        (str(stats.unique_client_mac_count), 6),
        (_percent(stats.selected_qbss_cu_percent), 6),
        (_optional(stats.selected_qbss_cu_raw), 6),
        (_optional(stats.selected_qbss_station_count), 7),
        (_percent(adc_percent), 6),
    ]
    if include_local_cu:
        values.append((_percent(stats.local_cu_percent), 7))
    values.extend(
        (
            (str(stats.received_frame_count), 7),
            (str(stats.retry_observed_frame_count), 7),
            (str(stats.retry_eligible_frame_count), 8),
            (str(stats.retry_frame_count), 7),
            (_percent(stats.retry_percent), 7),
            (_percent(stats.selected_beacon_rate_percent), 8),
            (str(stats.beacon_received_count), 6),
            (str(stats.beacon_expected_count), 7),
            (_percent(stats.beacon_loss_percent), 7),
            (stats.selected_qbss_ssid or "--", 20),
            (stats.selected_qbss_bssid or "--", 17),
            (_optional(stats.selected_qbss_rssi_dbm), 5),
            (_optional(stats.selected_qbss_strongest_rssi_dbm), 5),
            (stats.top_retry_bssid or "--", 17),
        )
    )
    return " ".join(_cell(value, width) for value, width in values)


def _format_second(second: int, local_timezone: Optional[tzinfo]) -> str:
    if local_timezone is None:
        timestamp = datetime.fromtimestamp(second).astimezone()
    else:
        timestamp = datetime.fromtimestamp(second, local_timezone)
    return timestamp.strftime("%H:%M:%S")


def _percent(value: Optional[float]) -> str:
    return "--" if value is None else f"{value:.1f}"


def _optional(value: Optional[object]) -> str:
    return "--" if value is None else str(value)


def _cell(value: str, width: int) -> str:
    return f"{_truncate(_clean_text(value), width):<{width}}"


def _truncate(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return value[:1]
    return f"{value[: width - 1]}…"


def _clean_text(value: str) -> str:
    return "".join(
        character if character.isprintable() else "?"
        for character in value
    )
