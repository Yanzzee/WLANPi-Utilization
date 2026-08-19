"""Offline retry-metric audit from a saved monitor-mode capture."""

from __future__ import annotations

import csv
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, TextIO

from beacon_live.analyzer import Analyzer
from beacon_live.analyzer import associate_frame_bssid
from beacon_live.live import available_tshark_fields
from beacon_live.live import select_tshark_frame_fields
from beacon_live.models import BeaconRecord
from beacon_live.models import FrameRecord
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES
from beacon_live.parser import TSHARK_MULTI_VALUE_SEPARATOR
from beacon_live.parser import parse_tshark_capture_record


RETRY_AUDIT_FIELD_NAMES = (
    "second",
    "scope",
    "bssid",
    "ssid",
    "footer_bssid",
    "is_footer_bssid",
    "decoded_frame_count",
    "retry_bit_readable_frame_count",
    "group_address_excluded_count",
    "missing_retry_bit_excluded_count",
    "non_retryable_type_excluded_count",
    "out_of_primary_scope_excluded_count",
    "retry_eligible_frame_count",
    "retry_frame_count",
    "retry_percent",
)

RETRY_FRAME_AUDIT_FIELD_NAMES = (
    "timestamp",
    "bssid",
    "ta",
    "ra",
    "sa",
    "da",
    "retry_bit",
    "retry_eligible",
    "retry_exclusion_reason",
    "sequence_number",
    "fragment_number",
    "qos_tid",
    "phy_bandwidth",
    "radio_frequency_mhz",
    "captured_length",
    "original_length",
    "fcs_status",
    "ampdu_reference",
)


@dataclass(frozen=True)
class RetryCaptureData:
    """Normalized frames and parse diagnostics from one saved capture."""

    frames: tuple[FrameRecord, ...]
    tshark_row_count: int
    malformed_row_count: int


@dataclass(frozen=True)
class RetryAuditRow:
    """Auditable retry counts for one channel or BSSID in one second."""

    second: int
    scope: str
    bssid: Optional[str]
    ssid: Optional[str]
    footer_bssid: Optional[str]
    decoded_frame_count: int
    retry_bit_readable_frame_count: int
    group_address_excluded_count: int
    missing_retry_bit_excluded_count: int
    non_retryable_type_excluded_count: int
    out_of_primary_scope_excluded_count: int
    retry_eligible_frame_count: int
    retry_frame_count: int
    retry_percent: Optional[float]

    @property
    def is_footer_bssid(self) -> bool:
        return self.bssid is not None and self.bssid == self.footer_bssid


class RetryDebugCommandError(Exception):
    """A TShark capture-replay command could not be completed."""

    def __init__(
        self,
        command: list[str],
        *,
        returncode: Optional[int] = None,
        stderr: str = "",
    ) -> None:
        super().__init__(stderr or "retry debug command failed")
        self.command = command
        self.returncode = returncode
        self.stderr = stderr

    @property
    def command_text(self) -> str:
        return " ".join(self.command)


def build_retry_debug_tshark_command(
    input_path: Path,
    *,
    tshark: str = "tshark",
    field_names: tuple[str, ...] = TSHARK_FRAME_FIELD_NAMES,
) -> list[str]:
    """Build the all-decoded-WLAN-frame export used by the live parser."""
    command = [
        tshark,
        "-n",
        "-r",
        str(input_path),
        "-Y",
        "wlan",
        "-T",
        "fields",
        "-E",
        "separator=\t",
        "-E",
        "occurrence=a",
        "-E",
        f"aggregator={TSHARK_MULTI_VALUE_SEPARATOR}",
    ]
    for field in field_names:
        command.extend(["-e", field])
    return command


def read_retry_debug_capture(
    input_path: Path,
    *,
    tshark: str = "tshark",
) -> RetryCaptureData:
    """Normalize all decoded 802.11 frames from a PCAP/PCAPNG file."""
    field_names = select_tshark_frame_fields(
        available_tshark_fields(tshark=tshark)
    )
    command = build_retry_debug_tshark_command(
        input_path,
        tshark=tshark,
        field_names=field_names,
    )
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as error_file:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=error_file,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise RetryDebugCommandError(command, stderr=str(exc)) from exc

        if process.stdout is None:
            raise RetryDebugCommandError(command, stderr="missing TShark stdout")

        frames: list[FrameRecord] = []
        tshark_row_count = 0
        malformed_row_count = 0
        for row in process.stdout:
            tshark_row_count += 1
            parsed = parse_tshark_capture_record(row, field_names=field_names)
            if not parsed:
                malformed_row_count += 1
            else:
                frames.extend(parsed)

        returncode = process.wait()
        error_file.seek(0)
        stderr = error_file.read().strip()
    if returncode != 0:
        raise RetryDebugCommandError(
            command,
            returncode=returncode,
            stderr=stderr,
        )

    return RetryCaptureData(
        frames=tuple(frames),
        tshark_row_count=tshark_row_count,
        malformed_row_count=malformed_row_count,
    )


def analyze_retry_frames(
    frames: Iterable[FrameRecord],
    *,
    window_seconds: int = 120,
    target_primary_frequency_mhz: Optional[int] = None,
    channel_definition_max_age_seconds: float = 10.0,
) -> tuple[RetryAuditRow, ...]:
    """Return channel and per-known-BSSID retry counts for every second."""
    ordered_frames = tuple(sorted(frames, key=lambda frame: frame.timestamp))
    if not ordered_frames:
        return ()

    analyzer = Analyzer(
        window_seconds=window_seconds,
        target_primary_frequency_mhz=target_primary_frequency_mhz,
        channel_definition_max_age_seconds=channel_definition_max_age_seconds,
    )
    for frame in ordered_frames:
        analyzer.ingest(frame, publish_snapshot=False)
    stats_by_second = {
        stats.second: stats for stats in analyzer.flush(include_history=False)
    }

    beacons = tuple(
        beacon
        for frame in ordered_frames
        for beacon in (frame.beacon_record(),)
        if beacon is not None
    )
    rows: list[RetryAuditRow] = []
    seconds = sorted({int(frame.timestamp) for frame in ordered_frames})
    for second in seconds:
        second_frames = tuple(
            frame
            for frame in ordered_frames
            if second <= frame.timestamp < second + 1
        )
        scoped_second_frames = second_frames
        out_of_scope_count = 0
        if target_primary_frequency_mhz is not None:
            scoped_second_frames = tuple(
                frame
                for frame in second_frames
                if _frame_in_primary_scope(
                    frame,
                    beacons,
                    target_primary_frequency_mhz,
                    channel_definition_max_age_seconds,
                )
            )
            out_of_scope_count = len(second_frames) - len(scoped_second_frames)
        identities = _beacon_identities_at(
            beacons,
            second=second,
            window_seconds=window_seconds,
        )
        if target_primary_frequency_mhz is not None:
            identities = {
                bssid: beacon
                for bssid, beacon in identities.items()
                if beacon.channel_definition is not None
                and beacon.channel_definition.primary_frequency_mhz
                == target_primary_frequency_mhz
                and second + 1 - beacon.timestamp
                <= channel_definition_max_age_seconds
            }
        known_bssids = tuple(sorted(identities))
        grouped: dict[str, list[FrameRecord]] = {
            bssid: [] for bssid in known_bssids
        }
        for frame in scoped_second_frames:
            bssid = associate_frame_bssid(frame, known_bssids)
            if bssid is not None:
                grouped.setdefault(bssid, []).append(frame)

        footer_bssid = (
            stats_by_second[second].top_retry_bssid
            if second in stats_by_second
            else None
        )
        rows.append(
            _audit_row(
                second,
                "channel",
                None,
                None,
                footer_bssid,
                scoped_second_frames,
                out_of_primary_scope_excluded_count=out_of_scope_count,
                decoded_frame_count_override=len(second_frames),
            )
        )
        for bssid in sorted(grouped):
            identity = identities.get(bssid)
            rows.append(
                _audit_row(
                    second,
                    "bssid",
                    bssid,
                    identity.ssid if identity is not None else None,
                    footer_bssid,
                    tuple(grouped[bssid]),
                )
            )
    return tuple(rows)


def write_retry_audit_csv(
    rows: Iterable[RetryAuditRow],
    output: TextIO,
) -> None:
    """Write retry diagnostics in a spreadsheet-friendly stable format."""
    writer = csv.DictWriter(output, fieldnames=RETRY_AUDIT_FIELD_NAMES)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "second": row.second,
                "scope": row.scope,
                "bssid": row.bssid or "",
                "ssid": row.ssid or "",
                "footer_bssid": row.footer_bssid or "",
                "is_footer_bssid": "1" if row.is_footer_bssid else "0",
                "decoded_frame_count": row.decoded_frame_count,
                "retry_bit_readable_frame_count": (
                    row.retry_bit_readable_frame_count
                ),
                "group_address_excluded_count": (
                    row.group_address_excluded_count
                ),
                "missing_retry_bit_excluded_count": (
                    row.missing_retry_bit_excluded_count
                ),
                "non_retryable_type_excluded_count": (
                    row.non_retryable_type_excluded_count
                ),
                "out_of_primary_scope_excluded_count": (
                    row.out_of_primary_scope_excluded_count
                ),
                "retry_eligible_frame_count": row.retry_eligible_frame_count,
                "retry_frame_count": row.retry_frame_count,
                "retry_percent": (
                    "" if row.retry_percent is None else f"{row.retry_percent:.6f}"
                ),
            }
        )


def write_retry_frame_audit_csv(
    frames: Iterable[FrameRecord],
    output: TextIO,
) -> None:
    """Write packet-level fields for comparison with an independent export."""
    writer = csv.DictWriter(output, fieldnames=RETRY_FRAME_AUDIT_FIELD_NAMES)
    writer.writeheader()
    for frame in frames:
        writer.writerow(
            {
                "timestamp": f"{frame.timestamp:.9f}",
                "bssid": frame.bssid or "",
                "ta": frame.transmitter_address or "",
                "ra": frame.receiver_address or "",
                "sa": frame.source_address or "",
                "da": frame.destination_address or "",
                "retry_bit": (
                    "" if frame.retry_flag is None else int(frame.retry_flag)
                ),
                "retry_eligible": int(frame.retry_eligible),
                "retry_exclusion_reason": frame.retry_exclusion_reason or "",
                "sequence_number": _optional(frame.sequence_number),
                "fragment_number": _optional(frame.fragment_number),
                "qos_tid": _optional(frame.qos_tid),
                "phy_bandwidth": frame.phy_bandwidth or "",
                "radio_frequency_mhz": _optional(frame.radio_frequency_mhz),
                "captured_length": _optional(frame.captured_length),
                "original_length": _optional(frame.original_length),
                "fcs_status": _optional(frame.fcs_status),
                "ampdu_reference": _optional(frame.ampdu_reference),
            }
        )


def _beacon_identities_at(
    beacons: tuple[BeaconRecord, ...],
    *,
    second: int,
    window_seconds: int,
) -> dict[str, BeaconRecord]:
    lower_bound = second + 1 - window_seconds
    identities: dict[str, BeaconRecord] = {}
    for beacon in beacons:
        if lower_bound <= beacon.timestamp < second + 1:
            identities[beacon.bssid] = beacon
    return identities


def _audit_row(
    second: int,
    scope: str,
    bssid: Optional[str],
    ssid: Optional[str],
    footer_bssid: Optional[str],
    frames: tuple[FrameRecord, ...],
    *,
    out_of_primary_scope_excluded_count: int = 0,
    decoded_frame_count_override: Optional[int] = None,
) -> RetryAuditRow:
    reasons = Counter(
        frame.retry_exclusion_reason
        for frame in frames
        if frame.retry_exclusion_reason is not None
    )
    eligible_frames = tuple(frame for frame in frames if frame.retry_eligible)
    retry_frame_count = sum(
        frame.retry_flag is True for frame in eligible_frames
    )
    retry_percent = (
        retry_frame_count / len(eligible_frames) * 100
        if eligible_frames
        else None
    )
    return RetryAuditRow(
        second=second,
        scope=scope,
        bssid=bssid,
        ssid=ssid,
        footer_bssid=footer_bssid,
        decoded_frame_count=(
            len(frames)
            if decoded_frame_count_override is None
            else decoded_frame_count_override
        ),
        retry_bit_readable_frame_count=sum(
            frame.retry_flag is not None for frame in frames
        ),
        group_address_excluded_count=reasons["group_address"],
        missing_retry_bit_excluded_count=reasons["missing_retry_bit"],
        non_retryable_type_excluded_count=reasons[
            "non_retryable_frame_type"
        ],
        out_of_primary_scope_excluded_count=(
            out_of_primary_scope_excluded_count
        ),
        retry_eligible_frame_count=len(eligible_frames),
        retry_frame_count=retry_frame_count,
        retry_percent=retry_percent,
    )


def _frame_in_primary_scope(
    frame: FrameRecord,
    beacons: tuple[BeaconRecord, ...],
    target_primary_frequency_mhz: int,
    max_age_seconds: float,
) -> bool:
    known_bssids = tuple(sorted({beacon.bssid for beacon in beacons}))
    bssid = associate_frame_bssid(frame, known_bssids)
    if bssid is None:
        return False
    latest = next(
        (
            beacon
            for beacon in reversed(beacons)
            if beacon.bssid == bssid
            and beacon.timestamp <= frame.timestamp
            and beacon.channel_definition is not None
        ),
        None,
    )
    return bool(
        latest is not None
        and frame.timestamp - latest.timestamp <= max_age_seconds
        and latest.channel_definition is not None
        and latest.channel_definition.primary_frequency_mhz
        == target_primary_frequency_mhz
    )


def _optional(value: Optional[object]) -> object:
    return "" if value is None else value
