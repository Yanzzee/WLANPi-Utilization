from __future__ import annotations

from typing import Optional

import pytest

from beacon_live.models import BeaconReceptionSnapshot
from beacon_live.models import BeaconRecord
from beacon_live.models import BssidState
from beacon_live.models import CompositionSnapshot
from beacon_live.models import MetricsSnapshot
from beacon_live.models import RetryBssidState
from beacon_live.models import SecondStats
from beacon_live.tui import CursesDashboard
from beacon_live.tui import TableViewport
from beacon_live.tui import calculate_layout
from beacon_live.tui import compress_history
from beacon_live.tui import should_use_curses


class _FakeCursesError(Exception):
    pass


class _FakeCurses:
    KEY_DOWN = 258
    KEY_UP = 259
    KEY_LEFT = 260
    KEY_RIGHT = 261
    KEY_HOME = 262
    KEY_NPAGE = 338
    KEY_PPAGE = 339
    KEY_END = 360
    KEY_RESIZE = 410
    A_BOLD = 1
    A_REVERSE = 2
    error = _FakeCursesError

    def __init__(self, window: "_FakeWindow") -> None:
        self.window = window
        self.wrapper_called = False
        self.wrapper_restored = False
        self.cursor_visibility: Optional[int] = None

    def wrapper(self, callback: object) -> int:
        self.wrapper_called = True
        try:
            return callback(self.window)  # type: ignore[operator]
        finally:
            self.wrapper_restored = True

    def curs_set(self, visibility: int) -> None:
        self.cursor_visibility = visibility


class _FakeWindow:
    def __init__(self, height: int, width: int) -> None:
        self.height = height
        self.width = width
        self.keys: list[int] = []
        self.lines: dict[int, str] = {}
        self.refresh_count = 0
        self.nonblocking = False
        self.keypad_enabled = False

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def resize(self, height: int, width: int) -> None:
        self.height = height
        self.width = width

    def erase(self) -> None:
        self.lines = {}

    def addnstr(
        self,
        row: int,
        column: int,
        text: str,
        count: int,
        attribute: int = 0,
    ) -> None:
        del attribute
        if row < 0 or row >= self.height or column < 0 or column >= self.width:
            raise _FakeCursesError
        available = min(count, self.width - column)
        prefix = self.lines.get(row, "").ljust(column)
        self.lines[row] = f"{prefix}{text[:available]}"[: self.width]

    def refresh(self) -> None:
        self.refresh_count += 1

    def nodelay(self, enabled: bool) -> None:
        self.nonblocking = enabled

    def keypad(self, enabled: bool) -> None:
        self.keypad_enabled = enabled

    def getch(self) -> int:
        return self.keys.pop(0) if self.keys else -1

    @property
    def text(self) -> str:
        return "\n".join(self.lines.get(row, "") for row in range(self.height))


class _TtyStream:
    def __init__(self, is_tty: bool) -> None:
        self.is_tty = is_tty

    def isatty(self) -> bool:
        return self.is_tty


def test_layout_reserves_graphs_and_scrollable_table_at_common_sizes() -> None:
    wide = calculate_layout(30, 120)
    narrow = calculate_layout(14, 30)
    tiny = calculate_layout(8, 20)

    assert wide.too_small is False
    assert wide.compact_graphs is False
    assert wide.graph_rows == 6
    assert wide.table_rows >= 1
    assert narrow.too_small is False
    assert narrow.compact_graphs is True
    assert narrow.graph_rows == 3
    assert narrow.table_rows == 1
    assert tiny.too_small is True


def test_history_compression_uses_the_full_window_including_newest_sample() -> None:
    compressed = compress_history(tuple(float(value) for value in range(120)), 12)

    assert len(compressed) == 12
    assert compressed[0] == pytest.approx(4.5)
    assert compressed[-1] == pytest.approx(114.5)
    assert compress_history((1.0, None, 3.0), 10) == (1.0, None, 3.0)


def test_table_viewport_follows_new_rows_until_the_user_scrolls_back() -> None:
    viewport = TableViewport()
    viewport.sync(
        row_count=30,
        page_rows=6,
        table_width=200,
        visible_width=80,
    )
    assert viewport.top_row == 24
    assert viewport.follow_newest is True

    viewport.move_vertical(-1, row_count=30, page_rows=6)
    assert viewport.top_row == 23
    assert viewport.follow_newest is False

    viewport.sync(
        row_count=31,
        page_rows=6,
        table_width=200,
        visible_width=80,
    )
    assert viewport.top_row == 23
    viewport.end(row_count=31, page_rows=6)
    assert viewport.top_row == 25
    assert viewport.follow_newest is True


def test_key_controls_scroll_both_axes_and_support_home_end_and_quit() -> None:
    window = _FakeWindow(20, 100)
    curses_module = _FakeCurses(window)
    dashboard = CursesDashboard(curses_module=curses_module)

    def interact() -> int:
        dashboard.refresh(_snapshot(40))
        newest_top = dashboard.viewport.top_row

        window.keys.append(curses_module.KEY_UP)
        assert dashboard.poll_input() is False
        assert dashboard.viewport.top_row == newest_top - 1
        assert dashboard.viewport.follow_newest is False

        window.keys.append(curses_module.KEY_PPAGE)
        dashboard.poll_input()
        assert dashboard.viewport.top_row < newest_top - 1
        page_up_top = dashboard.viewport.top_row

        window.keys.append(curses_module.KEY_NPAGE)
        dashboard.poll_input()
        assert dashboard.viewport.top_row > page_up_top

        window.keys.append(curses_module.KEY_HOME)
        dashboard.poll_input()
        assert dashboard.viewport.top_row == 0

        window.keys.append(curses_module.KEY_DOWN)
        dashboard.poll_input()
        assert dashboard.viewport.top_row == 1

        window.keys.append(curses_module.KEY_END)
        dashboard.poll_input()
        assert dashboard.viewport.follow_newest is True

        window.keys.append(curses_module.KEY_RIGHT)
        dashboard.poll_input()
        assert dashboard.viewport.horizontal_offset == 8
        window.keys.append(curses_module.KEY_LEFT)
        dashboard.poll_input()
        assert dashboard.viewport.horizontal_offset == 0

        window.keys.append(ord("q"))
        assert dashboard.poll_input() is True
        return 0

    assert dashboard.run(interact) == 0
    assert dashboard.exit_requested is True

    ctrl_c_dashboard = CursesDashboard(curses_module=curses_module)
    assert ctrl_c_dashboard.handle_key(3) is True
    assert ctrl_c_dashboard.exit_requested is True


def test_resize_recalculates_layout_and_narrow_terminal_keeps_all_graphs() -> None:
    window = _FakeWindow(24, 100)
    curses_module = _FakeCurses(window)
    dashboard = CursesDashboard(curses_module=curses_module)

    def interact() -> int:
        dashboard.refresh(_snapshot(20))
        assert dashboard.last_layout == calculate_layout(24, 100)
        window.resize(14, 30)
        window.keys.append(curses_module.KEY_RESIZE)
        dashboard.poll_input()
        assert dashboard.last_layout == calculate_layout(14, 30)
        return 0

    dashboard.run(interact)

    assert "CU" in window.text
    assert "ADC" in window.text
    assert "STA" in window.text
    assert "MAC" in window.text
    assert "RET" in window.text
    assert "LOSS" in window.text
    assert "Composition" in window.text
    assert "TIME" in window.text
    assert all(len(line) <= 30 for line in window.lines.values())


def test_curses_wrapper_detaches_window_and_restores_after_failure() -> None:
    window = _FakeWindow(24, 100)
    curses_module = _FakeCurses(window)
    dashboard = CursesDashboard(curses_module=curses_module)

    def fail() -> int:
        raise RuntimeError("capture failed")

    with pytest.raises(RuntimeError, match="capture failed"):
        dashboard.run(fail)

    assert curses_module.wrapper_called is True
    assert curses_module.wrapper_restored is True
    assert curses_module.cursor_visibility == 0
    assert window.nonblocking is True
    assert window.keypad_enabled is True
    assert dashboard._window is None


def test_non_tty_and_missing_curses_use_plain_fallback_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    assert should_use_curses(
        stdin=_TtyStream(True),  # type: ignore[arg-type]
        stdout=_TtyStream(False),  # type: ignore[arg-type]
    ) is False

    monkeypatch.setattr("beacon_live.tui._curses", None)
    assert should_use_curses(
        stdin=_TtyStream(True),  # type: ignore[arg-type]
        stdout=_TtyStream(True),  # type: ignore[arg-type]
    ) is False


def _snapshot(row_count: int) -> MetricsSnapshot:
    rows = tuple(_stats(1_000 + index) for index in range(row_count))
    current = rows[-1]
    beacon = BeaconRecord(
        timestamp=float(current.second),
        ssid="Alpha",
        bssid="aa:aa:aa:aa:aa:aa",
        qbss_cu_raw=128,
        qbss_cu_percent=50.2,
        qbss_station_count=12,
        qbss_admission_capacity=15_625,
        rssi_dbm=-45,
        ap_name="AP-Lobby",
        vendor="Example Vendor",
    )
    state = BssidState(
        bssid=beacon.bssid,
        ssid=beacon.ssid,
        last_seen_ts=beacon.timestamp,
        latest_beacon_ts=beacon.timestamp,
        latest_beacon_record=beacon,
        window_beacon_count=100,
        latest_station_count=12,
        latest_qbss_cu_raw=128,
        latest_qbss_cu_percent=50.2,
        latest_admission_capacity=15_625,
        latest_rssi_dbm=-45,
        peak_rssi_dbm=-40,
        latest_ap_name="AP-Lobby",
        latest_vendor="Example Vendor",
    )
    return MetricsSnapshot(
        generated_at=float(current.second + 1),
        window_seconds=120,
        bssids=(state,),
        selected_bssid=state.bssid,
        current=current,
        history=rows,
        top_station_bssid=state.bssid,
        top_retry_bssid=state.bssid,
        retry_bssids=(
            RetryBssidState(
                bssid=state.bssid,
                ssid=state.ssid,
                rssi_dbm=-44,
                window_frame_count=500,
                window_retry_observed_frame_count=400,
                retry_eligible_frame_count=300,
                window_retry_frame_count=30,
                window_retry_percent=10.0,
            ),
        ),
        composition=CompositionSnapshot(
            bssid_count=3,
            qbss_bssid_count=2,
            estimated_radio_count=1,
            strongest_radio_bssid_count=2,
            strongest_radio_bssids=(state.bssid,),
            strongest_radio_ap_name="AP-Lobby",
            strongest_radio_vendor="Example Vendor",
            displayed_ssid=state.ssid,
            displayed_bssid=state.bssid,
            displayed_rssi_dbm=-40,
        ),
        window_unique_client_mac_count=5,
        beacons=BeaconReceptionSnapshot(
            strongest_radio_bssids=(state.bssid,),
            bssids=(),
            received_count=98,
            expected_count=100,
            loss_percent=2.0,
            displayed_ssid=state.ssid,
            displayed_bssid=state.bssid,
            displayed_rssi_dbm=-40,
        ),
    )


def _stats(second: int) -> SecondStats:
    return SecondStats(
        second=second,
        unique_bssid_count=3,
        qbss_station_count_sum=12 + second % 5,
        selected_qbss_cu_percent=float(second % 101),
        selected_qbss_ssid="Alpha",
        selected_qbss_bssid="aa:aa:aa:aa:aa:aa",
        selected_qbss_rssi_dbm=-45,
        local_cu_percent=15.0,
        selected_qbss_cu_raw=second % 256,
        selected_qbss_station_count=12,
        selected_qbss_strongest_rssi_dbm=-40,
        selected_qbss_admission_capacity=15_625,
        received_frame_count=500,
        retry_observed_frame_count=400,
        retry_eligible_frame_count=300,
        retry_frame_count=30,
        retry_percent=10.0,
        selected_beacon_rate_percent=98.0,
        top_retry_bssid="aa:aa:aa:aa:aa:aa",
        unique_client_mac_count=5,
        beacon_received_count=98,
        beacon_expected_count=100,
        beacon_loss_percent=2.0,
    )
