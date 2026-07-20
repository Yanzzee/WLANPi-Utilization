"""Optional CSV and JSONL capture logging with shared disk safety."""

from __future__ import annotations

import csv
import json
import queue
import shutil
import threading
import time
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import IO, Callable, Optional

from beacon_live.models import BeaconRecord
from beacon_live.models import SecondStats

STATS_CSV_FIELDS = [
    "local_time",
    "interface",
    "channel",
    "frequency_mhz",
    "band",
    "unique_bssid_count",
    "qbss_station_count_sum",
    "unique_client_mac_count",
    "selected_qbss_cu_percent",
    "selected_qbss_ssid",
    "selected_qbss_bssid",
    "selected_qbss_rssi_dbm",
    "received_frame_count",
    "retry_observed_frame_count",
    "retry_eligible_frame_count",
    "retry_frame_count",
    "retry_percent",
    "selected_beacon_rate_percent",
    "beacon_received_count",
    "beacon_expected_count",
    "beacon_loss_percent",
    "top_retry_bssid",
    "local_cu_percent",
]

DEFAULT_MIN_FREE_BYTES = 256 * 1024 * 1024
DEFAULT_DISK_CHECK_INTERVAL_SECONDS = 30.0
DEFAULT_ROTATION_INTERVAL_SECONDS = 60.0 * 60.0
DEFAULT_LOG_QUEUE_CAPACITY = 4096
LOW_DISK_END_MESSAGE = (
    "END_OF_LOG: logging stopped because free disk space was nearly full"
)
_STOP_LOG_WRITER = object()


@dataclass(frozen=True)
class LogMetadata:
    interface: str
    channel: str
    frequency_mhz: Optional[int] = None
    band: Optional[str] = None


@dataclass(frozen=True)
class LiveLogPaths:
    """Application-generated paths for one live capture run."""

    stats_csv: Optional[Path]
    beacons_jsonl: Optional[Path]


def build_live_log_paths(
    log_dir: Path,
    *,
    write_stats_csv: bool,
    write_beacons_jsonl: bool,
    timestamp: Optional[datetime] = None,
    part_number: Optional[int] = None,
) -> LiveLogPaths:
    """Build timestamped live log paths inside the designated directory."""
    if timestamp is None:
        capture_time = datetime.now().astimezone()
    elif timestamp.tzinfo is None:
        capture_time = timestamp.astimezone()
    else:
        capture_time = timestamp
    stamp = capture_time.strftime("%Y%m%dT%H%M%S%f%z")
    basename = f"beacon_live_{stamp}"
    if part_number is not None:
        basename = f"{basename}_part{part_number:04d}"
    return LiveLogPaths(
        stats_csv=(log_dir / f"{basename}_stats.csv") if write_stats_csv else None,
        beacons_jsonl=(
            log_dir / f"{basename}_beacons.jsonl"
            if write_beacons_jsonl
            else None
        ),
    )


class CaptureLogWriter:
    """Write optional live or replay logs and flush every emitted record."""

    def __init__(
        self,
        *,
        metadata: LogMetadata,
        stats_csv: Optional[Path] = None,
        beacons_jsonl: Optional[Path] = None,
    ) -> None:
        self._metadata = metadata
        self._stats_csv_path = stats_csv
        self._beacons_jsonl_path = beacons_jsonl
        self._stats_file: Optional[IO[str]] = None
        self._stats_writer: Optional[csv.DictWriter] = None
        self._beacons_file: Optional[IO[str]] = None

    def __enter__(self) -> "CaptureLogWriter":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def open(self) -> None:
        try:
            if self._stats_csv_path is not None:
                _ensure_parent_dir(self._stats_csv_path)
                self._stats_file = self._stats_csv_path.open(
                    "w",
                    encoding="utf-8",
                    newline="",
                )
                self._stats_writer = csv.DictWriter(
                    self._stats_file,
                    fieldnames=STATS_CSV_FIELDS,
                )
                self._stats_writer.writeheader()
                self._stats_file.flush()

            if self._beacons_jsonl_path is not None:
                _ensure_parent_dir(self._beacons_jsonl_path)
                self._beacons_file = self._beacons_jsonl_path.open(
                    "w",
                    encoding="utf-8",
                )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._stats_file is not None:
            self._stats_file.close()
            self._stats_file = None
            self._stats_writer = None
        if self._beacons_file is not None:
            self._beacons_file.close()
            self._beacons_file = None

    def write_stats(self, stats: SecondStats) -> None:
        if self._stats_writer is None or self._stats_file is None:
            return
        self._stats_writer.writerow(_stats_csv_row(stats, self._metadata))
        self._stats_file.flush()

    def write_beacon(self, record: BeaconRecord) -> None:
        if self._beacons_file is None:
            return
        beacon_fields = asdict(record)
        beacon_fields.pop("timestamp")
        payload = {
            "local_time": _local_time_from_epoch(record.timestamp),
            **asdict(self._metadata),
            **beacon_fields,
        }
        self._beacons_file.write(json.dumps(payload) + "\n")
        self._beacons_file.flush()

    def write_low_disk_end_marker(
        self,
        *,
        free_bytes: Optional[int],
        threshold_bytes: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Append a format-compatible marker before a low-disk close."""
        marker_time = timestamp or datetime.now().astimezone()
        free_text = "unknown" if free_bytes is None else str(free_bytes)
        message = (
            f"{LOW_DISK_END_MESSAGE}; free_bytes={free_text}; "
            f"threshold_bytes={threshold_bytes}"
        )

        if self._stats_writer is not None and self._stats_file is not None:
            row: dict[str, object] = {field: "" for field in STATS_CSV_FIELDS}
            row.update(
                {
                    "local_time": marker_time.isoformat(),
                    "interface": self._metadata.interface,
                    "channel": self._metadata.channel,
                    "frequency_mhz": _optional_value(
                        self._metadata.frequency_mhz
                    ),
                    "band": self._metadata.band or "",
                    "selected_qbss_ssid": message,
                }
            )
            self._stats_writer.writerow(row)
            self._stats_file.flush()

        if self._beacons_file is not None:
            payload = {
                "record_type": "end_of_log",
                "local_time": marker_time.isoformat(),
                "reason": "disk_space_nearly_full",
                "message": message,
                "free_bytes": free_bytes,
                "threshold_bytes": threshold_bytes,
                **asdict(self._metadata),
            }
            self._beacons_file.write(json.dumps(payload) + "\n")
            self._beacons_file.flush()


class LoggingEvent(str, Enum):
    """Events that can change runtime behavior outside the logging layer."""

    LOW_DISK_STOP = "low_disk_stop"
    ROTATED = "rotated"


class LoggingService:
    """Own one toggleable logging session for display and logging-only modes."""

    def __init__(
        self,
        *,
        metadata: LogMetadata,
        log_dir: Path,
        write_stats_csv: bool,
        write_beacons_jsonl: bool,
        initial_paths: Optional[LiveLogPaths] = None,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        disk_check_interval_seconds: float = DEFAULT_DISK_CHECK_INTERVAL_SECONDS,
        rotation_interval_seconds: float = DEFAULT_ROTATION_INTERVAL_SECONDS,
        write_queue_capacity: int = DEFAULT_LOG_QUEUE_CAPACITY,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
    ) -> None:
        if not write_stats_csv and not write_beacons_jsonl:
            raise ValueError("at least one logging format must be enabled")
        if min_free_bytes < 0:
            raise ValueError("min_free_bytes must not be negative")
        if disk_check_interval_seconds <= 0:
            raise ValueError("disk check interval must be greater than zero")
        if rotation_interval_seconds <= 0:
            raise ValueError("rotation interval must be greater than zero")
        if write_queue_capacity <= 0:
            raise ValueError("write queue capacity must be greater than zero")

        self._metadata = metadata
        self._log_dir = log_dir
        self._write_stats_csv = write_stats_csv
        self._write_beacons_jsonl = write_beacons_jsonl
        self._initial_paths = initial_paths
        self._min_free_bytes = min_free_bytes
        self._disk_check_interval_seconds = disk_check_interval_seconds
        self._rotation_interval_seconds = rotation_interval_seconds
        self._write_queue_capacity = write_queue_capacity
        self._monotonic = monotonic
        self._now = now
        self._disk_usage = disk_usage
        self._writer: Optional[CaptureLogWriter] = None
        self._write_queue: Optional[queue.Queue[object]] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._write_failed = threading.Event()
        self._active = False
        self._file_number = 0
        self._next_disk_check: Optional[float] = None
        self._next_rotation: Optional[float] = None
        self._current_paths: Optional[LiveLogPaths] = None
        self._pending_event: Optional[LoggingEvent] = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def current_paths(self) -> Optional[LiveLogPaths]:
        return self._current_paths

    def start(self) -> bool:
        """Start logging without changing capture or analysis state."""
        if self._active or self._pending_event is not None:
            return False

        paths = self._next_paths()
        writer = CaptureLogWriter(
            metadata=self._metadata,
            stats_csv=paths.stats_csv,
            beacons_jsonl=paths.beacons_jsonl,
        )
        writer.open()
        started_at = self._monotonic()
        self._writer = writer
        self._start_writer_worker(writer)
        self._active = True
        self._current_paths = paths
        self._pending_event = None
        # Check immediately after the files exist so a low-disk session still
        # receives an explicit end marker.
        self._next_disk_check = started_at
        self._next_rotation = started_at + self._rotation_interval_seconds
        return True

    def stop(self) -> bool:
        """Stop a logging session while leaving capture and analysis alone."""
        if not self._active:
            return False
        self._close_writer()
        return True

    def write_stats(self, stats: SecondStats) -> None:
        self._enqueue(stats)

    def write_beacon(self, record: BeaconRecord) -> None:
        self._enqueue(record)

    def maintain(self, *, now: Optional[float] = None) -> Optional[LoggingEvent]:
        """Run deterministic disk and rollover checks when their deadlines pass."""
        if self._write_failed.is_set():
            self._stop_for_low_disk(None)
            return LoggingEvent.LOW_DISK_STOP
        if self._pending_event is not None:
            event = self._pending_event
            self._pending_event = None
            return event
        if not self._active:
            return None
        current = self._monotonic() if now is None else now

        if self._next_disk_check is not None and current >= self._next_disk_check:
            free_bytes: Optional[int]
            try:
                free_bytes = int(getattr(self._disk_usage(self._log_dir), "free"))
            except (OSError, TypeError, ValueError, AttributeError):
                free_bytes = None
            self._next_disk_check = _advance_deadline(
                self._next_disk_check,
                self._disk_check_interval_seconds,
                current,
            )
            if free_bytes is None or free_bytes < self._min_free_bytes:
                self._stop_for_low_disk(free_bytes)
                return LoggingEvent.LOW_DISK_STOP

        if self._next_rotation is not None and current >= self._next_rotation:
            self._rotate(current)
            return LoggingEvent.ROTATED

        return None

    def seconds_until_maintenance(self, *, now: Optional[float] = None) -> Optional[float]:
        if self._pending_event is not None or self._write_failed.is_set():
            return 0.0
        if not self._active:
            return None
        current = self._monotonic() if now is None else now
        deadlines = [
            deadline
            for deadline in (self._next_disk_check, self._next_rotation)
            if deadline is not None
        ]
        return max(0.0, min(deadlines) - current) if deadlines else None

    def _next_paths(self) -> LiveLogPaths:
        self._file_number += 1
        if self._file_number == 1 and self._initial_paths is not None:
            paths = self._initial_paths
            self._initial_paths = None
            return paths
        return build_live_log_paths(
            self._log_dir,
            write_stats_csv=self._write_stats_csv,
            write_beacons_jsonl=self._write_beacons_jsonl,
            timestamp=self._now(),
            part_number=(self._file_number if self._file_number > 1 else None),
        )

    def _rotate(self, current: float) -> None:
        self._close_writer()
        paths = self._next_paths()
        writer = CaptureLogWriter(
            metadata=self._metadata,
            stats_csv=paths.stats_csv,
            beacons_jsonl=paths.beacons_jsonl,
        )
        writer.open()
        self._writer = writer
        self._start_writer_worker(writer)
        self._active = True
        self._current_paths = paths
        self._next_disk_check = current + self._disk_check_interval_seconds
        self._next_rotation = current + self._rotation_interval_seconds

    def _stop_for_low_disk(self, free_bytes: Optional[int]) -> None:
        writer = self._writer
        self._stop_writer_worker()
        try:
            if writer is not None:
                writer.write_low_disk_end_marker(
                    free_bytes=free_bytes,
                    threshold_bytes=self._min_free_bytes,
                    timestamp=self._now(),
                )
        except OSError:
            # A sudden ENOSPC can prevent even the reserved marker write. The
            # threshold makes this unlikely, but capture must still survive.
            pass
        finally:
            self._close_writer()

    def _close_writer(self) -> None:
        writer = self._writer
        self._writer = None
        self._active = False
        self._next_disk_check = None
        self._next_rotation = None
        self._stop_writer_worker()
        self._write_failed.clear()
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass

    def _enqueue(self, record: object) -> None:
        work_queue = self._write_queue
        if self._active and work_queue is not None:
            # Backpressure is deliberate: never discard a log record merely to
            # keep capture moving. The bounded queue absorbs normal disk jitter
            # while preserving output correctness during a sustained slowdown.
            work_queue.put(record)

    def _start_writer_worker(self, writer: CaptureLogWriter) -> None:
        self._write_failed.clear()
        work_queue: queue.Queue[object] = queue.Queue(
            maxsize=self._write_queue_capacity
        )
        thread = threading.Thread(
            target=self._run_writer,
            args=(writer, work_queue),
            name="beacon-live-log-writer",
            daemon=True,
        )
        self._write_queue = work_queue
        self._writer_thread = thread
        thread.start()

    def _stop_writer_worker(self) -> None:
        work_queue = self._write_queue
        thread = self._writer_thread
        self._write_queue = None
        self._writer_thread = None
        if work_queue is None or thread is None:
            return
        work_queue.put(_STOP_LOG_WRITER)
        thread.join()

    def _run_writer(
        self,
        writer: CaptureLogWriter,
        work_queue: queue.Queue[object],
    ) -> None:
        write_failed = False
        while True:
            record = work_queue.get()
            if record is _STOP_LOG_WRITER:
                return
            if write_failed:
                continue
            try:
                if isinstance(record, BeaconRecord):
                    writer.write_beacon(record)
                elif isinstance(record, SecondStats):
                    writer.write_stats(record)
            except OSError:
                write_failed = True
                self._write_failed.set()


def _advance_deadline(deadline: float, interval: float, current: float) -> float:
    while deadline <= current:
        deadline += interval
    return deadline


def _stats_csv_row(stats: SecondStats, metadata: LogMetadata) -> dict[str, object]:
    return {
        "local_time": _local_time_from_epoch(stats.second),
        "interface": metadata.interface,
        "channel": metadata.channel,
        "frequency_mhz": _optional_value(metadata.frequency_mhz),
        "band": metadata.band or "",
        "unique_bssid_count": stats.unique_bssid_count,
        "qbss_station_count_sum": stats.qbss_station_count_sum,
        "unique_client_mac_count": stats.unique_client_mac_count,
        "selected_qbss_cu_percent": _format_optional_float(
            stats.selected_qbss_cu_percent
        ),
        "selected_qbss_ssid": stats.selected_qbss_ssid or "",
        "selected_qbss_bssid": stats.selected_qbss_bssid or "",
        "selected_qbss_rssi_dbm": _optional_value(stats.selected_qbss_rssi_dbm),
        "received_frame_count": stats.received_frame_count,
        "retry_observed_frame_count": stats.retry_observed_frame_count,
        "retry_eligible_frame_count": stats.retry_eligible_frame_count,
        "retry_frame_count": stats.retry_frame_count,
        "retry_percent": _format_optional_float(stats.retry_percent),
        "selected_beacon_rate_percent": _format_optional_float(
            stats.selected_beacon_rate_percent
        ),
        "beacon_received_count": stats.beacon_received_count,
        "beacon_expected_count": stats.beacon_expected_count,
        "beacon_loss_percent": _format_optional_float(
            stats.beacon_loss_percent
        ),
        "top_retry_bssid": stats.top_retry_bssid or "",
        "local_cu_percent": _format_optional_float(stats.local_cu_percent),
    }


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _format_optional_float(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.2f}"


def _optional_value(value: Optional[int]) -> object:
    return "" if value is None else value


def _local_time_from_epoch(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat()
