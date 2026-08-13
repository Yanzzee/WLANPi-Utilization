from dataclasses import FrozenInstanceError
from typing import Optional

import pytest

from beacon_live.analyzer import Analyzer
from beacon_live.analyzer import _beacon_reception_counts
from beacon_live.analyzer import select_bssid
from beacon_live.channel import ChannelDefinition
from beacon_live.channel import ChannelWidth
from beacon_live.models import BeaconRecord
from beacon_live.models import FrameRecord


def test_future_beacons_do_not_recompute_completed_frame_projections() -> None:
    analyzer = Analyzer(target_primary_frequency_mhz=5180)

    def beacon(timestamp: float) -> FrameRecord:
        return FrameRecord(
            timestamp=timestamp,
            bssid="aa:aa:aa:aa:aa:aa",
            frame_type=0,
            frame_subtype=8,
            retry_flag=False,
            receiver_address="ff:ff:ff:ff:ff:ff",
            channel_definition=ChannelDefinition(
                5180,
                ChannelWidth.MHZ80,
                5210,
                primary_channel=36,
            ),
        )

    analyzer.ingest(beacon(1000.0), publish_snapshot=False)
    analyzer.ingest(
        _frame(
            1000.2,
            "aa:aa:aa:aa:aa:aa",
            retry=True,
            receiver_address="00:11:22:33:44:55",
        ),
        publish_snapshot=False,
    )
    analyzer.ingest(beacon(1001.2), publish_snapshot=False)
    original = analyzer._frame_projections_by_second[1000][1]

    analyzer.ingest(beacon(1002.4), publish_snapshot=False)

    assert analyzer._frame_projections_by_second[1000][1] is original


def test_analyzer_expires_bssid_state_and_history_after_120_seconds() -> None:
    analyzer = Analyzer()

    for second in range(121):
        analyzer.ingest(
            _record(
                second + 0.1,
                bssid="aa",
                cu_percent=float(second % 100),
                station_count=second,
                rssi_dbm=-40,
            )
        )
        analyzer.advance(second + 1, None)
    analyzer.flush()

    snapshot = analyzer.snapshot
    state = snapshot.state_for("aa")

    assert [row.second for row in snapshot.history] == list(range(1, 121))
    assert state is not None
    assert state.window_beacon_count == 120

    analyzer.advance(242, None)

    assert analyzer.snapshot.bssids == ()
    assert analyzer.snapshot.selected_bssid is None
    assert analyzer.snapshot.composition.bssid_count == 0
    assert analyzer.snapshot.composition.estimated_radio_count == 0


def test_latest_beacon_wins_even_when_an_older_beacon_arrives_late() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(
            1000.8,
            bssid="aa",
            cu_percent=70.0,
            station_count=9,
            rssi_dbm=-60,
        )
    )
    analyzer.ingest(
        _record(
            1000.2,
            bssid="aa",
            cu_percent=10.0,
            station_count=1,
            rssi_dbm=-40,
        )
    )

    analyzer.advance(1002, None)
    state = analyzer.snapshot.state_for("aa")

    assert state is not None
    assert state.latest_beacon_ts == 1000.8
    assert state.latest_qbss_cu_percent == 70.0
    assert state.latest_station_count == 9
    assert state.latest_rssi_dbm == -60
    assert state.peak_rssi_dbm == -40
    assert analyzer.snapshot.history[-1].selected_qbss_cu_percent == 70.0


def test_bssid_selection_keeps_current_source_inside_hysteresis_band() -> None:
    analyzer = Analyzer(hysteresis_db=3)
    analyzer.ingest(
        _record(1000.1, bssid="aa", station_count=5, rssi_dbm=-50)
    )
    assert analyzer.snapshot.selected_bssid == "aa"

    analyzer.ingest(
        _record(1000.2, bssid="bb", station_count=5, rssi_dbm=-48)
    )
    assert analyzer.snapshot.selected_bssid == "aa"

    analyzer.ingest(
        _record(1000.3, bssid="cc", station_count=5, rssi_dbm=-46)
    )
    assert analyzer.snapshot.selected_bssid == "cc"


def test_near_equal_rssi_uses_latest_station_count_as_tie_breaker() -> None:
    analyzer = Analyzer(hysteresis_db=3)
    analyzer.ingest(
        _record(1000.1, bssid="aa", station_count=2, rssi_dbm=-50)
    )
    analyzer.ingest(
        _record(1000.2, bssid="bb", station_count=12, rssi_dbm=-49)
    )

    assert analyzer.snapshot.selected_bssid == "bb"
    assert analyzer.snapshot.current.selected_qbss_station_count == 12


def test_selection_does_not_use_beacon_timing_as_a_tie_breaker() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(1100.0, bssid="aa", station_count=5, rssi_dbm=-50)
    )
    analyzer.ingest(
        _record(1000.0, bssid="bb", station_count=5, rssi_dbm=-50)
    )

    assert select_bssid(analyzer.snapshot.bssids, None) == "bb"


def test_top_station_bssid_does_not_use_rssi_as_a_tie_breaker() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(1000.1, bssid="aa", station_count=10, rssi_dbm=-80)
    )
    analyzer.ingest(
        _record(1000.2, bssid="bb", station_count=10, rssi_dbm=-30)
    )

    assert analyzer.snapshot.selected_bssid == "bb"
    assert analyzer.snapshot.top_station_bssid == "aa"


def test_snapshot_contains_read_only_cu_screen_state_and_history() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(
            1000.1,
            ssid="Alpha",
            bssid="aa",
            cu_percent=25.0,
            cu_raw=64,
            station_count=7,
            admission_capacity=25000,
            rssi_dbm=-45,
        )
    )

    analyzer.advance(1002, 12.5)
    snapshot = analyzer.snapshot

    assert snapshot.selected_bssid == "aa"
    assert snapshot.selected is not None
    assert snapshot.selected.latest_beacon_record.ssid == "Alpha"
    assert snapshot.current.selected_qbss_cu_raw == 64
    assert snapshot.current.selected_qbss_station_count == 7
    assert snapshot.current.selected_qbss_admission_capacity == 25000
    assert snapshot.current.local_cu_percent == 12.5
    assert len(snapshot.history) == 1
    assert snapshot.history[0].selected_qbss_bssid == "aa"
    assert snapshot.history[0].selected_qbss_cu_percent == 25.0
    assert snapshot.history[0].selected_qbss_admission_capacity == 25000

    with pytest.raises(FrozenInstanceError):
        snapshot.selected_bssid = "bb"  # type: ignore[misc]


def test_composition_counts_all_beaconing_and_qbss_bssids_in_window() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        BeaconRecord(
            timestamp=1000.1,
            ssid=None,
            bssid="00:11:22:33:44:50",
            qbss_cu_raw=None,
            qbss_cu_percent=None,
            qbss_station_count=None,
            qbss_admission_capacity=None,
            rssi_dbm=-40,
        )
    )
    analyzer.ingest(
        BeaconRecord(
            timestamp=1000.2,
            ssid="QBSS",
            bssid="00:11:23:33:44:50",
            qbss_cu_raw=None,
            qbss_cu_percent=None,
            qbss_station_count=4,
            qbss_admission_capacity=None,
            rssi_dbm=-50,
        )
    )

    composition = analyzer.snapshot.composition

    assert composition.bssid_count == 2
    assert composition.qbss_bssid_count == 1


def test_composition_snapshot_selects_strongest_radio_and_rotates_members() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(
            1000.1,
            bssid="00:11:22:33:44:50",
            ssid="Alpha",
            station_count=4,
            rssi_dbm=-35,
            ap_name="Room-101",
            vendor="Example Wireless",
        )
    )
    analyzer.ingest(
        _record(
            1000.2,
            bssid="00:11:22:33:44:51",
            ssid=None,
            station_count=2,
            rssi_dbm=-37,
            ap_name="Room-101",
            vendor="Example Wireless",
        )
    )
    analyzer.ingest(
        _record(
            1000.3,
            bssid="00:11:23:aa:bb:cc",
            ssid="Weaker",
            station_count=1,
            rssi_dbm=-70,
        )
    )

    first = analyzer.snapshot.composition
    assert first.estimated_radio_count == 2
    assert first.strongest_radio_bssid_count == 2
    assert first.strongest_radio_ap_name == "Room-101"
    assert first.strongest_radio_vendor == "Example Wireless"
    assert first.displayed_ssid == "Alpha"
    assert first.displayed_bssid == "00:11:22:33:44:50"
    assert first.displayed_rssi_dbm == -35

    def refresh_strongest_radio(second: int) -> None:
        analyzer.ingest(
            _record(
                second + 0.1,
                bssid="00:11:22:33:44:50",
                ssid="Alpha",
                station_count=4,
                rssi_dbm=-35,
                ap_name="Room-101",
                vendor="Example Wireless",
            )
        )
        analyzer.ingest(
            _record(
                second + 0.2,
                bssid="00:11:22:33:44:51",
                ssid=None,
                station_count=2,
                rssi_dbm=-37,
                ap_name="Room-101",
                vendor="Example Wireless",
            )
        )

    refresh_strongest_radio(1001)
    analyzer.advance(1002, None)
    one_second_later = analyzer.snapshot.composition
    assert one_second_later.displayed_ssid == "Alpha"
    assert one_second_later.displayed_bssid == "00:11:22:33:44:50"

    refresh_strongest_radio(1002)
    analyzer.advance(1003, None)
    two_seconds_later = analyzer.snapshot.composition
    assert two_seconds_later.displayed_ssid is None
    assert two_seconds_later.displayed_bssid == "00:11:22:33:44:51"
    assert two_seconds_later.displayed_rssi_dbm == -37

    refresh_strongest_radio(1003)
    analyzer.advance(1004, None)
    assert (
        analyzer.snapshot.composition.displayed_bssid
        == "00:11:22:33:44:51"
    )

    refresh_strongest_radio(1004)
    analyzer.advance(1005, None)
    assert (
        analyzer.snapshot.composition.displayed_bssid
        == "00:11:22:33:44:50"
    )


def test_composition_rotation_restarts_when_strongest_radio_changes() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(
            1000.1,
            bssid="00:11:22:33:44:50",
            ssid="Alpha",
            rssi_dbm=-40,
        )
    )
    analyzer.ingest(
        _record(
            1001.1,
            bssid="00:11:23:33:44:50",
            ssid="New Strongest",
            rssi_dbm=-20,
        )
    )

    composition = analyzer.snapshot.composition
    assert composition.strongest_radio_bssid_count == 1
    assert composition.displayed_ssid == "New Strongest"
    assert composition.displayed_bssid == "00:11:23:33:44:50"


def test_retry_percentage_uses_eligible_frames_and_tracks_top_bssid() -> None:
    analyzer = Analyzer()
    analyzer.ingest(_beacon_frame(1000.0, "aa", "Alpha", -35, retry=False))
    analyzer.ingest(_frame(1000.1, "aa", retry=True))
    analyzer.ingest(_frame(1000.2, "aa", retry=False))
    analyzer.ingest(_beacon_frame(1000.3, "bb", "Bravo", -60, retry=False))
    analyzer.ingest(_frame(1000.4, "bb", retry=True))
    analyzer.ingest(_frame(1000.5, "bb", retry=True))
    analyzer.advance(1001, None)

    snapshot = analyzer.snapshot
    alpha = snapshot.state_for("aa")
    bravo = snapshot.state_for("bb")

    assert snapshot.current.received_frame_count == 6
    assert snapshot.current.retry_observed_frame_count == 6
    assert snapshot.current.retry_eligible_frame_count == 4
    assert snapshot.current.retry_frame_count == 3
    assert snapshot.current.retry_percent == pytest.approx(75.0)
    assert alpha is not None
    assert alpha.window_retry_percent == pytest.approx(50.0)
    assert bravo is not None
    assert bravo.window_retry_percent == pytest.approx(100.0)
    assert snapshot.top_retry_bssid == "bb"
    assert snapshot.current.top_retry_bssid == "bb"
    assert snapshot.selected_bssid == "aa"


def test_retry_samples_use_independent_one_second_windows() -> None:
    analyzer = Analyzer(window_seconds=2)
    analyzer.ingest(_beacon_frame(1000.0, "aa", "Alpha", -40, retry=False))
    analyzer.ingest(_frame(1000.1, "aa", retry=True))
    analyzer.ingest(_frame(1000.2, "aa", retry=False))
    analyzer.advance(1001, None)

    assert analyzer.snapshot.current.retry_percent == pytest.approx(50.0)

    analyzer.ingest(_beacon_frame(1002.0, "aa", "Alpha", -40, retry=False))
    analyzer.ingest(_frame(1002.1, "aa", retry=False))
    analyzer.flush()

    assert analyzer.snapshot.current.received_frame_count == 2
    assert analyzer.snapshot.current.retry_eligible_frame_count == 1
    assert analyzer.snapshot.current.retry_percent == 0.0
    assert analyzer.snapshot.top_retry_bssid == "aa"
    retry_state = analyzer.snapshot.retry_state_for("aa")
    assert retry_state is not None
    assert retry_state.window_retry_percent == 0.0
    assert [row.second for row in analyzer.snapshot.history] == [1002]


def test_capture_publication_waits_until_all_rows_for_second_are_ingested() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _frame(1000.1, "aa", retry=False), publish_snapshot=False
    )

    # A wall-clock refresh while TShark is still emitting second 1000 must not
    # make the partial retry bucket immutable.
    assert analyzer.publish_capture_complete(None) == []
    analyzer.ingest(
        _frame(1000.9, "aa", retry=True), publish_snapshot=False
    )
    assert analyzer.publish_capture_complete(None) == []

    # Crossing the wall-clock boundary alone is not enough because a beacon
    # scheduled in second 1000 may arrive up to 102.4 ms late.
    analyzer.ingest(
        _frame(1001.0, "aa", retry=False), publish_snapshot=False
    )
    assert analyzer.publish_capture_complete(None) == []

    # Once the ordered capture watermark passes the beacon-delay grace, the
    # second is immutable for both retry and beacon-loss accounting.
    analyzer.ingest(
        _frame(1001.11, "aa", retry=False), publish_snapshot=False
    )
    published = analyzer.publish_capture_complete(None)

    assert [row.second for row in published] == [1000]
    assert published[0].retry_eligible_frame_count == 2
    assert published[0].retry_frame_count == 1
    assert published[0].retry_percent == pytest.approx(50.0)
    assert analyzer.snapshot.current.second == 1000
    assert analyzer.snapshot.current.retry_percent == pytest.approx(50.0)


def test_selected_beacon_rate_uses_advertised_interval_when_available() -> None:
    analyzer = Analyzer()
    for index in range(5):
        analyzer.ingest(
            _beacon_frame(
                1000.0 + index * 0.1024,
                "aa",
                "Alpha",
                -40,
                retry=False,
                beacon_interval_tu=100,
            )
        )
    analyzer.advance(1001, None)

    assert analyzer.snapshot.current.selected_beacon_rate_percent == pytest.approx(
        51.2
    )


def test_beacon_reception_uses_all_bssids_on_strongest_radio_including_hidden() -> None:
    analyzer = Analyzer()
    visible = "00:11:22:33:44:50"
    hidden = "00:11:22:33:44:51"
    weaker = "00:11:23:33:44:50"
    records = [
        _radio_beacon(999.95, visible, "Alpha", -35, "Room-101"),
        _radio_beacon(999.95, hidden, None, -37, "Room-101"),
        _radio_beacon(999.95, weaker, "Weaker", -65, "Room-202"),
    ]
    for slot in range(1, 11):
        timestamp = 999.95 + slot * 0.1024
        records.append(
            _radio_beacon(timestamp, visible, "Alpha", -35, "Room-101")
        )
        if slot != 5:
            records.append(
                _radio_beacon(timestamp, hidden, None, -37, "Room-101")
            )
        records.append(
            _radio_beacon(timestamp, weaker, "Weaker", -65, "Room-202")
        )

    for record in sorted(records, key=lambda item: item.timestamp):
        analyzer.ingest(record)
    analyzer.advance(1001, None)

    beacons = analyzer.snapshot.beacons
    assert beacons.strongest_radio_bssids == (visible, hidden)
    assert tuple(member.bssid for member in beacons.bssids) == (visible, hidden)
    assert [(member.received_count, member.expected_count) for member in beacons.bssids] == [
        (10, 10),
        (9, 10),
    ]
    assert [member.loss_percent for member in beacons.bssids] == pytest.approx(
        [0.0, 10.0]
    )
    assert beacons.received_count == 19
    assert beacons.expected_count == 20
    assert beacons.loss_percent == pytest.approx(5.0)
    assert analyzer.snapshot.current.beacon_loss_percent == pytest.approx(5.0)


def test_beacon_reception_lookback_allows_nine_or_ten_expected_and_counts_drop() -> None:
    first_window = tuple(
        [999.99]
        + [999.99 + slot * 0.1024 for slot in range(1, 10) if slot != 5]
    )
    second_window = tuple(
        [1000.9116]
        + [1000.9116 + slot * 0.1024 for slot in range(1, 11)]
    )

    assert _beacon_reception_counts(
        first_window,
        interval_start=1000.0,
        interval_end=1001.0,
    ) == (8, 9)
    assert _beacon_reception_counts(
        second_window,
        interval_start=1001.0,
        interval_end=1002.0,
    ) == (10, 10)


def test_missed_and_delayed_beacon_do_not_shift_later_tbtt_count() -> None:
    interval = 0.1024
    timestamps = []
    for slot in range(23):
        if slot == 5:
            continue
        timestamp = 999.95 + slot * interval
        if slot == 8:
            timestamp += 0.05
        timestamps.append(timestamp)

    assert _beacon_reception_counts(
        tuple(timestamps),
        interval_start=1000.0,
        interval_end=1001.0,
    ) == (9, 10)
    assert _beacon_reception_counts(
        tuple(timestamps),
        interval_start=1001.0,
        interval_end=1002.0,
    ) == (10, 10)


def test_inactive_strongest_radio_cannot_pin_beacon_received_at_zero() -> None:
    analyzer = Analyzer()
    stale_bssid = "00:11:22:33:44:50"
    active_bssid = "00:11:23:33:44:50"
    records = [
        _radio_beacon(
            1000.05,
            stale_bssid,
            "Transient",
            -20,
            "Room-101",
        )
    ]
    records.extend(
        _radio_beacon(
            1000.02 + slot * 0.1024,
            active_bssid,
            "Active",
            -40,
            "Room-202",
        )
        for slot in range(35)
    )

    for record in sorted(records, key=lambda item: item.timestamp):
        analyzer.ingest(record, publish_snapshot=False)
    analyzer.flush()

    history = {row.second: row for row in analyzer.snapshot.history}
    assert history[1001].beacon_received_count > 0
    assert history[1001].beacon_expected_count in (9, 10)
    assert analyzer.snapshot.beacons.strongest_radio_bssids == (active_bssid,)
    assert analyzer.snapshot.beacons.received_count > 0


def test_strongest_radio_stays_eligible_for_one_completely_missed_second() -> None:
    analyzer = Analyzer()
    bssid = "00:11:22:33:44:50"
    analyzer.ingest(
        _radio_beacon(1000.95, bssid, "Alpha", -20, "Room-101"),
        publish_snapshot=False,
    )
    for timestamp in (1001.5, 1002.5, 1003.2):
        analyzer.ingest(
            _frame(timestamp, bssid, retry=False),
            publish_snapshot=False,
        )
    analyzer.flush()

    history = {row.second: row for row in analyzer.snapshot.history}
    assert history[1001].beacon_received_count == 0
    assert history[1001].beacon_expected_count == 10
    assert history[1001].beacon_loss_percent == 100.0
    assert history[1002].beacon_expected_count == 0
    assert history[1002].beacon_loss_percent is None


def test_beacon_delayed_across_second_boundary_is_credited_within_grace() -> None:
    bssid = "00:11:22:33:44:50"
    analyzer = Analyzer()
    scheduled = [1000.05 + slot * 0.1024 for slot in range(10)]

    for timestamp in scheduled[:-1]:
        analyzer.ingest(
            _radio_beacon(timestamp, bssid, "Alpha", -35, "Room-101"),
            publish_snapshot=False,
        )

    analyzer.advance(1001, None)
    assert analyzer.snapshot.history == ()
    analyzer.ingest(
        _radio_beacon(
            scheduled[-1] + 0.1024,
            bssid,
            "Alpha",
            -35,
            "Room-101",
        ),
        publish_snapshot=False,
    )
    assert analyzer.publish_capture_complete(None) == []
    analyzer.ingest(
        _frame(1001.1024, bssid, retry=False),
        publish_snapshot=False,
    )
    assert analyzer.publish_capture_complete(None) == []
    analyzer.ingest(
        _frame(1001.11, bssid, retry=False),
        publish_snapshot=False,
    )
    published = analyzer.publish_capture_complete(None)

    assert [row.second for row in published] == [1000]
    assert published[0].beacon_received_count == 10
    assert published[0].beacon_expected_count == 10
    assert published[0].beacon_loss_percent == 0.0


def test_beacon_delayed_beyond_one_interval_does_not_fill_previous_slot() -> None:
    scheduled = [1000.05 + slot * 0.1024 for slot in range(10)]
    timestamps = tuple([*scheduled[:-1], scheduled[-1] + 0.1025])

    assert _beacon_reception_counts(
        timestamps,
        interval_start=1000.0,
        interval_end=1001.0,
    ) == (9, 10)


def test_missing_retry_flag_degrades_to_unavailable() -> None:
    analyzer = Analyzer()
    analyzer.ingest(_beacon_frame(1000.0, "aa", "Alpha", -40, retry=None))
    analyzer.advance(1001, None)

    assert analyzer.snapshot.current.received_frame_count == 1
    assert analyzer.snapshot.current.retry_observed_frame_count == 0
    assert analyzer.snapshot.current.retry_eligible_frame_count == 0
    assert analyzer.snapshot.current.retry_percent is None


def test_retry_percentage_excludes_frames_that_cannot_be_retried() -> None:
    analyzer = Analyzer()
    analyzer.ingest(_beacon_frame(1000.0, "aa", "Alpha", -40, retry=False))
    analyzer.ingest(
        _frame(
            1000.1,
            "aa",
            retry=True,
            receiver_address="ff:ff:ff:ff:ff:ff",
        )
    )
    analyzer.ingest(
        _frame(1000.2, "aa", retry=True, frame_type=1)
    )
    analyzer.ingest(_frame(1000.3, "aa", retry=True))
    analyzer.ingest(_frame(1000.4, "aa", retry=False))
    analyzer.ingest(_frame(1000.5, "aa", retry=None))
    analyzer.advance(1001, None)

    snapshot = analyzer.snapshot
    retry_state = snapshot.retry_state_for("aa")
    assert snapshot.current.received_frame_count == 6
    assert snapshot.current.retry_observed_frame_count == 5
    assert snapshot.current.retry_eligible_frame_count == 2
    assert snapshot.current.retry_percent == pytest.approx(50.0)
    assert retry_state is not None
    assert retry_state.retry_eligible_frame_count == 2
    assert retry_state.window_retry_percent == pytest.approx(50.0)


def test_multicast_destination_is_excluded_even_with_unicast_receiver() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _address_frame(
            1000.1,
            retry=True,
            receiver_address="00:11:22:33:44:55",
            destination_address="01:00:5e:00:00:01",
        )
    )
    analyzer.advance(1001, None)

    assert analyzer.snapshot.current.retry_eligible_frame_count == 0
    assert analyzer.snapshot.current.retry_percent is None


def test_each_retry_copy_of_the_same_frame_is_counted() -> None:
    analyzer = Analyzer()
    analyzer.ingest(_frame(1000.1, "aa", retry=False))
    analyzer.ingest(_frame(1000.2, "aa", retry=True))
    analyzer.ingest(_frame(1000.3, "aa", retry=True))
    analyzer.advance(1001, None)

    assert analyzer.snapshot.current.retry_eligible_frame_count == 3
    assert analyzer.snapshot.current.retry_frame_count == 2
    assert analyzer.snapshot.current.retry_percent == pytest.approx(2 / 3 * 100)


def test_frames_associate_when_bssid_appears_in_any_mac_address_field() -> None:
    analyzer = Analyzer()
    bssid = "aa:bb:cc:dd:ee:ff"
    analyzer.ingest(_beacon_frame(1000.0, bssid, "Alpha", -40, retry=False))
    analyzer.ingest(
        _address_frame(1000.1, retry=True, transmitter_address=bssid)
    )
    analyzer.ingest(
        _address_frame(1000.2, retry=False, receiver_address=bssid)
    )
    analyzer.ingest(_address_frame(1000.3, retry=True, source_address=bssid))
    analyzer.ingest(
        _address_frame(1000.4, retry=False, destination_address=bssid)
    )
    analyzer.advance(1001, None)

    retry_state = analyzer.snapshot.retry_state_for(bssid)
    assert retry_state is not None
    assert retry_state.retry_eligible_frame_count == 4
    assert retry_state.window_retry_frame_count == 2
    assert retry_state.window_retry_percent == pytest.approx(50.0)


def test_cached_window_reassociates_older_frame_when_bssid_becomes_known() -> None:
    analyzer = Analyzer()
    bssid = "aa:bb:cc:dd:ee:ff"
    analyzer.ingest(
        _address_frame(1000.2, retry=True, transmitter_address=bssid),
        publish_snapshot=False,
    )
    analyzer.ingest(
        _beacon_frame(1001.0, bssid, "Alpha", -40, retry=False),
        publish_snapshot=False,
    )
    analyzer.ingest(
        _frame(1002.2, bssid, retry=False),
        publish_snapshot=False,
    )
    analyzer.publish_capture_complete(None)

    state = analyzer.snapshot.state_for(bssid)
    assert state is not None
    assert state.window_frame_count == 2
    assert state.window_retry_frame_count == 1


def test_top_retry_bssid_can_be_derived_before_its_beacon_is_seen() -> None:
    analyzer = Analyzer()
    analyzer.ingest(_beacon_frame(1000.0, "aa", "Alpha", -40, retry=False))
    analyzer.ingest(
        FrameRecord(
            timestamp=1000.1,
            bssid="cc",
            rssi_dbm=-55,
            frame_type=2,
            frame_subtype=0,
            retry_flag=True,
        )
    )
    analyzer.advance(1001, None)

    retry_state = analyzer.snapshot.retry_state_for("cc")
    assert analyzer.snapshot.top_retry_bssid == "cc"
    assert retry_state is not None
    assert retry_state.ssid is None
    assert retry_state.rssi_dbm is None


def test_top_retry_bssid_tie_keeps_the_previously_displayed_bssid() -> None:
    analyzer = Analyzer()
    analyzer.ingest(_beacon_frame(1000.0, "aa", "Alpha", -40, retry=False))
    analyzer.ingest(_beacon_frame(1000.1, "bb", "Bravo", -35, retry=False))
    analyzer.ingest(_frame(1000.2, "aa", retry=True))
    analyzer.ingest(_frame(1000.3, "bb", retry=True))
    analyzer.ingest(_frame(1000.4, "bb", retry=False))
    analyzer.advance(1001, None)

    assert analyzer.snapshot.top_retry_bssid == "aa"

    analyzer.ingest(
        _frame(1001.1, "aa", retry=True), publish_snapshot=False
    )
    analyzer.ingest(
        _frame(1001.2, "aa", retry=False), publish_snapshot=False
    )
    analyzer.ingest(
        _frame(1001.8, "bb", retry=True), publish_snapshot=False
    )
    analyzer.ingest(
        _frame(1001.9, "bb", retry=False), publish_snapshot=False
    )
    analyzer.advance(1002, None)

    assert analyzer.snapshot.top_retry_bssid == "aa"


def test_no_retries_selects_the_strongest_beacon_for_footer() -> None:
    analyzer = Analyzer()
    analyzer.ingest(_beacon_frame(1000.0, "aa", "Alpha", -35, retry=False))
    analyzer.ingest(_beacon_frame(1000.1, "bb", "Bravo", -60, retry=False))
    analyzer.ingest(_frame(1000.2, "aa", retry=False))
    analyzer.ingest(_frame(1000.3, "bb", retry=False))
    analyzer.advance(1001, None)

    assert analyzer.snapshot.current.retry_percent == 0.0
    assert analyzer.snapshot.top_retry_bssid == "aa"


def test_unique_client_macs_count_data_endpoints_per_second_and_window() -> None:
    bssid = "02:00:00:00:00:01"
    client_a = "02:00:00:00:10:01"
    client_b = "02:00:00:00:10:02"
    analyzer = Analyzer()
    analyzer.ingest(_beacon_frame(1000.0, bssid, "Alpha", -40, retry=False))
    analyzer.ingest(_client_data_frame(1000.1, bssid, client_a, from_ap=False))
    analyzer.ingest(_client_data_frame(1000.2, bssid, client_a, from_ap=True))
    analyzer.ingest(_client_data_frame(1000.3, bssid, client_b, from_ap=True))
    analyzer.advance(1001, None)

    assert analyzer.snapshot.current.unique_client_mac_count == 2
    assert analyzer.snapshot.window_unique_client_mac_count == 2

    analyzer.ingest(_client_data_frame(1001.1, bssid, client_a, from_ap=False))
    analyzer.flush()

    assert analyzer.snapshot.history[-1].unique_client_mac_count == 1
    assert analyzer.snapshot.window_unique_client_mac_count == 2


def test_unique_client_macs_exclude_management_group_and_unlinked_frames() -> None:
    bssid = "02:00:00:00:00:01"
    other_bssid = "02:00:00:00:00:02"
    client = "02:00:00:00:10:01"
    analyzer = Analyzer()
    analyzer.ingest(_beacon_frame(1000.0, bssid, "Alpha", -40, retry=False))
    analyzer.ingest(
        FrameRecord(
            timestamp=1000.1,
            bssid=bssid,
            frame_type=0,
            frame_subtype=11,
            transmitter_address=client,
            receiver_address=bssid,
        )
    )
    analyzer.ingest(
        FrameRecord(
            timestamp=1000.2,
            bssid=bssid,
            frame_type=2,
            frame_subtype=0,
            transmitter_address=bssid,
            receiver_address="ff:ff:ff:ff:ff:ff",
        )
    )
    analyzer.ingest(_client_data_frame(1000.3, other_bssid, client, from_ap=False))
    analyzer.ingest(
        FrameRecord(
            timestamp=1000.4,
            bssid=bssid,
            frame_type=2,
            frame_subtype=0,
            transmitter_address="02:00:00:00:20:01",
            receiver_address="02:00:00:00:20:02",
        )
    )
    analyzer.flush()

    assert analyzer.snapshot.history[-1].unique_client_mac_count == 0
    assert analyzer.snapshot.window_unique_client_mac_count == 0


def test_unique_client_mac_window_expires_old_clients() -> None:
    bssid = "02:00:00:00:00:01"
    analyzer = Analyzer(window_seconds=2)
    analyzer.ingest(_beacon_frame(1000.0, bssid, "Alpha", -40, retry=False))
    analyzer.ingest(
        _client_data_frame(
            1000.1,
            bssid,
            "02:00:00:00:10:01",
            from_ap=False,
        )
    )
    analyzer.advance(1001, None)
    analyzer.ingest(_beacon_frame(1001.5, bssid, "Alpha", -40, retry=False))
    analyzer.ingest(
        _client_data_frame(
            1001.6,
            bssid,
            "02:00:00:00:10:02",
            from_ap=False,
        )
    )
    analyzer.advance(1002, None)

    assert analyzer.snapshot.window_unique_client_mac_count == 2

    analyzer.advance(1003, None)

    assert analyzer.snapshot.window_unique_client_mac_count == 1


def test_high_load_buckets_keep_exact_retry_and_client_counts() -> None:
    analyzer = Analyzer()
    base_second = 1000
    second_count = 6
    frames_per_second = 1000
    bssid = "02:00:00:00:00:01"
    client = "02:00:00:00:10:01"

    for offset in range(second_count):
        second = base_second + offset
        analyzer.ingest(
            _beacon_frame(second, bssid, "Alpha", -40, retry=False),
            publish_snapshot=False,
        )
        for index in range(frames_per_second):
            analyzer.ingest(
                FrameRecord(
                    timestamp=second
                    + (index + 1) / (frames_per_second + 2),
                    bssid=bssid,
                    frame_type=2,
                    frame_subtype=0,
                    retry_flag=index % 5 == 0,
                    transmitter_address=client,
                    receiver_address=bssid,
                    source_address=client,
                    destination_address=bssid,
                ),
                publish_snapshot=False,
            )

    analyzer.ingest(
        _frame(
            base_second + second_count + 0.2,
            bssid,
            retry=False,
        ),
        publish_snapshot=False,
    )
    published = analyzer.publish_capture_complete(None)

    assert [row.second for row in published] == list(
        range(base_second, base_second + second_count)
    )
    assert all(
        row.received_frame_count == frames_per_second + 1
        and row.retry_eligible_frame_count == frames_per_second
        and row.retry_frame_count == frames_per_second // 5
        and row.retry_percent == pytest.approx(20.0)
        and row.unique_client_mac_count == 1
        for row in published
    )
    assert analyzer.snapshot.current == published[-1]
    assert analyzer.snapshot.window_unique_client_mac_count == 1


def _record(
    timestamp: float,
    *,
    bssid: str,
    ssid: Optional[str] = "Test",
    cu_percent: Optional[float] = 20.0,
    cu_raw: Optional[int] = None,
    station_count: Optional[int] = 1,
    admission_capacity: Optional[int] = 0,
    rssi_dbm: Optional[int] = -50,
    ap_name: Optional[str] = None,
    vendor: Optional[str] = None,
) -> BeaconRecord:
    return BeaconRecord(
        timestamp=timestamp,
        ssid=ssid,
        bssid=bssid,
        qbss_cu_raw=cu_raw,
        qbss_cu_percent=cu_percent,
        qbss_station_count=station_count,
        qbss_admission_capacity=admission_capacity,
        rssi_dbm=rssi_dbm,
        ap_name=ap_name,
        vendor=vendor,
    )


def _frame(
    timestamp: float,
    bssid: str,
    *,
    retry: Optional[bool],
    frame_type: int = 2,
    receiver_address: Optional[str] = None,
) -> FrameRecord:
    return FrameRecord(
        timestamp=timestamp,
        bssid=bssid,
        frame_type=frame_type,
        frame_subtype=0,
        retry_flag=retry,
        receiver_address=receiver_address,
    )


def _address_frame(
    timestamp: float,
    *,
    retry: bool,
    transmitter_address: Optional[str] = None,
    receiver_address: Optional[str] = None,
    source_address: Optional[str] = None,
    destination_address: Optional[str] = None,
) -> FrameRecord:
    return FrameRecord(
        timestamp=timestamp,
        frame_type=2,
        frame_subtype=0,
        retry_flag=retry,
        transmitter_address=transmitter_address,
        receiver_address=receiver_address,
        source_address=source_address,
        destination_address=destination_address,
    )


def _beacon_frame(
    timestamp: float,
    bssid: str,
    ssid: str,
    rssi_dbm: int,
    *,
    retry: Optional[bool],
    beacon_interval_tu: int = 100,
) -> FrameRecord:
    return FrameRecord(
        timestamp=timestamp,
        bssid=bssid,
        ssid=ssid,
        rssi_dbm=rssi_dbm,
        frame_type=0,
        frame_subtype=8,
        retry_flag=retry,
        beacon_interval_tu=beacon_interval_tu,
        qbss_cu_raw=64,
        qbss_cu_percent=64 / 255 * 100,
        qbss_station_count=3,
        qbss_admission_capacity=10_000,
    )


def _radio_beacon(
    timestamp: float,
    bssid: str,
    ssid: Optional[str],
    rssi_dbm: int,
    ap_name: str,
) -> BeaconRecord:
    return BeaconRecord(
        timestamp=timestamp,
        ssid=ssid,
        bssid=bssid,
        qbss_cu_raw=None,
        qbss_cu_percent=None,
        qbss_station_count=None,
        qbss_admission_capacity=None,
        rssi_dbm=rssi_dbm,
        beacon_interval_tu=200,
        ap_name=ap_name,
    )


def _client_data_frame(
    timestamp: float,
    bssid: str,
    client: str,
    *,
    from_ap: bool,
) -> FrameRecord:
    return FrameRecord(
        timestamp=timestamp,
        bssid=bssid,
        frame_type=2,
        frame_subtype=0,
        retry_flag=False,
        transmitter_address=bssid if from_ap else client,
        receiver_address=client if from_ap else bssid,
        source_address=bssid if from_ap else client,
        destination_address=client if from_ap else bssid,
    )
