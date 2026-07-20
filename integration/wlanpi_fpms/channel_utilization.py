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
import time
from pathlib import Path
from typing import Callable, Optional

EXECUTABLE = "/opt/wlanpi-beacon-live/bin/wlanpi-beacon-live"
FRAME_PATH = Path("/run/wlanpi-beacon-live/display.ppm")
LOG_DIR = Path("/var/log/wlanpi-beacon-live")
INTERFACE = "wlan0"
NAVIGATION_DEBOUNCE_SECONDS = 0.2
LOGGING_STATUS_SECONDS = 1.0

_WHITE = (255, 255, 255)
_DEFAULT_METRIC_COLOR = (0, 220, 120)
_TEXT_LINE_TOPS = (1, 17, 33, 49, 65, 81, 97, 113)
_TEXT_WIDTH = 124

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


def build_launch_command(
    *,
    band: str,
    channel: int,
    logging: bool,
    logging_only: bool = False,
) -> list[str]:
    command = [
        EXECUTABLE,
        "--iface",
        INTERFACE,
        "--band",
        band,
        "--channel",
        str(channel),
    ]
    if logging_only:
        command.append("--logging-only")
    else:
        command.extend(["--lcd-frame", str(FRAME_PATH)])
    command.extend(
        [
            "--stats-csv",
            "--beacons-jsonl",
            "--log-dir",
            str(LOG_DIR),
            "--logging-control",
            str(_logging_control_path()),
            "--logging-initial-state",
            "enabled" if logging else "disabled",
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
                    {
                        "name": "Start Logging",
                        "action": _logging_only_action(app, band, channel),
                    },
                    {
                        "name": "Stop Logging",
                        "action": _stop_logging_action(app),
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


def _logging_only_action(
    app: "ChannelUtilizationApp",
    band: str,
    channel: int,
) -> Callable[[], None]:
    def start_logging_only() -> None:
        app.start_logging_only(band=band, channel=channel)

    return start_logging_only


def _stop_logging_action(
    app: "ChannelUtilizationApp",
) -> Callable[[], None]:
    def stop_logging() -> None:
        app.stop_logging()

    return stop_logging


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.g_vars = g_vars
        self.popen = popen
        self.clock = clock

    def launch(self, *, band: str, channel: int, logging: bool) -> None:
        logging_only_session = self.g_vars.get(
            "channel_utilization_logging_session"
        )
        if isinstance(logging_only_session, _LoggingOnlySession):
            if not logging_only_session.finished:
                _display_error(
                    self.g_vars,
                    "Logging-only capture is active.\nStop logging first.",
                )
                return
            self.g_vars.pop("channel_utilization_logging_session", None)

        session = self.g_vars.get("channel_utilization_session")
        if not isinstance(session, _DisplaySession):
            _write_logging_enabled(logging)
            session = _DisplaySession(
                self.g_vars,
                build_launch_command(
                    band=band,
                    channel=channel,
                    logging=logging,
                ),
                band=band,
                channel=channel,
                logging_enabled=logging,
                popen=self.popen,
                clock=self.clock,
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
        else:
            session.set_logging(logging)

        self.g_vars["page_exit_handler"] = session.stop
        self.g_vars["page_up_handler"] = session.navigate_up
        self.g_vars["page_down_handler"] = session.navigate_down
        self.g_vars["display_state"] = "page"
        self.g_vars["start_up"] = False

    def start_logging_only(self, *, band: str, channel: int) -> None:
        display_session = self.g_vars.get("channel_utilization_session")
        if isinstance(display_session, _DisplaySession) and not display_session.finished:
            display_session.set_logging(True)
            _display_logging_status(
                self.g_vars,
                "Logging started",
                band=display_session.band,
                channel=display_session.channel,
            )
            return

        session = self.g_vars.get("channel_utilization_logging_session")
        if isinstance(session, _LoggingOnlySession) and not session.finished:
            _display_logging_status(
                self.g_vars,
                "Logging already active",
                band=session.band,
                channel=session.channel,
            )
            return

        _write_logging_enabled(True)
        session = _LoggingOnlySession(
            self.g_vars,
            build_launch_command(
                band=band,
                channel=channel,
                logging=True,
                logging_only=True,
            ),
            band=band,
            channel=channel,
            popen=self.popen,
        )
        self.g_vars["channel_utilization_logging_session"] = session
        try:
            session.start()
        except OSError:
            self.g_vars.pop("channel_utilization_logging_session", None)
            _display_error(self.g_vars, "Unable to start logging.")
            return

        _display_logging_status(
            self.g_vars,
            "Logging started",
            band=band,
            channel=channel,
        )

    def stop_logging(self) -> None:
        _write_logging_enabled(False)
        stopped_channel: Optional[tuple[str, int]] = None
        display_session = self.g_vars.get("channel_utilization_session")
        if isinstance(display_session, _DisplaySession):
            if display_session.logging_enabled and not display_session.finished:
                stopped_channel = (display_session.band, display_session.channel)
            display_session.set_logging(False)

        logging_session = self.g_vars.pop(
            "channel_utilization_logging_session",
            None,
        )
        if isinstance(logging_session, _LoggingOnlySession):
            if not logging_session.finished:
                stopped_channel = (logging_session.band, logging_session.channel)
            logging_session.stop()

        if stopped_channel is None:
            _display_status(self.g_vars, "Logging already stopped")
            return

        _display_logging_status(
            self.g_vars,
            "Logging stopped",
            band=stopped_channel[0],
            channel=stopped_channel[1],
        )


class _DisplaySession:
    def __init__(
        self,
        g_vars: dict[str, object],
        command: list[str],
        *,
        band: str,
        channel: int,
        logging_enabled: bool,
        popen: Callable[..., subprocess.Popen[bytes]],
        clock: Callable[[], float],
    ) -> None:
        self.g_vars = g_vars
        self.command = command
        self.band = band
        self.channel = channel
        self.logging_enabled = logging_enabled
        self.popen = popen
        self.clock = clock
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.exit_reported = False
        self.screen_offset = 0
        self.last_navigation_ts: Optional[float] = None

    @property
    def finished(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def start(self) -> None:
        FRAME_PATH.parent.mkdir(parents=True, exist_ok=True)
        FRAME_PATH.unlink(missing_ok=True)
        _screen_control_path(FRAME_PATH).unlink(missing_ok=True)
        self._write_screen_offset()
        self.process = self.popen(self.command)
        self.thread = threading.Thread(
            target=self._display_frames,
            name="channel-utilization-display",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.set_logging(False)
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
        self.g_vars.pop("page_up_handler", None)
        self.g_vars.pop("page_down_handler", None)

    def set_logging(self, enabled: bool) -> None:
        _write_logging_enabled(enabled)
        self.logging_enabled = enabled

    def navigate_up(self) -> None:
        self._navigate(-1)

    def navigate_down(self) -> None:
        self._navigate(1)

    def _navigate(self, offset: int) -> None:
        now = self.clock()
        if (
            self.last_navigation_ts is not None
            and now - self.last_navigation_ts < NAVIGATION_DEBOUNCE_SECONDS
        ):
            return
        self.last_navigation_ts = now
        self.screen_offset += offset
        self._write_screen_offset()

    def _write_screen_offset(self) -> None:
        _atomic_write_text(
            _screen_control_path(FRAME_PATH),
            json.dumps(
                {"active_screen_offset": self.screen_offset},
                separators=(",", ":"),
            ),
        )

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


class _LoggingOnlySession:
    """Own the background form of the same live capture executable."""

    def __init__(
        self,
        g_vars: dict[str, object],
        command: list[str],
        *,
        band: str,
        channel: int,
        popen: Callable[..., subprocess.Popen[bytes]],
    ) -> None:
        self.g_vars = g_vars
        self.command = command
        self.band = band
        self.channel = channel
        self.popen = popen
        self.process: Optional[subprocess.Popen[bytes]] = None

    @property
    def finished(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def start(self) -> None:
        self.process = self.popen(
            self.command,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop(self) -> None:
        _stop_process(self.process)


def _draw_frame(g_vars: dict[str, object], frame_path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    import fpms.modules.wlanpi_oled as oled
    from fpms.modules.constants import PAGE_SLEEP, SMART_FONT

    with Image.open(frame_path) as source:
        frame = source.convert("RGB").copy()
    state = _read_display_state(frame_path.with_suffix(".json"))
    draw = ImageDraw.Draw(frame)
    fonts = _scanner_font_candidates(SMART_FONT, ImageFont)
    metadata, metadata_font = _select_metadata_font(draw, state, fonts)
    _draw_metric_text(
        draw,
        1,
        _TEXT_LINE_TOPS[0],
        metadata,
        metadata_font,
        state["metric_color"],
        metric_token_count=state["metadata_metric_token_count"],
        metric_tokens_at_end=True,
        gap=3,
    )
    summary_font = _select_metric_line_font(
        draw,
        str(state["summary"]),
        fonts,
        max_width=_TEXT_WIDTH,
        gap=3,
    )
    _draw_metric_text(
        draw,
        2,
        _TEXT_LINE_TOPS[1],
        state["summary"],
        summary_font,
        state["metric_color"],
        metric_token_count=state["summary_metric_token_count"],
        metric_tokens_at_end=False,
        gap=3,
    )
    if state["text_only"]:
        for top_y, detail_line in zip(
            _TEXT_LINE_TOPS[2:6],
            state["detail_lines"],
        ):
            detail_text = str(detail_line)
            detail_font = _select_text_line_font(
                draw,
                detail_text,
                fonts,
                max_width=_TEXT_WIDTH,
            )
            _draw_text_top(
                draw,
                2,
                top_y,
                _truncate_text(
                    draw,
                    detail_text,
                    detail_font,
                    _TEXT_WIDTH,
                ),
                detail_font,
                _WHITE,
            )
    ssid_font = fonts[0]
    _draw_left_right(
        draw,
        2,
        _TEXT_LINE_TOPS[6],
        state["ssid"],
        state["rssi"],
        ssid_font,
        _WHITE,
        truncate_left=True,
    )
    bssid_font = _select_left_right_font(
        draw,
        str(state["bssid"]),
        str(state["channel"]),
        fonts,
        max_width=_TEXT_WIDTH,
    )
    _draw_left_right(
        draw,
        2,
        _TEXT_LINE_TOPS[7],
        state["bssid"],
        state["channel"],
        bssid_font,
        _WHITE,
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
        "metadata": "Channel",
        "metadata_candidates": ["Channel"],
        "metadata_metric_token_count": 1,
        "summary": "CU --% AVG --% MAX --%",
        "summary_metric_token_count": 2,
        "metric_color": _DEFAULT_METRIC_COLOR,
        "text_only": False,
        "detail_lines": [],
        "ssid": "--",
        "rssi": "--",
        "bssid": "--",
        "channel": "--",
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return defaults
    string_keys = tuple(
        key
        for key in defaults
        if key
        not in {
            "metadata_candidates",
            "metadata_metric_token_count",
            "summary_metric_token_count",
            "metric_color",
            "text_only",
            "detail_lines",
        }
    )
    state: dict[str, object] = {
        key: str(payload.get(key, default))
        for key, default in defaults.items()
        if key in string_keys
    }
    candidates = payload.get("metadata_candidates", defaults["metadata_candidates"])
    if not isinstance(candidates, list):
        candidates = defaults["metadata_candidates"]
    state["metadata_candidates"] = [str(value) for value in candidates]
    state["metadata_metric_token_count"] = _read_metric_token_count(
        payload.get("metadata_metric_token_count"),
        default=1,
    )
    state["summary_metric_token_count"] = _read_metric_token_count(
        payload.get("summary_metric_token_count"),
        default=2,
    )
    state["metric_color"] = _read_metric_color(payload.get("metric_color"))
    state["text_only"] = payload.get("text_only") is True
    detail_lines = payload.get("detail_lines", defaults["detail_lines"])
    if not isinstance(detail_lines, list):
        detail_lines = defaults["detail_lines"]
    state["detail_lines"] = [str(value) for value in detail_lines[:4]]
    return state


def _read_metric_token_count(value: object, *, default: int) -> int:
    return value if isinstance(value, int) and value >= 0 else default


def _read_metric_color(value: object) -> tuple[int, int, int]:
    if (
        isinstance(value, list)
        and len(value) == 3
        and all(
            isinstance(component, int) and 0 <= component <= 255
            for component in value
        )
    ):
        return tuple(value)  # type: ignore[return-value]
    return _DEFAULT_METRIC_COLOR


def _screen_control_path(frame_path: Path) -> Path:
    return frame_path.with_suffix(".control.json")


def _logging_control_path() -> Path:
    return FRAME_PATH.with_name("logging.control.json")


def _write_logging_enabled(enabled: bool) -> None:
    _atomic_write_text(
        _logging_control_path(),
        json.dumps({"logging_enabled": enabled}, separators=(",", ":")),
    )


def _stop_process(process: Optional[subprocess.Popen[bytes]]) -> None:
    if process is None or process.poll() is not None:
        return
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


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _scanner_font_candidates(smart_font, image_font_module):
    """Return Scanner fonts in the preferred per-line size order."""
    font_path = getattr(smart_font, "path", None)
    if font_path is None:
        return (smart_font,)
    return tuple(
        image_font_module.truetype(font_path, size) for size in (10, 9, 8)
    )


def _select_metadata_font(draw, state: dict[str, object], fonts):
    """Prefer a shorter metadata label at size 10 before shrinking it."""
    candidates = state["metadata_candidates"]
    for font in fonts:
        for candidate in candidates:
            if _compact_text_width(draw, candidate, font, gap=3) <= 126:
                return candidate, font
    return str(candidates[-1]), fonts[-1]


def _select_metric_line_font(
    draw,
    text: str,
    fonts,
    *,
    max_width: int,
    gap: int,
):
    for font in fonts:
        if _compact_text_width(draw, text, font, gap=gap) <= max_width:
            return font
    return fonts[-1]


def _select_text_line_font(
    draw,
    text: str,
    fonts,
    *,
    max_width: int,
):
    for font in fonts:
        if _text_width(draw, text, font) <= max_width:
            return font
    return fonts[-1]


def _select_left_right_font(
    draw,
    left: str,
    right: str,
    fonts,
    *,
    max_width: int,
):
    for font in fonts:
        width = (
            _text_width(draw, left, font)
            + 4
            + _text_width(draw, right, font)
        )
        if width <= max_width:
            return font
    return fonts[-1]


def _draw_metric_text(
    draw,
    x: int,
    y: int,
    text: object,
    font,
    metric_color: object,
    *,
    metric_token_count: object,
    metric_tokens_at_end: bool,
    gap: int,
) -> None:
    """Draw graph-associated leading or trailing fields in the graph color."""
    color = (
        metric_color
        if isinstance(metric_color, tuple) and len(metric_color) == 3
        else _DEFAULT_METRIC_COLOR
    )
    colored_tokens = (
        metric_token_count
        if isinstance(metric_token_count, int) and metric_token_count >= 0
        else 2
    )
    tokens = str(text).split()
    trailing_start = max(0, len(tokens) - colored_tokens)
    current_x = x
    for index, token in enumerate(tokens):
        is_metric = (
            index >= trailing_start
            if metric_tokens_at_end
            else index < colored_tokens
        )
        _draw_text_top(
            draw,
            current_x,
            y,
            token,
            font,
            color if is_metric else _WHITE,
        )
        current_x += _text_width(draw, token, font) + gap


def _compact_text_width(draw, text: str, font, *, gap: int) -> int:
    tokens = text.split()
    if not tokens:
        return 0
    return sum(_text_width(draw, token, font) for token in tokens) + gap * (
        len(tokens) - 1
    )


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


def _display_logging_status(
    g_vars: dict[str, object],
    message: str,
    *,
    band: str,
    channel: int,
) -> None:
    frequency = _frequency_mhz(band, channel)
    _display_status(g_vars, f"{message}: Ch {channel} {frequency} MHz")


def _display_status(g_vars: dict[str, object], message: str) -> None:
    """Briefly overlay a status message, then restore the active FPMS menu."""
    image = g_vars.get("image")
    draw = g_vars.get("draw")
    saved_image = None
    if image is not None and draw is not None:
        try:
            saved_image = image.copy()  # type: ignore[union-attr]
        except (AttributeError, OSError):
            saved_image = None

    try:
        if saved_image is not None:
            from fpms.modules.pages.alert import Alert

            Alert(g_vars).display_popup_alert(
                g_vars,
                message,
                delay=LOGGING_STATUS_SECONDS,
            )
    finally:
        try:
            if saved_image is not None:
                from PIL import ImageDraw

                import fpms.modules.wlanpi_oled as oled

                g_vars["image"] = saved_image
                g_vars["draw"] = ImageDraw.Draw(saved_image)
                oled.drawImage(saved_image)
        finally:
            g_vars["drawing_in_progress"] = False
            g_vars["display_state"] = "menu"
            g_vars["result_cache"] = False
