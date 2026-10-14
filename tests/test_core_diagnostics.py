import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from beacon_live.core_diagnostics import CoreDiagnostics
from test_radio_session import RadioHardware


def process():
    return SimpleNamespace(session_id='cap_test', received_bytes=1024,
        forwarded_bytes=1024, chunks=3, stop_confirmed=False, capture_error='')


@pytest.mark.parametrize('namespace', [None, 'lab'])
def test_baseline_precedes_mutation_and_report_verifies_restoration(tmp_path, namespace):
    hardware = RadioHardware(namespace)
    report = tmp_path / 'report.json'
    capture = process()
    with CoreDiagnostics(report) as diagnostics, hardware.session(tmp_path) as radio:
        diagnostics.bind_radio(radio)
        saved = json.loads(report.read_text())
        assert saved['restoration_verified'] is None
        assert all(state['up'] for state in saved['before'])
        radio.prepare()
        hardware.interfaces['capture0']['channel'] = 2437
        diagnostics.update(capture, records=9, frames=9, force=True)
        assert json.loads(report.read_text())['phase'] == 'capturing'
        capture.stop_confirmed = True
    saved = json.loads(report.read_text())
    assert saved['phase'] == 'finished' and saved['restoration_verified']
    assert saved['before'] == saved['after']
    assert saved['core_stop_confirmed'] and saved['decoded_records'] == 9
    assert saved['received_bytes'] == saved['forwarded_bytes'] == 1024


def test_readback_mismatch_is_not_reported_as_success(tmp_path):
    hardware = RadioHardware()
    report = tmp_path / 'report.json'
    with CoreDiagnostics(report) as diagnostics, hardware.session(tmp_path) as radio:
        diagnostics.bind_radio(radio)
        radio.prepare()
        # Pretend a successful tune command silently did nothing.
        radio.tune_builder = lambda name, definition: ['ip', 'link', 'set', name, 'up']
        hardware.interfaces['capture0']['channel'] = 2437
    saved = json.loads(report.read_text())
    assert not saved['restoration_verified']
    assert 'monitor tuning differs' in saved['restoration_issues'][0]


def test_unknown_previous_monitor_channel_is_explicit(tmp_path):
    hardware = RadioHardware()
    hardware.interfaces['capture0']['channel'] = None
    report = tmp_path / 'report.json'
    with CoreDiagnostics(report) as diagnostics, hardware.session(tmp_path) as radio:
        diagnostics.bind_radio(radio)
        radio.prepare()
    saved = json.loads(report.read_text())
    assert not saved['restoration_verified']
    assert 'original monitor tuning was unknown' in saved['restoration_issues'][0]


def test_start_failure_still_verifies_radio_and_transport_cleanup(tmp_path):
    hardware = RadioHardware()
    report = tmp_path / 'report.json'
    capture = process()
    with pytest.raises(ValueError, match='start failed'):
        with CoreDiagnostics(report) as diagnostics, hardware.session(tmp_path) as radio:
            diagnostics.bind_radio(radio)
            diagnostics.update(capture, force=True)
            radio.prepare()
            capture.stop_confirmed = True
            capture.capture_error = 'start failed'
            raise ValueError('start failed')
    saved = json.loads(report.read_text())
    assert saved['restoration_verified'] and saved['core_stop_confirmed']
    assert saved['transport_error'] == 'start failed'


def test_busy_radio_blocks_restoration_and_report_records_changed_sibling(tmp_path):
    hardware = RadioHardware()
    report = tmp_path / 'report.json'
    with pytest.raises(ValueError, match='cannot restore while another owner is active'):
        with CoreDiagnostics(report) as diagnostics, hardware.session(
            tmp_path, require_idle_before_restore=True) as radio:
            diagnostics.bind_radio(radio)
            radio.prepare()
            hardware.packet += '123 1 3 0003 6 1 0 0 0 999\n'
    saved = json.loads(report.read_text())
    assert not saved['restoration_verified']
    assert any('wifi1' in issue for issue in saved['restoration_issues'])


def test_shared_worker_reports_rejected_rows_before_capture_eof():
    from beacon_live.live import _start_live_aggregation_process, _stop_live_aggregation
    from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES
    from beacon_live.channel import ChannelDefinition, RadioCapabilities
    producer = subprocess.Popen([sys.executable, '-u', '-c',
        "import time; print('invalid-row'); time.sleep(10)"], stdout=subprocess.PIPE)
    aggregation = None
    try:
        aggregation = _start_live_aggregation_process(producer.stdout,
            field_names=TSHARK_FRAME_FIELD_NAMES, band='2.4', target_primary_frequency_mhz=2412,
            actual_definition=ChannelDefinition(2412), capabilities=RadioCapabilities.ht20_only())
        receiver, worker = aggregation
        assert receiver.poll(3), 'Parser rejection counts remained hidden until EOF'
        message = receiver.recv()
        assert producer.poll() is None and worker.is_alive() and not message.eof
        assert message.capture_record_count == message.malformed_capture_record_count == 1
        assert message.normalized_frame_count == 0 and message.stats == ()
    finally:
        producer.terminate()
        producer.wait(timeout=3)
        if aggregation is not None:
            _stop_live_aggregation(*aggregation)
        producer.stdout.close()
