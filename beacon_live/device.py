"""Foreground launcher intended for WLANPi front-panel integration."""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from beacon_live.cli import live_main


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Launch the core live command with no display-specific integration."""
    live_args = list(sys.argv[1:] if argv is None else argv)
    try:
        if live_args and live_args[0] == "serve":
            from beacon_live.service import main as service_main
            return service_main(live_args[1:])
        if live_args and live_args[0] == "session":
            from beacon_live.service_client import main as session_main
            return session_main(live_args[1:])
        if live_args and live_args[0] in {"replay", "retry-debug", "live"}:
            from beacon_live.cli import main as tools_main
            return tools_main(live_args)
        return live_main(live_args)
    except KeyboardInterrupt:
        print("\nStopping Utilization...", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
