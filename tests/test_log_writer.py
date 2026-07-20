import csv
import json
import threading
from collections import namedtuple
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from beacon_live.log_writer import CaptureLogWriter
from beacon_live.log_writer import LOW_DISK_END_MESSAGE
from beacon_live.log_writer import LogMetadata
from beacon_live.log_writer import LoggingEvent
from beacon_live.log_writer import LoggingService
from beacon_live.log_writer import build_live_log_paths
from beacon_live.models import BeaconRecord
from beacon_live.models import SecondStats


def test_live_log_paths_use_generated_timestamped_filenames() -> None:
    log_dir = Path("designated-logs")
    paths = build_live_log_paths(
        log_dir,
        write_stats_csv=True,
        write_beacons_jsonl=True,
        timestamp=datetime(
            2026,
            7,
            10,
            12,
            30,
            45,
            123456,
            tzinfo=timezone(timedelta(hours=-6)),
        ),
    )

    assert paths.stats_csv == (
        log_dir / "beacon_live_20260710T123045123456-0600_stats.csv"
    )
    assert paths.beacons_jsonl == (
        log_dir / "beacon_live_20260710T123045123456-0600_beacons.jsonl"
    )


def test_capture_log_writer_creates_directories_and_flushes_rows(
    tmp_path: Path,
) -> None:
    stats_path = tmp_path / "logs" / "nested" / "stats.csv"
    beacons_path = tmp_path / "logs" / "nested" / "beacons.jsonl"
    metadata = LogMetadata(
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
        rssi_dbm=-47,
    )
    stats = SecondStats(
        second=1000,
        unique_bssid_count=1,
        qbss_station_count_sum=3,
        unique_client_mac_count=2,
        selected_qbss_cu_percent=128 / 255 * 100,
        selected_qbss_ssid="Test AP",
        selected_qbss_bssid="aa:bb:cc:dd:ee:ff",
        selected_qbss_rssi_dbm=-47,
        local_cu_percent=None,
        beacon_received_count=19,
        beacon_expected_count=20,
        beacon_loss_percent=5.0,
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
    expected_stats_time = datetime.fromtimestamp(1000).astimezone().isoformat()
    expected_beacon_time = datetime.fromtimestamp(1000.25).astimezone().isoformat()

    assert next(iter(stats_rows[0])) == "local_time"
    assert "start_time" not in stats_rows[0]
    assert stats_rows[0]["interface"] == "wlan9"
    assert stats_rows[0]["channel"] == "5"
    assert stats_rows[0]["frequency_mhz"] == "5975"
    assert stats_rows[0]["band"] == "6"
    assert stats_rows[0]["local_time"] == expected_stats_time
    assert "channel_width_mhz" not in stats_rows[0]
    assert "second" not in stats_rows[0]
    assert stats_rows[0]["selected_qbss_cu_percent"] == "50.20"
    assert stats_rows[0]["selected_qbss_bssid"] == "aa:bb:cc:dd:ee:ff"
    assert stats_rows[0]["selected_qbss_rssi_dbm"] == "-47"
    assert stats_rows[0]["unique_client_mac_count"] == "2"
    assert stats_rows[0]["beacon_received_count"] == "19"
    assert stats_rows[0]["beacon_expected_count"] == "20"
    assert stats_rows[0]["beacon_loss_percent"] == "5.00"

    assert len(beacon_rows) == 1
    assert "record_type" not in beacon_rows[0]
    assert "channel_width_mhz" not in beacon_rows[0]
    assert "timestamp" not in beacon_rows[0]
    assert next(iter(beacon_rows[0])) == "local_time"
    assert "start_time" not in beacon_rows[0]
    assert beacon_rows[0]["local_time"] == expected_beacon_time
    assert beacon_rows[0]["interface"] == "wlan9"
    assert beacon_rows[0]["channel"] == "5"
    assert beacon_rows[0]["frequency_mhz"] == 5975
    assert beacon_rows[0]["band"] == "6"
    assert beacon_rows[0]["bssid"] == "aa:bb:cc:dd:ee:ff"
    assert beacon_rows[0]["rssi_dbm"] == -47


def test_logging_service_starts_and_stops_without_writing_while_inactive(
    tmp_path: Path,
) -> None:
    service = _logging_service(tmp_path)

    assert service.active is False
    assert service.start() is True
    first_paths = service.current_paths
    assert first_paths is not None
    assert service.active is True
    assert service.start() is False

    service.write_beacon(_beacon())
    assert service.stop() is True
    assert service.active is False
    assert service.stop() is False
    service.write_beacon(_beacon(timestamp=1001.0))

    assert first_paths.beacons_jsonl is not None
    assert len(first_paths.beacons_jsonl.read_text(encoding="utf-8").splitlines()) == 1


def test_logging_service_checks_disk_only_at_periodic_deadlines(
    tmp_path: Path,
) -> None:
    usage = namedtuple("usage", "total used free")
    checks: list[Path] = []

    def disk_usage(path: Path) -> object:
        checks.append(path)
        return usage(1000, 100, 900)

    service = _logging_service(
        tmp_path,
        disk_usage=disk_usage,
        disk_check_interval_seconds=10.0,
        min_free_bytes=500,
    )
    service.start()

    assert service.maintain(now=0.0) is None
    assert service.maintain(now=9.99) is None
    assert service.maintain(now=10.0) is None
    assert checks == [tmp_path, tmp_path]
    service.stop()


def test_logging_service_writes_on_worker_and_drains_in_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    entered_write = threading.Event()
    release_write = threading.Event()
    original_write_beacon = CaptureLogWriter.write_beacon

    def delayed_write(
        writer: CaptureLogWriter,
        record: BeaconRecord,
    ) -> None:
        entered_write.set()
        release_write.wait(timeout=2.0)
        original_write_beacon(writer, record)

    monkeypatch.setattr(CaptureLogWriter, "write_beacon", delayed_write)
    service = _logging_service(tmp_path)
    assert service.start()
    paths = service.current_paths
    assert paths is not None and paths.beacons_jsonl is not None

    service.write_beacon(_beacon(timestamp=1000.0))
    assert entered_write.wait(timeout=1.0)
    # The worker is still blocked in the first disk write, but the capture-side
    # caller can enqueue the next ordered record without waiting for that I/O.
    service.write_beacon(_beacon(timestamp=1001.0))
    release_write.set()
    assert service.stop()

    rows = [
        json.loads(line)
        for line in paths.beacons_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["local_time"] for row in rows] == [
        datetime.fromtimestamp(timestamp).astimezone().isoformat()
        for timestamp in (1000.0, 1001.0)
    ]


def test_logging_worker_write_error_preserves_low_disk_stop_behavior(
    tmp_path: Path,
    monkeypatch,
) -> None:
    attempted_write = threading.Event()

    def failing_write(
        writer: CaptureLogWriter,
        record: BeaconRecord,
    ) -> None:
        attempted_write.set()
        raise OSError("disk write failed")

    monkeypatch.setattr(CaptureLogWriter, "write_beacon", failing_write)
    service = _logging_service(tmp_path)
    assert service.start()
    service.write_beacon(_beacon())
    assert attempted_write.wait(timeout=1.0)
    assert service._write_failed.wait(timeout=1.0)

    assert service.seconds_until_maintenance(now=0.0) == 0.0
    assert service.maintain(now=0.0) is LoggingEvent.LOW_DISK_STOP
    assert service.active is False


def test_low_disk_stops_logging_and_writes_markers_to_both_formats(
    tmp_path: Path,
) -> None:
    usage = namedtuple("usage", "total used free")
    service = _logging_service(
        tmp_path,
        min_free_bytes=500,
        disk_usage=lambda path: usage(1000, 600, 400),
    )
    service.start()
    paths = service.current_paths
    assert paths is not None
    service.write_beacon(_beacon())

    assert service.maintain(now=0.0) is LoggingEvent.LOW_DISK_STOP
    assert service.active is False
    assert paths.stats_csv is not None
    assert paths.beacons_jsonl is not None

    with paths.stats_csv.open(encoding="utf-8", newline="") as stats_file:
        stats_rows = list(csv.DictReader(stats_file))
    jsonl_rows = [
        json.loads(line)
        for line in paths.beacons_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert LOW_DISK_END_MESSAGE in stats_rows[-1]["selected_qbss_ssid"]
    assert jsonl_rows[-1]["record_type"] == "end_of_log"
    assert jsonl_rows[-1]["reason"] == "disk_space_nearly_full"
    assert LOW_DISK_END_MESSAGE in jsonl_rows[-1]["message"]
    assert jsonl_rows[-1]["free_bytes"] == 400
    assert jsonl_rows[-1]["threshold_bytes"] == 500


def test_logging_service_rolls_to_predictable_new_files(
    tmp_path: Path,
) -> None:
    fixed_time = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    service = _logging_service(
        tmp_path,
        now=lambda: fixed_time,
        rotation_interval_seconds=60.0,
    )
    service.start()
    first_paths = service.current_paths
    service.write_beacon(_beacon())
    assert service.maintain(now=0.0) is None

    assert service.maintain(now=60.0) is LoggingEvent.ROTATED
    second_paths = service.current_paths
    assert first_paths is not None
    assert second_paths is not None
    assert second_paths != first_paths
    assert second_paths.beacons_jsonl is not None
    assert second_paths.beacons_jsonl.name.endswith("_part0002_beacons.jsonl")
    service.write_beacon(_beacon(timestamp=1001.0))
    service.stop()

    assert first_paths.beacons_jsonl is not None
    assert len(first_paths.beacons_jsonl.read_text(encoding="utf-8").splitlines()) == 1
    assert len(second_paths.beacons_jsonl.read_text(encoding="utf-8").splitlines()) == 1


def _logging_service(tmp_path: Path, **kwargs: object) -> LoggingService:
    return LoggingService(
        metadata=LogMetadata(interface="wlan0", channel="36"),
        log_dir=tmp_path,
        write_stats_csv=True,
        write_beacons_jsonl=True,
        monotonic=lambda: 0.0,
        **kwargs,
    )


def _beacon(*, timestamp: float = 1000.25) -> BeaconRecord:
    return BeaconRecord(
        timestamp=timestamp,
        ssid="Test AP",
        bssid="aa:bb:cc:dd:ee:ff",
        qbss_cu_raw=128,
        qbss_cu_percent=128 / 255 * 100,
        qbss_station_count=3,
        qbss_admission_capacity=0,
        rssi_dbm=-47,
    )
