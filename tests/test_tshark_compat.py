"""Trixie field contract and original packet regressions, without Wi-Fi hardware."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct

import pytest

from beacon_live import live
from beacon_live.channel import ChannelWidth
from beacon_live.parser import (
    TSHARK_FRAME_FIELD_NAMES, TSHARK_REQUIRED_FRAME_FIELD_NAMES,
    parse_tshark_capture_record, parse_tshark_live_record,
)
from beacon_live.retry_debug import analyze_retry_frames

ROOT = Path(__file__).parent / 'fixtures' / 'tshark_4_4'
CASES = [('ht40', 2412, '40', 2422, None), ('vht160', 5180, '160', 5250, None),
         ('he80', 5975, '80', 5985, None), ('eht320', 5955, '320', 6105, 4)]


def check_records(rows, fields, frequency, width, center, puncturing):
    frames = tuple(f for row in rows for f in parse_tshark_capture_record(row, field_names=fields))
    fast = tuple(f for row in rows for f in parse_tshark_live_record(row, field_names=fields))
    assert len(frames) == len(fast) == 9
    beacon = frames[0]
    assert beacon.is_beacon and beacon.rssi_dbm == -47
    assert beacon.qbss_cu_raw == 128
    assert beacon.qbss_cu_percent == pytest.approx(128 / 255 * 100)
    assert beacon.qbss_station_count == 12 and beacon.qbss_admission_capacity == 42
    assert beacon.beacon_interval_tu == 100
    definition = beacon.channel_definition
    assert definition is not None and definition.complete and not definition.ambiguous
    assert definition.primary_frequency_mhz == frequency
    assert definition.width is ChannelWidth(width)
    assert definition.center_frequency1_mhz == center
    assert definition.puncturing_bitmap == puncturing
    assert fast[0].channel_definition == definition
    # Two retry transmissions with the same sequence count twice, plus one
    # retry-capable authentication and one original data transmission.
    for records in (frames, fast):
        channel = next(r for r in analyze_retry_frames(records) if r.scope == 'channel')
        assert channel.retry_eligible_frame_count == 4
        assert channel.retry_frame_count == 3
        assert channel.retry_percent == 75.0
        assert channel.footer_bssid == '02:00:00:00:00:10'


@pytest.mark.parametrize('name,frequency,width,center,puncturing', CASES)
def test_saved_tshark_4_4_rows(name, frequency, width, center, puncturing):
    fields = tuple(json.loads((ROOT / 'fields.json').read_text()))
    rows = (ROOT / f'{name}.tsv').read_text().splitlines()
    check_records(rows, fields, frequency, width, center, puncturing)


@pytest.mark.parametrize('name,frequency,width,center,puncturing', CASES)
def test_real_tshark_decodes_packet_fixture(name, frequency, width, center, puncturing):
    tshark = os.environ.get('WLANPI_TEST_TSHARK') or shutil.which('tshark')
    if not tshark:
        pytest.skip('TShark unavailable; saved 4.4 output is tested separately')
    fields = live.select_tshark_frame_fields(live.available_tshark_fields(tshark=tshark))
    command = live.build_tshark_command('unused', field_names=fields)
    command[0] = tshark
    index = command.index('-i')
    command[index:index + 6] = ['-r', str(ROOT / f'{name}.pcap')]
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=15)
    check_records(result.stdout.splitlines(), fields, frequency, width, center, puncturing)


def test_real_tshark_boolean_fcs_and_multiple_antennas(tmp_path):
    tshark = os.environ.get('WLANPI_TEST_TSHARK') or shutil.which('tshark')
    if not tshark:
        pytest.skip('TShark unavailable; boolean/multi-antenna parser tests run separately')
    # Original synthetic frames with three radiotap namespaces, each exposing
    # an antenna signal. FLAGS is a boolean source, as on the device adapter.
    radio = (struct.pack('<BBHIII', 0, 0, 28, 0xA000082A, 0xA0000820, 0x820)
             + struct.pack('<BxHHbBbBbB', 0, 2412, 0x0040, -47, 0, -55, 1, -48, 2))
    source = (ROOT / 'ht40.pcap').read_bytes()
    capture = bytearray(source[:24])
    offset = 24
    while offset < len(source):
        sec, usec, length, original_length = struct.unpack_from('<IIII', source, offset)
        packet = source[offset + 16:offset + 16 + length]
        radio_length = int.from_bytes(packet[2:4], 'little')
        packet = radio + packet[radio_length:]
        capture += struct.pack('<IIII', sec, usec, len(packet), original_length + len(radio) - radio_length)
        capture += packet
        offset += 16 + length
    path = tmp_path / 'multi-antenna.pcap'
    path.write_bytes(capture)
    fields = live.select_tshark_frame_fields(live.available_tshark_fields(tshark=tshark))
    command = live.build_tshark_stream_command(field_names=fields)
    command[0] = tshark
    command[command.index('-r') + 1] = str(path)
    decoded = subprocess.run(command, capture_output=True, text=True, check=True, timeout=15)
    rows = decoded.stdout.splitlines()
    values = [dict(zip(fields, row.split('\t'))) for row in rows]
    assert all(row['radiotap.dbm_antsignal'] == '-47|-55|-48' for row in values)
    assert all(row['radiotap.flags.badfcs'].lower() in {'false', '0'} for row in values)
    check_records(rows, fields, 2412, '40', 2422, None)


def test_optional_metadata_missing_does_not_disable_capture():
    fields = live.select_tshark_frame_fields(frozenset(TSHARK_REQUIRED_FRAME_FIELD_NAMES))
    assert fields == TSHARK_REQUIRED_FRAME_FIELD_NAMES
    all_fields = tuple(json.loads((ROOT / 'fields.json').read_text()))
    values = dict(zip(all_fields, (ROOT / 'he80.tsv').read_text().splitlines()[0].split('\t')))
    row = '\t'.join(values[name] for name in fields)
    record = parse_tshark_capture_record(row, field_names=fields)[0]
    assert record.qbss_station_count == 12 and record.rssi_dbm == -47
    assert record.ap_name is None


def test_missing_required_field_fails_before_radio_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(live, 'available_tshark_fields', lambda: frozenset(TSHARK_FRAME_FIELD_NAMES) - {'wlan.fc.retry'})
    monkeypatch.setattr(live, 'discover_capture_target', lambda *a, **kw: pytest.fail('radio discovery must not run'))
    monkeypatch.setattr(live, 'RadioSession', lambda *a, **kw: pytest.fail('radio session must not run'))
    with pytest.raises(ValueError, match='missing required capture fields: wlan.fc.retry'):
        live.run_live(band='2.4', channel='1', logging_only=True, stats_csv=tmp_path / 'stats.csv')


@pytest.mark.parametrize('failure', [FileNotFoundError('missing tshark'), subprocess.TimeoutExpired('tshark', 15)])
def test_field_probe_reports_execution_failure(failure):
    def run(command, **options):
        assert options['timeout'] == 15
        raise failure
    with pytest.raises(ValueError, match='Unable to inspect TShark capture fields'):
        live.available_tshark_fields(command_runner=run)


@pytest.mark.parametrize('status,stdout,stderr,message', [
    (1, '', 'bad plugin', 'bad plugin'),
    (0, 'P\tWLAN\twlan\n', '', 'no field definitions'),
])
def test_field_probe_reports_bad_registry(status, stdout, stderr, message):
    with pytest.raises(ValueError, match=message):
        live.available_tshark_fields(command_runner=lambda command, **kw: subprocess.CompletedProcess(command, status, stdout, stderr))
