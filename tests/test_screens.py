from typing import Optional

import pytest

from beacon_live.analyzer import Analyzer
from beacon_live.models import BeaconRecord
from beacon_live.models import FrameRecord
from beacon_live.models import QBSS_ADMISSION_CAPACITY_MAX
from beacon_live.screen_manager import ScreenManager
from beacon_live.screens import ADMISSION_CAPACITY_SCREEN_ID
from beacon_live.screens import AdmissionCapacityScreen
from beacon_live.screens import COMPOSITION_SCREEN_ID
from beacon_live.screens import CompositionScreen
from beacon_live.screens import CU_SCREEN_ID
from beacon_live.screens import CuScreen
from beacon_live.screens import RETRY_SCREEN_ID
from beacon_live.screens import RetryScreen
from beacon_live.screens import STATION_GRAPH_MAXIMUM
from beacon_live.screens import TOTAL_STATION_COUNT_SCREEN_ID
from beacon_live.screens import TotalStationCountScreen
from beacon_live.screens import _retry_percent


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
    assert manager.active_screen_id == RETRY_SCREEN_ID
    assert manager.navigate_down(now=10.9)
    assert manager.active_screen_id == COMPOSITION_SCREEN_ID
    assert manager.navigate_down(now=11.2)
    assert manager.active_screen_id == CU_SCREEN_ID
    assert manager.navigate_up(now=11.5)
    assert manager.active_screen_id == COMPOSITION_SCREEN_ID

    assert manager.snapshot is snapshot
    assert manager.snapshot.history is snapshot.history


def test_admission_capacity_uses_selected_bssid_and_shared_adc_history() -> None:
    snapshot = _analyzer_with_history().snapshot

    view = AdmissionCapacityScreen().render(snapshot)

    assert view.title == "Admission Capacity"
    assert view.metadata_tokens == ("Admission",)
    assert view.summary == "ADC 80% AVG 45% MIN 10%"
    assert "CU" not in view.summary
    assert view.graph_label == "Selected ADC"
    assert view.graph_maximum == 100
    assert view.identity.bssid == "aa"
    assert [point.second for point in view.graph_points] == [1000, 1001]
    assert [point.value for point in view.graph_points] == pytest.approx(
        [10.0, 80.0], abs=0.01
    )


def test_admission_capacity_31250_is_100_percent() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(
            1000.1,
            ssid="Alpha",
            bssid="aa",
            cu_raw=64,
            station_count=3,
            admission_capacity=QBSS_ADMISSION_CAPACITY_MAX,
            rssi_dbm=-40,
        )
    )
    analyzer.advance(1001, None)

    view = AdmissionCapacityScreen().render(analyzer.snapshot)

    assert view.summary == "ADC 100% AVG 100% MIN 100%"
    assert [point.value for point in view.graph_points] == [100.0]


def test_total_station_count_uses_shared_history_and_highest_station_bssid() -> None:
    snapshot = _analyzer_with_history().snapshot

    view = TotalStationCountScreen().render(snapshot)

    assert view.title == "Total Station Count"
    assert view.summary == "SUM 15 MAC 0 TOP 10"
    assert view.metadata_tokens == ("Stations",)
    assert view.graph_maximum == STATION_GRAPH_MAXIMUM
    assert snapshot.selected_bssid == "aa"
    assert view.identity.ssid == "Bravo"
    assert view.identity.bssid == "bb"
    assert [point.second for point in view.graph_points] == [1000, 1001]
    assert [point.value for point in view.graph_points] == [3, 15]
    assert [point.value for point in view.secondary_graph_points] == [0, 0]
    assert view.secondary_graph_label == "Unique client MACs per second"


def test_total_station_graph_keeps_fixed_64_count_scale() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(
            1000.1,
            ssid="Crowded",
            bssid="aa",
            cu_raw=64,
            station_count=65,
            admission_capacity=15625,
            rssi_dbm=-40,
        )
    )
    analyzer.advance(1001, None)

    view = TotalStationCountScreen().render(analyzer.snapshot)

    assert view.graph_maximum == 64
    assert [point.value for point in view.graph_points] == [65]


def test_total_station_screen_shows_window_mac_total_and_per_second_series() -> None:
    bssid = "02:00:00:00:00:01"
    client_a = "02:00:00:00:10:01"
    client_b = "02:00:00:00:10:02"
    analyzer = Analyzer()
    analyzer.ingest(
        _record(
            1000.0,
            ssid="Alpha",
            bssid=bssid,
            cu_raw=64,
            station_count=7,
            admission_capacity=10_000,
            rssi_dbm=-40,
        )
    )
    for timestamp, client in ((1000.1, client_a), (1000.2, client_b)):
        analyzer.ingest(
            FrameRecord(
                timestamp=timestamp,
                bssid=bssid,
                frame_type=2,
                frame_subtype=0,
                transmitter_address=client,
                receiver_address=bssid,
            )
        )
    analyzer.advance(1001, None)
    analyzer.ingest(
        FrameRecord(
            timestamp=1001.1,
            bssid=bssid,
            frame_type=2,
            frame_subtype=0,
            transmitter_address=client_a,
            receiver_address=bssid,
        )
    )
    analyzer.advance(1002, None)

    view = TotalStationCountScreen().render(analyzer.snapshot)

    assert view.summary == "SUM 7 MAC 2 TOP 7"
    assert [point.value for point in view.secondary_graph_points] == [2, 1]


def test_cu_screen_uses_utilization_top_line_label() -> None:
    view = CuScreen().render(_analyzer_with_history().snapshot)

    assert view.metadata_tokens == ("Utilization",)


def test_composition_screen_renders_only_analyzer_snapshot_fields() -> None:
    analyzer = Analyzer()
    analyzer.ingest(
        _record(
            1000.1,
            ssid="Alpha",
            bssid="00:11:22:33:44:50",
            cu_raw=64,
            station_count=3,
            admission_capacity=10_000,
            rssi_dbm=-35,
            ap_name="Room-101",
            vendor="Example Wireless",
        )
    )
    analyzer.ingest(
        _record(
            1000.2,
            ssid="Bravo",
            bssid="00:11:22:33:44:51",
            cu_raw=64,
            station_count=2,
            admission_capacity=10_000,
            rssi_dbm=-37,
            ap_name="Room-101",
            vendor="Example Wireless",
        )
    )

    view = CompositionScreen().render(analyzer.snapshot)

    assert view.title == "Composition"
    assert view.metadata_tokens == ("Composition",)
    assert view.summary == "BSSIDs 2 QBSS 2"
    assert view.text_only
    assert view.graph_points == ()
    assert view.detail_lines == (
        "Est Radios 1",
        "Radio BSSIDs 2",
        "AP Name Room-101",
        "Vendor Example Wireless",
    )
    assert view.identity.ssid == "Alpha"
    assert view.identity.bssid == "00:11:22:33:44:50"
    assert view.identity.rssi_dbm == -35


def test_composition_screen_uses_required_missing_name_placeholders() -> None:
    view = CompositionScreen().render(Analyzer().snapshot)

    assert view.detail_lines[-2:] == (
        "AP Name <no AP name>",
        "Vendor <unknown>",
    )


def test_retry_screen_uses_shared_history_and_highest_retry_bssid() -> None:
    snapshot = _retry_analyzer_with_history().snapshot

    view = RetryScreen().render(snapshot)

    assert view.title == "Retry Percentage"
    assert view.metadata_tokens == ("Retries",)
    assert view.summary == "RET 50% AVG 65% MAX 80%"
    assert view.graph_label == "One-second retry percentage"
    assert view.graph_maximum == 100
    assert view.identity.ssid == "Bravo"
    assert view.identity.bssid == "bb"
    assert [point.second for point in view.graph_points] == [1000, 1001]
    assert [point.value for point in view.graph_points] == pytest.approx(
        [80.0, 50.0]
    )


def test_retry_screen_distinguishes_sub_one_percent_from_zero() -> None:
    assert _retry_percent(0.0) == "0"
    assert _retry_percent(0.4) == "<1"


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
            admission_capacity=23438,
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


def _retry_analyzer_with_history() -> Analyzer:
    analyzer = Analyzer()
    analyzer.ingest(_retry_beacon(1000.0, "aa", "Alpha", -35))
    analyzer.ingest(_retry_frame(1000.1, "aa", True))
    analyzer.ingest(_retry_frame(1000.2, "aa", False))
    analyzer.ingest(_retry_beacon(1000.3, "bb", "Bravo", -60))
    analyzer.ingest(_retry_frame(1000.4, "bb", True))
    analyzer.ingest(_retry_frame(1000.5, "bb", True))
    analyzer.ingest(_retry_frame(1000.6, "bb", True))
    analyzer.advance(1001, None)
    analyzer.ingest(_retry_frame(1001.1, "aa", False))
    analyzer.ingest(_retry_frame(1001.2, "bb", True))
    analyzer.advance(1002, None)
    return analyzer


def _retry_beacon(
    timestamp: float,
    bssid: str,
    ssid: str,
    rssi_dbm: int,
) -> FrameRecord:
    return FrameRecord(
        timestamp=timestamp,
        bssid=bssid,
        ssid=ssid,
        rssi_dbm=rssi_dbm,
        frame_type=0,
        frame_subtype=8,
        retry_flag=False,
        beacon_interval_tu=100,
        qbss_cu_raw=64,
        qbss_cu_percent=64 / 255 * 100,
        qbss_station_count=3,
        qbss_admission_capacity=10_000,
    )


def _retry_frame(timestamp: float, bssid: str, retry: bool) -> FrameRecord:
    return FrameRecord(
        timestamp=timestamp,
        bssid=bssid,
        frame_type=2,
        frame_subtype=0,
        retry_flag=retry,
    )


def _record(
    timestamp: float,
    *,
    ssid: str,
    bssid: str,
    cu_raw: int,
    station_count: int,
    admission_capacity: int,
    rssi_dbm: int,
    ap_name: Optional[str] = None,
    vendor: Optional[str] = None,
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
        ap_name=ap_name,
        vendor=vendor,
    )
