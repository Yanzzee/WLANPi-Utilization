import io
from dataclasses import replace
from datetime import timedelta
from datetime import timezone
from typing import Optional

import pytest

from beacon_live.dashboard import RollingStatsWindow
from beacon_live.dashboard import TerminalDashboard
from beacon_live.models import SecondStats


def test_rolling_window_keeps_only_the_most_recent_120_seconds() -> None:
    window = RollingStatsWindow()

    for second in range(1000, 1121):
        window.add(_stats(second))

    assert len(window.rows) == 120
    assert window.rows[0].second == 1001
    assert window.rows[-1].second == 1120


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
    dashboard = TerminalDashboard(include_local_cu=False, warmup_cycles=0)
    dashboard.update([_stats(1000)])

    rendered = dashboard.render()

    assert "rolling 120s" in rendered
    assert "BSSID COUNT" in rendered
    assert "QBSS STA SUM" in rendered
    assert "QBSS CU MIN/MEAN/MAX" in rendered
    assert "10.00%/20.00%/30.00%" in rendered
    assert "Alpha/aa:aa:aa:aa:aa:aa" in rendered
    assert "LOCAL SURVEY CU" not in rendered


def test_rolling_summary_uses_last_120_per_second_max_values() -> None:
    dashboard = TerminalDashboard(warmup_cycles=0)
    values = [float(index % 101) for index in range(121)]
    dashboard.update(
        [
            _stats(1000 + index, qbss_max_percent=value)
            for index, value in enumerate(values)
        ]
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


def test_graph_data_uses_only_per_second_max_qbss_cu() -> None:
    dashboard = TerminalDashboard(warmup_cycles=0)
    dashboard.update(
        [
            replace(
                _stats(1000),
                qbss_cu_min_percent=91.0,
                qbss_cu_mean_percent=92.0,
                qbss_cu_max_percent=25.0,
            ),
            replace(
                _stats(1001),
                qbss_cu_min_percent=81.0,
                qbss_cu_mean_percent=82.0,
                qbss_cu_max_percent=None,
            ),
        ]
    )

    assert dashboard.graph_data == ((1000, 25.0), (1001, None))
    assert dashboard.rolling_summary.sample_count == 1
    assert dashboard.rolling_summary.mean_percent == 25.0


def test_dashboard_formats_local_time() -> None:
    local_timezone = timezone(timedelta(hours=5, minutes=30))
    dashboard = TerminalDashboard(
        warmup_cycles=0,
        local_timezone=local_timezone,
    )
    dashboard.update([_stats(0)])

    rendered = dashboard.render()

    assert "LOCAL TIME" in rendered
    assert "05:30:00" in rendered


def test_first_two_cycles_are_explicit_warmup_and_suppressed() -> None:
    dashboard = TerminalDashboard(warmup_cycles=2)

    dashboard.update([_stats(1000, qbss_max_percent=90.0)])
    assert dashboard.graph_data == ()
    assert dashboard.rolling_summary.sample_count == 0
    assert "Warm-up: cycle 1/2" in dashboard.render()

    dashboard.update([_stats(1001, qbss_max_percent=80.0)])
    assert dashboard.graph_data == ()
    assert dashboard.rolling_summary.sample_count == 0
    assert "Warm-up: cycle 2/2" in dashboard.render()

    dashboard.update([_stats(1002, qbss_max_percent=30.0)])
    assert dashboard.graph_data == ((1002, 30.0),)
    assert dashboard.rolling_summary.sample_count == 1
    assert dashboard.rolling_summary.mean_percent == 30.0
    assert "Warm-up: complete; first 2 cycles excluded" in dashboard.render()


def test_dashboard_marks_enabled_but_missing_local_cu_unavailable() -> None:
    dashboard = TerminalDashboard(include_local_cu=True, warmup_cycles=0)
    dashboard.update([_stats(1000, local_cu_percent=None)])

    rendered = dashboard.render()

    assert "LOCAL SURVEY CU" in rendered
    assert "unavailable" in rendered


def test_dashboard_formats_available_local_cu() -> None:
    dashboard = TerminalDashboard(include_local_cu=True, warmup_cycles=0)
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
    qbss_max_percent: Optional[float] = 30.0,
) -> SecondStats:
    return SecondStats(
        second=second,
        unique_bssid_count=3,
        qbss_station_count_sum=12,
        qbss_cu_min_percent=10.0,
        qbss_cu_mean_percent=20.0,
        qbss_cu_max_percent=qbss_max_percent,
        top_qbss_cu_ssid="Alpha",
        top_qbss_cu_bssid="aa:aa:aa:aa:aa:aa",
        top_qbss_cu_percent=qbss_max_percent,
        local_cu_percent=local_cu_percent,
    )
