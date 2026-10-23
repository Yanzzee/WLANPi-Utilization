#!/usr/bin/env python3
"""Export beacon-only pcapng offline; never capture, retune or control a session."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from beacon_live.pcapng import PcapngError, write_beacons


def export_file(source: Path, destination: Path):
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Output already exists: {destination}")
    if destination.suffix.lower() != ".pcapng":
        raise ValueError("Use a .pcapng output filename")
    with source.open("rb") as reader:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as writer:
                stats = write_beacons(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            # Atomic publication without replacing a file created concurrently.
            os.link(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing full-packet pcapng source")
    parser.add_argument("destination", type=Path, help="New output filename; existing files are never replaced")
    args = parser.parse_args(argv)
    try:
        stats = export_file(args.source, args.destination)
    except (OSError, ValueError, PcapngError) as error:
        parser.exit(1, f"Utilization beacon export failed: {error}\n")
    print(json.dumps({"source": str(args.source), "destination": str(args.destination), **asdict(stats)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
