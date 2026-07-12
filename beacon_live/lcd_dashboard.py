"""Dependency-free 128x128 dashboard renderer for WLAN Pi FPMS integration."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import tzinfo
from pathlib import Path
from typing import Iterable, Optional

from beacon_live.dashboard import RollingStatsWindow
from beacon_live.models import SecondStats

LCD_WIDTH = 128
LCD_HEIGHT = 128
GRAPH_X = 4
GRAPH_Y = 32
GRAPH_WIDTH = 120
GRAPH_HEIGHT = 64

_BLACK = (0, 0, 0)
_WHITE = (255, 255, 255)
_DIM = (48, 64, 64)
_GRAPH = (0, 220, 120)
_CURRENT = (255, 220, 0)


class LcdDashboard:
    """Render the rolling QBSS series to an atomically replaced PPM frame."""

    def __init__(
        self,
        frame_path: Path,
        *,
        band: Optional[str],
        channel: str,
        frequency_mhz: Optional[int],
        local_timezone: Optional[tzinfo] = None,
    ) -> None:
        self.frame_path = frame_path
        self.band = band
        self.channel = channel
        self.frequency_mhz = frequency_mhz
        self.local_timezone = local_timezone
        self.window = RollingStatsWindow(max_seconds=GRAPH_WIDTH)
        self.refresh()

    def update(self, stats_rows: Iterable[SecondStats]) -> None:
        self.window.extend(stats_rows)

    @property
    def latest(self) -> Optional[SecondStats]:
        return self.window.rows[-1] if self.window.rows else None

    @property
    def graph_data(self) -> tuple[tuple[int, Optional[int]], ...]:
        """Return the raw 0-255 QBSS values shown in the 120-pixel plot."""
        values: list[tuple[int, Optional[int]]] = []
        for stats in self.window.rows:
            raw = stats.selected_qbss_cu_raw
            if raw is None and stats.selected_qbss_cu_percent is not None:
                raw = round(stats.selected_qbss_cu_percent * 255 / 100)
            values.append((stats.second, raw))
        return tuple(values)

    @property
    def text_lines(self) -> tuple[str, str, str, str, str]:
        latest = self.latest
        values = [
            stats.selected_qbss_cu_percent
            for stats in self.window.rows
            if stats.selected_qbss_cu_percent is not None
        ]
        current_cu = _whole_percent(
            latest.selected_qbss_cu_percent if latest is not None else None
        )
        station_sum = str(latest.qbss_station_count_sum) if latest else "--"
        bssid_station_count = (
            str(latest.selected_qbss_station_count)
            if latest is not None
            and latest.selected_qbss_station_count is not None
            else "--"
        )
        if values:
            summary = (
                f"MIN {round(min(values))}% AVG "
                f"{round(sum(values) / len(values))}% MAX {round(max(values))}%"
            )
        else:
            summary = "MIN --% AVG --% MAX --%"

        metadata = f"{_short_band(self.band)} CH{self.channel}"
        if self.frequency_mhz is not None:
            metadata += f" {self.frequency_mhz}"
        metadata += f" {_local_time(latest, self.local_timezone)}"

        rssi = "--"
        ssid = "--"
        bssid = "--"
        if latest is not None:
            selected_rssi = (
                latest.selected_qbss_strongest_rssi_dbm
                if latest.selected_qbss_strongest_rssi_dbm is not None
                else latest.selected_qbss_rssi_dbm
            )
            if selected_rssi is not None:
                rssi = str(selected_rssi)
            ssid = latest.selected_qbss_ssid or "<HIDDEN>"
            bssid = latest.selected_qbss_bssid or "--"

        return (
            f"CU {current_cu}% SUM {station_sum} BSS {bssid_station_count}",
            summary,
            metadata,
            f"RSSI {rssi} {ssid}",
            f"BSSID {bssid}",
        )

    def render(self) -> bytes:
        canvas = _Canvas(LCD_WIDTH, LCD_HEIGHT)
        top_current, top_summary, top_metadata, bottom_rssi, bottom_bssid = (
            self.text_lines
        )

        canvas.text(4, 1, top_current, _CURRENT)
        canvas.text(4, 9, top_summary, _WHITE)
        canvas.text(4, 17, top_metadata, _WHITE)

        for y in (GRAPH_Y, GRAPH_Y + 16, GRAPH_Y + 32, GRAPH_Y + 48, GRAPH_Y + 63):
            canvas.horizontal_line(GRAPH_X, GRAPH_X + GRAPH_WIDTH - 1, y, _DIM)
        canvas.vertical_line(GRAPH_X - 1, GRAPH_Y, GRAPH_Y + GRAPH_HEIGHT - 1, _DIM)
        canvas.vertical_line(
            GRAPH_X + GRAPH_WIDTH,
            GRAPH_Y,
            GRAPH_Y + GRAPH_HEIGHT - 1,
            _DIM,
        )

        data = self.graph_data[-GRAPH_WIDTH:]
        start_x = GRAPH_X + GRAPH_WIDTH - len(data)
        baseline = GRAPH_Y + GRAPH_HEIGHT - 1
        for offset, (_, raw) in enumerate(data):
            if raw is None:
                continue
            height = _raw_to_graph_height(raw)
            if height:
                canvas.vertical_line(
                    start_x + offset,
                    baseline - height + 1,
                    baseline,
                    _GRAPH,
                )

        canvas.text(4, 99, _truncate_pixels(bottom_rssi), _WHITE)
        canvas.text(4, 107, _truncate_pixels(bottom_bssid), _WHITE)
        canvas.text(4, 120, "LEFT EXIT", _DIM)
        return canvas.ppm()

    def refresh(self, stats_rows: Iterable[SecondStats] = ()) -> None:
        self.update(stats_rows)
        _atomic_write(self.frame_path, self.render())


def _whole_percent(value: Optional[float]) -> str:
    return "--" if value is None else str(round(value))


def _short_band(band: Optional[str]) -> str:
    return "?G" if band is None else f"{band}G"


def _local_time(stats: Optional[SecondStats], timezone: Optional[tzinfo]) -> str:
    timestamp = stats.second if stats is not None else datetime.now().timestamp()
    if timezone is None:
        value = datetime.fromtimestamp(timestamp).astimezone()
    else:
        value = datetime.fromtimestamp(timestamp, timezone)
    return value.strftime("%H:%M:%S")


def _raw_to_graph_height(raw: int) -> int:
    """Map raw QBSS 0-255 to 64 equal four-integer display buckets."""
    bounded = min(255, max(0, raw))
    return bounded // 4 + 1


def _truncate_pixels(value: str) -> str:
    return value[:30]


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


class _Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(width * height * 3)

    def pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        index = (y * self.width + x) * 3
        self.pixels[index : index + 3] = bytes(color)

    def horizontal_line(
        self,
        start_x: int,
        end_x: int,
        y: int,
        color: tuple[int, int, int],
    ) -> None:
        for x in range(start_x, end_x + 1):
            self.pixel(x, y, color)

    def vertical_line(
        self,
        x: int,
        start_y: int,
        end_y: int,
        color: tuple[int, int, int],
    ) -> None:
        for y in range(start_y, end_y + 1):
            self.pixel(x, y, color)

    def text(
        self,
        x: int,
        y: int,
        value: str,
        color: tuple[int, int, int],
    ) -> None:
        cursor = x
        for character in value.upper():
            glyph = _FONT.get(character, _FONT["?"])
            for row, bits in enumerate(glyph):
                for column in range(3):
                    if bits & (1 << (2 - column)):
                        self.pixel(cursor + column, y + row, color)
            cursor += 4

    def ppm(self) -> bytes:
        return f"P6\n{self.width} {self.height}\n255\n".encode("ascii") + bytes(
            self.pixels
        )


_FONT = {
    " ": (0, 0, 0, 0, 0),
    "-": (0, 0, 7, 0, 0),
    ".": (0, 0, 0, 0, 2),
    ":": (0, 2, 0, 2, 0),
    "%": (5, 1, 2, 4, 5),
    "<": (1, 2, 4, 2, 1),
    ">": (4, 2, 1, 2, 4),
    "?": (6, 1, 2, 0, 2),
    "0": (7, 5, 5, 5, 7),
    "1": (2, 6, 2, 2, 7),
    "2": (6, 1, 7, 4, 7),
    "3": (6, 1, 3, 1, 6),
    "4": (5, 5, 7, 1, 1),
    "5": (7, 4, 6, 1, 6),
    "6": (3, 4, 7, 5, 7),
    "7": (7, 1, 2, 2, 2),
    "8": (7, 5, 7, 5, 7),
    "9": (7, 5, 7, 1, 6),
    "A": (2, 5, 7, 5, 5),
    "B": (6, 5, 6, 5, 6),
    "C": (3, 4, 4, 4, 3),
    "D": (6, 5, 5, 5, 6),
    "E": (7, 4, 6, 4, 7),
    "F": (7, 4, 6, 4, 4),
    "G": (3, 4, 5, 5, 3),
    "H": (5, 5, 7, 5, 5),
    "I": (7, 2, 2, 2, 7),
    "J": (1, 1, 1, 5, 2),
    "K": (5, 5, 6, 5, 5),
    "L": (4, 4, 4, 4, 7),
    "M": (5, 7, 7, 5, 5),
    "N": (5, 7, 7, 7, 5),
    "O": (2, 5, 5, 5, 2),
    "P": (6, 5, 6, 4, 4),
    "Q": (2, 5, 5, 7, 3),
    "R": (6, 5, 6, 5, 5),
    "S": (3, 4, 2, 1, 6),
    "T": (7, 2, 2, 2, 2),
    "U": (5, 5, 5, 5, 7),
    "V": (5, 5, 5, 5, 2),
    "W": (5, 5, 7, 7, 5),
    "X": (5, 5, 2, 5, 5),
    "Y": (5, 5, 2, 2, 2),
    "Z": (7, 1, 2, 4, 7),
}
