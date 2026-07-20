import csv
import io
from pathlib import Path
from typing import Optional

import pytest

from beacon_live.models import FrameRecord
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES
from beacon_live.retry_debug import analyze_retry_frames
from beacon_live.retry_debug import build_retry_debug_tshark_command
from beacon_live.retry_debug import write_retry_audit_csv


def test_retry_debug_command_exports_same_fields_as_live_capture() -> None:
    command = build_retry_debug_tshark_command(
        input_path=_path("capture.pcapng")
    )

    assert command[:4] == ["tshark", "-n", "-r", "capture.pcapng"]
    assert command[command.index("-Y") + 1] == "wlan"
    assert _field_args(command) == list(TSHARK_FRAME_FIELD_NAMES)


def test_retry_audit_explains_channel_and_per_bssid_calculations() -> None:
    bssid = "aa:bb:cc:dd:ee:ff"
    frames = (
        _frame(
            1000.0,
            frame_type=0,
            frame_subtype=8,
            retry=False,
            bssid=bssid,
            receiver="ff:ff:ff:ff:ff:ff",
            ssid="Alpha",
            rssi=-40,
        ),
        _frame(1000.1, retry=True, transmitter=bssid),
        _frame(1000.2, retry=True, receiver=bssid),
        _frame(1000.3, retry=False, source=bssid),
        _frame(
            1000.4,
            retry=True,
            destination="01:00:5e:00:00:01",
        ),
        _frame(1000.5, frame_type=1, frame_subtype=11, retry=True),
        _frame(1000.6, retry=None),
    )

    rows = analyze_retry_frames(frames)
    channel = next(row for row in rows if row.scope == "channel")
    bssid_row = next(
        row for row in rows if row.scope == "bssid" and row.bssid == bssid
    )

    assert channel.decoded_frame_count == 7
    assert channel.group_address_excluded_count == 2
    assert channel.non_retryable_type_excluded_count == 1
    assert channel.missing_retry_bit_excluded_count == 1
    assert channel.retry_eligible_frame_count == 3
    assert channel.retry_frame_count == 2
    assert channel.retry_percent == pytest.approx(2 / 3 * 100)
    assert channel.footer_bssid == bssid
    assert bssid_row.retry_eligible_frame_count == 3
    assert bssid_row.retry_frame_count == 2
    assert bssid_row.retry_percent == pytest.approx(2 / 3 * 100)
    assert bssid_row.is_footer_bssid


def test_retry_audit_csv_preserves_counts_and_unavailable_percent() -> None:
    rows = analyze_retry_frames(
        (
            _frame(
                1000.0,
                frame_type=0,
                frame_subtype=8,
                retry=False,
                bssid="aa",
                receiver="ff:ff:ff:ff:ff:ff",
                ssid="Alpha",
            ),
        )
    )
    output = io.StringIO()

    write_retry_audit_csv(rows, output)

    parsed = list(csv.DictReader(io.StringIO(output.getvalue())))
    channel = next(row for row in parsed if row["scope"] == "channel")
    assert channel["decoded_frame_count"] == "1"
    assert channel["group_address_excluded_count"] == "1"
    assert channel["retry_eligible_frame_count"] == "0"
    assert channel["retry_percent"] == ""


def _frame(
    timestamp: float,
    *,
    frame_type: int = 2,
    frame_subtype: int = 0,
    retry: Optional[bool],
    bssid: Optional[str] = None,
    transmitter: Optional[str] = None,
    receiver: Optional[str] = "00:11:22:33:44:55",
    source: Optional[str] = None,
    destination: Optional[str] = "00:11:22:33:44:55",
    ssid: Optional[str] = None,
    rssi: Optional[int] = None,
) -> FrameRecord:
    return FrameRecord(
        timestamp=timestamp,
        frame_type=frame_type,
        frame_subtype=frame_subtype,
        retry_flag=retry,
        bssid=bssid,
        transmitter_address=transmitter,
        receiver_address=receiver,
        source_address=source,
        destination_address=destination,
        ssid=ssid,
        rssi_dbm=rssi,
    )


def _field_args(command: list[str]) -> list[str]:
    return [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "-e"
    ]


def _path(value: str) -> Path:
    return Path(value)
