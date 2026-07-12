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
