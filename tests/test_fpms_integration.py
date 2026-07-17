import json
import signal
from pathlib import Path
from typing import Optional

import pytest

from integration.wlanpi_fpms import channel_utilization


def test_fpms_menu_is_band_channel_then_display_mode() -> None:
    menu = channel_utilization.build_channel_utilization_menu({})

    assert menu["name"] == "Utilization"
    bands = menu["action"]
    assert isinstance(bands, list)
    assert [band["name"] for band in bands] == [
        "2.4 GHz",
        "5 GHz",
        "6 GHz PSC",
        "6 GHz All",
    ]

    six_all = bands[-1]["action"]
    assert six_all[0]["name"] == "Ch 1 5955 MHz"
    assert six_all[1]["name"] == "PSC 5 5975 MHz"
    assert [item["name"] for item in six_all[1]["action"]] == [
        "Display",
        "Display + Log",
    ]
    five_ghz = bands[1]["action"]
    assert five_ghz[-1]["name"] == "Ch 181 5905 MHz"


def test_fpms_launch_command_enables_both_logs_together() -> None:
    display_only = channel_utilization.build_launch_command(
        band="5", channel=36, logging=False
    )
    display_and_log = channel_utilization.build_launch_command(
        band="6", channel=5, logging=True
    )

    assert display_only == [
        "/opt/wlanpi-beacon-live/bin/wlanpi-beacon-live",
        "--iface",
        "wlan0",
        "--band",
        "5",
        "--channel",
        "36",
        "--lcd-frame",
        "/run/wlanpi-beacon-live/display.ppm",
    ]
    assert "--stats-csv" in display_and_log
    assert "--beacons-jsonl" in display_and_log
    assert display_and_log[-2:] == [
        "--log-dir",
        "/var/log/wlanpi-beacon-live",
    ]


def test_left_exit_handler_interrupts_child_and_clears_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = tmp_path / "display.ppm"
    monkeypatch.setattr(channel_utilization, "FRAME_PATH", frame)
    processes: list[_FakeProcess] = []

    def fake_popen(command: list[str]) -> _FakeProcess:
        process = _FakeProcess(command)
        processes.append(process)
        return process

    g_vars: dict[str, object] = {
        "display_state": "menu",
        "start_up": True,
    }
    app = channel_utilization.ChannelUtilizationApp(g_vars, popen=fake_popen)

    app.launch(band="5", channel=36, logging=False)
    exit_handler = g_vars["page_exit_handler"]
    assert callable(exit_handler)
    exit_handler()

    assert len(processes) == 1
    assert processes[0].signal_received == signal.SIGINT
    assert processes[0].wait_timeout == 5
    assert "channel_utilization_session" not in g_vars
    assert "page_exit_handler" not in g_vars
    assert "page_up_handler" not in g_vars
    assert "page_down_handler" not in g_vars


def test_up_down_navigation_keeps_one_capture_process_and_debounces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = tmp_path / "display.ppm"
    monkeypatch.setattr(channel_utilization, "FRAME_PATH", frame)
    processes: list[_FakeProcess] = []
    now = 10.0

    def fake_popen(command: list[str]) -> _FakeProcess:
        process = _FakeProcess(command)
        processes.append(process)
        return process

    app = channel_utilization.ChannelUtilizationApp(
        {},
        popen=fake_popen,
        clock=lambda: now,
    )
    app.launch(band="5", channel=36, logging=False)
    session = app.g_vars["channel_utilization_session"]
    down = app.g_vars["page_down_handler"]
    up = app.g_vars["page_up_handler"]
    assert callable(down)
    assert callable(up)

    down()
    control_path = frame.with_suffix(".control.json")
    assert json.loads(control_path.read_text(encoding="utf-8")) == {
        "active_screen_offset": 1
    }

    now = 10.1
    down()
    assert session.screen_offset == 1

    now = 10.3
    down()
    assert session.screen_offset == 2
    now = 10.6
    up()
    assert session.screen_offset == 1

    assert len(processes) == 1
    assert processes[0].poll() is None
    session.stop()


def test_launch_failure_is_reported_without_leaving_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(channel_utilization, "FRAME_PATH", tmp_path / "frame.ppm")
    errors: list[str] = []
    monkeypatch.setattr(
        channel_utilization,
        "_display_error",
        lambda g_vars, message: errors.append(message),
    )

    def failed_popen(command: list[str]) -> _FakeProcess:
        raise FileNotFoundError(command[0])

    g_vars: dict[str, object] = {}
    app = channel_utilization.ChannelUtilizationApp(g_vars, popen=failed_popen)

    app.launch(band="5", channel=36, logging=False)

    assert errors == ["Unable to start capture."]
    assert "channel_utilization_session" not in g_vars


@pytest.mark.parametrize(
    ("metadata", "summary"),
    [
        ("2484MHz Utilization", "CU 99% AVG 99% MAX 99%"),
        ("2484MHz Admission", "ADC 99% AVG 99% MIN 99%"),
        ("2484MHz Stations", "SUM 999 MAX 999 TOP 999"),
        ("2484MHz Retries", "RET 99% AVG 99% MAX 99%"),
    ],
)
def test_all_screen_text_allows_ten_pixel_scanner_font(
    metadata: str,
    summary: str,
) -> None:
    state = _font_state(metadata=metadata, summary=summary)
    smart_font = _FakeFont(size=10, path="scanner.ttf", character_width=6)

    selected = channel_utilization._select_scanner_font(
        _FakeDraw(),
        state,
        smart_font,
        _FakeImageFontModule,
    )

    assert selected.size == 10


def test_three_digit_percentage_uses_nine_pixel_font_fallback() -> None:
    state = _font_state(
        metadata="2484MHz Admission",
        summary="ADC 100% AVG 100% MIN 100%",
    )

    selected = channel_utilization._select_scanner_font(
        _FakeDraw(),
        state,
        _FakeFont(size=10, path="scanner.ttf", character_width=6),
        _FakeImageFontModule,
    )

    assert selected.size == 9


def test_metric_text_colors_summary_prefix_and_metadata_suffix() -> None:
    draw = _FakeDraw()
    metric_color = (0, 160, 255)

    channel_utilization._draw_metric_text(
        draw,
        2,
        17,
        "ADC 80% AVG 45% MIN 10%",
        _FakeFont(size=10, path="scanner.ttf", character_width=6),
        metric_color,
        metric_token_count=2,
        metric_tokens_at_end=False,
        gap=3,
    )
    channel_utilization._draw_metric_text(
        draw,
        1,
        3,
        "2484MHz Admission",
        _FakeFont(size=10, path="scanner.ttf", character_width=6),
        metric_color,
        metric_token_count=1,
        metric_tokens_at_end=True,
        gap=3,
    )

    assert [call[0] for call in draw.text_calls] == [
        "ADC",
        "80%",
        "AVG",
        "45%",
        "MIN",
        "10%",
        "2484MHz",
        "Admission",
    ]
    assert [call[1] for call in draw.text_calls] == [
        metric_color,
        metric_color,
        (255, 255, 255),
        (255, 255, 255),
        (255, 255, 255),
        (255, 255, 255),
        (255, 255, 255),
        metric_color,
    ]


def test_metadata_uses_ordered_frequency_fallbacks_based_on_width() -> None:
    state = {
        "metadata": "2484MHz Admission Capacity",
        "metadata_candidates": [
            "2484MHz Admission Capacity",
            "2484 Admission",
        ],
        "summary": "CU 99% AVG 99% MAX 99%",
        "ssid": "Example",
        "rssi": "-45",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "channel": "14",
    }
    font = _FakeFont(size=10, path="scanner.ttf", character_width=6.1)

    selected = channel_utilization._select_metadata(_FakeDraw(), state, font)

    assert selected == "2484 Admission"


def test_utilization_metadata_never_includes_band_information() -> None:
    state = {
        "metadata": "2484MHz Utilization",
        "metadata_candidates": [
            "2484MHz Utilization",
            "2484 Utilization",
        ],
        "summary": "CU 9% AVG 9% MAX 9%",
        "ssid": "Example",
        "rssi": "-45",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "channel": "14",
    }
    font = _FakeFont(size=10, path="scanner.ttf", character_width=6)

    selected = channel_utilization._select_metadata(_FakeDraw(), state, font)

    assert selected == "2484MHz Utilization"
    assert "G" not in selected


def test_ssid_truncation_uses_ellipsis_but_keeps_right_field() -> None:
    value = channel_utilization._truncate_text(
        _FakeDraw(),
        "VeryLongNetworkName",
        _FakeFont(size=9, path="scanner.ttf", character_width=5),
        40,
    )

    assert value.endswith("…")
    assert len(value) * 5 <= 40


def _font_state(*, metadata: str, summary: str) -> dict[str, object]:
    return {
        "metadata": metadata,
        "metadata_candidates": [metadata],
        "summary": summary,
        "ssid": "Example",
        "rssi": "-45",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "channel": "233",
    }


class _FakeProcess:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.running = True
        self.signal_received: Optional[int] = None
        self.wait_timeout: Optional[float] = None

    def poll(self) -> Optional[int]:
        return None if self.running else 0

    def send_signal(self, value: int) -> None:
        self.signal_received = value
        self.running = False

    def wait(self, timeout: float) -> int:
        self.wait_timeout = timeout
        return 0

    def terminate(self) -> None:
        self.running = False

    def kill(self) -> None:
        self.running = False


class _FakeFont:
    def __init__(self, *, size: int, path: str, character_width: float) -> None:
        self.size = size
        self.path = path
        self.character_width = character_width


class _FakeDraw:
    def __init__(self) -> None:
        self.text_calls: list[tuple[str, tuple[int, int, int]]] = []

    def textbbox(
        self,
        position: tuple[int, int],
        text: str,
        *,
        font: _FakeFont,
    ) -> tuple[int, int, int, int]:
        return (0, 0, len(text) * font.character_width, font.size)

    def text(
        self,
        position: tuple[float, float],
        text: str,
        *,
        font: _FakeFont,
        fill: tuple[int, int, int],
    ) -> None:
        self.text_calls.append((text, fill))


class _FakeImageFontModule:
    @staticmethod
    def truetype(path: str, size: int) -> _FakeFont:
        return _FakeFont(
            size=size,
            path=path,
            character_width={10: 6, 9: 5, 8: 4}[size],
        )
