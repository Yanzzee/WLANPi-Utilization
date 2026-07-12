import json
from pathlib import Path
from typing import Optional

from beacon_live.lcd_dashboard import GRAPH_HEIGHT
from beacon_live.lcd_dashboard import GRAPH_WIDTH
from beacon_live.lcd_dashboard import GRAPH_X
from beacon_live.lcd_dashboard import GRAPH_Y
from beacon_live.lcd_dashboard import LcdDashboard
from beacon_live.lcd_dashboard import _CU_GRAPH
from beacon_live.lcd_dashboard import _STATION_GRAPH
from beacon_live.lcd_dashboard import _format_station_count
from beacon_live.lcd_dashboard import _raw_to_graph_height
from beacon_live.lcd_dashboard import _station_count_to_graph_height
from beacon_live.models import SecondStats


def test_lcd_dashboard_writes_128_square_ppm_and_creates_directory(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "run" / "display.ppm"
    dashboard = LcdDashboard(
        frame,
        band="5",
        channel="36",
    )

    payload = frame.read_bytes()

    assert payload.startswith(b"P6\n128 128\n255\n")
    assert len(payload.split(b"\n", 3)[3]) == 128 * 128 * 3
    assert dashboard.text_lines[0] == "5G STA -- SUM --"
    assert dashboard.text_lines[1] == "CU --% AV --% MX --%"
    state = json.loads(frame.with_suffix(".json").read_text(encoding="utf-8"))
    assert state["metadata"] == "5G STA -- SUM --"
    assert state["channel"] == "36"


def test_lcd_dashboard_uses_requested_two_row_header_and_footer(
    tmp_path: Path,
) -> None:
    dashboard = LcdDashboard(
        tmp_path / "display.ppm",
        band="6",
        channel="5",
    )
    dashboard.update(
        [
            _stats(0, 25.0, 64, station_count=12, bssid_station_count=7),
            _stats(1, 75.0, 191, station_count=18, bssid_station_count=12),
        ]
    )

    metadata, summary, ssid, rssi, bssid, channel = dashboard.text_lines

    assert metadata == "6G STA 12 SUM 18"
    assert summary == "CU 75% AV 50% MX 75%"
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
        [
            _stats(second, 50.0, second % 256, station_count=0)
            for second in range(121)
        ]
    )

    assert GRAPH_WIDTH == 120
    assert GRAPH_HEIGHT == 64
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


def test_station_sum_and_bssid_count_support_three_digit_values(
    tmp_path: Path,
) -> None:
    dashboard = LcdDashboard(
        tmp_path / "display.ppm",
        band="6",
        channel="233",
    )
    dashboard.update(
        [
            _stats(
                1000,
                100.0,
                255,
                station_count=999,
                bssid_station_count=999,
            )
        ]
    )

    assert dashboard.text_lines[0] == "6G STA 999 SUM 999"


def test_station_count_above_999_uses_infinity_symbol_in_text() -> None:
    assert _format_station_count(999) == "999"
    assert _format_station_count(1000) == "∞"
    assert _format_station_count(99999) == "∞"


def test_shorter_graph_bar_overlays_taller_bar(tmp_path: Path) -> None:
    dashboard = LcdDashboard(
        tmp_path / "display.ppm",
        band="5",
        channel="36",
    )
    dashboard.refresh(
        [
            _stats(1000, 80.0, 204, station_count=25),
            _stats(1001, 25.0, 64, station_count=80),
        ]
    )

    pixel_data = dashboard.render().split(b"\n", 3)[3]
    baseline = GRAPH_Y + GRAPH_HEIGHT - 1
    first_x = GRAPH_X + GRAPH_WIDTH - 2
    second_x = GRAPH_X + GRAPH_WIDTH - 1

    assert _pixel(pixel_data, first_x, baseline) == _STATION_GRAPH
    assert _pixel(pixel_data, first_x, baseline - 20) == _CU_GRAPH
    assert _pixel(pixel_data, second_x, baseline) == _CU_GRAPH
    assert _pixel(pixel_data, second_x, baseline - 20) == _STATION_GRAPH


def test_station_graph_clamps_above_100_without_changing_text_value() -> None:
    assert _station_count_to_graph_height(0) == 0
    assert _station_count_to_graph_height(50) == 32
    assert _station_count_to_graph_height(100) == 64
    assert _station_count_to_graph_height(9999) == 64


def _stats(
    second: int,
    percent: float,
    raw: int,
    *,
    station_count: int = 12,
    bssid_station_count: Optional[int] = None,
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
    )


def _pixel(payload: bytes, x: int, y: int) -> tuple[int, int, int]:
    index = (y * 128 + x) * 3
    return tuple(payload[index : index + 3])  # type: ignore[return-value]
