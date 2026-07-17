import pytest

from beacon_live.analyzer import Analyzer
from beacon_live.models import BeaconRecord
from beacon_live.models import QBSS_ADMISSION_CAPACITY_MAX
from beacon_live.screen_manager import ScreenManager
from beacon_live.screens import ADMISSION_CAPACITY_SCREEN_ID
from beacon_live.screens import AdmissionCapacityScreen
from beacon_live.screens import CU_SCREEN_ID
from beacon_live.screens import TOTAL_STATION_COUNT_SCREEN_ID
from beacon_live.screens import TotalStationCountScreen


def test_screen_navigation_wraps_and_debounces_without_changing_snapshot() -> None:
    analyzer = _analyzer_with_history()
    snapshot = analyzer.snapshot
    manager = ScreenManager(snapshot=snapshot, debounce_seconds=0.2)

    assert manager.active_screen_id == CU_SCREEN_ID
    assert manager.navigate_down(now=10.0)
    assert manager.active_screen_id == ADMISSION_CAPACITY_SCREEN_ID
    assert not manager.navigate_down(now=10.1)
    assert manager.active_screen_id == ADMISSION_CAPACITY_SCREEN_ID
    assert manager.navigate_down(now=10.3)
    assert manager.active_screen_id == TOTAL_STATION_COUNT_SCREEN_ID
    assert manager.navigate_down(now=10.6)
    assert manager.active_screen_id == CU_SCREEN_ID
    assert manager.navigate_up(now=10.9)
    assert manager.active_screen_id == TOTAL_STATION_COUNT_SCREEN_ID

    assert manager.snapshot is snapshot
    assert manager.snapshot.history is snapshot.history


def test_admission_capacity_uses_selected_bssid_and_shared_adc_history() -> None:
    snapshot = _analyzer_with_history().snapshot

    view = AdmissionCapacityScreen().render(snapshot)

    assert view.title == "Admission Capacity"
    assert view.summary == "ADC 80% AVG 45% MIN 10%"
    assert "CU" not in view.summary
    assert view.graph_label == "Selected ADC"
    assert view.graph_maximum == 100
    assert view.identity.bssid == "aa"
    assert [point.second for point in view.graph_points] == [1000, 1001]
    assert [point.value for point in view.graph_points] == pytest.approx(
        [10.0, 80.0], abs=0.01
    )


def test_total_station_count_uses_shared_history_and_highest_station_bssid() -> None:
    snapshot = _analyzer_with_history().snapshot

    view = TotalStationCountScreen().render(snapshot)

    assert view.title == "Total Station Count"
    assert view.summary == "SUM 15 AVG 9 MAX 15"
    assert view.metadata_tokens == ("BSS", "2", "TOP", "10")
    assert view.graph_maximum == 100
    assert snapshot.selected_bssid == "aa"
    assert view.identity.ssid == "Bravo"
    assert view.identity.bssid == "bb"
    assert [point.second for point in view.graph_points] == [1000, 1001]
    assert [point.value for point in view.graph_points] == [3, 15]


def test_total_station_graph_keeps_fixed_scale_above_100() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(
            1000.1,
            ssid="Crowded",
            bssid="aa",
            cu_raw=64,
            station_count=150,
            admission_capacity=32768,
            rssi_dbm=-40,
        )
    )
    analyzer.advance(1001, None)

    view = TotalStationCountScreen().render(analyzer.snapshot)

    assert view.graph_maximum == 100
    assert [point.value for point in view.graph_points] == [150]


def test_analyzer_and_history_continue_while_another_screen_is_active() -> None:
    analyzer = _analyzer_with_history()
    analyzer_identity = id(analyzer)
    manager = ScreenManager(snapshot=analyzer.snapshot, debounce_seconds=0)
    manager.set_active_index(2)
    original_seconds = tuple(row.second for row in manager.snapshot.history)

    analyzer.ingest(
        _record(
            1002.1,
            ssid="Alpha",
            bssid="aa",
            cu_raw=191,
            station_count=6,
            admission_capacity=49151,
            rssi_dbm=-40,
        )
    )
    analyzer.advance(1003, None)
    manager.update(analyzer.snapshot)

    assert id(analyzer) == analyzer_identity
    assert manager.active_screen_id == TOTAL_STATION_COUNT_SCREEN_ID
    assert tuple(row.second for row in manager.snapshot.history) == (
        *original_seconds,
        1002,
    )


def _analyzer_with_history() -> Analyzer:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(
            1000.1,
            ssid="Alpha",
            bssid="aa",
            cu_raw=64,
            station_count=3,
            admission_capacity=round(QBSS_ADMISSION_CAPACITY_MAX * 0.10),
            rssi_dbm=-40,
        )
    )
    analyzer.advance(1001, None)
    analyzer.ingest(
        _record(
            1001.1,
            ssid="Alpha",
            bssid="aa",
            cu_raw=128,
            station_count=5,
            admission_capacity=round(QBSS_ADMISSION_CAPACITY_MAX * 0.80),
            rssi_dbm=-40,
        )
    )
    analyzer.ingest(
        _record(
            1001.2,
            ssid="Bravo",
            bssid="bb",
            cu_raw=32,
            station_count=10,
            admission_capacity=round(QBSS_ADMISSION_CAPACITY_MAX * 0.75),
            rssi_dbm=-60,
        )
    )
    analyzer.advance(1002, None)
    return analyzer


def _record(
    timestamp: float,
    *,
    ssid: str,
    bssid: str,
    cu_raw: int,
    station_count: int,
    admission_capacity: int,
    rssi_dbm: int,
) -> BeaconRecord:
    return BeaconRecord(
        timestamp=timestamp,
        ssid=ssid,
        bssid=bssid,
        qbss_cu_raw=cu_raw,
        qbss_cu_percent=cu_raw / 255 * 100,
        qbss_station_count=station_count,
        qbss_admission_capacity=admission_capacity,
        rssi_dbm=rssi_dbm,
    )
