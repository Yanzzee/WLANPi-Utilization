"""Command-line interface for WLAN Pi beacon replay workflows."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from beacon_live.aggregator import Aggregator
from beacon_live.models import SecondStats
from beacon_live.parser import parse_tshark_row


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "replay":
        return _run_replay(args.input)

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
        required=True,
        type=Path,
        help="Path to a TShark TSV file.",
    )

    return parser


def _run_replay(input_path: Path) -> int:
    aggregator = Aggregator()
    stats: list[SecondStats] = []
    malformed_rows = 0

    with input_path.open("r", encoding="utf-8") as input_file:
        for row in input_file:
            if not row.strip():
                continue
            record = parse_tshark_row(row)
            if record is None:
                malformed_rows += 1
                continue
            stats.extend(aggregator.add(record))

    stats.extend(aggregator.flush())

    print(_format_header())
    for second_stats in stats:
        print(_format_stats(second_stats))

    if malformed_rows:
        print(f"Skipped malformed rows: {malformed_rows}", file=sys.stderr)

    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
