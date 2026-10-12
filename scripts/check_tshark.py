#!/usr/bin/env python3
"""Read-only TShark field/packet check; never opens or tunes a live interface."""
from pathlib import Path
import argparse
import subprocess
import sys

# Support the production venv without installing development dependencies.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from beacon_live.live import available_tshark_fields, build_tshark_command, select_tshark_frame_fields
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES, TSHARK_OPTIONAL_FRAME_FIELD_NAMES, parse_tshark_live_record
from beacon_live.retry_debug import analyze_retry_frames

CASES = (
    ('ht40', 2412, '40', 2422, None),
    ('vht160', 5180, '160', 5250, None),
    ('he80', 5975, '80', 5985, None),
    ('eht320', 5955, '320', 6105, 4),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tshark', default='tshark', help='TShark executable to verify')
    args = parser.parse_args()
    try:
        version = subprocess.run([args.tshark, '--version'], capture_output=True, text=True, check=True, timeout=15)
        print(version.stdout.splitlines()[0], flush=True)
        available = available_tshark_fields(tshark=args.tshark)
        fields = select_tshark_frame_fields(available)
        missing = [f for f in TSHARK_FRAME_FIELD_NAMES + TSHARK_OPTIONAL_FRAME_FIELD_NAMES if f not in available]
        print(f'Required fields: OK; negotiated {len(fields)} fields', flush=True)
        print('Unavailable optional fields: ' + (', '.join(missing) or 'none'), flush=True)
        for name, frequency, width, center, puncturing in CASES:
            # Preserve production dissector/preferences/occurrences; replace only
            # live input and live-only buffer/snapshot options with a saved PCAP.
            command = build_tshark_command('unused', field_names=fields)
            command[0] = args.tshark
            index = command.index('-i')
            command[index:index + 6] = ['-r', str(ROOT / 'tests' / 'fixtures' / 'tshark_4_4' / f'{name}.pcap')]
            decoded = subprocess.run(command, capture_output=True, text=True, check=True, timeout=15)
            frames = tuple(f for row in decoded.stdout.splitlines() for f in parse_tshark_live_record(row, field_names=fields))
            if len(frames) != 9:
                raise ValueError(f'{name}: expected 9 decoded frames, received {len(frames)}')
            beacon = frames[0]
            definition = beacon.channel_definition
            if (not beacon.is_beacon or beacon.rssi_dbm != -47 or beacon.qbss_cu_raw != 128
                    or beacon.qbss_station_count != 12 or beacon.qbss_admission_capacity != 42
                    or definition is None or not definition.complete
                    or definition.primary_frequency_mhz != frequency or definition.width.value != width
                    or definition.center_frequency1_mhz != center or definition.puncturing_bitmap != puncturing):
                raise ValueError(f'{name}: beacon metrics or channel definition differ from fixture expectations')
            audit = next(r for r in analyze_retry_frames(frames) if r.scope == 'channel')
            if audit.retry_eligible_frame_count != 4 or audit.retry_frame_count != 3:
                raise ValueError(f'{name}: expected 3 retries / 4 eligible frames')
            print(f'{name}: OK (RSSI/QBSS/width, 3/4 retries = 75%)', flush=True)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'TShark check failed: {exc}', file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        return 1
    print('Packet/field checks passed. Live driver and survey validation remains device-specific.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
