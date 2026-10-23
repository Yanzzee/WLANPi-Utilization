from pathlib import Path
from typing import Optional
from dataclasses import replace
import json

import pytest

from beacon_live.device import main
from beacon_live.runtime import ManagedRuntime, SessionPhase
from beacon_live.service_client import ServiceClient, ServiceError


@pytest.mark.parametrize("phase", [SessionPhase.STARTING, SessionPhase.RUNNING, SessionPhase.STOPPING])
def test_session_stop_defaults_to_current_id_and_preserves_explicit_id(monkeypatch, capsys, phase):
    current = replace(ManagedRuntime().read(), session_id="a" * 32, phase=phase)
    stopped = replace(current, phase=SessionPhase.STOPPING)
    calls = []
    monkeypatch.setattr(ServiceClient, "read", lambda self: calls.append("read") or current)
    monkeypatch.setattr(ServiceClient, "stop", lambda self, identifier: calls.append(identifier) or stopped)
    assert main(["session", "stop", "--summary"]) == 0
    assert calls == ["read", current.session_id]
    assert json.loads(capsys.readouterr().out)["phase"] == "stopping"
    calls.clear()
    assert main(["session", "stop", "--session-id", "explicit", "--summary"]) == 0
    assert calls == ["explicit"]  # Explicit targets never follow a replacement session.


@pytest.mark.parametrize("phase", [SessionPhase.IDLE, SessionPhase.STOPPED, SessionPhase.FAILED])
def test_session_stop_without_id_reports_no_active_session(monkeypatch, capsys, phase):
    current = replace(ManagedRuntime().read(), phase=phase,
                      session_id=None if phase is SessionPhase.IDLE else "a" * 32)
    monkeypatch.setattr(ServiceClient, "read", lambda self: current)
    monkeypatch.setattr(ServiceClient, "stop", lambda *args: pytest.fail("No active session should be stopped"))
    assert main(["session", "stop", "--summary"]) == 1
    assert "No active Utilization session to stop" in capsys.readouterr().err


def test_session_stop_does_not_retry_or_follow_a_replacement_after_stale_id(monkeypatch, capsys):
    current = replace(ManagedRuntime().read(), session_id="a" * 32, phase=SessionPhase.RUNNING)
    calls = []
    monkeypatch.setattr(ServiceClient, "read", lambda self: calls.append("read") or current)
    def stop(self, identifier):
        calls.append(identifier)
        raise ServiceError("Stale Utilization session ID")
    monkeypatch.setattr(ServiceClient, "stop", stop)
    assert main(["session", "stop", "--summary"]) == 1
    assert calls == ["read", current.session_id]
    assert "Stale Utilization session ID" in capsys.readouterr().err


@pytest.mark.parametrize("operation", ["logging", "export"])
def test_other_session_controls_still_require_an_explicit_id(operation):
    with pytest.raises(SystemExit) as error:
        main(["session", operation, "--enabled", "no"])
    assert error.value.code == 2


def test_device_launcher_enters_live_mode_and_forwards_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    log_dir = tmp_path / "logs"

    def fake_run_live(
        *,
        iface: str,
        channel: str,
        frequency_mhz: Optional[int],
        band: Optional[str],
        interval_seconds: float,
        local_cu: bool,
        survey_debug: bool,
        stats_csv: Optional[Path],
        beacons_jsonl: Optional[Path],
    ) -> int:
        calls.append(
            {
                "iface": iface,
                "channel": channel,
                "frequency_mhz": frequency_mhz,
                "band": band,
                "interval_seconds": interval_seconds,
                "local_cu": local_cu,
                "survey_debug": survey_debug,
                "stats_csv": stats_csv,
                "beacons_jsonl": beacons_jsonl,
            }
        )
        return 0

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(
        [
            "--iface",
            "wlan9",
            "--band",
            "6",
            "--channel",
            "5",
            "--local-cu",
            "--stats-csv",
            "--log-dir",
            str(log_dir),
        ]
    ) == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["iface"] == "wlan9"
    assert call["channel"] == "5"
    assert call["frequency_mhz"] is None
    assert call["band"] == "6"
    assert call["interval_seconds"] == 1.0
    assert call["local_cu"] is True
    assert call["survey_debug"] is False
    assert call["beacons_jsonl"] is None
    stats_csv = call["stats_csv"]
    assert isinstance(stats_csv, Path)
    assert stats_csv.parent == log_dir
    assert stats_csv.name.endswith("_stats.csv")


def test_device_launcher_uses_live_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded_args: list[list[str]] = []

    def fake_live_main(argv: list[str]) -> int:
        forwarded_args.append(argv)
        return 7

    monkeypatch.setattr("beacon_live.device.live_main", fake_live_main)

    assert main([]) == 7
    assert forwarded_args == [[]]


def test_device_launcher_exposes_live_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    help_output = capsys.readouterr().out
    assert "usage: wlanpi-utilization" in help_output
    assert "wlanpi-utilization live" not in help_output
    assert "--iface" in help_output
    assert "--local-cu" in help_output
    assert "--stats-csv" in help_output
    assert "--log-dir" in help_output


def test_device_launcher_exits_cleanly_if_setup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted_cli(argv: list[str]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("beacon_live.device.live_main", interrupted_cli)

    assert main(["--iface", "wlan0"]) == 130
    assert "Stopping Utilization" in capsys.readouterr().err
