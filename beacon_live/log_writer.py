"""Optional CSV and JSONL capture logging."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Optional

from beacon_live.models import BeaconRecord
from beacon_live.models import SecondStats

STATS_CSV_FIELDS = [
    "start_time",
    "interface",
    "channel",
    "frequency_mhz",
    "band",
    "local_time",
    "unique_bssid_count",
    "qbss_station_count_sum",
    "selected_qbss_cu_percent",
    "selected_qbss_ssid",
    "selected_qbss_bssid",
    "selected_qbss_rssi_dbm",
    "local_cu_percent",
]


@dataclass(frozen=True)
class LogMetadata:
    start_time: str
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
            **asdict(self._metadata),
            "local_time": _local_time_from_epoch(record.timestamp),
            **beacon_fields,
        }
        self._beacons_file.write(json.dumps(payload, sort_keys=True) + "\n")
        self._beacons_file.flush()


def _stats_csv_row(stats: SecondStats, metadata: LogMetadata) -> dict[str, object]:
    return {
        "start_time": metadata.start_time,
        "interface": metadata.interface,
        "channel": metadata.channel,
        "frequency_mhz": _optional_value(metadata.frequency_mhz),
        "band": metadata.band or "",
        "local_time": _local_time_from_epoch(stats.second),
        "unique_bssid_count": stats.unique_bssid_count,
        "qbss_station_count_sum": stats.qbss_station_count_sum,
        "selected_qbss_cu_percent": _format_optional_float(
            stats.selected_qbss_cu_percent
        ),
        "selected_qbss_ssid": stats.selected_qbss_ssid or "",
        "selected_qbss_bssid": stats.selected_qbss_bssid or "",
        "selected_qbss_rssi_dbm": _optional_value(stats.selected_qbss_rssi_dbm),
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
