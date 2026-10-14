import csv
import json

import pytest

from beacon_live.ssid import normalize_tshark_ssid
from beacon_live.parser import (TSHARK_FRAME_FIELD_NAMES, TSHARK_OPTIONAL_FRAME_FIELD_NAMES,
    parse_tshark_row, parse_tshark_rows, parse_tshark_frame_row,
    parse_tshark_capture_record, parse_tshark_frame_rows, parse_tshark_live_record)


@pytest.mark.parametrize('value,expected', [
    ('5472697869652d54657374', 'Trixie-Test'),
    ('Café Wi-Fi'.encode().hex(), 'Café Wi-Fi'),
    ('東京 📡'.encode().hex(), '東京 📡'),
    ('4142'.encode().hex(), '4142'),
    ('deadbeef'.encode().hex(), 'deadbeef'),
    ('<MISSING>'.encode().hex(), '<MISSING>'),
    ('Lab|Guest'.encode().hex(), 'Lab|Guest'),
    (' Home '.encode().hex(), ' Home '), ('2020', '  '), ('30', '0'),
    ('41:42:43', 'ABC'), ('414243', 'ABC'),
    ('', None), ('<MISSING>', None), ('00', None), ('00' * 32, None),
    ('fffe', 'hex:fffe'), ('436166e9', 'hex:436166e9'),
    ('576966690a', 'hex:576966690a'), ('410042', 'hex:410042'),
    ('411b5b324a', 'hex:411b5b324a'), ('e280a8', 'hex:e280a8'),
    ('Alpha', 'Alpha'), ('41zz', '41zz'),
])
def test_hex_ssid_normalization(value, expected):
    assert normalize_tshark_ssid(value, encoding='hex') == expected


@pytest.mark.parametrize('value', ['Cafe', '4142', 'deadbeef', '<MISSING>', ' Home '])
def test_explicit_text_input_is_never_guessed_to_be_hex(value):
    assert normalize_tshark_ssid(value, encoding='text') == value


def test_invalid_ssid_encoding_is_reported():
    with pytest.raises(ValueError, match='text or hex'):
        normalize_tshark_ssid('4142', encoding='auto')


def beacon_row(ssid):
    fields = TSHARK_FRAME_FIELD_NAMES + TSHARK_OPTIONAL_FRAME_FIELD_NAMES
    values = dict.fromkeys(fields, '')
    values.update({'frame.time_epoch': '1000.25', 'wlan.fc.type': '0',
        'wlan.fc.subtype': '8', 'wlan.fc.retry': 'False',
        'wlan.bssid': '02:00:00:00:00:10', 'wlan.ssid': ssid,
        'wlan.qbss.cu': '128', 'wlan.qbss.scount': '2', 'wlan.qbss.adc': '1000',
        'radiotap.dbm_antsignal': '-42|-48|-43', 'radiotap.flags.badfcs': 'False',
        'wlan.fixed.beacon': '100', 'wlan_radio.frequency': '2412'})
    return '\t'.join(values[name] for name in fields), fields


@pytest.mark.parametrize('ssid,expected', [('Café'.encode().hex(), 'Café'),
    ('4142'.encode().hex(), '4142'), ('', None), ('<MISSING>', None), ('ff', 'hex:ff')])
def test_all_parser_paths_decode_once_and_keep_metrics(ssid, expected):
    row, fields = beacon_row(ssid)
    for parse in (parse_tshark_capture_record, parse_tshark_live_record):
        frames = parse(row, field_names=fields, ssid_encoding='hex')
        assert len(frames) == 1 and frames[0].ssid == expected
        assert frames[0].qbss_cu_raw == 128 and frames[0].rssi_dbm == -42
    # Named frame input and seven-field beacon replay use the same decoder.
    frame = parse_tshark_frame_row(row.replace('-42|-48|-43', '-42'),
                                  field_names=fields, ssid_encoding='hex')
    assert frame is not None and frame.ssid == expected
    beacon = parse_tshark_row(f'1000.25\t{ssid}\t02:00:00:00:00:10\t128\t2\t1000\t-42',
                              ssid_encoding='hex')
    assert beacon is not None and beacon.ssid == expected
    assert list(parse_tshark_rows(
        [f'1000.25\t{ssid}\t02:00:00:00:00:10\t128\t2\t1000\t-42'],
        ssid_encoding='hex'))[0].ssid == expected
    core_row = row.split('\t')[:len(TSHARK_FRAME_FIELD_NAMES)]
    core_row[13] = '-42'
    assert list(parse_tshark_frame_rows(['\t'.join(core_row)], ssid_encoding='hex'))[0].ssid == expected


def test_shared_snapshot_supplies_readable_ssid_to_displays_and_logs(tmp_path):
    from beacon_live.analyzer import Analyzer
    from beacon_live.dashboard import TerminalDashboard
    from beacon_live.lcd_dashboard import LcdDashboard
    from beacon_live.log_writer import CaptureLogWriter, LogMetadata
    row, fields = beacon_row('Café'.encode().hex())
    frame = parse_tshark_live_record(row, field_names=fields, ssid_encoding='hex')[0]
    analyzer = Analyzer()
    analyzer.ingest(frame)
    analyzer.flush()
    snapshot = analyzer.snapshot
    terminal = TerminalDashboard()
    terminal.update(snapshot)
    lcd = LcdDashboard(tmp_path / 'display.ppm', band='2.4', channel='1')
    lcd.refresh(snapshot)
    assert 'Café' in terminal.render()
    assert lcd.text_lines[2] == 'Café'
    assert json.loads((tmp_path / 'display.json').read_text())['ssid'] == 'Café'
    stats, beacons = tmp_path / 'stats.csv', tmp_path / 'beacons.jsonl'
    with CaptureLogWriter(metadata=LogMetadata(interface='wlanpi0', channel='1'),
                         stats_csv=stats, beacons_jsonl=beacons) as writer:
        writer.write_beacon(frame.beacon_record())
        writer.write_stats(snapshot.history[-1])
    assert json.loads(beacons.read_text())['ssid'] == 'Café'
    assert next(csv.DictReader(stats.open()))['selected_qbss_ssid'] == 'Café'
