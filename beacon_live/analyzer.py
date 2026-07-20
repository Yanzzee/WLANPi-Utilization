"""Single-pass rolling frame analysis and immutable snapshot publication."""

from __future__ import annotations

from bisect import bisect_left
from bisect import insort
from dataclasses import dataclass
from dataclasses import replace
from functools import lru_cache
from math import ceil
from math import floor
from typing import Optional, Union

from beacon_live.models import BeaconRecord
from beacon_live.models import BeaconBssidReception
from beacon_live.models import BeaconReceptionSnapshot
from beacon_live.models import BssidState
from beacon_live.models import CompositionSnapshot
from beacon_live.models import FrameRecord
from beacon_live.models import MetricsSnapshot
from beacon_live.models import RetryBssidState
from beacon_live.models import SecondStats
from beacon_live.radio_grouping import estimate_radio_groups
from beacon_live.radio_grouping import select_strongest_radio

DEFAULT_WINDOW_SECONDS = 120
DEFAULT_RSSI_HYSTERESIS_DB = 3
COMPOSITION_ROTATION_SECONDS = 2
ASSUMED_BEACON_INTERVAL_SECONDS = 0.1024


@dataclass
class _BssidFrameSummary:
    frame_count: int = 0
    retry_observed_count: int = 0
    retry_eligible_count: int = 0
    retry_count: int = 0
    first_timestamp: Optional[float] = None
    last_timestamp: Optional[float] = None


@dataclass(frozen=True)
class _FrameProjection:
    """One-pass derived values for an immutable tuple of frames."""

    frame_count: int
    retry_observed_count: int
    retry_eligible_count: int
    retry_count: int
    by_bssid: dict[str, _BssidFrameSummary]
    unique_client_macs: frozenset[str]


@dataclass(frozen=True)
class _CompletedProjection:
    """Reusable analysis for the second most recently made immutable."""

    second: int
    states: tuple[BssidState, ...]
    second_frames: _FrameProjection
    retry_states: tuple[RetryBssidState, ...]
    window_unique_client_mac_count: int


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
        self._composition_rotation_second: Optional[int] = None
        self._strongest_radio_bssids: tuple[str, ...] = ()
        self._history_strongest_radio_bssids: tuple[str, ...] = ()
        self._composition_display_bssid: Optional[str] = None
        self._frame_projections_by_second: dict[
            int, tuple[tuple[str, ...], _FrameProjection]
        ] = {}
        self._completed_projection: Optional[_CompletedProjection] = None
        self._snapshot = MetricsSnapshot.empty(window_seconds=window_seconds)

    @property
    def snapshot(self) -> MetricsSnapshot:
        """Return the latest immutable metrics snapshot."""
        return self._snapshot

    @property
    def has_ready_stats(self) -> bool:
        """Whether an ordered capture watermark completed at least one second."""
        return bool(self._ready_stats)

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
        self._finalize_pending_after_beacon_grace(record.timestamp)

        if self._is_expired_late_record(record.timestamp):
            return

        if (
            self._completed_projection is not None
            and record.timestamp < self._completed_projection.second + 1
        ):
            # Preserve support for callers that insert an older, still-retained
            # record after a second was completed. Ordered live capture does not
            # take this path, but the cached projection must never hide it.
            self._completed_projection = None

        self._sequence += 1
        beacon = record if isinstance(record, BeaconRecord) else record.beacon_record()
        if isinstance(record, FrameRecord):
            self._frame_projections_by_second.pop(record_second, None)
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
        self._finalize_pending_after_beacon_grace(float(second))
        return self._publish_ready(before_second=second)

    def advance(
        self,
        wall_second: int,
        local_cu_percent: Optional[float],
        *,
        include_history: bool = True,
    ) -> list[SecondStats]:
        """Advance analysis without closing a delayed-beacon grace period."""
        self._latest_local_cu_percent = local_cu_percent
        self._finalize_pending_after_beacon_grace(float(wall_second))
        current_second = wall_second - 1

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
        latest_finalizable_second = floor(
            wall_second - 1 - ASSUMED_BEACON_INTERVAL_SECONDS
        )
        self._prune_completed_seconds(latest_finalizable_second)
        self._refresh_current_snapshot(
            reference_timestamp=float(wall_second),
            current_second=current_second,
            upper_exclusive=float(wall_second),
        )
        return published

    def publish_capture_complete(
        self,
        local_cu_percent: Optional[float],
        *,
        include_history: bool = True,
        capture_ended: bool = False,
    ) -> list[SecondStats]:
        """Publish seconds completed by the ordered capture stream.

        TShark can have many rows buffered when a wall-clock refresh fires. A
        second is therefore complete only after ordered capture time passes
        its boundary plus the delayed-beacon allowance. This prevents live
        publication from making retry or beacon-loss data immutable while
        older rows are still being drained from TShark stdout.
        """
        self._latest_local_cu_percent = local_cu_percent
        if self._reference_timestamp is None:
            return []

        capture_second = int(self._reference_timestamp)
        if capture_ended:
            # EOF proves stdout is fully drained even when capture stopped
            # during the 102.4 ms delayed-beacon grace period. Keep the final
            # partial capture second pending, matching normal live behavior.
            self._finalize_pending_before(capture_second)
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

    def _finalize_pending_after_beacon_grace(
        self,
        reference_timestamp: float,
    ) -> None:
        """Close seconds only after their delayed-beacon grace has passed."""
        if not self._pending_seconds:
            return
        tolerance = 1e-9
        earliest_pending = min(self._pending_seconds)
        if (
            reference_timestamp
            - (earliest_pending + 1 + ASSUMED_BEACON_INTERVAL_SECONDS)
            <= tolerance
        ):
            return
        for pending_second in sorted(
            value
            for value in self._pending_seconds
            if reference_timestamp
            - (value + 1 + ASSUMED_BEACON_INTERVAL_SECONDS)
            > tolerance
        ):
            self._complete_second(pending_second)

    def _complete_second(self, second: int) -> None:
        if second in self._completed_seconds:
            self._pending_seconds.discard(second)
            return

        states, window_frames = self._states_at(
            reference_timestamp=float(second + 1),
            upper_exclusive=float(second + 1),
            frames=(),
            cache_by_second=True,
        )
        second_frames = self._frame_projection_for_second(
            second,
            tuple(state.bssid for state in states),
        )
        retry_states = _retry_states_from_projection(second_frames, states)
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
        strongest_radio = select_strongest_radio(
            estimate_radio_groups(states),
            self._history_strongest_radio_bssids,
        )
        strongest_radio_bssids = (
            strongest_radio.bssids if strongest_radio is not None else ()
        )
        self._history_strongest_radio_bssids = strongest_radio_bssids
        beacon_reception = self._beacon_reception_snapshot(
            second=second,
            interval_end=float(second + 1),
            strongest_radio_bssids=strongest_radio_bssids,
        )
        self._ready_stats.append(
            _stats_from_states(
                second,
                states,
                selected_bssid,
                self._local_cu_by_second.get(second),
                second_frames,
                self._history_retry_bssid,
                beacon_reception,
            )
        )
        self._completed_projection = _CompletedProjection(
            second=second,
            states=states,
            second_frames=second_frames,
            retry_states=retry_states,
            window_unique_client_mac_count=len(window_frames.unique_client_macs),
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
        completed_projection = self._completed_projection
        reuse_completed = bool(
            completed_projection is not None
            and completed_projection.second == current_second
            and reference_timestamp == float(current_second + 1)
            and upper_exclusive == float(current_second + 1)
        )
        if reuse_completed:
            assert completed_projection is not None
            states = completed_projection.states
            second_frames = completed_projection.second_frames
            retry_states = completed_projection.retry_states
            window_unique_client_mac_count = (
                completed_projection.window_unique_client_mac_count
            )
        else:
            frames = self._frames_at(
                reference_timestamp=reference_timestamp,
                upper_exclusive=upper_exclusive,
            )
            states, window_frames = self._states_at(
                reference_timestamp=reference_timestamp,
                upper_exclusive=upper_exclusive,
                frames=frames,
            )
            second_frames = _project_frames(
                self._frames_between(
                    float(current_second),
                    (
                        upper_exclusive
                        if upper_exclusive is not None
                        else float(current_second + 1)
                    ),
                ),
                tuple(state.bssid for state in states),
            )
            retry_states = _retry_states_from_projection(second_frames, states)
            window_unique_client_mac_count = len(
                window_frames.unique_client_macs
            )
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
        composition = self._composition_snapshot(states, current_second)
        interval_end = min(
            float(current_second + 1),
            (
                upper_exclusive
                if upper_exclusive is not None
                else reference_timestamp + 1e-9
            ),
        )
        beacon_reception = self._beacon_reception_snapshot(
            second=current_second,
            interval_end=interval_end,
            strongest_radio_bssids=(
                composition.strongest_radio_bssids
            ),
            displayed_ssid=composition.displayed_ssid,
            displayed_bssid=composition.displayed_bssid,
            displayed_rssi_dbm=composition.displayed_rssi_dbm,
        )
        current = _stats_from_states(
            current_second,
            states,
            self._current_selected_bssid,
            self._latest_local_cu_percent,
            second_frames,
            self._current_retry_bssid,
            beacon_reception,
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
            composition=composition,
            window_unique_client_mac_count=window_unique_client_mac_count,
            beacons=beacon_reception,
        )

    def _beacon_reception_snapshot(
        self,
        *,
        second: int,
        interval_end: float,
        strongest_radio_bssids: tuple[str, ...],
        displayed_ssid: Optional[str] = None,
        displayed_bssid: Optional[str] = None,
        displayed_rssi_dbm: Optional[int] = None,
    ) -> BeaconReceptionSnapshot:
        members: list[BeaconBssidReception] = []
        for bssid in strongest_radio_bssids:
            timestamps = tuple(
                entry[0]
                for entry in self._records_by_bssid.get(bssid, ())
            )
            received, expected = _beacon_reception_counts(
                timestamps,
                interval_start=float(second),
                interval_end=interval_end,
            )
            members.append(
                BeaconBssidReception(
                    bssid=bssid,
                    received_count=received,
                    expected_count=expected,
                    loss_percent=_beacon_loss_percent(received, expected),
                )
            )

        received_count = sum(member.received_count for member in members)
        expected_count = sum(member.expected_count for member in members)
        return BeaconReceptionSnapshot(
            strongest_radio_bssids=strongest_radio_bssids,
            bssids=tuple(members),
            received_count=received_count,
            expected_count=expected_count,
            loss_percent=_beacon_loss_percent(
                received_count,
                expected_count,
            ),
            displayed_ssid=displayed_ssid,
            displayed_bssid=displayed_bssid,
            displayed_rssi_dbm=displayed_rssi_dbm,
        )

    def _composition_snapshot(
        self,
        states: tuple[BssidState, ...],
        current_second: int,
    ) -> CompositionSnapshot:
        groups = estimate_radio_groups(states)
        rotation_second = self._composition_rotation_second
        previous_radio_bssids = self._strongest_radio_bssids

        strongest = select_strongest_radio(
            groups,
            previous_radio_bssids,
        )

        if strongest is None:
            self._composition_rotation_second = None
            self._strongest_radio_bssids = ()
            self._composition_display_bssid = None
            return CompositionSnapshot(
                bssid_count=len(states),
                qbss_bssid_count=sum(
                    state.advertises_qbss for state in states
                ),
                estimated_radio_count=len(groups),
                strongest_radio_bssid_count=0,
                strongest_radio_bssids=(),
                strongest_radio_ap_name=None,
                strongest_radio_vendor=None,
                displayed_ssid=None,
                displayed_bssid=None,
                displayed_rssi_dbm=None,
            )

        radio_unchanged = bool(
            set(previous_radio_bssids).intersection(strongest.bssids)
        )
        displayed_bssid = self._composition_display_bssid
        if not radio_unchanged or rotation_second is None:
            displayed_bssid = strongest.bssids[0]
            rotation_second = current_second
        elif displayed_bssid not in strongest.bssids:
            displayed_bssid = strongest.bssids[0]
            rotation_second = current_second
        elif rotation_second < current_second:
            elapsed_seconds = current_second - rotation_second
            rotation_steps = elapsed_seconds // COMPOSITION_ROTATION_SECONDS
            if rotation_steps:
                previous_index = strongest.bssids.index(displayed_bssid)
                displayed_bssid = strongest.bssids[
                    (previous_index + rotation_steps) % len(strongest.bssids)
                ]
                rotation_second += (
                    rotation_steps * COMPOSITION_ROTATION_SECONDS
                )

        if displayed_bssid is None:
            displayed_bssid = strongest.bssids[0]
            rotation_second = current_second

        displayed_state = next(
            member
            for member in strongest.members
            if member.bssid == displayed_bssid
        )
        self._composition_rotation_second = rotation_second
        self._strongest_radio_bssids = strongest.bssids
        self._composition_display_bssid = displayed_bssid
        return CompositionSnapshot(
            bssid_count=len(states),
            qbss_bssid_count=sum(
                state.advertises_qbss for state in states
            ),
            estimated_radio_count=len(groups),
            strongest_radio_bssid_count=len(strongest.members),
            strongest_radio_bssids=strongest.bssids,
            strongest_radio_ap_name=strongest.ap_name,
            strongest_radio_vendor=strongest.vendor,
            displayed_ssid=displayed_state.ssid,
            displayed_bssid=displayed_state.bssid,
            displayed_rssi_dbm=(
                displayed_state.peak_rssi_dbm
                if displayed_state.peak_rssi_dbm is not None
                else displayed_state.latest_rssi_dbm
            ),
        )

    def _states_at(
        self,
        *,
        reference_timestamp: float,
        upper_exclusive: Optional[float],
        frames: tuple[FrameRecord, ...],
        cache_by_second: bool = False,
    ) -> tuple[tuple[BssidState, ...], _FrameProjection]:
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
        known_bssids = tuple(window_entries_by_bssid)
        frame_projection = (
            self._rolling_frame_projection(
                int(reference_timestamp) - 1,
                known_bssids,
            )
            if cache_by_second
            else _project_frames(frames, known_bssids)
        )
        states: list[BssidState] = []
        for bssid, window_entries in window_entries_by_bssid.items():
            latest = window_entries[-1][2]
            rssi_values = tuple(
                entry[2].rssi_dbm
                for entry in window_entries
                if entry[2].rssi_dbm is not None
            )
            frame_summary = frame_projection.by_bssid.get(bssid)
            frame_count = frame_summary.frame_count if frame_summary else 0
            retry_observed_count = (
                frame_summary.retry_observed_count if frame_summary else 0
            )
            retry_eligible_count = (
                frame_summary.retry_eligible_count if frame_summary else 0
            )
            retry_frame_count = frame_summary.retry_count if frame_summary else 0
            states.append(
                BssidState(
                    bssid=bssid,
                    ssid=latest.ssid,
                    last_seen_ts=max(
                        latest.timestamp,
                        (
                            frame_summary.last_timestamp
                            if frame_summary is not None
                            and frame_summary.last_timestamp is not None
                            else latest.timestamp
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
                    latest_ap_name=latest.ap_name,
                    latest_vendor=latest.vendor,
                    window_frame_count=frame_count,
                    window_retry_observed_frame_count=retry_observed_count,
                    window_retry_frame_count=retry_frame_count,
                    window_retry_percent=_percentage(
                        retry_frame_count,
                        retry_eligible_count,
                    ),
                    window_beacon_rate_percent=_beacon_rate_percent(
                        beacon_count=len(window_entries),
                        beacon_interval_tu=latest.beacon_interval_tu,
                        reference_timestamp=reference_timestamp,
                        window_seconds=self.window_seconds,
                        observation_start_timestamp=(
                            min(
                                window_entries[0][0],
                                (
                                    frame_summary.first_timestamp
                                    if frame_summary is not None
                                    and frame_summary.first_timestamp is not None
                                    else window_entries[0][0]
                                ),
                            )
                        ),
                    ),
                )
            )
        return (
            tuple(sorted(states, key=lambda state: state.bssid)),
            frame_projection,
        )

    def _frames_at(
        self,
        *,
        reference_timestamp: float,
        upper_exclusive: Optional[float],
    ) -> tuple[FrameRecord, ...]:
        cutoff = reference_timestamp - self.window_seconds
        return self._frames_between(cutoff, upper_exclusive)

    def _frames_between(
        self,
        lower_inclusive: float,
        upper_exclusive: Optional[float],
    ) -> tuple[FrameRecord, ...]:
        """Slice ordered frame storage without scanning unrelated seconds."""
        start = bisect_left(self._frames, (lower_inclusive, -1))
        end = (
            len(self._frames)
            if upper_exclusive is None
            else bisect_left(self._frames, (upper_exclusive, -1))
        )
        return tuple(entry[2] for entry in self._frames[start:end])

    def _frame_projection_for_second(
        self,
        second: int,
        known_bssids: tuple[str, ...],
    ) -> _FrameProjection:
        cache_key = tuple(sorted(known_bssids))
        cached = self._frame_projections_by_second.get(second)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        projection = _project_frames(
            self._frames_between(float(second), float(second + 1)),
            cache_key,
        )
        self._frame_projections_by_second[second] = (cache_key, projection)
        return projection

    def _rolling_frame_projection(
        self,
        end_second: int,
        known_bssids: tuple[str, ...],
    ) -> _FrameProjection:
        first_second = end_second - self.window_seconds + 1
        return _merge_frame_projections(
            tuple(
                self._frame_projection_for_second(second, known_bssids)
                for second in range(first_second, end_second + 1)
            )
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

        earliest_cached_second = floor(cutoff)
        for second in tuple(self._frame_projections_by_second):
            if second < earliest_cached_second:
                del self._frame_projections_by_second[second]

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
    frames: _FrameProjection,
    retry_bssid: Optional[str],
    beacon_reception: Optional[BeaconReceptionSnapshot] = None,
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
        selected_qbss_admission_capacity=(
            selected.latest_admission_capacity if selected is not None else None
        ),
        received_frame_count=frames.frame_count,
        retry_observed_frame_count=frames.retry_observed_count,
        retry_eligible_frame_count=frames.retry_eligible_count,
        retry_frame_count=frames.retry_count,
        retry_percent=_percentage(
            frames.retry_count,
            frames.retry_eligible_count,
        ),
        selected_beacon_rate_percent=(
            selected.window_beacon_rate_percent
            if selected is not None
            else None
        ),
        top_retry_bssid=retry_bssid,
        unique_client_mac_count=len(frames.unique_client_macs),
        beacon_received_count=(
            beacon_reception.received_count
            if beacon_reception is not None
            else 0
        ),
        beacon_expected_count=(
            beacon_reception.expected_count
            if beacon_reception is not None
            else 0
        ),
        beacon_loss_percent=(
            beacon_reception.loss_percent
            if beacon_reception is not None
            else None
        ),
    )


def _merge_frame_projections(
    projections: tuple[_FrameProjection, ...],
) -> _FrameProjection:
    frame_count = 0
    retry_observed_count = 0
    retry_eligible_count = 0
    retry_count = 0
    by_bssid: dict[str, _BssidFrameSummary] = {}
    clients: set[str] = set()

    for projection in projections:
        frame_count += projection.frame_count
        retry_observed_count += projection.retry_observed_count
        retry_eligible_count += projection.retry_eligible_count
        retry_count += projection.retry_count
        clients.update(projection.unique_client_macs)
        for bssid, source in projection.by_bssid.items():
            target = by_bssid.get(bssid)
            if target is None:
                target = _BssidFrameSummary()
                by_bssid[bssid] = target
            target.frame_count += source.frame_count
            target.retry_observed_count += source.retry_observed_count
            target.retry_eligible_count += source.retry_eligible_count
            target.retry_count += source.retry_count
            if source.first_timestamp is not None and (
                target.first_timestamp is None
                or source.first_timestamp < target.first_timestamp
            ):
                target.first_timestamp = source.first_timestamp
            if source.last_timestamp is not None and (
                target.last_timestamp is None
                or source.last_timestamp > target.last_timestamp
            ):
                target.last_timestamp = source.last_timestamp

    return _FrameProjection(
        frame_count=frame_count,
        retry_observed_count=retry_observed_count,
        retry_eligible_count=retry_eligible_count,
        retry_count=retry_count,
        by_bssid=by_bssid,
        unique_client_macs=frozenset(clients),
    )


def _project_frames(
    frames: tuple[FrameRecord, ...],
    known_bssids: tuple[str, ...],
) -> _FrameProjection:
    """Derive retry, association, and client metrics in one frame pass."""
    known_by_address = {
        _canonical_address(bssid): bssid for bssid in known_bssids
    }
    canonical_bssids = set(known_by_address)
    retry_observed_count = 0
    retry_eligible_count = 0
    retry_count = 0
    by_bssid: dict[str, _BssidFrameSummary] = {}
    clients: set[str] = set()

    for frame in frames:
        retry_observed = frame.retry_flag is not None
        retry_eligible = frame.retry_eligible
        is_retry = retry_eligible and frame.retry_flag is True
        retry_observed_count += retry_observed
        retry_eligible_count += retry_eligible
        retry_count += is_retry

        associated_bssid = _associated_bssid(frame, known_by_address)
        if associated_bssid is not None:
            summary = by_bssid.get(associated_bssid)
            if summary is None:
                summary = _BssidFrameSummary()
                by_bssid[associated_bssid] = summary
            summary.frame_count += 1
            summary.retry_observed_count += retry_observed
            summary.retry_eligible_count += retry_eligible
            summary.retry_count += is_retry
            if (
                summary.first_timestamp is None
                or frame.timestamp < summary.first_timestamp
            ):
                summary.first_timestamp = frame.timestamp
            if (
                summary.last_timestamp is None
                or frame.timestamp > summary.last_timestamp
            ):
                summary.last_timestamp = frame.timestamp

        if frame.frame_type != 2:
            continue

        # Only TA/RA link endpoints count as observed wireless clients. SA/DA
        # can identify hosts behind a distribution system.
        frame_bssid = _known_frame_bssid(frame, canonical_bssids)
        if frame_bssid is None:
            continue

        link_addresses = tuple(
            _canonical_address(address)
            for address in (
                frame.transmitter_address,
                frame.receiver_address,
            )
            if address is not None
        )
        if frame_bssid not in link_addresses:
            continue

        for address in link_addresses:
            if (
                address != frame_bssid
                and address not in canonical_bssids
                and _is_unicast_mac(address)
            ):
                clients.add(address)
    return _FrameProjection(
        frame_count=len(frames),
        retry_observed_count=retry_observed_count,
        retry_eligible_count=retry_eligible_count,
        retry_count=retry_count,
        by_bssid=by_bssid,
        unique_client_macs=frozenset(clients),
    )


def _known_frame_bssid(
    frame: FrameRecord,
    known_bssids: set[str],
) -> Optional[str]:
    for address in frame.mac_addresses:
        canonical = _canonical_address(address)
        if canonical in known_bssids:
            return canonical
    return None


@lru_cache(maxsize=16_384)
def _is_unicast_mac(address: str) -> bool:
    octets = address.split(":")
    if len(octets) != 6 or any(len(octet) != 2 for octet in octets):
        return False
    try:
        values = tuple(int(octet, 16) for octet in octets)
    except ValueError:
        return False
    return any(values) and not bool(values[0] & 1)


def _retry_states_from_projection(
    frames: _FrameProjection,
    beacon_states: tuple[BssidState, ...],
) -> tuple[RetryBssidState, ...]:
    beacon_by_bssid = {state.bssid: state for state in beacon_states}

    states: list[RetryBssidState] = []
    for bssid in sorted(set(beacon_by_bssid) | set(frames.by_bssid)):
        summary = frames.by_bssid.get(bssid)
        if summary is None:
            summary = _BssidFrameSummary()
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
                window_frame_count=summary.frame_count,
                window_retry_observed_frame_count=summary.retry_observed_count,
                retry_eligible_frame_count=summary.retry_eligible_count,
                window_retry_frame_count=summary.retry_count,
                window_retry_percent=_percentage(
                    summary.retry_count,
                    summary.retry_eligible_count,
                ),
            )
        )
    return tuple(sorted(states, key=lambda state: state.bssid))


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


@lru_cache(maxsize=32_768)
def _canonical_address(address: str) -> str:
    return address.strip().lower()


def _percentage(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator * 100


def _beacon_loss_percent(
    received_count: int,
    expected_count: int,
) -> Optional[float]:
    if expected_count <= 0:
        return None
    missing_count = max(0, expected_count - received_count)
    return missing_count / expected_count * 100


def _beacon_reception_counts(
    timestamps: tuple[float, ...],
    *,
    interval_start: float,
    interval_end: float,
    beacon_interval_seconds: float = ASSUMED_BEACON_INTERVAL_SECONDS,
) -> tuple[int, int]:
    """Count expected slots and slots received within one interval of delay.

    Capture timestamps do not expose the beacon's scheduled transmit time. We
    therefore infer the latest schedule requiring the fewest missing slots
    while allowing every observation to arrive from zero through one complete
    beacon interval late. This lets an observation after ``interval_end``
    satisfy a slot before it, without counting that observation twice.
    """
    if beacon_interval_seconds <= 0:
        raise ValueError("beacon_interval_seconds must be greater than zero")
    if interval_end <= interval_start:
        return (0, 0)

    tolerance = 1e-9
    observations = tuple(
        timestamp
        for timestamp in sorted(timestamps)
        if timestamp < interval_end + beacon_interval_seconds + tolerance
    )
    if not observations:
        return (0, 0)

    # The first observation is logical slot zero. Its actual schedule can be
    # anywhere from one interval before its capture time through that time.
    phase_low = observations[0] - beacon_interval_seconds
    phase_high = observations[0]
    assigned_slots = [0]
    previous_slot = 0

    for timestamp in observations[1:]:
        first_candidate = previous_slot + 1
        last_candidate = floor(
            (timestamp - phase_low) / beacon_interval_seconds + tolerance
        )
        for candidate in range(first_candidate, last_candidate + 1):
            candidate_phase_low = max(
                phase_low,
                timestamp - (candidate + 1) * beacon_interval_seconds,
            )
            candidate_phase_high = min(
                phase_high,
                timestamp - candidate * beacon_interval_seconds,
            )
            if candidate_phase_low <= candidate_phase_high + tolerance:
                phase_low = candidate_phase_low
                phase_high = candidate_phase_high
                assigned_slots.append(candidate)
                previous_slot = candidate
                break

    # The latest feasible phase minimizes inferred delay without changing the
    # already minimized number of missing logical slots.
    phase = phase_high
    first_expected_slot = max(
        0,
        ceil(
            (interval_start - phase) / beacon_interval_seconds - tolerance
        ),
    )
    end_expected_slot = max(
        first_expected_slot,
        ceil((interval_end - phase) / beacon_interval_seconds - tolerance),
    )
    expected_count = end_expected_slot - first_expected_slot
    received_count = sum(
        first_expected_slot <= slot < end_expected_slot
        for slot in assigned_slots
    )
    return received_count, expected_count


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
