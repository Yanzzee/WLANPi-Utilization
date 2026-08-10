import csv
import io
import json
from datetime import datetime
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
from beacon_live.live import _write_raw_capture_metadata
from beacon_live.live import channel_to_frequency_mhz
from beacon_live.live import configure_monitor_interface
from beacon_live.live import default_channel_definition
from beacon_live.live import frequency_to_band
from beacon_live.live import resolve_survey_target_frequency_mhz
from beacon_live.live import run_live
from beacon_live.channel import ChannelDefinition
from beacon_live.channel import ChannelWidth
from beacon_live.channel import RadioCapabilities
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES
from beacon_live.models import SecondStats
from beacon_live.models import SurveySample
from beacon_live.survey import SurveyCuResult
from beacon_live.channel import CoverageDecision


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


def test_frequency_to_band_resolves_log_metadata() -> None:
    assert frequency_to_band(2437) == "2.4"
    assert frequency_to_band(5180) == "5"
    assert frequency_to_band(5975) == "6"


def test_band_defaults_use_20mhz_on_24_and_80mhz_on_5_and_6ghz() -> None:
    definition_24 = default_channel_definition(
        primary_frequency_mhz=2437,
        band="2.4",
    )
    definition_5 = default_channel_definition(
        primary_frequency_mhz=5180,
        band="5",
    )
    definition_6 = default_channel_definition(
        primary_frequency_mhz=5975,
        band="6",
    )

    assert definition_24.width is ChannelWidth.MHZ20
    assert definition_24.center_frequency1_mhz == 2437
    assert definition_5.width is ChannelWidth.MHZ80
    assert definition_5.center_frequency1_mhz == 5210
    assert definition_6.width is ChannelWidth.MHZ80
    assert definition_6.center_frequency1_mhz == 5985


def test_build_tshark_command_uses_line_buffered_all_frame_fields() -> None:
    command = build_tshark_command("wlan9")

    assert command[:4] == ["tshark", "-l", "-i", "wlan9"]
    assert command[4:6] == ["-B", "16"]
    assert command[6:8] == ["-s", "512"]
    assert command[8:12] == [
        "--disable-protocol",
        "ALL",
        "--enable-protocol",
        "radiotap,wlan_radio,wlan,wlan_ext,wlan_aggregate",
    ]
    assert [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "-o"
    ] == ["wlan.defragment:FALSE", "wlan.enable_decryption:FALSE"]
    assert command[command.index("-N") + 1] == "m"
    assert command[command.index("-Y") + 1] == "wlan"
    assert _field_args(command) == [
        "frame.time_epoch",
        "wlan.fc.type",
        "wlan.fc.subtype",
        "wlan.fc.retry",
        "wlan.bssid",
        "wlan.ta",
        "wlan.ra",
        "wlan.sa",
        "wlan.da",
        "wlan.ssid",
        "wlan.qbss.cu",
        "wlan.qbss.scount",
        "wlan.qbss.adc",
        "radiotap.dbm_antsignal",
        "wlan.fixed.beacon",
        "wlan.cisco.ccx1.name",
        "wlan.vs.aruba.ap_name",
        "wlan.vs.extreme.ap_name",
        "wlan.vs.aerohive.hostname",
        "wlan.bssid_resolved",
    ]


def test_raw_capture_command_uses_same_process_full_packet_stream(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "capture.pcapng"
    command = build_tshark_command("wlan9", raw_capture_path=raw)

    assert "-P" in command
    assert command[command.index("-s") + 1] == "0"
    assert command[command.index("-w") + 1] == str(raw)
    assert command[command.index("-F") + 1] == "pcapng"
    assert "-Y" not in command
    assert "occurrence=a" in command
    assert "aggregator=|" in command


def test_raw_capture_sidecar_records_width_and_drop_diagnostics(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "capture.pcapng"
    requested = ChannelDefinition(5180, ChannelWidth.MHZ80, 5210)
    actual = ChannelDefinition(5180, ChannelWidth.MHZ20, 5180)
    coverage = CoverageDecision(
        requested,
        ("aa",),
        ("bb",),
        "partial",
        "driver fallback",
    )

    _write_raw_capture_metadata(
        raw,
        interface="wlan0",
        selected_channel="36",
        requested=requested,
        actual=actual,
        actual_verified=True,
        coverage=coverage,
        tshark_fields=TSHARK_FRAME_FIELD_NAMES,
        capture_record_count=10,
        normalized_frame_count=11,
        malformed_capture_record_count=1,
        tshark_stderr="10 packets captured, 2 packets dropped",
    )

    payload = json.loads(
        raw.with_suffix(".pcapng.json").read_text(encoding="utf-8")
    )
    assert payload["snapshot_length"] == 0
    assert payload["requested_channel_definition"]["width"] == "80"
    assert payload["actual_channel_definition"]["width"] == "20"
    assert payload["coverage_status"] == "partial"
    assert payload["reported_drop_count"] == 2


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
    assert str(exc_info.value) == "set type failed"
    assert commands_seen == [
        ["ip", "link", "set", "wlan0", "down"],
        failing_command,
    ]


def test_live_command_error_without_stderr_still_has_a_message() -> None:
    error = LiveCommandError(
        ["iw", "dev", "wlan0", "set", "freq", "5180", "80MHz", "5210"],
        returncode=1,
    )

    assert str(error) == (
        "iw dev wlan0 set freq 5180 80MHz 5210 exited with status 1"
    )


def test_explicit_unsupported_width_fails_before_interface_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "beacon_live.live._read_radio_capabilities_safely",
        lambda iface: RadioCapabilities.ht20_only(),
    )
    monkeypatch.setattr(
        "beacon_live.live.configure_monitor_interface",
        lambda *args, **kwargs: pytest.fail("interface should not be changed"),
    )

    with pytest.raises(ValueError, match="explicit capture definition.*80 MHz"):
        run_live(
            channel_width=ChannelWidth.MHZ80,
            center_frequency1_mhz=5210,
        )


def test_default_80mhz_tune_failure_retries_once_at_20mhz(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_one_interval_live_run(monkeypatch)
    capabilities = RadioCapabilities(
        frozenset({ChannelWidth.MHZ20, ChannelWidth.MHZ80}),
        frozenset({5180, 5200, 5220, 5240}),
        supports_monitor=True,
        source_complete=True,
    )
    definitions: list[ChannelDefinition] = []

    def configure(*args: object, **kwargs: object) -> None:
        definition = kwargs["channel_definition"]
        assert isinstance(definition, ChannelDefinition)
        definitions.append(definition)
        if definition.width is ChannelWidth.MHZ80:
            raise LiveCommandError(
                ["iw", "dev", "wlan0", "set", "freq", "5180"],
                returncode=1,
                stderr="driver rejected 80 MHz",
            )

    monkeypatch.setattr(
        "beacon_live.live._read_radio_capabilities_safely",
        lambda iface: capabilities,
    )
    monkeypatch.setattr(
        "beacon_live.live.configure_monitor_interface",
        configure,
    )
    monkeypatch.setattr(
        "beacon_live.live._read_actual_channel_safely",
        lambda iface, requested: (requested, True),
    )

    assert run_live(interval_seconds=0.1) == 0

    assert [definition.width for definition in definitions] == [
        ChannelWidth.MHZ80,
        ChannelWidth.MHZ20,
    ]
    assert (
        "driver rejected 80 MHz); retrying once at 20 MHz"
        in capsys.readouterr().err
    )


def test_all_frame_live_mode_skips_survey_and_keeps_beacon_logging(
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
    assert stats_rows[0]["local_time"]
    assert stats_rows[0]["frequency_mhz"] == "5180"
    assert stats_rows[0]["band"] == "5"
    assert next(iter(stats_rows[0])) == "local_time"
    assert "start_time" not in stats_rows[0]
    assert (
        datetime.fromisoformat(stats_rows[0]["local_time"]).utcoffset()
        is not None
    )
    assert stats_rows[0]["unique_bssid_count"] == "1"
    assert stats_rows[0]["selected_qbss_cu_percent"] == "25.10"
    assert stats_rows[0]["received_frame_count"] == "3"
    assert stats_rows[0]["retry_eligible_frame_count"] == "2"
    assert stats_rows[0]["retry_frame_count"] == "1"
    assert stats_rows[0]["retry_percent"] == "50.00"
    assert stats_rows[0]["local_cu_percent"] == ""

    beacons = [
        json.loads(line)
        for line in beacons_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert len(beacons) == 2
    assert {beacon["bssid"] for beacon in beacons} == {"aa:aa:aa:aa:aa:aa"}
    assert [beacon["rssi_dbm"] for beacon in beacons] == [-45, -44]
    assert {beacon["frequency_mhz"] for beacon in beacons} == {5180}
    assert {beacon["band"] for beacon in beacons} == {"5"}
    assert all("record_type" not in beacon for beacon in beacons)
    assert all(
        datetime.fromisoformat(beacon["local_time"]).utcoffset() is not None
        for beacon in beacons
    )


def test_interactive_terminal_runs_capture_inside_curses_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_one_interval_live_run(monkeypatch)
    events: list[str] = []
    snapshots: list[object] = []
    logging_statuses: list[dict[str, object]] = []
    stats_csv = tmp_path / "display-stats.csv"
    beacons_jsonl = tmp_path / "display-beacons.jsonl"

    class FakeCursesDashboard:
        def __init__(
            self,
            *,
            include_local_cu: bool,
            band: str,
            channel: str,
            frequency_mhz: int,
        ) -> None:
            assert include_local_cu is False
            assert band == "5"
            assert channel == "36"
            assert frequency_mhz == 5180
            events.append("created")

        def run(self, callback: object) -> int:
            events.append("wrapper-enter")
            try:
                return callback()  # type: ignore[operator]
            finally:
                events.append("wrapper-exit")

        def refresh(self, snapshot: object = None) -> None:
            snapshots.append(snapshot)

        def poll_input(self) -> bool:
            return False

        def set_logging_status(self, **status: object) -> None:
            logging_statuses.append(status)

    monkeypatch.setattr("beacon_live.live.should_use_curses", lambda: True)
    monkeypatch.setattr("beacon_live.live.CursesDashboard", FakeCursesDashboard)

    assert run_live(
        interval_seconds=0.1,
        stats_csv=stats_csv,
        beacons_jsonl=beacons_jsonl,
    ) == 0

    assert events == ["created", "wrapper-enter", "wrapper-exit"]
    assert snapshots
    assert logging_statuses == [
        {
            "active": True,
            "paths": (stats_csv, beacons_jsonl),
            "message": None,
        }
    ]
    assert "Logging started" not in capsys.readouterr().err


def test_non_tty_live_mode_keeps_plain_renderer_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_one_interval_live_run(monkeypatch)
    monkeypatch.setattr("beacon_live.live.should_use_curses", lambda: False)

    class UnexpectedCursesDashboard:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError(f"curses should not start: {kwargs}")

    monkeypatch.setattr(
        "beacon_live.live.CursesDashboard",
        UnexpectedCursesDashboard,
    )

    assert run_live(interval_seconds=0.1) == 0
    assert "WLANPi Beacon Live" in capsys.readouterr().out


def test_logging_only_never_starts_the_curses_dashboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_one_interval_live_run(monkeypatch)
    monkeypatch.setattr("beacon_live.live.should_use_curses", lambda: True)

    class UnexpectedCursesDashboard:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError(f"logging-only started curses: {kwargs}")

    monkeypatch.setattr(
        "beacon_live.live.CursesDashboard",
        UnexpectedCursesDashboard,
    )

    assert run_live(
        interval_seconds=0.1,
        stats_csv=tmp_path / "stats.csv",
        beacons_jsonl=tmp_path / "beacons.jsonl",
        logging_only=True,
    ) == 0
    assert capsys.readouterr().out == ""


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
            [SurveySample(4.0, 4000, 700, 0, 0, None, 5180, True)],
            [SurveySample(5.0, 5000, 900, 0, 0, None, 5180, True)],
            [SurveySample(6.0, 6000, 1100, 0, 0, None, 5180, True)],
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
    assert "falling back to HT20 partial coverage" in captured.err


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
        lambda iface, **kwargs: process,
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


def test_low_disk_stops_display_logging_but_capture_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stats_csv = tmp_path / "display-stats.csv"
    beacons_jsonl = tmp_path / "display-beacons.jsonl"
    logging_status = tmp_path / "logging.status.json"
    _prepare_one_interval_live_run(monkeypatch)

    assert run_live(
        stats_csv=stats_csv,
        beacons_jsonl=beacons_jsonl,
        logging_status_path=logging_status,
        min_free_bytes=10**30,
    ) == 0

    rows = [
        json.loads(line)
        for line in beacons_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["record_type"] == "end_of_log"
    assert rows[-1]["reason"] == "disk_space_nearly_full"
    assert sum(row.get("record_type") == "end_of_log" for row in rows) == 1
    assert json.loads(logging_status.read_text(encoding="utf-8")) == {
        "reason": "disk_space_nearly_full"
    }
    # The terminal dashboard still receives later analyzed seconds after the
    # logging service has closed.
    assert "Alpha/aa:aa:aa:aa:aa:aa" in capsys.readouterr().out


def test_low_disk_cleanly_exits_logging_only_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stats_csv = tmp_path / "only-stats.csv"
    beacons_jsonl = tmp_path / "only-beacons.jsonl"
    _prepare_one_interval_live_run(monkeypatch)

    assert run_live(
        stats_csv=stats_csv,
        beacons_jsonl=beacons_jsonl,
        logging_only=True,
        min_free_bytes=10**30,
    ) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "below the safety threshold" in captured.err
    rows = [
        json.loads(line)
        for line in beacons_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["record_type"] == "end_of_log"


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

    assert warmup_filter.filter([]) == []
    assert warmup_filter.remaining_cycles == 1
    assert warmup_filter.filter([stats]) == []
    assert warmup_filter.filter([stats]) == [stats]


def test_fixed_band_width_preserves_process_analyzer_logging_and_dashboard_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    optional_fields = (
        "wlan.ht.info.primarychannel",
        "wlan.ht.info.secchanoffset",
        "wlan.vht.op.channelwidth",
        "wlan.vht.op.channelcenter0",
        "wlan.vht.op.channelcenter1",
        "frame.cap_len",
        "frame.len",
    )
    field_names = TSHARK_FRAME_FIELD_NAMES + optional_fields
    rows = [
        _wide_beacon_row(field_names, timestamp=1000.1),
        _wide_beacon_row(field_names, timestamp=1001.1),
        _wide_beacon_row(field_names, timestamp=1002.1),
        _wide_data_row(field_names, timestamp=1003.1, retry=True),
        _wide_data_row(field_names, timestamp=1004.1, retry=False),
    ]

    class Process:
        def __init__(self) -> None:
            self.stdout = io.StringIO("\n".join(rows) + "\n")
            self.stderr = io.StringIO("")

        def poll(self) -> None:
            return None

    process_starts: list[str] = []
    configured_definitions: list[ChannelDefinition] = []
    snapshots: list[object] = []

    class Dashboard:
        def refresh(self, snapshot: object = None) -> None:
            snapshots.append(snapshot)

        def poll_input(self) -> bool:
            return False

    monotonic = iter(float(value) for value in range(100))
    capabilities = RadioCapabilities(
        frozenset({ChannelWidth.MHZ20, ChannelWidth.MHZ40, ChannelWidth.MHZ80}),
        frozenset({5180, 5200, 5220, 5240}),
        supports_monitor=True,
        source_complete=True,
    )
    monkeypatch.setattr(
        "beacon_live.live._read_radio_capabilities_safely",
        lambda iface: capabilities,
    )
    monkeypatch.setattr(
        "beacon_live.live._read_actual_channel_safely",
        lambda iface, requested: (requested, True),
    )
    monkeypatch.setattr(
        "beacon_live.live.configure_monitor_interface",
        lambda *args, **kwargs: configured_definitions.append(
            kwargs["channel_definition"]
        ),
    )
    monkeypatch.setattr(
        "beacon_live.live.available_tshark_fields",
        lambda: frozenset(field_names),
    )
    monkeypatch.setattr(
        "beacon_live.live.start_tshark_process",
        lambda iface, **kwargs: (process_starts.append(iface) or Process()),
    )
    monkeypatch.setattr(
        "beacon_live.live.terminate_tshark_process",
        lambda process: None,
    )
    monkeypatch.setattr("beacon_live.live.selectors.DefaultSelector", _FakeSelector)
    monkeypatch.setattr(
        "beacon_live.live.time.monotonic",
        lambda: next(monotonic),
    )

    stats_csv = tmp_path / "stats.csv"
    beacons_jsonl = tmp_path / "beacons.jsonl"
    assert run_live(
        interval_seconds=1.0,
        stats_csv=stats_csv,
        beacons_jsonl=beacons_jsonl,
        min_free_bytes=0,
        _dashboard=Dashboard(),
    ) == 0

    assert process_starts == ["wlan0"]
    assert len(configured_definitions) == 1
    assert configured_definitions[0].width is ChannelWidth.MHZ80
    assert configured_definitions[0].center_frequency1_mhz == 5210
    assert snapshots
    assert snapshots[-1].history
    assert stats_csv.exists() and beacons_jsonl.exists()
    with stats_csv.open(encoding="utf-8") as input_file:
        logged = list(csv.DictReader(input_file))
    assert logged[-1]["actual_capture_width_mhz"] == "80"
    assert "was truncated to 512" in capsys.readouterr().err


def _wide_beacon_row(field_names: tuple[str, ...], *, timestamp: float) -> str:
    values = {name: "" for name in field_names}
    values.update(
        {
            "frame.time_epoch": str(timestamp),
            "wlan.fc.type": "0",
            "wlan.fc.subtype": "8",
            "wlan.fc.retry": "0",
            "wlan.bssid": "aa:aa:aa:aa:aa:aa",
            "wlan.ta": "aa:aa:aa:aa:aa:aa",
            "wlan.ra": "ff:ff:ff:ff:ff:ff",
            "wlan.sa": "aa:aa:aa:aa:aa:aa",
            "wlan.da": "ff:ff:ff:ff:ff:ff",
            "wlan.ssid": "Wide",
            "wlan.fixed.beacon": "100",
            "wlan.ht.info.primarychannel": "36",
            "wlan.ht.info.secchanoffset": "1",
            "wlan.vht.op.channelwidth": "1",
            "wlan.vht.op.channelcenter0": "42",
            "frame.cap_len": "512",
            "frame.len": "700",
        }
    )
    return "\t".join(values[name] for name in field_names)


def _wide_data_row(
    field_names: tuple[str, ...],
    *,
    timestamp: float,
    retry: bool,
) -> str:
    values = {name: "" for name in field_names}
    values.update(
        {
            "frame.time_epoch": str(timestamp),
            "wlan.fc.type": "2",
            "wlan.fc.subtype": "0",
            "wlan.fc.retry": str(int(retry)),
            "wlan.bssid": "aa:aa:aa:aa:aa:aa",
            "wlan.ta": "00:11:22:33:44:55",
            "wlan.ra": "aa:aa:aa:aa:aa:aa",
            "wlan.sa": "00:11:22:33:44:55",
            "wlan.da": "aa:aa:aa:aa:aa:aa",
        }
    )
    return "\t".join(values[name] for name in field_names)


class _FakeTsharkProcess:
    def __init__(self) -> None:
        self.stdout = io.StringIO(
            "1000.100\t0\t8\t0\taa:aa:aa:aa:aa:aa\taa:aa:aa:aa:aa:aa\tff:ff:ff:ff:ff:ff\taa:aa:aa:aa:aa:aa\tff:ff:ff:ff:ff:ff\tAlpha\t128\t2\t0\t-45\t256\t100\n"
            "1001.100\t0\t8\t0\taa:aa:aa:aa:aa:aa\taa:aa:aa:aa:aa:aa\tff:ff:ff:ff:ff:ff\taa:aa:aa:aa:aa:aa\tff:ff:ff:ff:ff:ff\tAlpha\t64\t3\t0\t-44\t256\t100\n"
            "1001.200\t2\t0\t1\taa:aa:aa:aa:aa:aa\t10:11:11:11:11:10\taa:aa:aa:aa:aa:aa\t10:11:11:11:11:10\taa:aa:aa:aa:aa:aa\t\t\t\t\t-50\t100\t\n"
            "1001.300\t2\t0\t0\taa:aa:aa:aa:aa:aa\t10:11:11:11:11:10\taa:aa:aa:aa:aa:aa\t10:11:11:11:11:10\taa:aa:aa:aa:aa:aa\t\t\t\t\t-50\t100\t\n"
            "1002.100\t2\t0\t0\taa:aa:aa:aa:aa:aa\t10:11:11:11:11:12\taa:aa:aa:aa:aa:aa\t10:11:11:11:11:12\taa:aa:aa:aa:aa:aa\t\t\t\t\t-50\t100\t\n"
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
        lambda iface, **kwargs: _FakeTsharkProcess(),
    )
    monkeypatch.setattr(
        "beacon_live.live.terminate_tshark_process",
        lambda process: None,
    )
    monkeypatch.setattr("beacon_live.live.selectors.DefaultSelector", _FakeSelector)
    monkeypatch.setattr("beacon_live.live.time.monotonic", fake_monotonic)


def _field_args(command: list[str]) -> list[str]:
    fields: list[str] = []
    for index, value in enumerate(command):
        if value == "-e":
            fields.append(command[index + 1])
    return fields
