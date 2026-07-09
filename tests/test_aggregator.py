import pytest

from beacon_live.aggregator import Aggregator, aggregate_records
from beacon_live.models import BeaconRecord


def test_aggregator_emits_completed_second_when_later_second_arrives() -> None:
    aggregator = Aggregator()

    assert aggregator.add(_record(1000.1, "Alpha", "aa", 10.0, 2)) == []
    assert aggregator.add(_record(1000.2, "Bravo", "bb", 50.0, 3)) == []
    assert aggregator.add(_record(1000.9, "Alpha", "aa", 20.0, 4)) == []

    completed = aggregator.add(_record(1001.0, "Charlie", "cc", 5.0, 1))

    assert len(completed) == 1
    stats = completed[0]
    assert stats.second == 1000
    assert stats.unique_bssid_count == 2
    assert stats.qbss_station_count_sum == 7
    assert stats.qbss_cu_min_percent == pytest.approx(10.0)
    assert stats.qbss_cu_mean_percent == pytest.approx((10.0 + 50.0 + 20.0) / 3)
    assert stats.qbss_cu_max_percent == pytest.approx(50.0)
    assert stats.top_qbss_cu_ssid == "Bravo"
    assert stats.top_qbss_cu_bssid == "bb"
    assert stats.top_qbss_cu_percent == pytest.approx(50.0)
    assert stats.local_cu_percent is None


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
    assert stats[0].qbss_cu_min_percent is None
    assert stats[0].qbss_cu_mean_percent is None
    assert stats[0].qbss_cu_max_percent is None
    assert stats[0].top_qbss_cu_ssid is None
    assert stats[0].top_qbss_cu_bssid is None
    assert stats[0].top_qbss_cu_percent is None
    assert stats[0].local_cu_percent == pytest.approx(12.5)


def _record(
    timestamp: float,
    ssid: str | None,
    bssid: str,
    qbss_cu_percent: float | None,
    station_count: int | None,
) -> BeaconRecord:
    return BeaconRecord(
        timestamp=timestamp,
        ssid=ssid,
        bssid=bssid,
        qbss_cu_raw=None,
        qbss_cu_percent=qbss_cu_percent,
        qbss_station_count=station_count,
        qbss_admission_capacity=None,
    )
