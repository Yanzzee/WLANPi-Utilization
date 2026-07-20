import io
from dataclasses import replace
from datetime import timedelta
from datetime import timezone
from typing import Optional

import pytest

from beacon_live.dashboard import TerminalDashboard
from beacon_live.models import MetricsSnapshot
from beacon_live.models import SecondStats
from beacon_live.screens import COMPOSITION_SCREEN_ID


def test_dashboard_formats_all_beacon_metrics_without_local_cu() -> None:
    dashboard = TerminalDashboard(include_local_cu=False)
    dashboard.update(_snapshot(_stats(1000)))

    rendered = dashboard.render()

    assert "rolling 120s" in rendered
    assert "BSSID COUNT" in rendered
    assert "QBSS STA SUM" in rendered
    assert "QBSS CU" in rendered
    assert "QBSS CU MIN/MEAN/MAX" not in rendered
    assert "30.00%" in rendered
    assert "Alpha/aa:aa:aa:aa:aa:aa" in rendered
    assert "-45 dBm" in rendered
    assert "LOCAL SURVEY CU" not in rendered


def test_rolling_summary_uses_displayed_values_from_last_120_seconds() -> None:
    dashboard = TerminalDashboard()
    values = [float(index % 101) for index in range(121)]
    dashboard.update(
        _snapshot(
            *[
                _stats(1000 + index, selected_qbss_cu_percent=value)
                for index, value in enumerate(values)
            ]
        )
    )

    expected = values[-120:]
    summary = dashboard.rolling_summary

    assert summary.sample_count == 120
    assert summary.min_percent == min(expected)
    assert summary.max_percent == max(expected)
    assert summary.mean_percent == pytest.approx(sum(expected) / 120)
    rendered = dashboard.render()
    assert f"min={min(expected):.2f}%" in rendered
    assert f"mean={sum(expected) / 120:.2f}%" in rendered
    assert f"max={max(expected):.2f}%" in rendered


def test_graph_data_uses_exactly_the_displayed_per_second_qbss_cu() -> None:
    dashboard = TerminalDashboard()
    dashboard.update(
        _snapshot(
            replace(
                _stats(1000),
                selected_qbss_cu_percent=25.0,
            ),
            replace(
                _stats(1001),
                selected_qbss_cu_percent=None,
            ),
        )
    )

    assert dashboard.graph_data == ((1000, 25.0), (1001, None))
    assert dashboard.rolling_summary.sample_count == 1
    assert dashboard.rolling_summary.mean_percent == 25.0


def test_dashboard_formats_local_time() -> None:
    local_timezone = timezone(timedelta(hours=5, minutes=30))
    dashboard = TerminalDashboard(
        local_timezone=local_timezone,
    )
    dashboard.update(_snapshot(_stats(0)))

    rendered = dashboard.render()

    assert "LOCAL TIME" in rendered
    assert "05:30:00" in rendered


def test_dashboard_marks_enabled_but_missing_local_cu_unavailable() -> None:
    dashboard = TerminalDashboard(include_local_cu=True)
    dashboard.update(_snapshot(_stats(1000, local_cu_percent=None)))

    rendered = dashboard.render()

    assert "LOCAL SURVEY CU" in rendered
    assert "unavailable" in rendered


def test_dashboard_formats_available_local_cu() -> None:
    dashboard = TerminalDashboard(include_local_cu=True)
    dashboard.update(_snapshot(_stats(1000, local_cu_percent=12.5)))

    rendered = dashboard.render()

    assert "LOCAL SURVEY CU" in rendered
    assert "12.50%" in rendered


def test_dashboard_refresh_clears_and_redraws_terminal() -> None:
    dashboard = TerminalDashboard()
    output = io.StringIO()

    dashboard.refresh(_snapshot(_stats(1000)), stream=output)

    assert output.getvalue().startswith("\x1b[2J\x1b[H")
    assert "Alpha/aa:aa:aa:aa:aa:aa" in output.getvalue()


def test_dashboard_replaces_its_view_from_shared_snapshot() -> None:
    dashboard = TerminalDashboard()
    dashboard.update(_snapshot(_stats(1000)))

    dashboard.update(_snapshot(_stats(2000)))

    assert dashboard.graph_data == ((2000, 30.0),)


def test_terminal_dashboard_uses_shared_screen_registry() -> None:
    dashboard = TerminalDashboard()
    snapshot = _snapshot(_stats(1000), _stats(1001))
    dashboard.update(snapshot)

    dashboard.navigate_down(now=1.0)
    admission = dashboard.render()
    dashboard.navigate_down(now=1.3)
    station_total = dashboard.render()
    dashboard.navigate_down(now=1.6)
    retry = dashboard.render()
    dashboard.navigate_down(now=1.9)
    composition = dashboard.render()

    assert dashboard.active_screen_id == COMPOSITION_SCREEN_ID
    assert "Admission Capacity" in admission
    assert "ADC --" in admission
    assert "Total Station Count" in station_total
    assert "SUM 12" in station_total
    assert "Retry Percentage" in retry
    assert "Composition" in composition
    assert "Est Radios" in composition
    assert dashboard.snapshot is snapshot


def _stats(
    second: int,
    *,
    local_cu_percent: Optional[float] = None,
    selected_qbss_cu_percent: Optional[float] = 30.0,
) -> SecondStats:
    return SecondStats(
        second=second,
        unique_bssid_count=3,
        qbss_station_count_sum=12,
        selected_qbss_cu_percent=selected_qbss_cu_percent,
        selected_qbss_ssid="Alpha",
        selected_qbss_bssid="aa:aa:aa:aa:aa:aa",
        selected_qbss_rssi_dbm=-45,
        local_cu_percent=local_cu_percent,
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
