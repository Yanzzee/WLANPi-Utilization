"""Single-pass rolling beacon analysis and immutable snapshot publication."""

from __future__ import annotations

from bisect import insort
from dataclasses import replace
from typing import Optional

from beacon_live.models import BeaconRecord
from beacon_live.models import BssidState
from beacon_live.models import MetricsSnapshot
from beacon_live.models import SecondStats

DEFAULT_WINDOW_SECONDS = 120
DEFAULT_RSSI_HYSTERESIS_DB = 3


def select_bssid(
    states: tuple[BssidState, ...],
    current_bssid: Optional[str],
    *,
    hysteresis_db: int = DEFAULT_RSSI_HYSTERESIS_DB,
) -> Optional[str]:
    """Choose a stable QBSS-reporting BSSID from immutable rolling states.

    Candidates within the hysteresis band of the strongest signal are treated
    as near-equal. Station count breaks that near-equal tie, and an existing
    selection wins any remaining tie to avoid display churn.
    """
    if hysteresis_db < 0:
        raise ValueError("hysteresis_db must not be negative")

    candidates = tuple(state for state in states if state.qbss_present)
    if not candidates:
        return None

    candidates_with_rssi = tuple(
        state for state in candidates if state.peak_rssi_dbm is not None
    )
    if candidates_with_rssi:
        strongest_rssi = max(
            state.peak_rssi_dbm
            for state in candidates_with_rssi
            if state.peak_rssi_dbm is not None
        )
        near_equal = tuple(
            state
            for state in candidates_with_rssi
            if state.peak_rssi_dbm is not None
            and strongest_rssi - state.peak_rssi_dbm <= hysteresis_db
        )
    else:
        near_equal = candidates

    highest_station_count = max(_station_count_key(state) for state in near_equal)
    finalists = tuple(
        state
        for state in near_equal
        if _station_count_key(state) == highest_station_count
    )

    if current_bssid is not None and any(
        state.bssid == current_bssid for state in finalists
    ):
        return current_bssid

    return max(
        finalists,
        key=lambda state: (
            state.peak_rssi_dbm is not None,
            state.peak_rssi_dbm if state.peak_rssi_dbm is not None else -200,
            state.last_seen_ts,
            state.bssid,
        ),
    ).bssid


class Analyzer:
    """Ingest each beacon once and maintain one shared rolling analysis state."""

    def __init__(
        self,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        hysteresis_db: int = DEFAULT_RSSI_HYSTERESIS_DB,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than zero")
        if hysteresis_db < 0:
            raise ValueError("hysteresis_db must not be negative")

        self.window_seconds = window_seconds
        self.hysteresis_db = hysteresis_db
        self._records_by_bssid: dict[
            str, list[tuple[float, int, BeaconRecord]]
        ] = {}
        self._pending_seconds: set[int] = set()
        self._completed_seconds: set[int] = set()
        self._ready_stats: list[SecondStats] = []
        self._history_by_second: dict[int, SecondStats] = {}
        self._local_cu_by_second: dict[int, Optional[float]] = {}
        self._sequence = 0
        self._reference_timestamp: Optional[float] = None
        self._latest_local_cu_percent: Optional[float] = None
        self._current_selected_bssid: Optional[str] = None
        self._history_selected_bssid: Optional[str] = None
        self._snapshot = MetricsSnapshot.empty(window_seconds=window_seconds)

    @property
    def snapshot(self) -> MetricsSnapshot:
        """Return the latest immutable metrics snapshot."""
        return self._snapshot

    def set_local_cu_percent(self, second: int, percent: Optional[float]) -> None:
        self._local_cu_by_second[second] = percent

    def ingest(self, record: BeaconRecord) -> None:
        """Analyze one normalized beacon record exactly once."""
        record_second = int(record.timestamp)
        self._finalize_pending_before(record_second)

        if self._is_expired_late_record(record.timestamp):
            return

        self._sequence += 1
        entries = self._records_by_bssid.setdefault(record.bssid, [])
        insort(entries, (record.timestamp, self._sequence, record))
        if record_second not in self._completed_seconds:
            self._pending_seconds.add(record_second)

        if (
            self._reference_timestamp is None
            or record.timestamp > self._reference_timestamp
        ):
            self._reference_timestamp = record.timestamp

        self._expire_records(self._reference_timestamp)
        self._refresh_current_snapshot(
            reference_timestamp=self._reference_timestamp,
            current_second=int(self._reference_timestamp),
            upper_exclusive=None,
        )

    def add_pending(self, record: BeaconRecord) -> None:
        """Compatibility wrapper for callers that publish on another cadence."""
        self.ingest(record)

    def add(self, record: BeaconRecord) -> list[SecondStats]:
        """Ingest one record and return newly completed per-second samples."""
        self.ingest(record)
        return self._publish_ready()

    def pop_completed_before(self, second: int) -> list[SecondStats]:
        """Publish all observed samples whose seconds precede ``second``."""
        self._finalize_pending_before(second)
        return self._publish_ready(before_second=second)

    def advance(
        self,
        wall_second: int,
        local_cu_percent: Optional[float],
        *,
        include_history: bool = True,
    ) -> list[SecondStats]:
        """Advance live analysis and publish the previous whole-second sample."""
        self._latest_local_cu_percent = local_cu_percent
        self._finalize_pending_before(wall_second)
        target_second = wall_second - 1
        if target_second not in self._completed_seconds:
            self._complete_second(target_second)

        published = self._publish_ready(
            before_second=wall_second,
            default_local_cu_percent=local_cu_percent,
            include_history=include_history,
        )
        self._reference_timestamp = max(
            float(wall_second),
            self._reference_timestamp
            if self._reference_timestamp is not None
            else float(wall_second),
        )
        self._expire_records(self._reference_timestamp)
        self._prune_completed_seconds(target_second)
        self._refresh_current_snapshot(
            reference_timestamp=float(wall_second),
            current_second=target_second,
            upper_exclusive=float(wall_second),
        )
        return published

    def flush(self, *, include_history: bool = True) -> list[SecondStats]:
        """Publish every observed second still buffered by the analyzer."""
        for second in sorted(self._pending_seconds):
            self._complete_second(second)
        published = self._publish_ready(include_history=include_history)
        self._refresh_after_publication()
        return published

    def _finalize_pending_before(self, second: int) -> None:
        for pending_second in sorted(
            value for value in self._pending_seconds if value < second
        ):
            self._complete_second(pending_second)

    def _complete_second(self, second: int) -> None:
        if second in self._completed_seconds:
            self._pending_seconds.discard(second)
            return

        states = self._states_at(
            reference_timestamp=float(second + 1),
            upper_exclusive=float(second + 1),
        )
        selected_bssid = select_bssid(
            states,
            self._history_selected_bssid,
            hysteresis_db=self.hysteresis_db,
        )
        self._history_selected_bssid = selected_bssid
        self._ready_stats.append(
            _stats_from_states(
                second,
                states,
                selected_bssid,
                self._local_cu_by_second.get(second),
            )
        )
        self._completed_seconds.add(second)
        self._pending_seconds.discard(second)

    def _publish_ready(
        self,
        *,
        before_second: Optional[int] = None,
        default_local_cu_percent: Optional[float] = None,
        include_history: bool = True,
    ) -> list[SecondStats]:
        published: list[SecondStats] = []
        retained: list[SecondStats] = []
        for stats in sorted(self._ready_stats, key=lambda row: row.second):
            if before_second is not None and stats.second >= before_second:
                retained.append(stats)
                continue
            if stats.second not in self._local_cu_by_second:
                stats = replace(
                    stats,
                    local_cu_percent=default_local_cu_percent,
                )
            published.append(stats)
            if include_history:
                self._record_history(stats)

        self._ready_stats = retained
        self._refresh_after_publication()
        return published

    def _record_history(self, stats: SecondStats) -> None:
        self._history_by_second[stats.second] = stats
        latest_second = max(self._history_by_second)
        earliest_second = latest_second - self.window_seconds + 1
        self._history_by_second = {
            second: row
            for second, row in self._history_by_second.items()
            if second >= earliest_second
        }

    def _refresh_after_publication(self) -> None:
        if self._reference_timestamp is None:
            self._snapshot = MetricsSnapshot.empty(
                window_seconds=self.window_seconds
            )
            return
        self._refresh_current_snapshot(
            reference_timestamp=self._reference_timestamp,
            current_second=int(self._reference_timestamp),
            upper_exclusive=None,
        )

    def _refresh_current_snapshot(
        self,
        *,
        reference_timestamp: float,
        current_second: int,
        upper_exclusive: Optional[float],
    ) -> None:
        states = self._states_at(
            reference_timestamp=reference_timestamp,
            upper_exclusive=upper_exclusive,
        )
        self._current_selected_bssid = select_bssid(
            states,
            self._current_selected_bssid,
            hysteresis_db=self.hysteresis_db,
        )
        current = _stats_from_states(
            current_second,
            states,
            self._current_selected_bssid,
            self._latest_local_cu_percent,
        )
        history = tuple(
            self._history_by_second[second]
            for second in sorted(self._history_by_second)
        )
        self._snapshot = MetricsSnapshot(
            generated_at=reference_timestamp,
            window_seconds=self.window_seconds,
            bssids=states,
            selected_bssid=self._current_selected_bssid,
            current=current,
            history=history,
        )

    def _states_at(
        self,
        *,
        reference_timestamp: float,
        upper_exclusive: Optional[float],
    ) -> tuple[BssidState, ...]:
        cutoff = reference_timestamp - self.window_seconds
        states: list[BssidState] = []
        for bssid, entries in self._records_by_bssid.items():
            window_entries = tuple(
                entry
                for entry in entries
                if entry[0] >= cutoff
                and (upper_exclusive is None or entry[0] < upper_exclusive)
            )
            if not window_entries:
                continue
            latest = window_entries[-1][2]
            rssi_values = tuple(
                entry[2].rssi_dbm
                for entry in window_entries
                if entry[2].rssi_dbm is not None
            )
            states.append(
                BssidState(
                    bssid=bssid,
                    ssid=latest.ssid,
                    last_seen_ts=latest.timestamp,
                    latest_beacon_ts=latest.timestamp,
                    latest_beacon_record=latest,
                    window_beacon_count=len(window_entries),
                    latest_station_count=latest.qbss_station_count,
                    latest_qbss_cu_raw=latest.qbss_cu_raw,
                    latest_qbss_cu_percent=latest.qbss_cu_percent,
                    latest_admission_capacity=(
                        latest.qbss_admission_capacity
                    ),
                    latest_rssi_dbm=latest.rssi_dbm,
                    peak_rssi_dbm=max(rssi_values) if rssi_values else None,
                )
            )
        return tuple(sorted(states, key=lambda state: state.bssid))

    def _expire_records(self, reference_timestamp: float) -> None:
        cutoff = reference_timestamp - self.window_seconds
        for bssid in tuple(self._records_by_bssid):
            entries = self._records_by_bssid[bssid]
            first_retained = 0
            while (
                first_retained < len(entries)
                and entries[first_retained][0] < cutoff
            ):
                first_retained += 1
            if first_retained:
                del entries[:first_retained]
            if not entries:
                del self._records_by_bssid[bssid]

    def _is_expired_late_record(self, timestamp: float) -> bool:
        return (
            self._reference_timestamp is not None
            and timestamp < self._reference_timestamp - self.window_seconds
        )

    def _prune_completed_seconds(self, latest_second: int) -> None:
        earliest_second = latest_second - self.window_seconds + 1
        self._completed_seconds = {
            second
            for second in self._completed_seconds
            if second >= earliest_second
        }
        self._local_cu_by_second = {
            second: value
            for second, value in self._local_cu_by_second.items()
            if second >= earliest_second
        }


def _station_count_key(state: BssidState) -> int:
    return state.latest_station_count if state.latest_station_count is not None else -1


def _stats_from_states(
    second: int,
    states: tuple[BssidState, ...],
    selected_bssid: Optional[str],
    local_cu_percent: Optional[float],
) -> SecondStats:
    selected = next(
        (state for state in states if state.bssid == selected_bssid),
        None,
    )
    return SecondStats(
        second=second,
        unique_bssid_count=len(states),
        qbss_station_count_sum=sum(
            state.latest_station_count or 0 for state in states
        ),
        selected_qbss_cu_percent=(
            selected.latest_qbss_cu_percent if selected is not None else None
        ),
        selected_qbss_ssid=selected.ssid if selected is not None else None,
        selected_qbss_bssid=selected.bssid if selected is not None else None,
        selected_qbss_rssi_dbm=(
            selected.latest_rssi_dbm if selected is not None else None
        ),
        local_cu_percent=local_cu_percent,
        selected_qbss_cu_raw=(
            selected.latest_qbss_cu_raw if selected is not None else None
        ),
        selected_qbss_station_count=(
            selected.latest_station_count if selected is not None else None
        ),
        selected_qbss_strongest_rssi_dbm=(
            selected.peak_rssi_dbm if selected is not None else None
        ),
    )
