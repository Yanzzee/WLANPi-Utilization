from pathlib import Path

import pytest

from beacon_live.cli import main


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
