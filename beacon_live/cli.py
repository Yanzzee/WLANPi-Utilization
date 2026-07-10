"""Command-line interface for WLAN Pi beacon replay workflows."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Optional, Sequence

from beacon_live.aggregator import aggregate_records
from beacon_live.models import BeaconRecord
from beacon_live.models import SecondStats
from beacon_live.parser import parse_tshark_row
from beacon_live.survey import (
    compute_local_cu_percent_from_samples,
    parse_survey_dump,
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "replay":
        if args.input is not None and args.beacons_tsv is not None:
            parser.error("replay accepts --input or --beacons-tsv, not both")
        beacons_tsv = args.beacons_tsv or args.input
        if beacons_tsv is None:
            parser.error("replay requires --beacons-tsv or --input")
        return _run_replay(
            beacons_tsv,
            args.survey_before,
            args.survey_after,
            stats_csv=args.stats_csv,
            beacons_jsonl=args.beacons_jsonl,
            interface=args.interface,
            channel=args.channel,
        )

    parser.print_help()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beacon-live",
        description="Replay saved WLAN beacon QBSS samples.",
    )
    subparsers = parser.add_subparsers(dest="command")

    replay = subparsers.add_parser("replay", help="Replay saved TShark TSV samples.")
    replay.add_argument(
        "--input",
        required=False,
        type=Path,
        help="Path to a TShark beacon TSV file. Kept for compatibility.",
    )
    replay.add_argument(
        "--beacons-tsv",
        required=False,
        type=Path,
        help="Path to a TShark beacon TSV file from pi_smoke.sh.",
    )
    replay.add_argument(
        "--survey-before",
        required=False,
        type=Path,
        help="Path to iw survey dump captured before replay input.",
    )
    replay.add_argument(
        "--survey-after",
        required=False,
        type=Path,
        help="Path to iw survey dump captured after replay input.",
    )
    replay.add_argument(
        "--stats-csv",
        required=False,
        type=Path,
        help="Write per-second stats CSV to this path, for example logs/stats.csv.",
    )
    replay.add_argument(
        "--beacons-jsonl",
        required=False,
        type=Path,
        help="Write valid raw beacon records as JSONL, for example logs/beacons.jsonl.",
    )
    replay.add_argument(
        "--interface",
        default="wlan0",
        help="Interface metadata for logs. Default: wlan0.",
    )
    replay.add_argument(
        "--channel",
        default="36",
        help="Channel or frequency metadata for logs. Default: 36.",
    )

    return parser


def _run_replay(
    beacons_tsv: Path,
    survey_before: Optional[Path] = None,
    survey_after: Optional[Path] = None,
    *,
    stats_csv: Optional[Path] = None,
    beacons_jsonl: Optional[Path] = None,
    interface: str = "wlan0",
    channel: str = "36",
) -> int:
    log_metadata = _LogMetadata(
        start_time=_utc_now_iso(),
        interface=interface,
        channel=channel,
        channel_width_mhz=20,
    )
    local_cu_percent = _load_local_survey_cu_percent(survey_before, survey_after)
    replay_data = _read_beacon_replay(beacons_tsv)
    local_cu_by_second = {
        int(record.timestamp): local_cu_percent
        for record in replay_data.records
        if local_cu_percent is not None
    }
    stats = aggregate_records(
        replay_data.records,
        local_cu_by_second=local_cu_by_second,
    )
    if beacons_jsonl is not None:
        _write_beacons_jsonl(beacons_jsonl, replay_data.records, log_metadata)
    if stats_csv is not None:
        _write_stats_csv(stats, stats_csv, log_metadata)

    print(_format_header())
    for second_stats in stats:
        print(_format_stats(second_stats))

    print()
    print(_format_summary(replay_data, local_cu_percent))

    if replay_data.malformed_rows:
        print(
            f"Warning: skipped malformed beacon rows: {replay_data.malformed_rows}",
            file=sys.stderr,
        )

    if (survey_before is None) != (survey_after is None):
        print(
            "Warning: provide both --survey-before and --survey-after "
            "to compute local survey CU",
            file=sys.stderr,
        )

    return 0


@dataclass(frozen=True)
class _BeaconReplayData:
    total_rows: int
    malformed_rows: int
    rows_with_qbss_cu: int
    records: list[BeaconRecord]

    @property
    def valid_rows(self) -> int:
        return len(self.records)

    @property
    def unique_bssid_count(self) -> int:
        return len({record.bssid for record in self.records})

    @property
    def first_timestamp(self) -> Optional[float]:
        if not self.records:
            return None
        return min(record.timestamp for record in self.records)

    @property
    def last_timestamp(self) -> Optional[float]:
        if not self.records:
            return None
        return max(record.timestamp for record in self.records)

    @property
    def duration_seconds(self) -> Optional[float]:
        first_timestamp = self.first_timestamp
        last_timestamp = self.last_timestamp
        if first_timestamp is None or last_timestamp is None:
            return None
        return last_timestamp - first_timestamp


@dataclass(frozen=True)
class _LogMetadata:
    start_time: str
    interface: str
    channel: str
    channel_width_mhz: int


def _read_beacon_replay(beacons_tsv: Path) -> _BeaconReplayData:
    records: list[BeaconRecord] = []
    total_rows = 0
    malformed_rows = 0
    rows_with_qbss_cu = 0

    with beacons_tsv.open("r", encoding="utf-8") as input_file:
        for row in input_file:
            total_rows += 1
            record = parse_tshark_row(row)
            if record is None:
                malformed_rows += 1
                continue
            records.append(record)
            if record.qbss_cu_percent is not None:
                rows_with_qbss_cu += 1

    return _BeaconReplayData(
        total_rows=total_rows,
        malformed_rows=malformed_rows,
        rows_with_qbss_cu=rows_with_qbss_cu,
        records=records,
    )


def _load_local_survey_cu_percent(
    survey_before: Optional[Path],
    survey_after: Optional[Path],
) -> Optional[float]:
    if survey_before is None or survey_after is None:
        return None

    previous_samples = parse_survey_dump(survey_before.read_text(encoding="utf-8"))
    current_samples = parse_survey_dump(survey_after.read_text(encoding="utf-8"))
    return compute_local_cu_percent_from_samples(previous_samples, current_samples)


def _write_stats_csv(
    stats: list[SecondStats],
    output_path: Path,
    metadata: _LogMetadata,
) -> None:
    _ensure_parent_dir(output_path)
    fieldnames = [
        "start_time",
        "interface",
        "channel",
        "channel_width_mhz",
        "second",
        "unique_bssid_count",
        "qbss_station_count_sum",
        "qbss_cu_min_percent",
        "qbss_cu_mean_percent",
        "qbss_cu_max_percent",
        "top_qbss_cu_ssid",
        "top_qbss_cu_bssid",
        "top_qbss_cu_percent",
        "local_cu_percent",
    ]

    with output_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        for second_stats in stats:
            writer.writerow(_stats_csv_row(second_stats, metadata))
            output_file.flush()


def _write_beacons_jsonl(
    output_path: Path,
    records: list[BeaconRecord],
    metadata: _LogMetadata,
) -> None:
    _ensure_parent_dir(output_path)
    metadata_fields = asdict(metadata)

    with output_path.open("w", encoding="utf-8") as output_file:
        for record in records:
            payload = {
                "record_type": "beacon",
                **metadata_fields,
                **asdict(record),
            }
            output_file.write(json.dumps(payload, sort_keys=True) + "\n")
            output_file.flush()


def _stats_csv_row(stats: SecondStats, metadata: _LogMetadata) -> dict[str, object]:
    return {
        "start_time": metadata.start_time,
        "interface": metadata.interface,
        "channel": metadata.channel,
        "channel_width_mhz": metadata.channel_width_mhz,
        "second": stats.second,
        "unique_bssid_count": stats.unique_bssid_count,
        "qbss_station_count_sum": stats.qbss_station_count_sum,
        "qbss_cu_min_percent": _format_optional_float(stats.qbss_cu_min_percent),
        "qbss_cu_mean_percent": _format_optional_float(stats.qbss_cu_mean_percent),
        "qbss_cu_max_percent": _format_optional_float(stats.qbss_cu_max_percent),
        "top_qbss_cu_ssid": stats.top_qbss_cu_ssid or "",
        "top_qbss_cu_bssid": stats.top_qbss_cu_bssid or "",
        "top_qbss_cu_percent": _format_optional_float(stats.top_qbss_cu_percent),
        "local_cu_percent": _format_optional_float(stats.local_cu_percent),
    }


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_header() -> str:
    return "\t".join(
        [
            "second",
            "unique_bssids",
            "qbss_station_sum",
            "qbss_cu_min",
            "qbss_cu_mean",
            "qbss_cu_max",
            "top_ssid",
            "top_bssid",
            "top_qbss_cu",
            "local_cu",
        ]
    )


def _format_stats(stats: SecondStats) -> str:
    return "\t".join(
        [
            str(stats.second),
            str(stats.unique_bssid_count),
            str(stats.qbss_station_count_sum),
            _format_optional_float(stats.qbss_cu_min_percent),
            _format_optional_float(stats.qbss_cu_mean_percent),
            _format_optional_float(stats.qbss_cu_max_percent),
            stats.top_qbss_cu_ssid or "",
            stats.top_qbss_cu_bssid or "",
            _format_optional_float(stats.top_qbss_cu_percent),
            _format_optional_float(stats.local_cu_percent),
        ]
    )


def _format_optional_float(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.2f}"


def _format_summary(
    replay_data: _BeaconReplayData,
    local_cu_percent: Optional[float],
) -> str:
    return "\n".join(
        [
            "Summary",
            f"total_beacon_rows_read: {replay_data.total_rows}",
            f"valid_beacon_rows: {replay_data.valid_rows}",
            f"malformed_skipped_rows: {replay_data.malformed_rows}",
            f"rows_with_qbss_cu: {replay_data.rows_with_qbss_cu}",
            f"unique_bssid_count: {replay_data.unique_bssid_count}",
            f"first_timestamp: {_format_optional_summary_float(replay_data.first_timestamp)}",
            f"last_timestamp: {_format_optional_summary_float(replay_data.last_timestamp)}",
            f"duration_seconds: {_format_optional_summary_float(replay_data.duration_seconds)}",
            f"local_survey_cu_percent: {_format_optional_float(local_cu_percent)}",
        ]
    )


def _format_optional_summary_float(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.6f}".rstrip("0").rstrip(".")


if __name__ == "__main__":
    raise SystemExit(main())
