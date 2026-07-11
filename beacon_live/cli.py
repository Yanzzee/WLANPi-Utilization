"""Command-line interface for WLAN Pi beacon analysis workflows."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Optional, Sequence

from beacon_live.aggregator import aggregate_records
from beacon_live.models import BeaconRecord
from beacon_live.models import SecondStats
from beacon_live.parser import parse_tshark_row
from beacon_live.live import LiveCommandError
from beacon_live.live import SUPPORTED_BANDS
from beacon_live.live import run_live
from beacon_live.log_writer import CaptureLogWriter
from beacon_live.log_writer import LogMetadata
from beacon_live.log_writer import build_live_log_paths
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

    if args.command == "live":
        return _run_live_command(args, parser)

    parser.print_help()
    return 0


def live_main(argv: Optional[Sequence[str]] = None) -> int:
    """Run only the live CLI, for the dedicated on-device entrypoint."""
    parser = argparse.ArgumentParser(
        prog="wlanpi-beacon-live",
        description="Run foreground WLANPi beacon analysis.",
    )
    _add_live_arguments(parser)
    return _run_live_command(parser.parse_args(argv), parser)


def _run_live_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    command_prefix = "live " if parser.prog == "beacon-live" else ""
    if args.interval_seconds <= 0:
        parser.error(
            f"{command_prefix}--interval-seconds must be greater than zero"
        )
    if args.frequency_mhz is not None and args.band is not None:
        parser.error(
            f"{command_prefix}accepts --frequency-mhz or --band/--channel, "
            "not both"
        )
    log_paths = build_live_log_paths(
        args.log_dir,
        write_stats_csv=args.stats_csv,
        write_beacons_jsonl=args.beacons_jsonl,
    )
    try:
        return run_live(
            iface=args.iface,
            channel=args.channel,
            frequency_mhz=args.frequency_mhz,
            band=args.band,
            interval_seconds=args.interval_seconds,
            local_cu=args.local_cu or args.survey_debug,
            survey_debug=args.survey_debug,
            stats_csv=log_paths.stats_csv,
            beacons_jsonl=log_paths.beacons_jsonl,
        )
    except ValueError as exc:
        parser.error(f"{command_prefix}{exc}")
    except LiveCommandError as exc:
        print(f"Command failed: {exc.command_text}", file=sys.stderr)
        if exc.returncode is not None:
            print(f"Exit status: {exc.returncode}", file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beacon-live",
        description="Analyze WLAN beacon QBSS samples from replay or live capture.",
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

    live = subparsers.add_parser(
        "live",
        help="Run live beacon analysis with optional local survey CU.",
    )
    _add_live_arguments(live)

    return parser


def _add_live_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--iface",
        default="wlan0",
        help="Wireless interface to use. Default: wlan0.",
    )
    parser.add_argument(
        "--channel",
        default="36",
        help="Channel number to tune with HT20. Default: 36.",
    )
    parser.add_argument(
        "--frequency-mhz",
        required=False,
        type=int,
        help="Explicit center frequency in MHz, for example 5975 for 6 GHz PSC channel 5.",
    )
    parser.add_argument(
        "--band",
        required=False,
        choices=sorted(SUPPORTED_BANDS),
        help=(
            "Band used to map --channel to frequency. Use 6 with --channel 5 "
            "for 5975 MHz."
        ),
    )
    parser.add_argument(
        "--interval-seconds",
        default=1.0,
        type=float,
        help="Terminal update interval and optional survey polling interval. Default: 1.",
    )
    parser.add_argument(
        "--survey-debug",
        action="store_true",
        help=(
            "Enable local survey CU and print counter selection and deltas "
            "to stderr."
        ),
    )
    parser.add_argument(
        "--local-cu",
        action="store_true",
        help=(
            "Poll optional, driver-dependent iw survey counters and include "
            "local survey CU in live output."
        ),
    )
    parser.add_argument(
        "--stats-csv",
        action="store_true",
        help="Write per-second stats to an app-named CSV in --log-dir.",
    )
    parser.add_argument(
        "--beacons-jsonl",
        action="store_true",
        help="Write beacons to an app-named JSONL file in --log-dir.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("logs"),
        help="Directory for generated live log filenames. Default: logs.",
    )


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
    log_metadata = LogMetadata(
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
    with CaptureLogWriter(
        metadata=log_metadata,
        stats_csv=stats_csv,
        beacons_jsonl=beacons_jsonl,
    ) as log_writer:
        for record in replay_data.records:
            log_writer.write_beacon(record)
        for second_stats in stats:
            log_writer.write_stats(second_stats)

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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_header() -> str:
    return "\t".join(
        [
            "second",
            "unique_bssids",
            "qbss_station_sum",
            "selected_qbss_cu",
            "selected_qbss_ssid",
            "selected_qbss_bssid",
            "selected_qbss_rssi_dbm",
            "local_cu",
        ]
    )


def _format_stats(stats: SecondStats) -> str:
    return "\t".join(
        [
            str(stats.second),
            str(stats.unique_bssid_count),
            str(stats.qbss_station_count_sum),
            _format_optional_float(stats.selected_qbss_cu_percent),
            stats.selected_qbss_ssid or "",
            stats.selected_qbss_bssid or "",
            _format_optional_int(stats.selected_qbss_rssi_dbm),
            _format_optional_float(stats.local_cu_percent),
        ]
    )


def _format_optional_float(value: Optional[float]) -> str:
    return "" if value is None else f"{value:.2f}"


def _format_optional_int(value: Optional[int]) -> str:
    return "" if value is None else str(value)


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
