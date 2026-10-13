"""PCAPNG transport can feed the existing live decoder without another capture."""
import os
from pathlib import Path
import select
import shutil
import subprocess

import pytest

from beacon_live.live import available_tshark_fields, build_tshark_command, select_tshark_frame_fields
from beacon_live.parser import parse_tshark_live_record
from beacon_live.retry_debug import analyze_retry_frames

FIXTURES = Path(__file__).parent / 'fixtures/tshark_4_4'


@pytest.mark.parametrize('name,width,frequency', [('ht40', '40', 2412), ('vht160', '160', 5180),
                                                ('he80', '80', 5975), ('eht320', '320', 5955)])
def test_incremental_pcapng_stdin_decodes_before_stream_closes(name, width, frequency):
    tshark = os.environ.get('WLANPI_TEST_TSHARK') or shutil.which('tshark')
    if not tshark:
        pytest.skip('TShark is required for the decoder transport experiment')
    pcapng = subprocess.run([tshark, '-r', str(FIXTURES / f'{name}.pcap'), '-F', 'pcapng', '-w', '-'],
                            capture_output=True, check=True, timeout=15).stdout
    fields = select_tshark_frame_fields(available_tshark_fields(tshark=tshark))
    command = build_tshark_command('unused', field_names=fields)
    command[0] = tshark
    index = command.index('-i')
    command[index:index + 6] = ['-r', '-']
    assert '-i' not in command
    assert '-w' not in command
    # This authored fixture conversion uses little-endian PCAPNG. Feed section,
    # interface and the first complete packet while leaving stdin open.
    offset = 0
    while offset < len(pcapng):
        kind = int.from_bytes(pcapng[offset:offset + 4], 'little')
        size = int.from_bytes(pcapng[offset + 4:offset + 8], 'little')
        assert size >= 12 and offset + size <= len(pcapng)
        offset += size
        if kind == 6:
            break
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    try:
        process.stdin.write(pcapng[:offset])
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 5)[0], 'Decoder buffered the first packet until EOF'
        first = process.stdout.readline().decode()
        # Arbitrary WebSocket chunk boundaries need not align with PCAPNG blocks.
        for start in range(offset, len(pcapng), 17):
            process.stdin.write(pcapng[start:start + 17])
        process.stdin.close()
        process.stdin = None
        remaining, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr.decode(errors='replace')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    rows = [first] + remaining.decode().splitlines()
    frames = tuple(frame for row in rows for frame in parse_tshark_live_record(row, field_names=fields))
    assert len(frames) == 9
    assert frames[0].rssi_dbm == -47 and frames[0].qbss_cu_raw == 128
    assert frames[0].channel_definition.width.value == width
    assert frames[0].channel_definition.primary_frequency_mhz == frequency
    audit = next(r for r in analyze_retry_frames(frames) if r.scope == 'channel')
    assert audit.retry_eligible_frame_count == 4 and audit.retry_frame_count == 3
