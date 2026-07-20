"""Build the 128x128 graph and display state for WLAN Pi FPMS integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from beacon_live.models import MetricsSnapshot
from beacon_live.models import SecondStats
from beacon_live.screen_manager import ScreenManager
from beacon_live.screens import ADMISSION_CAPACITY_SCREEN_ID
from beacon_live.screens import BEACONS_SCREEN_ID
from beacon_live.screens import COMPOSITION_SCREEN_ID
from beacon_live.screens import CU_SCREEN_ID
from beacon_live.screens import RETRY_SCREEN_ID
from beacon_live.screens import TOTAL_STATION_COUNT_SCREEN_ID

LCD_WIDTH = 128
LCD_HEIGHT = 128
GRAPH_X = 4
GRAPH_Y = 30
GRAPH_WIDTH = 120
GRAPH_HEIGHT = 64

_BLACK = (0, 0, 0)
_DIM = (48, 64, 64)
_CU_GRAPH = (0, 220, 120)
_ADMISSION_GRAPH = (0, 160, 255)
_STATION_GRAPH = (255, 190, 0)
_MAC_GRAPH = (0, 220, 220)
_RETRY_GRAPH = (210, 90, 255)
_BEACONS_GRAPH = (255, 110, 60)
_COMPOSITION_TEXT = (255, 255, 255)
_OVERFLOW_GRAPH = (255, 0, 0)
_GRAPH_COLORS = {
    CU_SCREEN_ID: _CU_GRAPH,
    ADMISSION_CAPACITY_SCREEN_ID: _ADMISSION_GRAPH,
    COMPOSITION_SCREEN_ID: _COMPOSITION_TEXT,
    TOTAL_STATION_COUNT_SCREEN_ID: _STATION_GRAPH,
    RETRY_SCREEN_ID: _RETRY_GRAPH,
    BEACONS_SCREEN_ID: _BEACONS_GRAPH,
}


class LcdDashboard:
    """Render the rolling QBSS series to an atomically replaced PPM frame."""

    def __init__(
        self,
        frame_path: Path,
        *,
        band: Optional[str],
        channel: str,
        frequency_mhz: Optional[int] = None,
        control_path: Optional[Path] = None,
    ) -> None:
        self.frame_path = frame_path
        self.band = band
        self.channel = channel
        self.frequency_mhz = frequency_mhz
        self.control_path = control_path or frame_path.with_suffix(".control.json")
        self.screen_manager = ScreenManager(
            snapshot=MetricsSnapshot.empty(window_seconds=GRAPH_WIDTH)
        )
        self.refresh()

    @property
    def snapshot(self) -> MetricsSnapshot:
        return self.screen_manager.snapshot

    def update(self, snapshot: MetricsSnapshot) -> None:
        self.screen_manager.update(snapshot)

    @property
    def active_screen_id(self) -> str:
        return self.screen_manager.active_screen_id

    @property
    def active_screen_index(self) -> int:
        return self.screen_manager.active_index

    def set_active_screen(self, index: int) -> bool:
        return self.screen_manager.set_active_index(index)

    def navigate_up(self, *, now: Optional[float] = None) -> bool:
        return self.screen_manager.navigate_up(now=now)

    def navigate_down(self, *, now: Optional[float] = None) -> bool:
        return self.screen_manager.navigate_down(now=now)

    @property
    def latest(self) -> Optional[SecondStats]:
        return (
            self.snapshot.current
            if self.snapshot.generated_at is not None
            else None
        )

    @property
    def graph_data(self) -> tuple[tuple[int, Optional[float]], ...]:
        """Return the active screen's values on the shared graph time base."""
        return tuple(
            (point.second, point.value)
            for point in self.screen_manager.view.graph_points
        )

    @property
    def secondary_graph_data(self) -> tuple[tuple[int, Optional[float]], ...]:
        """Return an optional overlaid series on the shared time base."""
        return tuple(
            (point.second, point.value)
            for point in self.screen_manager.view.secondary_graph_points
        )

    @property
    def text_lines(self) -> tuple[str, str, str, str, str, str]:
        view = self.screen_manager.view
        metric_fields = " ".join(view.metadata_tokens)
        metadata = (
            f"{self.frequency_mhz}MHz {metric_fields}"
            if self.frequency_mhz is not None
            else metric_fields
        )

        identity = view.identity
        rssi = "--" if identity.rssi_dbm is None else str(identity.rssi_dbm)
        ssid = (
            identity.ssid or "<HIDDEN>"
            if identity.bssid is not None
            else identity.unavailable_text
        )
        bssid = identity.bssid or "--"

        return (
            metadata,
            view.summary,
            ssid,
            rssi,
            bssid,
            self.channel,
        )

    @property
    def metadata_candidates(self) -> tuple[str, ...]:
        metric_fields = " ".join(self.screen_manager.view.metadata_tokens)
        if self.frequency_mhz is None:
            return (metric_fields,)

        frequency = f"{self.frequency_mhz}MHz"
        return (
            f"{frequency} {metric_fields}",
            f"{self.frequency_mhz} {metric_fields}",
        )

    @property
    def metric_color(self) -> tuple[int, int, int]:
        """Color shared by the active screen's graph and primary value."""
        return _GRAPH_COLORS[self.active_screen_id]

    @property
    def secondary_metric_color(self) -> Optional[tuple[int, int, int]]:
        if self.active_screen_id == TOTAL_STATION_COUNT_SCREEN_ID:
            return _MAC_GRAPH
        return None

    def render(self) -> bytes:
        canvas = _Canvas(LCD_WIDTH, LCD_HEIGHT)
        view = self.screen_manager.view

        if not view.text_only:
            for y in (
                GRAPH_Y,
                GRAPH_Y + 16,
                GRAPH_Y + 32,
                GRAPH_Y + 48,
                GRAPH_Y + 63,
            ):
                canvas.horizontal_line(
                    GRAPH_X,
                    GRAPH_X + GRAPH_WIDTH - 1,
                    y,
                    _DIM,
                )
            canvas.vertical_line(
                GRAPH_X - 1,
                GRAPH_Y,
                GRAPH_Y + GRAPH_HEIGHT - 1,
                _DIM,
            )
            canvas.vertical_line(
                GRAPH_X + GRAPH_WIDTH,
                GRAPH_Y,
                GRAPH_Y + GRAPH_HEIGHT - 1,
                _DIM,
            )

        graph_data = self.graph_data[-GRAPH_WIDTH:]
        secondary_by_second = dict(self.secondary_graph_data[-GRAPH_WIDTH:])
        start_x = GRAPH_X + GRAPH_WIDTH - len(graph_data)
        baseline = GRAPH_Y + GRAPH_HEIGHT - 1
        graph_color = _GRAPH_COLORS[view.screen_id]
        for offset, (_, value) in enumerate(graph_data):
            second = graph_data[offset][0]
            bars: list[tuple[int, tuple[int, int, int]]] = []
            if value is not None:
                bar_color = (
                    _OVERFLOW_GRAPH
                    if view.screen_id == TOTAL_STATION_COUNT_SCREEN_ID
                    and value > view.graph_maximum
                    else graph_color
                )
                bars.append(
                    (
                        _value_to_graph_height(value, view.graph_maximum),
                        bar_color,
                    )
                )
            secondary_value = secondary_by_second.get(second)
            if (
                secondary_value is not None
                and secondary_value > 0
                and self.secondary_metric_color is not None
            ):
                bars.append(
                    (
                        _value_to_graph_height(
                            secondary_value,
                            view.graph_maximum,
                        ),
                        self.secondary_metric_color,
                    )
                )
            for height, bar_color in sorted(
                bars,
                key=lambda item: item[0],
                reverse=True,
            ):
                canvas.vertical_line(
                    start_x + offset,
                    baseline - height + 1,
                    baseline,
                    bar_color,
                )

        return canvas.ppm()

    def refresh(self, snapshot: Optional[MetricsSnapshot] = None) -> None:
        if snapshot is not None:
            self.update(snapshot)
        self._sync_requested_screen()
        view = self.screen_manager.view
        metadata, summary, ssid, rssi, bssid, channel = self.text_lines
        state = {
            "screen_id": view.screen_id,
            "screen_title": view.title,
            "screen_index": self.screen_manager.active_index,
            "screen_count": self.screen_manager.screen_count,
            "metadata": metadata,
            "metadata_candidates": self.metadata_candidates,
            "metadata_metric_token_count": view.metadata_metric_token_count,
            "summary": summary,
            "summary_metric_token_count": view.summary_metric_token_count,
            "metric_color": self.metric_color,
            "secondary_metric_color": self.secondary_metric_color,
            "summary_secondary_metric_token_start": (
                view.summary_secondary_metric_token_start
            ),
            "summary_secondary_metric_token_count": (
                view.summary_secondary_metric_token_count
            ),
            "text_only": view.text_only,
            "detail_lines": view.detail_lines,
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

    def _sync_requested_screen(self) -> None:
        try:
            payload = json.loads(self.control_path.read_text(encoding="utf-8"))
            requested_offset = int(payload["active_screen_offset"])
        except (KeyError, OSError, TypeError, ValueError, UnicodeError):
            return
        self.set_active_screen(requested_offset)


def _whole_percent(value: Optional[float]) -> str:
    return "--" if value is None else str(round(value))


def _format_station_count(value: int) -> str:
    return "∞" if value > 999 else str(value)


def _raw_to_graph_height(raw: int) -> int:
    """Map raw QBSS 0-255 to 64 equal four-integer display buckets."""
    bounded = min(255, max(0, raw))
    return bounded // 4 + 1


def _value_to_graph_height(value: float, maximum: float) -> int:
    if maximum == 255:
        return _raw_to_graph_height(round(value))
    if maximum == GRAPH_HEIGHT:
        return min(GRAPH_HEIGHT, max(1, round(value)))
    if maximum <= 0:
        return 1
    bounded = min(maximum, max(0, value))
    return round(bounded / maximum * (GRAPH_HEIGHT - 1)) + 1


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
