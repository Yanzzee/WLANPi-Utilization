import io
from dataclasses import replace
from typing import Optional

import pytest

from beacon_live.dashboard import RollingStatsWindow
from beacon_live.dashboard import TerminalDashboard
from beacon_live.models import SecondStats


def test_rolling_window_keeps_only_the_most_recent_60_seconds() -> None:
    window = RollingStatsWindow()

    for second in range(1000, 1061):
        window.add(_stats(second))

    assert len(window.rows) == 60
    assert window.rows[0].second == 1001
    assert window.rows[-1].second == 1060


def test_rolling_window_replaces_an_existing_second() -> None:
    window = RollingStatsWindow(max_seconds=3)
    window.extend([_stats(1000), _stats(1001), _stats(1002)])

    window.add(replace(_stats(1002), unique_bssid_count=99))

    assert [row.second for row in window.rows] == [1000, 1001, 1002]
    assert window.rows[-1].unique_bssid_count == 99


def test_rolling_window_rejects_invalid_size() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        RollingStatsWindow(max_seconds=0)


def test_dashboard_formats_all_beacon_metrics_without_local_cu() -> None:
    dashboard = TerminalDashboard(include_local_cu=False)
    dashboard.update([_stats(1000)])

    rendered = dashboard.render()

    assert "rolling 60s" in rendered
    assert "BSSID COUNT" in rendered
    assert "QBSS STA SUM" in rendered
    assert "QBSS CU MIN/MEAN/MAX" in rendered
    assert "10.00%/20.00%/30.00%" in rendered
    assert "Alpha/aa:aa:aa:aa:aa:aa" in rendered
    assert "LOCAL SURVEY CU" not in rendered
    assert "unavailable" not in rendered


def test_dashboard_marks_enabled_but_missing_local_cu_unavailable() -> None:
    dashboard = TerminalDashboard(include_local_cu=True)
    dashboard.update([_stats(1000, local_cu_percent=None)])

    rendered = dashboard.render()

    assert "LOCAL SURVEY CU" in rendered
    assert "unavailable" in rendered


def test_dashboard_formats_available_local_cu() -> None:
    dashboard = TerminalDashboard(include_local_cu=True)
    dashboard.update([_stats(1000, local_cu_percent=12.5)])

    rendered = dashboard.render()

    assert "LOCAL SURVEY CU" in rendered
    assert "12.50%" in rendered


def test_dashboard_refresh_clears_and_redraws_terminal() -> None:
    dashboard = TerminalDashboard()
    output = io.StringIO()

    dashboard.refresh([_stats(1000)], stream=output)

    assert output.getvalue().startswith("\x1b[2J\x1b[H")
    assert "Alpha/aa:aa:aa:aa:aa:aa" in output.getvalue()


def _stats(
    second: int,
    *,
    local_cu_percent: Optional[float] = None,
) -> SecondStats:
    return SecondStats(
        second=second,
        unique_bssid_count=3,
        qbss_station_count_sum=12,
        qbss_cu_min_percent=10.0,
        qbss_cu_mean_percent=20.0,
        qbss_cu_max_percent=30.0,
        top_qbss_cu_ssid="Alpha",
        top_qbss_cu_bssid="aa:aa:aa:aa:aa:aa",
        top_qbss_cu_percent=30.0,
        local_cu_percent=local_cu_percent,
    )
