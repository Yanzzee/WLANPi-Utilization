"""Thin WLAN Pi FPMS adapter for the beacon-live foreground command.

This module deliberately contains launch, display, and menu wiring only. QBSS
capture, selection, aggregation, summaries, logging, and frame rendering remain
in the ``beacon_live`` package launched as a child process.
"""

from __future__ import annotations

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
        "name": "Channel Utilization",
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
    from PIL import Image, ImageDraw

    import fpms.modules.wlanpi_oled as oled
    from fpms.modules.constants import PAGE_SLEEP

    with Image.open(frame_path) as source:
        frame = source.convert("RGB").copy()
    g_vars["drawing_in_progress"] = True
    try:
        g_vars["pageSleepCountdown"] = PAGE_SLEEP
        g_vars["image"] = frame
        g_vars["draw"] = ImageDraw.Draw(frame)
        oled.drawImage(frame)
    finally:
        g_vars["drawing_in_progress"] = False


def _display_error(g_vars: dict[str, object], message: str) -> None:
    from fpms.modules.pages.alert import Alert

    Alert(g_vars).display_alert_error(g_vars, message)
