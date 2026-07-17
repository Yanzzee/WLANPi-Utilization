from dataclasses import FrozenInstanceError
from typing import Optional

import pytest

from beacon_live.analyzer import Analyzer
from beacon_live.analyzer import select_bssid
from beacon_live.models import BeaconRecord


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

    snapshot = analyzer.snapshot
    state = snapshot.state_for("aa")

    assert [row.second for row in snapshot.history] == list(range(1, 121))
    assert state is not None
    assert state.window_beacon_count == 120

    analyzer.advance(242, None)

    assert analyzer.snapshot.bssids == ()
    assert analyzer.snapshot.selected_bssid is None


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

    analyzer.advance(1001, None)
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

    analyzer.advance(1001, 12.5)
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
    )
