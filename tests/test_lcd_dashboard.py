import json
from pathlib import Path
from typing import Optional

import pytest

from beacon_live.analyzer import Analyzer
from beacon_live.lcd_dashboard import GRAPH_HEIGHT
from beacon_live.lcd_dashboard import GRAPH_WIDTH
from beacon_live.lcd_dashboard import GRAPH_X
from beacon_live.lcd_dashboard import GRAPH_Y
from beacon_live.lcd_dashboard import LcdDashboard
from beacon_live.lcd_dashboard import _ADMISSION_GRAPH
from beacon_live.lcd_dashboard import _CU_GRAPH
from beacon_live.lcd_dashboard import _OVERFLOW_GRAPH
from beacon_live.lcd_dashboard import _STATION_GRAPH
from beacon_live.lcd_dashboard import _format_station_count
from beacon_live.lcd_dashboard import _raw_to_graph_height
from beacon_live.lcd_dashboard import _value_to_graph_height
from beacon_live.models import BeaconRecord
from beacon_live.models import MetricsSnapshot
from beacon_live.models import QBSS_ADMISSION_CAPACITY_MAX
from beacon_live.models import SecondStats
from beacon_live.screens import ADMISSION_CAPACITY_SCREEN_ID
from beacon_live.screens import TOTAL_STATION_COUNT_SCREEN_ID


def test_lcd_dashboard_writes_128_square_ppm_and_creates_directory(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "run" / "display.ppm"
    dashboard = LcdDashboard(
        frame,
        band="5",
        channel="36",
        frequency_mhz=5180,
    )

    payload = frame.read_bytes()

    assert payload.startswith(b"P6\n128 128\n255\n")
    assert len(payload.split(b"\n", 3)[3]) == 128 * 128 * 3
    assert dashboard.text_lines[0] == "5180MHz Channel"
    assert dashboard.text_lines[1] == "CU --% AVG --% MAX --%"
    state = json.loads(frame.with_suffix(".json").read_text(encoding="utf-8"))
    assert state["metadata"] == "5180MHz Channel"
    assert state["metadata_candidates"] == [
        "5180MHz Channel",
        "5180 Channel",
    ]
    assert state["metadata_metric_token_count"] == 1
    assert state["metric_color"] == list(_CU_GRAPH)
    assert state["summary_metric_token_count"] == 2
    assert state["channel"] == "36"


def test_lcd_dashboard_uses_requested_two_row_header_and_footer(
    tmp_path: Path,
) -> None:
    dashboard = LcdDashboard(
        tmp_path / "display.ppm",
        band="6",
        channel="5",
        frequency_mhz=5975,
    )
    dashboard.update(
        _snapshot(
            _stats(0, 25.0, 64, station_count=12, bssid_station_count=7),
            _stats(1, 75.0, 191, station_count=18, bssid_station_count=12),
        )
    )

    metadata, summary, ssid, rssi, bssid, channel = dashboard.text_lines

    assert metadata == "5975MHz Channel"
    assert summary == "CU 75% AVG 50% MAX 75%"
    assert ssid == "Alpha"
    assert rssi == "-45"
    assert bssid == "aa:aa:aa:aa:aa:aa"
    assert channel == "5"


def test_lcd_graph_keeps_one_pixel_per_second_for_latest_120_seconds(
    tmp_path: Path,
) -> None:
    dashboard = LcdDashboard(
        tmp_path / "display.ppm",
        band="5",
        channel="36",
    )
    dashboard.refresh(
        _snapshot(
            *[
                _stats(second, 50.0, second % 256, station_count=0)
                for second in range(121)
            ]
        )
    )

    assert GRAPH_WIDTH == 120
    assert GRAPH_HEIGHT == 64
    assert GRAPH_Y == 30
    assert [second for second, _ in dashboard.graph_data] == list(range(1, 121))

    pixel_data = dashboard.render().split(b"\n", 3)[3]
    latest_height = _raw_to_graph_height(120)
    latest_x = GRAPH_X + GRAPH_WIDTH - 1
    graph_bottom = GRAPH_Y + GRAPH_HEIGHT - 1
    assert _pixel(pixel_data, latest_x, graph_bottom) == (0, 220, 120)
    assert _pixel(pixel_data, latest_x, graph_bottom - latest_height) != (
        0,
        220,
        120,
    )


def test_raw_qbss_values_map_to_quarter_scale_graph_height() -> None:
    assert _raw_to_graph_height(0) == 1
    assert _raw_to_graph_height(3) == 1
    assert _raw_to_graph_height(4) == 2
    assert _raw_to_graph_height(128) == 33
    assert _raw_to_graph_height(255) == 64


def test_station_counts_map_one_to_one_to_64_graph_pixels() -> None:
    assert _value_to_graph_height(0, 64) == 1
    assert _value_to_graph_height(1, 64) == 1
    assert _value_to_graph_height(32, 64) == 32
    assert _value_to_graph_height(64, 64) == 64
    assert _value_to_graph_height(65, 64) == 64


def test_station_sum_supports_three_digit_values(
    tmp_path: Path,
) -> None:
    dashboard = LcdDashboard(
        tmp_path / "display.ppm",
        band="6",
        channel="233",
    )
    dashboard.update(
        _snapshot(
            _stats(
                1000,
                100.0,
                255,
                station_count=999,
                bssid_station_count=999,
            )
        )
    )
    dashboard.set_active_screen(2)

    assert dashboard.text_lines[0] == "Stations"
    assert dashboard.text_lines[1] == "SUM 999 MAX 999 TOP --"


def test_station_count_above_999_uses_infinity_symbol_in_text() -> None:
    assert _format_station_count(999) == "999"
    assert _format_station_count(1000) == "∞"
    assert _format_station_count(99999) == "∞"


def test_graph_contains_only_cu_bar(tmp_path: Path) -> None:
    dashboard = LcdDashboard(
        tmp_path / "display.ppm",
        band="5",
        channel="36",
    )
    dashboard.refresh(
        _snapshot(
            _stats(1000, 80.0, 204, station_count=25),
            _stats(1001, 25.0, 64, station_count=9999),
        )
    )

    pixel_data = dashboard.render().split(b"\n", 3)[3]
    baseline = GRAPH_Y + GRAPH_HEIGHT - 1
    second_x = GRAPH_X + GRAPH_WIDTH - 1

    assert _pixel(pixel_data, second_x, baseline) == _CU_GRAPH
    assert _pixel(pixel_data, second_x, baseline - 20) != _CU_GRAPH


def test_no_selected_qbss_beacon_uses_explicit_ssid_message(tmp_path: Path) -> None:
    dashboard = LcdDashboard(
        tmp_path / "display.ppm",
        band="5",
        channel="36",
        frequency_mhz=5180,
    )
    dashboard.update(
        _snapshot(
            SecondStats(
                second=1000,
                unique_bssid_count=0,
                qbss_station_count_sum=0,
                selected_qbss_cu_percent=None,
                selected_qbss_ssid=None,
                selected_qbss_bssid=None,
                selected_qbss_rssi_dbm=None,
                local_cu_percent=None,
            )
        )
    )

    assert dashboard.text_lines[2] == "<No QBSS Beacons>"


def test_lcd_navigation_renders_admission_and_total_station_screens(
    tmp_path: Path,
) -> None:
    dashboard = LcdDashboard(
        tmp_path / "display.ppm",
        band="5",
        channel="36",
        frequency_mhz=5180,
    )
    snapshot = _phase_two_snapshot()
    dashboard.update(snapshot)

    assert dashboard.navigate_down(now=1.0)
    assert dashboard.active_screen_id == ADMISSION_CAPACITY_SCREEN_ID
    assert dashboard.metric_color == _ADMISSION_GRAPH
    assert dashboard.text_lines[0] == "5180MHz Admission"
    assert dashboard.text_lines[1] == "ADC 80% AVG 45% MIN 10%"
    assert dashboard.text_lines[2] == "Alpha"
    assert [second for second, _ in dashboard.graph_data] == [1000, 1001]
    assert [value for _, value in dashboard.graph_data] == pytest.approx(
        [10.0, 80.0], abs=0.01
    )
    admission_pixels = dashboard.render().split(b"\n", 3)[3]
    latest_x = GRAPH_X + GRAPH_WIDTH - 1
    baseline = GRAPH_Y + GRAPH_HEIGHT - 1
    assert _pixel(admission_pixels, latest_x, baseline) == _ADMISSION_GRAPH

    assert dashboard.navigate_down(now=1.3)
    assert dashboard.active_screen_id == TOTAL_STATION_COUNT_SCREEN_ID
    assert dashboard.metric_color == _STATION_GRAPH
    assert dashboard.text_lines[0] == "5180MHz Stations"
    assert dashboard.text_lines[1] == "SUM 15 MAX 15 TOP 10"
    assert dashboard.text_lines[2] == "Bravo"
    assert dashboard.graph_data == ((1000, 3), (1001, 15))
    station_pixels = dashboard.render().split(b"\n", 3)[3]
    assert _pixel(station_pixels, latest_x, baseline) == _STATION_GRAPH
    assert len({_CU_GRAPH, _ADMISSION_GRAPH, _STATION_GRAPH}) == 3
    assert dashboard.snapshot is snapshot


def test_total_station_graph_truncates_over_64_and_colors_overflow_red(
    tmp_path: Path,
) -> None:
    dashboard = LcdDashboard(
        tmp_path / "display.ppm",
        band="5",
        channel="36",
    )
    dashboard.update(
        _snapshot(
            _stats(1000, 25.0, 64, station_count=64),
            _stats(1001, 50.0, 128, station_count=65),
        )
    )
    dashboard.set_active_screen(2)

    pixel_data = dashboard.render().split(b"\n", 3)[3]
    latest_x = GRAPH_X + GRAPH_WIDTH - 1

    assert dashboard.graph_data == ((1000, 64), (1001, 65))
    assert _pixel(pixel_data, latest_x - 1, GRAPH_Y) == _STATION_GRAPH
    assert _pixel(pixel_data, latest_x, GRAPH_Y) == _OVERFLOW_GRAPH
    assert _pixel(pixel_data, latest_x, GRAPH_Y + GRAPH_HEIGHT - 1) == (
        _OVERFLOW_GRAPH
    )


def test_lcd_refresh_applies_fpms_screen_request_without_losing_history(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "display.ppm"
    control = frame.with_suffix(".control.json")
    dashboard = LcdDashboard(frame, band="5", channel="36")
    snapshot = _phase_two_snapshot()
    control.write_text('{"active_screen_offset":2}', encoding="utf-8")

    dashboard.refresh(snapshot)

    assert dashboard.active_screen_id == TOTAL_STATION_COUNT_SCREEN_ID
    assert dashboard.snapshot is snapshot
    assert [row.second for row in dashboard.snapshot.history] == [1000, 1001]
    state = json.loads(frame.with_suffix(".json").read_text(encoding="utf-8"))
    assert state["screen_id"] == TOTAL_STATION_COUNT_SCREEN_ID
    assert state["screen_index"] == 2
    assert state["screen_count"] == 3


def _stats(
    second: int,
    percent: float,
    raw: int,
    *,
    station_count: int = 12,
    bssid_station_count: Optional[int] = None,
    admission_capacity: Optional[int] = None,
) -> SecondStats:
    return SecondStats(
        second=second,
        unique_bssid_count=1,
        qbss_station_count_sum=station_count,
        selected_qbss_cu_percent=percent,
        selected_qbss_ssid="Alpha",
        selected_qbss_bssid="aa:aa:aa:aa:aa:aa",
        selected_qbss_rssi_dbm=-45,
        local_cu_percent=None,
        selected_qbss_cu_raw=raw,
        selected_qbss_station_count=(
            station_count if bssid_station_count is None else bssid_station_count
        ),
        selected_qbss_strongest_rssi_dbm=-45,
        selected_qbss_admission_capacity=admission_capacity,
    )


def _pixel(payload: bytes, x: int, y: int) -> tuple[int, int, int]:
    index = (y * 128 + x) * 3
    return tuple(payload[index : index + 3])  # type: ignore[return-value]


def _phase_two_snapshot() -> MetricsSnapshot:
    analyzer = Analyzer()
    analyzer.ingest(
        _phase_two_record(
            1000.1,
            "Alpha",
            "aa",
            64,
            3,
            round(QBSS_ADMISSION_CAPACITY_MAX * 0.10),
            -40,
        )
    )
    analyzer.advance(1001, None)
    analyzer.ingest(
        _phase_two_record(
            1001.1,
            "Alpha",
            "aa",
            128,
            5,
            round(QBSS_ADMISSION_CAPACITY_MAX * 0.80),
            -40,
        )
    )
    analyzer.ingest(
        _phase_two_record(
            1001.2,
            "Bravo",
            "bb",
            32,
            10,
            round(QBSS_ADMISSION_CAPACITY_MAX * 0.75),
            -60,
        )
    )
    analyzer.advance(1002, None)
    return analyzer.snapshot


def _phase_two_record(
    timestamp: float,
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


def _snapshot(*rows: SecondStats) -> MetricsSnapshot:
    history = tuple(rows[-120:])
    current = history[-1]
    return MetricsSnapshot(
        generated_at=float(current.second + 1),
        window_seconds=120,
        bssids=(),
        selected_bssid=current.selected_qbss_bssid,
        current=current,
        history=history,
    )
