from pathlib import Path
from typing import Optional

import pytest

from beacon_live.device import main


def test_device_launcher_enters_live_mode_and_forwards_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    stats_csv = tmp_path / "stats.csv"

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
            str(stats_csv),
        ]
    ) == 0
    assert calls == [
        {
            "iface": "wlan9",
            "channel": "5",
            "frequency_mhz": None,
            "band": "6",
            "interval_seconds": 1.0,
            "local_cu": True,
            "survey_debug": False,
            "stats_csv": stats_csv,
            "beacons_jsonl": None,
        }
    ]


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


def test_device_launcher_exits_cleanly_if_setup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted_cli(argv: list[str]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("beacon_live.device.live_main", interrupted_cli)

    assert main(["--iface", "wlan0"]) == 130
    assert "Stopping WLANPi beacon capture" in capsys.readouterr().err
