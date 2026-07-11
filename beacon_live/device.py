"""Foreground launcher intended for WLANPi front-panel integration."""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from beacon_live.cli import live_main


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Launch the core live command with no display-specific integration."""
    live_args = list(sys.argv[1:] if argv is None else argv)
    try:
        return live_main(live_args)
    except KeyboardInterrupt:
        print("\nStopping WLANPi beacon capture...", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
