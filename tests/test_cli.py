import csv
import json
from pathlib import Path

import pytest

from beacon_live.cli import main
from beacon_live.live import LiveCommandError


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
    assert stats_rows[0]["channel_width_mhz"] == "20"
    assert stats_rows[0]["second"] == "1000"
    assert stats_rows[0]["unique_bssid_count"] == "2"
    assert stats_rows[0]["top_qbss_cu_ssid"] == "Alpha"
    assert stats_rows[1]["second"] == "1001"

    beacon_lines = beacons_jsonl.read_text(encoding="utf-8").splitlines()
    beacon_records = [json.loads(line) for line in beacon_lines]

    assert len(beacon_records) == 3
    assert {record["record_type"] for record in beacon_records} == {"beacon"}
    assert {record["interface"] for record in beacon_records} == {"wlan9"}
    assert {record["channel"] for record in beacon_records} == {"5975"}
    assert {record["channel_width_mhz"] for record in beacon_records} == {20}
    assert beacon_records[0]["ssid"] == "Alpha"
    assert beacon_records[0]["qbss_cu_raw"] == 128


def test_live_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_live(*, iface: str, channel: str, interval_seconds: float) -> int:
        calls.append(
            {
                "iface": iface,
                "channel": channel,
                "interval_seconds": interval_seconds,
            }
        )
        return 0

    monkeypatch.setattr("beacon_live.cli.run_live", fake_run_live)

    assert main(["live"]) == 0
    assert calls == [
        {
            "iface": "wlan0",
            "channel": "36",
            "interval_seconds": 1.0,
        }
    ]


def test_live_reports_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed_command = ["iw", "dev", "wlan0", "set", "channel", "36", "HT20"]

    def fake_run_live(*, iface: str, channel: str, interval_seconds: float) -> int:
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
