"""Build the 128x128 graph and display state for WLAN Pi FPMS integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

from beacon_live.dashboard import RollingStatsWindow
from beacon_live.models import SecondStats

LCD_WIDTH = 128
LCD_HEIGHT = 128
GRAPH_X = 4
GRAPH_Y = 30
GRAPH_WIDTH = 120
GRAPH_HEIGHT = 64

_BLACK = (0, 0, 0)
_DIM = (48, 64, 64)
_CU_GRAPH = (0, 220, 120)


class LcdDashboard:
    """Render the rolling QBSS series to an atomically replaced PPM frame."""

    def __init__(
        self,
        frame_path: Path,
        *,
        band: Optional[str],
        channel: str,
        frequency_mhz: Optional[int] = None,
    ) -> None:
        self.frame_path = frame_path
        self.band = band
        self.channel = channel
        self.frequency_mhz = frequency_mhz
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
    def text_lines(self) -> tuple[str, str, str, str, str, str]:
        latest = self.latest
        values = [
            stats.selected_qbss_cu_percent
            for stats in self.window.rows
            if stats.selected_qbss_cu_percent is not None
        ]
        current_cu = _whole_percent(
            latest.selected_qbss_cu_percent if latest is not None else None
        )
        station_sum = (
            _format_station_count(latest.qbss_station_count_sum)
            if latest
            else "--"
        )
        bssid_station_count = (
            _format_station_count(latest.selected_qbss_station_count)
            if latest is not None
            and latest.selected_qbss_station_count is not None
            else "--"
        )
        if values:
            summary = (
                f"CU {current_cu}% AVG {round(sum(values) / len(values))}% "
                f"MAX {round(max(values))}%"
            )
        else:
            summary = "CU --% AVG --% MAX --%"

        metadata = (
            f"{_short_band(self.band)} STA {bssid_station_count} SUM {station_sum}"
        )

        rssi = "--"
        ssid = "<No QBSS Beacons>"
        bssid = "--"
        if latest is not None and latest.selected_qbss_bssid is not None:
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
            metadata,
            summary,
            ssid,
            rssi,
            bssid,
            self.channel,
        )

    @property
    def metadata_candidates(self) -> tuple[str, ...]:
        latest = self.latest
        station_sum = (
            _format_station_count(latest.qbss_station_count_sum)
            if latest
            else "--"
        )
        bssid_station_count = (
            _format_station_count(latest.selected_qbss_station_count)
            if latest is not None
            and latest.selected_qbss_station_count is not None
            else "--"
        )
        station_fields = f"STA {bssid_station_count} SUM {station_sum}"
        band = _short_band(self.band)
        if self.frequency_mhz is None:
            return (f"{band} {station_fields}",)

        frequency = f"{self.frequency_mhz}MHz"
        candidates = [f"{band} {frequency} {station_fields}"]
        if band == "2.4G":
            candidates.append(f"2G {frequency} {station_fields}")
        candidates.append(f"{frequency} {station_fields}")
        return tuple(candidates)

    def render(self) -> bytes:
        canvas = _Canvas(LCD_WIDTH, LCD_HEIGHT)

        for y in (GRAPH_Y, GRAPH_Y + 16, GRAPH_Y + 32, GRAPH_Y + 48, GRAPH_Y + 63):
            canvas.horizontal_line(GRAPH_X, GRAPH_X + GRAPH_WIDTH - 1, y, _DIM)
        canvas.vertical_line(GRAPH_X - 1, GRAPH_Y, GRAPH_Y + GRAPH_HEIGHT - 1, _DIM)
        canvas.vertical_line(
            GRAPH_X + GRAPH_WIDTH,
            GRAPH_Y,
            GRAPH_Y + GRAPH_HEIGHT - 1,
            _DIM,
        )

        cu_data = self.graph_data[-GRAPH_WIDTH:]
        start_x = GRAPH_X + GRAPH_WIDTH - len(cu_data)
        baseline = GRAPH_Y + GRAPH_HEIGHT - 1
        for offset, (_, raw) in enumerate(cu_data):
            if raw is None:
                continue
            height = _raw_to_graph_height(raw)
            canvas.vertical_line(
                start_x + offset,
                baseline - height + 1,
                baseline,
                _CU_GRAPH,
            )

        return canvas.ppm()

    def refresh(self, stats_rows: Iterable[SecondStats] = ()) -> None:
        self.update(stats_rows)
        metadata, summary, ssid, rssi, bssid, channel = self.text_lines
        state = {
            "metadata": metadata,
            "metadata_candidates": self.metadata_candidates,
            "summary": summary,
            "ssid": ssid,
            "rssi": rssi,
            "bssid": bssid,
            "channel": channel,
        }
        _atomic_write(
            _frame_state_path(self.frame_path),
            json.dumps(state, ensure_ascii=False).encode("utf-8"),
        )
        _atomic_write(self.frame_path, self.render())


def _whole_percent(value: Optional[float]) -> str:
    return "--" if value is None else str(round(value))


def _format_station_count(value: int) -> str:
    return "∞" if value > 999 else str(value)


def _short_band(band: Optional[str]) -> str:
    return "?G" if band is None else f"{band}G"


def _raw_to_graph_height(raw: int) -> int:
    """Map raw QBSS 0-255 to 64 equal four-integer display buckets."""
    bounded = min(255, max(0, raw))
    return bounded // 4 + 1


def _frame_state_path(frame_path: Path) -> Path:
    return frame_path.with_suffix(".json")


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

    def ppm(self) -> bytes:
        return f"P6\n{self.width} {self.height}\n255\n".encode("ascii") + bytes(
            self.pixels
        )
