import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from beacon_live.live import LiveCommandError
from beacon_live.live import LiveWarmupFilter
from beacon_live.live import _read_survey_samples_safely
from beacon_live.live import _format_survey_debug_line
from beacon_live.live import _format_survey_unavailable_warning
from beacon_live.live import build_monitor_setup_commands
from beacon_live.live import build_survey_command
from beacon_live.live import build_tshark_command
from beacon_live.live import channel_to_frequency_mhz
from beacon_live.live import configure_monitor_interface
from beacon_live.live import resolve_survey_target_frequency_mhz
from beacon_live.live import run_live
from beacon_live.models import SecondStats
from beacon_live.models import SurveySample
from beacon_live.survey import SurveyCuResult


def test_build_monitor_setup_commands_uses_ht20_channel() -> None:
    assert build_monitor_setup_commands("wlan0", "36") == [
        ["ip", "link", "set", "wlan0", "down"],
        ["iw", "dev", "wlan0", "set", "type", "monitor"],
        ["ip", "link", "set", "wlan0", "up"],
        ["iw", "dev", "wlan0", "set", "channel", "36", "HT20"],
    ]


def test_build_monitor_setup_commands_can_use_explicit_frequency() -> None:
    assert build_monitor_setup_commands("wlan0", "36", frequency_mhz=5975) == [
        ["ip", "link", "set", "wlan0", "down"],
        ["iw", "dev", "wlan0", "set", "type", "monitor"],
        ["ip", "link", "set", "wlan0", "up"],
        ["iw", "dev", "wlan0", "set", "freq", "5975", "HT20"],
    ]


def test_build_monitor_setup_commands_maps_6ghz_channel_to_frequency() -> None:
    assert build_monitor_setup_commands("wlan0", "5", band="6") == [
        ["ip", "link", "set", "wlan0", "down"],
        ["iw", "dev", "wlan0", "set", "type", "monitor"],
        ["ip", "link", "set", "wlan0", "up"],
        ["iw", "dev", "wlan0", "set", "freq", "5975", "HT20"],
    ]


def test_channel_to_frequency_mhz_disambiguates_5_and_6_ghz_channels() -> None:
    assert channel_to_frequency_mhz("149", "5") == 5745
    assert channel_to_frequency_mhz("149", "6") == 6695


def test_channel_to_frequency_mhz_supports_24_ghz_channels() -> None:
    assert channel_to_frequency_mhz("1", "2.4") == 2412
    assert channel_to_frequency_mhz("14", "2.4") == 2484


def test_resolve_survey_target_frequency_mhz_matches_live_tuning() -> None:
    assert resolve_survey_target_frequency_mhz("36") == 5180
    assert resolve_survey_target_frequency_mhz("6") == 2437
    assert resolve_survey_target_frequency_mhz("5", band="6") == 5975
    assert resolve_survey_target_frequency_mhz("36", frequency_mhz=5975) == 5975


def test_build_tshark_command_uses_line_buffered_beacon_fields() -> None:
    command = build_tshark_command("wlan9")

    assert command[:4] == ["tshark", "-l", "-i", "wlan9"]
    assert "wlan.fc.type_subtype == 8" in command
    assert _field_args(command) == [
        "frame.time_epoch",
        "wlan.ssid",
        "wlan.bssid",
        "wlan.qbss.cu",
        "wlan.qbss.scount",
        "wlan.qbss.adc",
        "radiotap.dbm_antsignal",
    ]


def test_build_survey_command() -> None:
    assert build_survey_command("wlan0") == ["iw", "dev", "wlan0", "survey", "dump"]


def test_unavailable_survey_diagnostics_keep_target_frequency() -> None:
    result = SurveyCuResult(
        local_cu_percent=None,
        frequency_mhz=5180,
        active_delta_ms=None,
        busy_delta_ms=None,
        reason="target frequency 5180 MHz counters unavailable",
    )

    assert "freq=5180MHz" in _format_survey_debug_line(result)
    warning = _format_survey_unavailable_warning(result)
    assert "local survey CU unavailable" in warning
    assert "target frequency 5180 MHz counters unavailable" in warning


def test_configure_monitor_interface_stops_on_failed_command() -> None:
    commands_seen: list[list[str]] = []
    failing_command = ["iw", "dev", "wlan0", "set", "type", "monitor"]

    def runner(command: list[str]) -> None:
        commands_seen.append(command)
        if command == failing_command:
            raise LiveCommandError(command, returncode=1, stderr="set type failed")

    with pytest.raises(LiveCommandError) as exc_info:
        configure_monitor_interface("wlan0", "36", command_runner=runner)

    assert exc_info.value.command == failing_command
    assert exc_info.value.stderr == "set type failed"
    assert commands_seen == [
        ["ip", "link", "set", "wlan0", "down"],
        failing_command,
    ]


def test_beacon_only_live_mode_skips_survey_and_keeps_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stats_csv = tmp_path / "stats.csv"
    beacons_jsonl = tmp_path / "beacons.jsonl"
    _prepare_one_interval_live_run(monkeypatch)

    def unexpected_survey_read(iface: str) -> list[SurveySample]:
        raise AssertionError(f"survey should be disabled for {iface}")

    monkeypatch.setattr(
        "beacon_live.live._read_survey_samples_safely",
        unexpected_survey_read,
    )

    assert run_live(
        local_cu=False,
        interval_seconds=0.1,
        stats_csv=stats_csv,
        beacons_jsonl=beacons_jsonl,
    ) == 0

    captured = capsys.readouterr()
    beacon_row = next(
        line
        for line in captured.out.splitlines()
        if "Alpha/aa:aa:aa:aa:aa:aa" in line
    )
    assert beacon_row.split()[1:4] == ["1", "3", "25.10%"]
    assert "50.20%" not in captured.out
    assert "Warm-up" not in captured.out
    assert "LOCAL SURVEY CU" not in captured.out
    assert f"Stats CSV log: {stats_csv}" in captured.err
    assert f"Beacon JSONL log: {beacons_jsonl}" in captured.err

    with stats_csv.open("r", encoding="utf-8", newline="") as stats_file:
        stats_rows = list(csv.DictReader(stats_file))
    assert len(stats_rows) == 1
    assert stats_rows[0]["second"] == "1001"
    assert stats_rows[0]["unique_bssid_count"] == "1"
    assert stats_rows[0]["selected_qbss_cu_percent"] == "25.10"
    assert stats_rows[0]["local_cu_percent"] == ""

    beacons = [
        json.loads(line)
        for line in beacons_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert len(beacons) == 2
    assert {beacon["bssid"] for beacon in beacons} == {"aa:aa:aa:aa:aa:aa"}
    assert [beacon["rssi_dbm"] for beacon in beacons] == [-45, -44]


def test_survey_enabled_live_mode_uses_available_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_one_interval_live_run(monkeypatch)
    survey_reads = iter(
        [
            [SurveySample(1.0, 1000, 100, 0, 0, None, 5180, True)],
            [SurveySample(2.0, 2000, 300, 0, 0, None, 5180, True)],
            [SurveySample(3.0, 3000, 500, 0, 0, None, 5180, True)],
        ]
    )
    monkeypatch.setattr(
        "beacon_live.live._read_survey_samples_safely",
        lambda iface: next(survey_reads),
    )

    assert run_live(local_cu=True, interval_seconds=0.1) == 0

    captured = capsys.readouterr()
    assert "Alpha/aa:aa:aa:aa:aa:aa" in captured.out
    assert "LOCAL SURVEY CU" in captured.out
    assert "20.00%" in captured.out
    assert captured.err == ""


def test_survey_enabled_live_mode_survives_unsupported_driver(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_one_interval_live_run(monkeypatch)
    monkeypatch.setattr(
        "beacon_live.live._read_survey_samples_safely",
        lambda iface: None,
    )

    assert run_live(local_cu=True, interval_seconds=0.1) == 0

    captured = capsys.readouterr()
    assert "Alpha/aa:aa:aa:aa:aa:aa" in captured.out
    assert "LOCAL SURVEY CU" in captured.out
    assert "unavailable" in captured.out
    assert "survey counters unavailable" in captured.err


def test_survey_parse_failure_is_treated_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_one_interval_live_run(monkeypatch)

    def unparseable_survey(text: str) -> list[SurveySample]:
        raise ValueError(f"unparseable survey output: {text}")

    monkeypatch.setattr("beacon_live.live.parse_survey_dump", unparseable_survey)
    monkeypatch.setattr(
        "beacon_live.live.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="unsupported driver output",
            stderr="",
        ),
    )

    assert _read_survey_samples_safely("wlan0") is None
    assert run_live(local_cu=True, interval_seconds=0.1) == 0

    captured = capsys.readouterr()
    assert "Alpha/aa:aa:aa:aa:aa:aa" in captured.out
    assert "LOCAL SURVEY CU" in captured.out
    assert "unavailable" in captured.out
    assert "survey counters unavailable" in captured.err


def test_live_ctrl_c_exits_cleanly_and_terminates_tshark(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = _FakeTsharkProcess()
    terminated_processes: list[_FakeTsharkProcess] = []

    class InterruptingSelector:
        def register(self, fileobj: object, events: int) -> None:
            pass

        def select(self, timeout: float) -> list[object]:
            raise KeyboardInterrupt

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "beacon_live.live.configure_monitor_interface",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "beacon_live.live.start_tshark_process",
        lambda iface: process,
    )
    monkeypatch.setattr(
        "beacon_live.live.terminate_tshark_process",
        terminated_processes.append,
    )
    monkeypatch.setattr(
        "beacon_live.live.selectors.DefaultSelector",
        InterruptingSelector,
    )

    assert run_live(local_cu=False) == 0
    assert terminated_processes == [process]
    assert "Stopping live capture" in capsys.readouterr().err


def test_live_warmup_filter_drops_exactly_one_complete_cycle() -> None:
    warmup_filter = LiveWarmupFilter()
    stats = SecondStats(
        second=1000,
        unique_bssid_count=1,
        qbss_station_count_sum=2,
        selected_qbss_cu_percent=25.0,
        selected_qbss_ssid="Alpha",
        selected_qbss_bssid="aa:aa:aa:aa:aa:aa",
        selected_qbss_rssi_dbm=-45,
        local_cu_percent=None,
    )

    assert warmup_filter.filter([stats]) == []
    assert warmup_filter.filter([stats]) == [stats]


class _FakeTsharkProcess:
    def __init__(self) -> None:
        self.stdout = io.StringIO(
            "1000.100\tAlpha\taa:aa:aa:aa:aa:aa\t128\t2\t0\t-45\n"
            "1001.100\tAlpha\taa:aa:aa:aa:aa:aa\t64\t3\t0\t-44\n"
        )
        self.stderr = io.StringIO("")

    def poll(self) -> None:
        return None


class _FakeSelector:
    def __init__(self) -> None:
        self.fileobj: object = None

    def register(self, fileobj: object, events: int) -> None:
        self.fileobj = fileobj

    def select(self, timeout: float) -> list[tuple[SimpleNamespace, None]]:
        return [(SimpleNamespace(fileobj=self.fileobj), None)]

    def close(self) -> None:
        pass


def _prepare_one_interval_live_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monotonic_value = -1.0
    wall_times = iter([1001.0, 1002.0])

    def fake_monotonic() -> float:
        nonlocal monotonic_value
        monotonic_value += 1.0
        return monotonic_value

    monkeypatch.setattr(
        "beacon_live.live.configure_monitor_interface",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "beacon_live.live.start_tshark_process",
        lambda iface: _FakeTsharkProcess(),
    )
    monkeypatch.setattr(
        "beacon_live.live.terminate_tshark_process",
        lambda process: None,
    )
    monkeypatch.setattr("beacon_live.live.selectors.DefaultSelector", _FakeSelector)
    monkeypatch.setattr("beacon_live.live.time.monotonic", fake_monotonic)
    monkeypatch.setattr("beacon_live.live.time.time", lambda: next(wall_times))


def _field_args(command: list[str]) -> list[str]:
    fields: list[str] = []
    for index, value in enumerate(command):
        if value == "-e":
            fields.append(command[index + 1])
    return fields
