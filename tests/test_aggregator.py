import pytest
from typing import Optional

from beacon_live.aggregator import Aggregator, aggregate_records
from beacon_live.models import BeaconRecord


def test_aggregator_selects_latest_qbss_from_strongest_rssi_bssid() -> None:
    aggregator = Aggregator()

    assert aggregator.add(_record(1000.1, "Alpha", "aa", 10.0, 2, -40)) == []
    assert aggregator.add(_record(1000.2, "Bravo", "bb", 50.0, 3, -50)) == []
    assert aggregator.add(_record(1000.9, "Alpha", "aa", 20.0, 4, -55)) == []

    completed = aggregator.add(_record(1001.0, "Charlie", "cc", 5.0, 1, -60))

    assert len(completed) == 1
    stats = completed[0]
    assert stats.second == 1000
    assert stats.unique_bssid_count == 2
    assert stats.qbss_station_count_sum == 7
    assert stats.selected_qbss_cu_percent == pytest.approx(20.0)
    assert stats.selected_qbss_ssid == "Alpha"
    assert stats.selected_qbss_bssid == "aa"
    assert stats.selected_qbss_rssi_dbm == -55
    assert stats.local_cu_percent is None


def test_strongest_rssi_selection_ignores_beacons_without_qbss() -> None:
    stats = aggregate_records(
        [
            _record(1000.1, "No QBSS", "aa", None, None, -20),
            _record(1000.2, "QBSS", "bb", 25.0, 3, -70),
        ]
    )[0]

    assert stats.selected_qbss_cu_percent == 25.0
    assert stats.selected_qbss_bssid == "bb"


def test_rssi_tie_prefers_latest_beacon_then_latest_from_selected_bssid() -> None:
    stats = aggregate_records(
        [
            _record(1000.1, "Alpha", "aa", 10.0, 1, -45),
            _record(1000.3, "Bravo", "bb", 20.0, 1, -45),
            _record(1000.8, "Bravo", "bb", 30.0, 1, -60),
        ]
    )[0]

    assert stats.selected_qbss_bssid == "bb"
    assert stats.selected_qbss_cu_percent == 30.0
    assert stats.selected_qbss_rssi_dbm == -60


def test_aggregate_records_returns_none_qbss_stats_when_no_cu_present() -> None:
    stats = aggregate_records(
        [
            _record(1000.1, "Alpha", "aa", None, None),
            _record(1000.2, "Bravo", "bb", None, 3),
        ],
        local_cu_by_second={1000: 12.5},
    )

    assert len(stats) == 1
    assert stats[0].unique_bssid_count == 2
    assert stats[0].qbss_station_count_sum == 3
    assert stats[0].selected_qbss_cu_percent is None
    assert stats[0].selected_qbss_ssid is None
    assert stats[0].selected_qbss_bssid is None
    assert stats[0].selected_qbss_rssi_dbm is None
    assert stats[0].local_cu_percent == pytest.approx(12.5)


def _record(
    timestamp: float,
    ssid: Optional[str],
    bssid: str,
    qbss_cu_percent: Optional[float],
    station_count: Optional[int],
    rssi_dbm: Optional[int] = None,
) -> BeaconRecord:
    return BeaconRecord(
        timestamp=timestamp,
        ssid=ssid,
        bssid=bssid,
        qbss_cu_raw=None,
        qbss_cu_percent=qbss_cu_percent,
        qbss_station_count=station_count,
        qbss_admission_capacity=None,
        rssi_dbm=rssi_dbm,
    )
