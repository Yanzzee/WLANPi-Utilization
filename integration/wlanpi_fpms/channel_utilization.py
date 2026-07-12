"""Thin WLAN Pi FPMS adapter for the beacon-live foreground command.

This module deliberately contains launch, display, and menu wiring only. QBSS
capture, selection, aggregation, summaries, logging, and frame rendering remain
in the ``beacon_live`` package launched as a child process.
"""

from __future__ import annotations

import json
import signal
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

EXECUTABLE = "/opt/wlanpi-beacon-live/bin/wlanpi-beacon-live"
FRAME_PATH = Path("/run/wlanpi-beacon-live/display.ppm")
LOG_DIR = Path("/var/log/wlanpi-beacon-live")
INTERFACE = "wlan0"

_24_GHZ_CHANNELS = tuple(range(1, 15))
_5_GHZ_CHANNELS = (
    36,
    40,
    44,
    48,
    52,
    56,
    60,
    64,
    100,
    104,
    108,
    112,
    116,
    120,
    124,
    128,
    132,
    136,
    140,
    144,
    149,
    153,
    157,
    161,
    165,
    169,
    173,
    177,
    181,
)
_6_GHZ_CHANNELS = tuple(range(1, 234, 4))
_6_GHZ_PSC_CHANNELS = tuple(range(5, 230, 16))


def build_launch_command(*, band: str, channel: int, logging: bool) -> list[str]:
    command = [
        EXECUTABLE,
        "--iface",
        INTERFACE,
        "--band",
        band,
        "--channel",
        str(channel),
        "--lcd-frame",
        str(FRAME_PATH),
    ]
    if logging:
        command.extend(
            [
                "--stats-csv",
                "--beacons-jsonl",
                "--log-dir",
                str(LOG_DIR),
            ]
        )
    return command


def build_channel_utilization_menu(g_vars: dict[str, object]) -> dict[str, object]:
    """Return one FPMS Apps menu node with band/channel-first selection."""
    app = ChannelUtilizationApp(g_vars)
    return {
        "name": "Utilization",
        "action": [
            _band_menu(app, "2.4 GHz", "2.4", _24_GHZ_CHANNELS),
            _band_menu(app, "5 GHz", "5", _5_GHZ_CHANNELS),
            _band_menu(app, "6 GHz PSC", "6", _6_GHZ_PSC_CHANNELS),
            _band_menu(
                app,
                "6 GHz All",
                "6",
                _6_GHZ_CHANNELS,
                mark_psc=True,
            ),
        ],
    }


def _band_menu(
    app: "ChannelUtilizationApp",
    name: str,
    band: str,
    channels: tuple[int, ...],
    *,
    mark_psc: bool = False,
) -> dict[str, object]:
    actions: list[dict[str, object]] = []
    for channel in channels:
        frequency = _frequency_mhz(band, channel)
        prefix = "PSC" if mark_psc and channel in _6_GHZ_PSC_CHANNELS else "Ch"
        actions.append(
            {
                "name": f"{prefix} {channel} {frequency} MHz",
                "action": [
                    {
                        "name": "Display",
                        "action": _launch_action(app, band, channel, False),
                    },
                    {
                        "name": "Display + Log",
                        "action": _launch_action(app, band, channel, True),
                    },
                ],
            }
        )
    return {"name": name, "action": actions}


def _launch_action(
    app: "ChannelUtilizationApp",
    band: str,
    channel: int,
    logging: bool,
) -> Callable[[], None]:
    def launch() -> None:
        app.launch(band=band, channel=channel, logging=logging)

    return launch


def _frequency_mhz(band: str, channel: int) -> int:
    if band == "2.4":
        return 2484 if channel == 14 else 2407 + channel * 5
    if band == "5":
        return 5000 + channel * 5
    return 5950 + channel * 5


class ChannelUtilizationApp:
    """Own one child capture process and mirror its frames onto the FPMS LCD."""

    def __init__(
        self,
        g_vars: dict[str, object],
        *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.g_vars = g_vars
        self.popen = popen

    def launch(self, *, band: str, channel: int, logging: bool) -> None:
        session = self.g_vars.get("channel_utilization_session")
        if not isinstance(session, _DisplaySession):
            session = _DisplaySession(
                self.g_vars,
                build_launch_command(
                    band=band,
                    channel=channel,
                    logging=logging,
                ),
                popen=self.popen,
            )
            self.g_vars["channel_utilization_session"] = session
            try:
                session.start()
            except OSError:
                self.g_vars.pop("channel_utilization_session", None)
                _display_error(self.g_vars, "Unable to start capture.")
                return
        elif session.finished and not session.exit_reported:
            session.exit_reported = True
            _display_error(self.g_vars, "Capture stopped.\nCheck journal.")

        self.g_vars["page_exit_handler"] = session.stop
        self.g_vars["display_state"] = "page"
        self.g_vars["start_up"] = False


class _DisplaySession:
    def __init__(
        self,
        g_vars: dict[str, object],
        command: list[str],
        *,
        popen: Callable[..., subprocess.Popen[bytes]],
    ) -> None:
        self.g_vars = g_vars
        self.command = command
        self.popen = popen
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.exit_reported = False

    @property
    def finished(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def start(self) -> None:
        FRAME_PATH.parent.mkdir(parents=True, exist_ok=True)
        FRAME_PATH.unlink(missing_ok=True)
        self.process = self.popen(self.command)
        self.thread = threading.Thread(
            target=self._display_frames,
            name="channel-utilization-display",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        process = self.process
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.g_vars.pop("channel_utilization_session", None)
        self.g_vars.pop("page_exit_handler", None)

    def _display_frames(self) -> None:
        last_modified_ns: Optional[int] = None
        while not self.stop_event.is_set():
            try:
                modified_ns = FRAME_PATH.stat().st_mtime_ns
                if modified_ns != last_modified_ns:
                    _draw_frame(self.g_vars, FRAME_PATH)
                    last_modified_ns = modified_ns
            except (FileNotFoundError, OSError):
                pass

            if self.process is not None and self.process.poll() is not None:
                return
            self.stop_event.wait(0.1)


def _draw_frame(g_vars: dict[str, object], frame_path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    import fpms.modules.wlanpi_oled as oled
    from fpms.modules.constants import PAGE_SLEEP, SMART_FONT

    with Image.open(frame_path) as source:
        frame = source.convert("RGB").copy()
    state = _read_display_state(frame_path.with_suffix(".json"))
    draw = ImageDraw.Draw(frame)
    font = _select_scanner_font(draw, state, SMART_FONT, ImageFont)
    metadata = _select_metadata(draw, state, font)
    _draw_text_top(draw, 1, 1, metadata, font, (255, 255, 255))
    _draw_text_top(draw, 2, 17, state["summary"], font, (255, 220, 0))
    _draw_left_right(
        draw,
        2,
        98,
        state["ssid"],
        state["rssi"],
        font,
        (255, 255, 255),
        truncate_left=True,
    )
    _draw_left_right(
        draw,
        2,
        114,
        state["bssid"],
        state["channel"],
        font,
        (255, 255, 255),
        truncate_left=False,
    )
    g_vars["drawing_in_progress"] = True
    try:
        g_vars["pageSleepCountdown"] = PAGE_SLEEP
        g_vars["image"] = frame
        g_vars["draw"] = draw
        oled.drawImage(frame)
    finally:
        g_vars["drawing_in_progress"] = False


def _read_display_state(path: Path) -> dict[str, object]:
    defaults = {
        "metadata": "?G STA -- SUM --",
        "metadata_candidates": ["?G STA -- SUM --"],
        "summary": "CU --% AVG --% MAX --%",
        "ssid": "--",
        "rssi": "--",
        "bssid": "--",
        "channel": "--",
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return defaults
    state: dict[str, object] = {
        key: str(payload.get(key, default))
        for key, default in defaults.items()
        if key != "metadata_candidates"
    }
    candidates = payload.get("metadata_candidates", defaults["metadata_candidates"])
    if not isinstance(candidates, list):
        candidates = defaults["metadata_candidates"]
    state["metadata_candidates"] = [str(value) for value in candidates]
    return state


def _select_scanner_font(draw, state, smart_font, image_font_module):
    """Use a stable 9 px Scanner font, with an 8 px safety fallback."""
    font_path = getattr(smart_font, "path", None)
    candidates = (
        [image_font_module.truetype(font_path, size) for size in (9, 8)]
        if font_path is not None
        else [smart_font]
    )

    for font in candidates:
        if _font_fits(draw, state, font):
            return font
    return candidates[-1]


def _font_fits(draw, state: dict[str, object], font) -> bool:
    width = 124
    if _text_width(draw, str(state["summary"]), font) > width:
        return False
    if not any(
        _text_width(draw, candidate, font) <= 126
        for candidate in state["metadata_candidates"]
    ):
        return False
    footer_width = (
        _text_width(draw, str(state["bssid"]), font)
        + _text_width(draw, " ", font)
        + _text_width(draw, str(state["channel"]), font)
    )
    return footer_width <= width


def _select_metadata(draw, state: dict[str, object], font) -> str:
    candidates = state["metadata_candidates"]
    for candidate in candidates:
        if _text_width(draw, candidate, font) <= 126:
            return candidate
    return str(candidates[-1])


def _draw_left_right(
    draw,
    left_x: int,
    top_y: int,
    left: str,
    right: str,
    font,
    color,
    *,
    truncate_left: bool,
) -> None:
    right_x = 126 - _text_width(draw, right, font)
    available_left_width = max(0, right_x - left_x - 4)
    displayed_left = (
        _truncate_text(draw, left, font, available_left_width)
        if truncate_left
        else left
    )
    _draw_text_top(draw, left_x, top_y, displayed_left, font, color)
    _draw_text_top(draw, right_x, top_y, right, font, color)


def _truncate_text(draw, text: str, font, max_width: int) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    value = text
    while value and _text_width(draw, value + ellipsis, font) > max_width:
        value = value[:-1]
    return value + ellipsis if value else ""


def _draw_text_top(draw, x: int, y: int, text: str, font, color) -> None:
    bounds = draw.textbbox((0, 0), text, font=font)
    draw.text((x - bounds[0], y - bounds[1]), text, font=font, fill=color)


def _text_width(draw, text: str, font) -> int:
    bounds = draw.textbbox((0, 0), text, font=font)
    return bounds[2] - bounds[0]


def _display_error(g_vars: dict[str, object], message: str) -> None:
    from fpms.modules.pages.alert import Alert

    Alert(g_vars).display_alert_error(g_vars, message)
