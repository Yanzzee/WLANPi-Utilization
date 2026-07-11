import csv
import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

from beacon_live.log_writer import CaptureLogWriter
from beacon_live.log_writer import LogMetadata
from beacon_live.log_writer import build_live_log_paths
from beacon_live.models import BeaconRecord
from beacon_live.models import SecondStats


def test_live_log_paths_use_generated_timestamped_filenames() -> None:
    log_dir = Path("designated-logs")
    paths = build_live_log_paths(
        log_dir,
        write_stats_csv=True,
        write_beacons_jsonl=True,
        timestamp=datetime(2026, 7, 10, 18, 30, 45, 123456, tzinfo=timezone.utc),
    )

    assert paths.stats_csv == (
        log_dir / "beacon_live_20260710T183045123456Z_stats.csv"
    )
    assert paths.beacons_jsonl == (
        log_dir / "beacon_live_20260710T183045123456Z_beacons.jsonl"
    )


def test_capture_log_writer_creates_directories_and_flushes_rows(
    tmp_path: Path,
) -> None:
    stats_path = tmp_path / "logs" / "nested" / "stats.csv"
    beacons_path = tmp_path / "logs" / "nested" / "beacons.jsonl"
    metadata = LogMetadata(
        start_time="2026-07-10T18:00:00Z",
        interface="wlan9",
        channel="5",
        frequency_mhz=5975,
        band="6",
    )
    beacon = BeaconRecord(
        timestamp=1000.25,
        ssid="Test AP",
        bssid="aa:bb:cc:dd:ee:ff",
        qbss_cu_raw=128,
        qbss_cu_percent=128 / 255 * 100,
        qbss_station_count=3,
        qbss_admission_capacity=0,
    )
    stats = SecondStats(
        second=1000,
        unique_bssid_count=1,
        qbss_station_count_sum=3,
        qbss_cu_min_percent=128 / 255 * 100,
        qbss_cu_mean_percent=128 / 255 * 100,
        qbss_cu_max_percent=128 / 255 * 100,
        top_qbss_cu_ssid="Test AP",
        top_qbss_cu_bssid="aa:bb:cc:dd:ee:ff",
        top_qbss_cu_percent=128 / 255 * 100,
        local_cu_percent=None,
    )

    with CaptureLogWriter(
        metadata=metadata,
        stats_csv=stats_path,
        beacons_jsonl=beacons_path,
    ) as writer:
        writer.write_beacon(beacon)
        writer.write_stats(stats)

        assert stats_path.read_text(encoding="utf-8").count("\n") == 2
        assert beacons_path.read_text(encoding="utf-8").count("\n") == 1

    with stats_path.open("r", encoding="utf-8", newline="") as stats_file:
        stats_rows = list(csv.DictReader(stats_file))
    beacon_rows = [
        json.loads(line)
        for line in beacons_path.read_text(encoding="utf-8").splitlines()
    ]

    assert len(stats_rows) == 1
    assert stats_rows[0]["start_time"] == "2026-07-10T18:00:00Z"
    assert stats_rows[0]["interface"] == "wlan9"
    assert stats_rows[0]["channel"] == "5"
    assert stats_rows[0]["frequency_mhz"] == "5975"
    assert stats_rows[0]["band"] == "6"
    assert stats_rows[0]["channel_width_mhz"] == "20"
    assert stats_rows[0]["second"] == "1000"

    assert len(beacon_rows) == 1
    assert beacon_rows[0]["record_type"] == "beacon"
    assert beacon_rows[0]["start_time"] == "2026-07-10T18:00:00Z"
    assert beacon_rows[0]["interface"] == "wlan9"
    assert beacon_rows[0]["channel"] == "5"
    assert beacon_rows[0]["frequency_mhz"] == 5975
    assert beacon_rows[0]["band"] == "6"
    assert beacon_rows[0]["bssid"] == "aa:bb:cc:dd:ee:ff"
