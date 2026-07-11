"""Per-second aggregation for parsed beacon records."""

from dataclasses import dataclass, field
from typing import Iterable, Optional, Tuple

from beacon_live.models import BeaconRecord, SecondStats


class Aggregator:
    """Aggregate timestamp-ordered beacon records into completed seconds."""

    def __init__(self) -> None:
        self._buckets: dict[int, _SecondBucket] = {}
        self._latest_second: Optional[int] = None
        self._local_cu_by_second: dict[int, Optional[float]] = {}

    def set_local_cu_percent(self, second: int, percent: Optional[float]) -> None:
        self._local_cu_by_second[second] = percent

    def add_pending(self, record: BeaconRecord) -> None:
        """Add one record without emitting completed seconds."""
        second = int(record.timestamp)
        if self._latest_second is None or second > self._latest_second:
            self._latest_second = second

        bucket = self._buckets.setdefault(second, _SecondBucket())
        bucket.add(record)

    def add(self, record: BeaconRecord) -> list[SecondStats]:
        """Add one record and return stats for any newly completed seconds."""
        self.add_pending(record)
        return self._pop_completed()

    def pop_completed_before(self, second: int) -> list[SecondStats]:
        """Return stats for buffered seconds before ``second``."""
        completed_seconds = sorted(
            bucket_second for bucket_second in self._buckets if bucket_second < second
        )
        completed: list[SecondStats] = []
        for completed_second in completed_seconds:
            bucket = self._buckets.pop(completed_second)
            completed.append(
                _build_stats(
                    completed_second,
                    bucket,
                    self._local_cu_by_second.get(completed_second),
                )
            )
        return completed

    def flush(self) -> list[SecondStats]:
        """Return stats for all buffered seconds."""
        completed = [
            _build_stats(
                second,
                self._buckets[second],
                self._local_cu_by_second.get(second),
            )
            for second in sorted(self._buckets)
        ]
        self._buckets.clear()
        return completed

    def _pop_completed(self) -> list[SecondStats]:
        if self._latest_second is None:
            return []

        return self.pop_completed_before(self._latest_second)


def aggregate_records(
    records: Iterable[BeaconRecord],
    *,
    local_cu_by_second: Optional[dict[int, Optional[float]]] = None,
) -> list[SecondStats]:
    """Aggregate a finite iterable of records into sorted per-second stats."""
    aggregator = Aggregator()
    for second, percent in (local_cu_by_second or {}).items():
        aggregator.set_local_cu_percent(second, percent)

    stats: list[SecondStats] = []
    for record in records:
        stats.extend(aggregator.add(record))
    stats.extend(aggregator.flush())
    return stats


@dataclass
class _SecondBucket:
    records: list[BeaconRecord] = field(default_factory=list)
    latest_station_by_bssid: dict[str, Tuple[float, Optional[int]]] = field(
        default_factory=dict
    )

    def add(self, record: BeaconRecord) -> None:
        self.records.append(record)
        previous = self.latest_station_by_bssid.get(record.bssid)
        if previous is None or record.timestamp >= previous[0]:
            self.latest_station_by_bssid[record.bssid] = (
                record.timestamp,
                record.qbss_station_count,
            )


def _build_stats(
    second: int,
    bucket: _SecondBucket,
    local_cu_percent: Optional[float],
) -> SecondStats:
    bssids = {record.bssid for record in bucket.records}
    station_count_sum = sum(
        station_count or 0
        for _, station_count in bucket.latest_station_by_bssid.values()
    )

    selected_record = _select_qbss_record(bucket.records)

    return SecondStats(
        second=second,
        unique_bssid_count=len(bssids),
        qbss_station_count_sum=station_count_sum,
        selected_qbss_cu_percent=(
            selected_record.qbss_cu_percent
            if selected_record is not None
            else None
        ),
        selected_qbss_ssid=(selected_record.ssid if selected_record else None),
        selected_qbss_bssid=(selected_record.bssid if selected_record else None),
        selected_qbss_rssi_dbm=(
            selected_record.rssi_dbm if selected_record else None
        ),
        local_cu_percent=local_cu_percent,
    )


def _select_qbss_record(records: list[BeaconRecord]) -> Optional[BeaconRecord]:
    """Select the latest QBSS beacon from the strongest observed BSSID."""
    qbss_records = [
        record for record in records if record.qbss_cu_percent is not None
    ]
    if not qbss_records:
        return None

    strongest_by_bssid: dict[str, BeaconRecord] = {}
    for record in qbss_records:
        previous = strongest_by_bssid.get(record.bssid)
        if previous is None or _signal_key(record) > _signal_key(previous):
            strongest_by_bssid[record.bssid] = record

    strongest_bssid = max(
        strongest_by_bssid.values(),
        key=lambda record: (*_signal_key(record), record.bssid),
    ).bssid
    return max(
        (record for record in qbss_records if record.bssid == strongest_bssid),
        key=lambda record: record.timestamp,
    )


def _signal_key(record: BeaconRecord) -> tuple[bool, int, float]:
    return (
        record.rssi_dbm is not None,
        record.rssi_dbm if record.rssi_dbm is not None else -200,
        record.timestamp,
    )
