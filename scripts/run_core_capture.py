#!/usr/bin/env python3
"""Run the Core-owner prototype using Core's interpreter/dependencies."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from beacon_live.device import main

if __name__ == '__main__':
    if any(arg == '--capture-backend' or arg.startswith('--capture-backend=') for arg in sys.argv[1:]):
        raise SystemExit('run_core_capture.py selects Core automatically; omit --capture-backend')
    raise SystemExit(main(['--capture-backend', 'core', *sys.argv[1:]]))
