from pathlib import Path
from typing import Optional

import pytest

from beacon_live.device import main


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
    assert "usage: wlanpi-beacon-live" in help_output
    assert "wlanpi-beacon-live live" not in help_output
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
    assert "Stopping WLANPi beacon capture" in capsys.readouterr().err
