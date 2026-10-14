#!/usr/bin/env python3
"""Run the Core-owner prototype using Core's interpreter/dependencies."""
from pathlib import Path
import argparse
import json
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from beacon_live.device import main

def run(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg == '--capture-backend' or arg.startswith('--capture-backend=') for arg in argv):
        raise SystemExit('run_core_capture.py selects Core automatically; omit --capture-backend')
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--core-diagnostics', type=Path,
                        default=Path(f'/tmp/wlanpi-utilization-core-{os.getpid()}.json'))
    args, remaining = parser.parse_known_args(argv)
    previous_stamp = args.core_diagnostics.stat().st_mtime_ns if args.core_diagnostics.exists() else None
    try:
        return main(['--capture-backend', 'core', '--core-diagnostics', str(args.core_diagnostics), *remaining])
    finally:
        if (args.core_diagnostics.exists() and '--help' not in argv and '-h' not in argv
                and args.core_diagnostics.stat().st_mtime_ns != previous_stamp):
            print(f'Core diagnostics: {args.core_diagnostics}', file=sys.stderr)
            try:
                report = json.loads(args.core_diagnostics.read_text())
                print(f"Received {report.get('received_bytes', 0)} bytes; "
                      f"decoded {report.get('decoded_records', 0)} rows; "
                      f"normalized {report.get('normalized_frames', 0)} frames.", file=sys.stderr)
                print(f"Core stop confirmed: {report.get('core_stop_confirmed', False)}; "
                      f"radio restoration verified: {report.get('restoration_verified') is True}.", file=sys.stderr)
                for issue in report.get('restoration_issues', []):
                    print(f'  {issue}', file=sys.stderr)
            except (OSError, ValueError, AttributeError):
                print('Could not read Core diagnostics; inspect the report file.', file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(run())
