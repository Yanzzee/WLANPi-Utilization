from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional

import pytest

from beacon_live.models import BeaconBssidReception
from beacon_live.models import BeaconReceptionSnapshot
from beacon_live.models import BeaconRecord
from beacon_live.models import BssidState
from beacon_live.models import CompositionSnapshot
from beacon_live.models import MetricsSnapshot
from beacon_live.models import RetryBssidState
from beacon_live.models import SecondStats
from beacon_live.tui import CursesDashboard
from beacon_live.tui import TableViewport
from beacon_live.tui import _format_table_header
from beacon_live.tui import _format_table_row
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
        self.writes: list[tuple[int, int, str, int]] = []
        self.refresh_count = 0
        self.nonblocking = False
        self.keypad_enabled = False
        self.clear_requested = False

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def resize(self, height: int, width: int) -> None:
        self.height = height
        self.width = width

    def erase(self) -> None:
        self.lines = {}
        self.writes = []

    def addnstr(
        self,
        row: int,
        column: int,
        text: str,
        count: int,
        attribute: int = 0,
    ) -> None:
        if row < 0 or row >= self.height or column < 0 or column >= self.width:
            raise _FakeCursesError
        available = min(count, self.width - column)
        self.writes.append((row, column, text[:available], attribute))
        prefix = self.lines.get(row, "").ljust(column)
        self.lines[row] = f"{prefix}{text[:available]}"[: self.width]

    def refresh(self) -> None:
        self.refresh_count += 1

    def nodelay(self, enabled: bool) -> None:
        self.nonblocking = enabled

    def keypad(self, enabled: bool) -> None:
        self.keypad_enabled = enabled

    def clearok(self, enabled: bool) -> None:
        self.clear_requested = enabled

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
    narrow = calculate_layout(16, 30)
    tiny = calculate_layout(8, 20)

    assert wide.too_small is False
    assert wide.compact_graphs is False
    assert wide.graph_rows == 6
    assert wide.table_rows >= 1
    assert narrow.too_small is False
    assert narrow.compact_graphs is True
    assert narrow.graph_rows == 3
    assert narrow.table_rows >= 1
    assert tiny.too_small is True

    logging = calculate_layout(30, 120, radio_bssid_rows=2, logging_rows=2)
    assert logging.composition_rows == 7
    assert logging.table_rows >= 1


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


def test_space_pauses_only_rendered_snapshot_and_resume_jumps_to_newest() -> None:
    window = _FakeWindow(24, 120)
    curses_module = _FakeCurses(window)
    dashboard = CursesDashboard(curses_module=curses_module)

    def interact() -> int:
        dashboard.refresh(_snapshot(20))
        window.keys.append(ord(" "))
        dashboard.poll_input()
        assert dashboard.display_paused is True
        assert "PAUSED" in window.lines[0]

        paused_refresh_count = window.refresh_count
        dashboard.refresh(_snapshot(21))
        assert len(dashboard.snapshot.history) == 20
        assert window.refresh_count == paused_refresh_count

        dashboard.viewport.follow_newest = False
        window.keys.append(ord(" "))
        dashboard.poll_input()
        assert dashboard.display_paused is False
        assert len(dashboard.snapshot.history) == 21
        assert dashboard.viewport.follow_newest is True
        assert "FOLLOW" in window.lines[0]
        return 0

    assert dashboard.run(interact) == 0


def test_resize_recalculates_layout_and_narrow_terminal_keeps_all_graphs() -> None:
    window = _FakeWindow(24, 100)
    curses_module = _FakeCurses(window)
    dashboard = CursesDashboard(curses_module=curses_module)

    def interact() -> int:
        dashboard.refresh(_snapshot(20))
        assert dashboard.last_layout == calculate_layout(
            24,
            100,
            radio_bssid_rows=2,
        )
        window.resize(16, 30)
        window.keys.append(curses_module.KEY_RESIZE)
        dashboard.poll_input()
        assert dashboard.last_layout == calculate_layout(
            16,
            30,
            radio_bssid_rows=2,
        )
        return 0

    dashboard.run(interact)

    assert "CU" in window.text
    assert "ADC" in window.text
    assert "STA" in window.text
    assert "MAC" in window.text
    assert "RET" in window.text
    assert "LOSS" in window.text
    assert "TIME" in window.text
    assert all(len(line) <= 30 for line in window.lines.values())


def test_header_composition_and_selected_identity_columns_are_deduplicated() -> None:
    window = _FakeWindow(30, 140)
    curses_module = _FakeCurses(window)
    dashboard = CursesDashboard(
        curses_module=curses_module,
        band="5",
        channel="36",
        frequency_mhz=5180,
    )
    dashboard.set_capture_width("80")

    dashboard.run(lambda: (dashboard.refresh(_snapshot(20)), 0)[1])

    assert (
        "5180 MHz | Band 5 GHz | Channel 36 | Width 80 MHz"
        in window.lines[0]
    )
    assert "Composition:" not in window.text
    assert "Strongest AP: AP-Lobby" in window.text
    assert "aa:aa:aa:aa:aa:aa" in window.text
    assert "bb:bb:bb:bb:bb:bb" in window.text
    assert "Alpha" in window.text
    assert "Bravo" in window.text
    assert "Selected BSSIDs" in window.text
    assert "QBSS CU  50%" in window.text
    assert "ADC  50%" in window.text
    assert "STA  12" in window.text
    assert "RET  10%" in window.text
    assert "LOSS   2%" in window.text

    bold_text = "".join(
        text
        for _, _, text, attribute in window.writes
        if attribute == curses_module.A_BOLD
    )
    assert "aa:aa:aa:aa:aa:aa" in bold_text
    assert "Alpha" in bold_text


def test_full_graph_fields_have_fixed_edges_and_requested_vertical_order() -> None:
    window = _FakeWindow(30, 140)
    curses_module = _FakeCurses(window)
    dashboard = CursesDashboard(curses_module=curses_module)

    dashboard.run(lambda: (dashboard.refresh(_snapshot(20)), 0)[1])

    assert dashboard.last_layout is not None
    graph_start = dashboard.last_layout.graph_start
    graph_lines = [window.lines[graph_start + index] for index in range(6)]
    assert [line.split()[0] for line in graph_lines] == [
        "CU",
        "ADC",
        "MAC",
        "LOSS",
        "STA",
        "RET",
    ]
    assert {len(line) for line in graph_lines} == {140}
    assert {len(line[-31:]) for line in graph_lines} == {31}


def test_full_history_graph_summary_follows_graph_on_very_wide_terminal() -> None:
    window = _FakeWindow(30, 220)
    curses_module = _FakeCurses(window)
    dashboard = CursesDashboard(curses_module=curses_module)

    dashboard.run(lambda: (dashboard.refresh(_snapshot(120)), 0)[1])

    assert dashboard.last_layout is not None
    graph_start = dashboard.last_layout.graph_start
    graph_lines = [window.lines[graph_start + index] for index in range(6)]
    assert {len(line) for line in graph_lines} == {158}
    assert all(len(line) < 220 for line in graph_lines)
    assert {len(line[-31:]) for line in graph_lines} == {31}


def test_logging_paths_render_below_selected_bssids_with_blank_row() -> None:
    window = _FakeWindow(30, 140)
    curses_module = _FakeCurses(window)
    dashboard = CursesDashboard(curses_module=curses_module)

    def enable_logging() -> int:
        dashboard.refresh(_snapshot(20))
        dashboard.set_logging_status(
            active=True,
            paths=(
                Path("/var/log/wlanpi-beacon-live/live_stats.csv"),
                Path("/var/log/wlanpi-beacon-live/live_beacons.jsonl"),
            ),
        )
        return 0

    dashboard.run(enable_logging)

    assert dashboard.last_layout is not None
    assert window.clear_requested is True
    right_start = (dashboard.last_layout.width - 1) // 2 + 1
    selected_start = dashboard.last_layout.composition_start
    blank_row = selected_start + 4
    assert not any(
        row == blank_row and column == right_start
        for row, column, _, _ in window.writes
    )
    assert "Logging to /var/log/wlanpi-beacon-live/live_stats.csv" in window.text
    assert (
        "Logging to /var/log/wlanpi-beacon-live/live_beacons.jsonl"
        in window.text
    )


def test_table_headers_use_single_tokens_and_hide_requested_display_fields() -> None:
    header = _format_table_header(include_local_cu=True)
    row = _format_table_row(
        _stats(1_000),
        include_local_cu=True,
        local_timezone=None,
    )

    assert "STA_SUM" in header
    assert "SEL_STA" in header
    assert "RET_ELIG" in header
    assert "QBSS_BSSID" in header
    assert "TOP_RETRY_BSSID" in header
    assert "CU RAW" not in header
    assert "RET OBS" not in header
    assert "BCN RATE" not in header
    assert len(row) == len(header)


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
    second_beacon = replace(
        beacon,
        ssid="Bravo",
        bssid="bb:bb:bb:bb:bb:bb",
        rssi_dbm=-50,
    )
    second_state = replace(
        state,
        bssid=second_beacon.bssid,
        ssid=second_beacon.ssid,
        latest_beacon_record=second_beacon,
        latest_rssi_dbm=-50,
        peak_rssi_dbm=-48,
    )
    return MetricsSnapshot(
        generated_at=float(current.second + 1),
        window_seconds=120,
        bssids=(state, second_state),
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
            strongest_radio_bssids=(state.bssid, second_state.bssid),
            strongest_radio_ap_name="AP-Lobby",
            strongest_radio_vendor="Example Vendor",
            displayed_ssid=state.ssid,
            displayed_bssid=state.bssid,
            displayed_rssi_dbm=-40,
        ),
        window_unique_client_mac_count=5,
        beacons=BeaconReceptionSnapshot(
            strongest_radio_bssids=(state.bssid,),
            bssids=(
                BeaconBssidReception(
                    bssid=state.bssid,
                    received_count=98,
                    expected_count=100,
                    loss_percent=2.0,
                ),
            ),
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
