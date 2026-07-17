"""Single-pass rolling frame analysis and immutable snapshot publication."""

from __future__ import annotations

from bisect import insort
from dataclasses import replace
from typing import Optional, Union

from beacon_live.models import BeaconRecord
from beacon_live.models import BssidState
from beacon_live.models import FrameRecord
from beacon_live.models import MetricsSnapshot
from beacon_live.models import RetryBssidState
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
            state.bssid,
        ),
    ).bssid


class Analyzer:
    """Ingest each normalized frame once and publish one shared snapshot."""

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
        self._frames: list[tuple[float, int, FrameRecord]] = []
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
        self._current_retry_bssid: Optional[str] = None
        self._history_retry_bssid: Optional[str] = None
        self._snapshot = MetricsSnapshot.empty(window_seconds=window_seconds)

    @property
    def snapshot(self) -> MetricsSnapshot:
        """Return the latest immutable metrics snapshot."""
        return self._snapshot

    def set_local_cu_percent(self, second: int, percent: Optional[float]) -> None:
        self._local_cu_by_second[second] = percent

    def ingest(
        self,
        record: Union[BeaconRecord, FrameRecord],
        *,
        publish_snapshot: bool = True,
    ) -> None:
        """Analyze one record, optionally deferring immutable publication."""
        record_second = int(record.timestamp)
        self._finalize_pending_before(record_second)

        if self._is_expired_late_record(record.timestamp):
            return

        self._sequence += 1
        beacon = record if isinstance(record, BeaconRecord) else record.beacon_record()
        if isinstance(record, FrameRecord):
            frame_entry = (record.timestamp, self._sequence, record)
            if not self._frames or frame_entry[:2] >= self._frames[-1][:2]:
                self._frames.append(frame_entry)
            else:
                insort(self._frames, frame_entry)
        if beacon is not None:
            entries = self._records_by_bssid.setdefault(beacon.bssid, [])
            beacon_entry = (beacon.timestamp, self._sequence, beacon)
            if not entries or beacon_entry[:2] >= entries[-1][:2]:
                entries.append(beacon_entry)
            else:
                insort(entries, beacon_entry)
        if record_second not in self._completed_seconds:
            self._pending_seconds.add(record_second)

        if (
            self._reference_timestamp is None
            or record.timestamp > self._reference_timestamp
        ):
            self._reference_timestamp = record.timestamp

        if publish_snapshot:
            self._expire_records(self._reference_timestamp)
            self._refresh_current_snapshot(
                reference_timestamp=self._reference_timestamp,
                current_second=int(self._reference_timestamp),
                upper_exclusive=None,
            )

    def add_pending(self, record: Union[BeaconRecord, FrameRecord]) -> None:
        """Compatibility wrapper for callers that publish on another cadence."""
        self.ingest(record)

    def add(self, record: Union[BeaconRecord, FrameRecord]) -> list[SecondStats]:
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

    def publish_capture_complete(
        self,
        local_cu_percent: Optional[float],
        *,
        include_history: bool = True,
    ) -> list[SecondStats]:
        """Publish seconds completed by the ordered capture stream.

        TShark can have many rows buffered when a wall-clock refresh fires. A
        second is therefore complete only after a frame from a later capture
        second has been ingested. This prevents live publication from making
        an incomplete retry bucket immutable while older rows are still being
        drained from TShark stdout.
        """
        self._latest_local_cu_percent = local_cu_percent
        if self._reference_timestamp is None:
            return []

        capture_second = int(self._reference_timestamp)
        published = self._publish_ready(
            before_second=capture_second,
            default_local_cu_percent=local_cu_percent,
            include_history=include_history,
            refresh_snapshot=False,
        )
        if not published:
            return []

        latest_second = published[-1].second
        self._expire_records(self._reference_timestamp)
        self._prune_completed_seconds(latest_second)
        self._refresh_current_snapshot(
            reference_timestamp=float(latest_second + 1),
            current_second=latest_second,
            upper_exclusive=float(latest_second + 1),
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

        frames = self._frames_at(
            reference_timestamp=float(second + 1),
            upper_exclusive=float(second + 1),
        )
        states = self._states_at(
            reference_timestamp=float(second + 1),
            upper_exclusive=float(second + 1),
            frames=frames,
        )
        second_frames = _frames_for_second(frames, second)
        retry_states = _retry_states_from_frames(second_frames, states)
        selected_bssid = select_bssid(
            states,
            self._history_selected_bssid,
            hysteresis_db=self.hysteresis_db,
        )
        self._history_selected_bssid = selected_bssid
        self._history_retry_bssid = select_retry_bssid(
            retry_states,
            states,
            self._history_retry_bssid,
        )
        self._ready_stats.append(
            _stats_from_states(
                second,
                states,
                selected_bssid,
                self._local_cu_by_second.get(second),
                second_frames,
                self._history_retry_bssid,
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
        refresh_snapshot: bool = True,
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
        if refresh_snapshot:
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
        frames = self._frames_at(
            reference_timestamp=reference_timestamp,
            upper_exclusive=upper_exclusive,
        )
        states = self._states_at(
            reference_timestamp=reference_timestamp,
            upper_exclusive=upper_exclusive,
            frames=frames,
        )
        second_frames = _frames_for_second(frames, current_second)
        retry_states = _retry_states_from_frames(second_frames, states)
        self._current_selected_bssid = select_bssid(
            states,
            self._current_selected_bssid,
            hysteresis_db=self.hysteresis_db,
        )
        self._current_retry_bssid = select_retry_bssid(
            retry_states,
            states,
            self._current_retry_bssid,
        )
        current = _stats_from_states(
            current_second,
            states,
            self._current_selected_bssid,
            self._latest_local_cu_percent,
            second_frames,
            self._current_retry_bssid,
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
            top_station_bssid=_top_station_bssid(states),
            top_retry_bssid=self._current_retry_bssid,
            retry_bssids=retry_states,
        )

    def _states_at(
        self,
        *,
        reference_timestamp: float,
        upper_exclusive: Optional[float],
        frames: tuple[FrameRecord, ...],
    ) -> tuple[BssidState, ...]:
        cutoff = reference_timestamp - self.window_seconds
        window_entries_by_bssid: dict[
            str, tuple[tuple[float, int, BeaconRecord], ...]
        ] = {}
        for bssid, entries in self._records_by_bssid.items():
            window_entries = tuple(
                entry
                for entry in entries
                if entry[0] >= cutoff
                and (upper_exclusive is None or entry[0] < upper_exclusive)
            )
            if window_entries:
                window_entries_by_bssid[bssid] = window_entries
        frames_by_bssid = _frames_by_bssid(
            frames,
            tuple(window_entries_by_bssid),
        )
        states: list[BssidState] = []
        for bssid, window_entries in window_entries_by_bssid.items():
            latest = window_entries[-1][2]
            rssi_values = tuple(
                entry[2].rssi_dbm
                for entry in window_entries
                if entry[2].rssi_dbm is not None
            )
            bssid_frames = tuple(frames_by_bssid.get(bssid, ()))
            retry_observations = tuple(
                frame
                for frame in bssid_frames
                if frame.retry_flag is not None
            )
            retry_eligible_frames = tuple(
                frame for frame in bssid_frames if frame.retry_eligible
            )
            retry_frame_count = sum(
                frame.retry_flag is True for frame in retry_eligible_frames
            )
            states.append(
                BssidState(
                    bssid=bssid,
                    ssid=latest.ssid,
                    last_seen_ts=max(
                        latest.timestamp,
                        max(
                            (frame.timestamp for frame in bssid_frames),
                            default=latest.timestamp,
                        ),
                    ),
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
                    window_frame_count=len(bssid_frames),
                    window_retry_observed_frame_count=len(retry_observations),
                    window_retry_frame_count=retry_frame_count,
                    window_retry_percent=_percentage(
                        retry_frame_count,
                        len(retry_eligible_frames),
                    ),
                    window_beacon_rate_percent=_beacon_rate_percent(
                        beacon_count=len(window_entries),
                        beacon_interval_tu=latest.beacon_interval_tu,
                        reference_timestamp=reference_timestamp,
                        window_seconds=self.window_seconds,
                        observation_start_timestamp=(
                            min(
                                window_entries[0][0],
                                min(
                                    (
                                        frame.timestamp
                                        for frame in bssid_frames
                                    ),
                                    default=window_entries[0][0],
                                ),
                            )
                        ),
                    ),
                )
            )
        return tuple(sorted(states, key=lambda state: state.bssid))

    def _frames_at(
        self,
        *,
        reference_timestamp: float,
        upper_exclusive: Optional[float],
    ) -> tuple[FrameRecord, ...]:
        cutoff = reference_timestamp - self.window_seconds
        return tuple(
            entry[2]
            for entry in self._frames
            if entry[0] >= cutoff
            and (upper_exclusive is None or entry[0] < upper_exclusive)
        )

    def _expire_records(self, reference_timestamp: float) -> None:
        cutoff = reference_timestamp - self.window_seconds
        first_retained_frame = 0
        while (
            first_retained_frame < len(self._frames)
            and self._frames[first_retained_frame][0] < cutoff
        ):
            first_retained_frame += 1
        if first_retained_frame:
            del self._frames[:first_retained_frame]

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


def _top_station_bssid(states: tuple[BssidState, ...]) -> Optional[str]:
    candidates = tuple(
        state for state in states if state.latest_station_count is not None
    )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda state: (
            -(state.latest_station_count or 0),
            state.bssid,
        ),
    ).bssid


def select_retry_bssid(
    retry_states: tuple[RetryBssidState, ...],
    beacon_states: tuple[BssidState, ...],
    current_bssid: Optional[str],
) -> Optional[str]:
    """Select the retry footer source without using frame timing."""
    candidates = tuple(
        state
        for state in retry_states
        if state.window_retry_frame_count > 0
        and state.window_retry_percent is not None
    )
    if not candidates:
        return _strongest_signal_bssid(beacon_states, current_bssid)

    highest = max(
        candidates,
        key=lambda state: (
            state.window_retry_frame_count
            / state.retry_eligible_frame_count
        ),
    )
    finalists = tuple(
        state
        for state in candidates
        if (
            state.window_retry_frame_count
            * highest.retry_eligible_frame_count
            == highest.window_retry_frame_count
            * state.retry_eligible_frame_count
        )
    )
    if current_bssid is not None and any(
        state.bssid == current_bssid for state in finalists
    ):
        return current_bssid

    return min(
        finalists,
        key=lambda state: (
            -(state.rssi_dbm if state.rssi_dbm is not None else -200),
            state.bssid,
        ),
    ).bssid


def _strongest_signal_bssid(
    states: tuple[BssidState, ...],
    current_bssid: Optional[str],
) -> Optional[str]:
    if not states:
        return None
    with_rssi = tuple(
        state for state in states if state.peak_rssi_dbm is not None
    )
    if not with_rssi:
        if current_bssid is not None and any(
            state.bssid == current_bssid for state in states
        ):
            return current_bssid
        return min(state.bssid for state in states)

    strongest_rssi = max(
        state.peak_rssi_dbm
        for state in with_rssi
        if state.peak_rssi_dbm is not None
    )
    finalists = tuple(
        state for state in with_rssi if state.peak_rssi_dbm == strongest_rssi
    )
    if current_bssid is not None and any(
        state.bssid == current_bssid for state in finalists
    ):
        return current_bssid
    return min(state.bssid for state in finalists)


def _stats_from_states(
    second: int,
    states: tuple[BssidState, ...],
    selected_bssid: Optional[str],
    local_cu_percent: Optional[float],
    frames: tuple[FrameRecord, ...],
    retry_bssid: Optional[str],
) -> SecondStats:
    selected = next(
        (state for state in states if state.bssid == selected_bssid),
        None,
    )
    retry_observations = tuple(
        frame for frame in frames if frame.retry_flag is not None
    )
    retry_eligible_frames = tuple(
        frame for frame in frames if frame.retry_eligible
    )
    retry_frame_count = sum(
        frame.retry_flag is True for frame in retry_eligible_frames
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
        selected_qbss_admission_capacity=(
            selected.latest_admission_capacity if selected is not None else None
        ),
        received_frame_count=len(frames),
        retry_observed_frame_count=len(retry_observations),
        retry_eligible_frame_count=len(retry_eligible_frames),
        retry_frame_count=retry_frame_count,
        retry_percent=_percentage(
            retry_frame_count,
            len(retry_eligible_frames),
        ),
        selected_beacon_rate_percent=(
            selected.window_beacon_rate_percent
            if selected is not None
            else None
        ),
        top_retry_bssid=retry_bssid,
    )


def _retry_states_from_frames(
    frames: tuple[FrameRecord, ...],
    beacon_states: tuple[BssidState, ...],
) -> tuple[RetryBssidState, ...]:
    beacon_by_bssid = {state.bssid: state for state in beacon_states}
    frames_by_bssid = _frames_by_bssid(frames, tuple(beacon_by_bssid))

    states: list[RetryBssidState] = []
    for bssid in sorted(set(beacon_by_bssid) | set(frames_by_bssid)):
        bssid_frames = frames_by_bssid.get(bssid, [])
        retry_observations = tuple(
            frame for frame in bssid_frames if frame.retry_flag is not None
        )
        retry_eligible_frames = tuple(
            frame for frame in bssid_frames if frame.retry_eligible
        )
        retry_frame_count = sum(
            frame.retry_flag is True for frame in retry_eligible_frames
        )
        beacon_state = beacon_by_bssid.get(bssid)
        states.append(
            RetryBssidState(
                bssid=bssid,
                ssid=beacon_state.ssid if beacon_state is not None else None,
                rssi_dbm=(
                    beacon_state.peak_rssi_dbm
                    if beacon_state is not None
                    else None
                ),
                window_frame_count=len(bssid_frames),
                window_retry_observed_frame_count=len(retry_observations),
                retry_eligible_frame_count=len(retry_eligible_frames),
                window_retry_frame_count=retry_frame_count,
                window_retry_percent=_percentage(
                    retry_frame_count,
                    len(retry_eligible_frames),
                ),
            )
        )
    return tuple(sorted(states, key=lambda state: state.bssid))


def _frames_for_second(
    frames: tuple[FrameRecord, ...],
    second: int,
) -> tuple[FrameRecord, ...]:
    return tuple(
        frame
        for frame in frames
        if second <= frame.timestamp < second + 1
    )


def _frames_by_bssid(
    frames: tuple[FrameRecord, ...],
    known_bssids: tuple[str, ...],
) -> dict[str, list[FrameRecord]]:
    grouped: dict[str, list[FrameRecord]] = {}
    known_by_address = {
        _canonical_address(bssid): bssid for bssid in known_bssids
    }
    for frame in frames:
        bssid = _associated_bssid(frame, known_by_address)
        if bssid is not None:
            grouped.setdefault(bssid, []).append(frame)
    return grouped


def associate_frame_bssid(
    frame: FrameRecord,
    known_bssids: tuple[str, ...],
) -> Optional[str]:
    """Associate a frame using BSSID/TA/RA/SA/DA address matches."""
    known_by_address = {
        _canonical_address(bssid): bssid for bssid in known_bssids
    }
    return _associated_bssid(frame, known_by_address)


def _associated_bssid(
    frame: FrameRecord,
    known_by_address: dict[str, str],
) -> Optional[str]:
    for address in frame.mac_addresses:
        known_bssid = known_by_address.get(_canonical_address(address))
        if known_bssid is not None:
            return known_bssid
    return frame.bssid


def _canonical_address(address: str) -> str:
    return address.strip().lower()


def _percentage(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator * 100


def _beacon_rate_percent(
    *,
    beacon_count: int,
    beacon_interval_tu: Optional[int],
    reference_timestamp: float,
    window_seconds: int,
    observation_start_timestamp: Optional[float],
) -> Optional[float]:
    if beacon_interval_tu is None or observation_start_timestamp is None:
        return None
    observation_start = max(
        reference_timestamp - window_seconds,
        observation_start_timestamp,
    )
    duration_seconds = reference_timestamp - observation_start
    interval_seconds = beacon_interval_tu * 0.001024
    if duration_seconds <= 0 or interval_seconds <= 0:
        return None
    expected_beacons = duration_seconds / interval_seconds
    if expected_beacons <= 0:
        return None
    return min(100.0, beacon_count / expected_beacons * 100)
