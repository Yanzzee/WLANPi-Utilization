import csv
import json
from pathlib import Path
from typing import Optional

import pytest

from beacon_live.cli import main
from beacon_live.live import LiveCommandError
from beacon_live.models import FrameRecord
from beacon_live.retry_debug import RetryCaptureData


def test_replay_accepts_pi_smoke_files_and_prints_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    beacons_tsv = tmp_path / "pi_tshark_qbss_sample.tsv"
    survey_before = tmp_path / "pi_survey_before.txt"
    survey_after = tmp_path / "pi_survey_after.txt"

    beacons_tsv.write_text(
        "\n".join(
            [
                "1000.100\tAlpha\taa:aa:aa:aa:aa:aa\t128\t2\t0",
                "not-a-valid-row",
                "1000.900\tBravo\tbb:bb:bb:bb:bb:bb\t64\t3\t0",
                "1001.100\tAlpha\taa:aa:aa:aa:aa:aa\t\t4\t0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    survey_before.write_text(
        "\n".join(
            [
                "Survey data from wlan0",
                "        frequency:                      5180 MHz",
                "        channel active time:            1000 ms",
                "        channel busy time:              100 ms",
                "        channel receive time:           0 ms",
                "        channel transmit time:          0 ms",
                "Survey data from wlan0",
                "        frequency:                      5200 MHz",
                "        channel active time:            5000 ms",
                "        channel busy time:              1000 ms",
            ]
        ),
        encoding="utf-8",
    )
    survey_after.write_text(
        "\n".join(
            [
                "Survey data from wlan0",
                "        frequency:                      5180 MHz",
                "        channel active time:            2000 ms",
                "        channel busy time:              300 ms",
                "        channel receive time:           0 ms",
                "        channel transmit time:          0 ms",
                "Survey data from wlan0",
                "        frequency:                      5200 MHz",
                "        channel active time:            5100 ms",
                "        channel busy time:              1010 ms",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "replay",
            "--beacons-tsv",
            str(beacons_tsv),
            "--survey-before",
            str(survey_before),
            "--survey-after",
            str(survey_after),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "\t20.00" in captured.out
    assert "Summary" in captured.out
    assert "total_beacon_rows_read: 4" in captured.out
    assert "valid_beacon_rows: 3" in captured.out
    assert "malformed_skipped_rows: 1" in captured.out
    assert "rows_with_qbss_cu: 2" in captured.out
    assert "unique_bssid_count: 2" in captured.out
    assert "first_timestamp: 1000.1" in captured.out
    assert "last_timestamp: 1001.1" in captured.out
    assert "duration_seconds: 1" in captured.out
    assert "local_survey_cu_percent: 20.00" in captured.out
    assert "Warning: skipped malformed beacon rows: 1" in captured.err


def test_replay_keeps_legacy_input_option(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    beacons_tsv = tmp_path / "beacons.tsv"
    beacons_tsv.write_text(
        "1000.100\tAlpha\taa:aa:aa:aa:aa:aa\t128\t2\t0\n",
        encoding="utf-8",
    )

    exit_code = main(["replay", "--input", str(beacons_tsv)])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "valid_beacon_rows: 1" in captured.out
    assert "local_survey_cu_percent: " in captured.out
    assert captured.err == ""


def test_retry_debug_writes_auditable_channel_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_csv = tmp_path / "retry-audit.csv"
    monkeypatch.setattr(
        "beacon_live.cli.read_retry_debug_capture",
        lambda path: RetryCaptureData(
            frames=(
                FrameRecord(
                    timestamp=1000.1,
                    bssid="aa:bb:cc:dd:ee:ff",
                    frame_type=2,
                    frame_subtype=0,
                    retry_flag=True,
                    receiver_address="00:11:22:33:44:55",
                ),
            ),
            tshark_row_count=1,
            malformed_row_count=0,
        ),
    )

    assert main(
        [
            "retry-debug",
            "--input",
            str(tmp_path / "capture.pcapng"),
            "--output-csv",
            str(output_csv),
        ]
    ) == 0

    with output_csv.open(encoding="utf-8") as output_file:
        rows = list(csv.DictReader(output_file))
    channel = next(row for row in rows if row["scope"] == "channel")
    assert channel["retry_eligible_frame_count"] == "1"
    assert channel["retry_frame_count"] == "1"
    assert channel["retry_percent"] == "100.000000"
    captured = capsys.readouterr()
    assert f"Retry audit CSV: {output_csv}" in captured.err
    assert "decoded_frames=1" in captured.err


def test_replay_writes_stats_csv_and_beacon_jsonl_logs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    beacons_tsv = tmp_path / "beacons.tsv"
    stats_csv = tmp_path / "logs" / "nested" / "stats.csv"
    beacons_jsonl = tmp_path / "logs" / "nested" / "beacons.jsonl"

    beacons_tsv.write_text(
        "\n".join(
            [
                "1000.100\tAlpha\taa:aa:aa:aa:aa:aa\t128\t2\t0",
                "1000.900\tBravo\tbb:bb:bb:bb:bb:bb\t64\t3\t0",
                "malformed",
                "1001.100\tAlpha\taa:aa:aa:aa:aa:aa\t32\t4\t0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "replay",
            "--beacons-tsv",
            str(beacons_tsv),
            "--stats-csv",
            str(stats_csv),
            "--beacons-jsonl",
            str(beacons_jsonl),
            "--interface",
            "wlan9",
            "--channel",
            "5975",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Warning: skipped malformed beacon rows: 1" in captured.err
    assert stats_csv.exists()
    assert beacons_jsonl.exists()

    with stats_csv.open("r", encoding="utf-8", newline="") as stats_file:
        stats_rows = list(csv.DictReader(stats_file))

    assert len(stats_rows) == 2
    assert stats_rows[0]["interface"] == "wlan9"
    assert stats_rows[0]["channel"] == "5975"
    assert stats_rows[0]["frequency_mhz"] == "5975"
    assert stats_rows[0]["band"] == "6"
    assert "channel_width_mhz" not in stats_rows[0]
    assert "second" not in stats_rows[0]
    assert stats_rows[0]["local_time"]
    assert stats_rows[0]["unique_bssid_count"] == "2"
    assert stats_rows[0]["selected_qbss_ssid"] == "Bravo"
    assert stats_rows[1]["local_time"]

    beacon_lines = beacons_jsonl.read_text(encoding="utf-8").splitlines()
    beacon_records = [json.loads(line) for line in beacon_lines]

    assert len(beacon_records) == 3
    assert all("record_type" not in record for record in beacon_records)
    assert {record["interface"] for record in beacon_records} == {"wlan9"}
    assert {record["channel"] for record in beacon_records} == {"5975"}
    assert {record["frequency_mhz"] for record in beacon_records} == {5975}
    assert {record["band"] for record in beacon_records} == {"6"}
    assert all("channel_width_mhz" not in record for record in beacon_records)
    assert all("timestamp" not in record for record in beacon_records)
    assert all(record["local_time"] for record in beacon_records)
    assert beacon_records[0]["ssid"] == "Alpha"
    assert beacon_records[0]["qbss_cu_raw"] == 128


def test_live_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

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
            }
        )
        return 0

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(["live"]) == 0
    assert calls == [
        {
            "iface": "wlan0",
            "channel": "36",
            "frequency_mhz": None,
            "band": None,
            "interval_seconds": 1.0,
            "local_cu": False,
            "survey_debug": False,
        }
    ]


def test_live_generates_log_filenames_in_configured_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    log_dir = tmp_path / "designated-logs"

    def fake_run_live(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(
        [
            "live",
            "--iface",
            "wlan9",
            "--channel",
            "44",
            "--stats-csv",
            "--beacons-jsonl",
            "--log-dir",
            str(log_dir),
        ]
    ) == 0

    stats_csv = calls[0]["stats_csv"]
    beacons_jsonl = calls[0]["beacons_jsonl"]
    assert isinstance(stats_csv, Path)
    assert isinstance(beacons_jsonl, Path)
    assert stats_csv.parent == log_dir
    assert beacons_jsonl.parent == log_dir
    assert stats_csv.name.startswith("beacon_live_")
    assert stats_csv.name.endswith("_stats.csv")
    assert beacons_jsonl.name.startswith("beacon_live_")
    assert beacons_jsonl.name.endswith("_beacons.jsonl")
    assert stats_csv.name.removesuffix("_stats.csv") == (
        beacons_jsonl.name.removesuffix("_beacons.jsonl")
    )
    assert calls[0]["iface"] == "wlan9"
    assert calls[0]["channel"] == "44"


def test_live_wires_hidden_lcd_frame_for_fpms_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    frame = tmp_path / "display.ppm"

    def fake_run_live(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(
        [
            "live",
            "--band",
            "5",
            "--channel",
            "36",
            "--lcd-frame",
            str(frame),
        ]
    ) == 0
    assert calls[0]["lcd_frame"] == frame


def test_live_does_not_pass_lcd_frame_for_normal_terminal_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_live(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(["live"]) == 0
    assert "lcd_frame" not in calls[0]


def test_live_log_flags_reject_arbitrary_filenames(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["live", "--stats-csv", "custom.csv"])

    assert exc_info.value.code == 2
    assert "unrecognized arguments: custom.csv" in capsys.readouterr().err


def test_live_accepts_explicit_frequency_mhz(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

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
            }
        )
        return 0

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(["live", "--iface", "wlan0", "--frequency-mhz", "5975"]) == 0
    assert calls == [
        {
            "iface": "wlan0",
            "channel": "36",
            "frequency_mhz": 5975,
            "band": None,
            "interval_seconds": 1.0,
            "local_cu": False,
            "survey_debug": False,
        }
    ]


def test_live_accepts_band_qualified_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

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
            }
        )
        return 0

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(["live", "--iface", "wlan0", "--band", "6", "--channel", "5"]) == 0
    assert calls == [
        {
            "iface": "wlan0",
            "channel": "5",
            "frequency_mhz": None,
            "band": "6",
            "interval_seconds": 1.0,
            "local_cu": False,
            "survey_debug": False,
        }
    ]


def test_live_accepts_local_cu(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

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
            }
        )
        return 0

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(["live", "--local-cu"]) == 0
    assert calls == [
        {
            "iface": "wlan0",
            "channel": "36",
            "frequency_mhz": None,
            "band": None,
            "interval_seconds": 1.0,
            "local_cu": True,
            "survey_debug": False,
        }
    ]


def test_live_accepts_survey_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

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
            }
        )
        return 0

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(["live", "--survey-debug"]) == 0
    assert calls == [
        {
            "iface": "wlan0",
            "channel": "36",
            "frequency_mhz": None,
            "band": None,
            "interval_seconds": 1.0,
            "local_cu": True,
            "survey_debug": True,
        }
    ]


def test_live_rejects_frequency_and_band_together(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["live", "--frequency-mhz", "5975", "--band", "6"])

    captured = capsys.readouterr()

    assert exc_info.value.code == 2
    assert "live accepts --frequency-mhz or --band/--channel" in captured.err


def test_live_reports_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed_command = ["iw", "dev", "wlan0", "set", "channel", "36", "HT20"]

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
        raise LiveCommandError(
            failed_command,
            returncode=1,
            stderr="channel set failed",
        )

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(["live", "--iface", "wlan0", "--channel", "36"]) == 1
    captured = capsys.readouterr()

    assert "Command failed: iw dev wlan0 set channel 36 HT20" in captured.err
    assert "Exit status: 1" in captured.err
    assert "channel set failed" in captured.err
