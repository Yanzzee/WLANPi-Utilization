"""Core acquisition uses the same run_live analysis/logging/display path."""
import csv
import json

import pytest

from beacon_live import live, core_capture
from beacon_live.capture_target import CaptureTarget
from beacon_live.channel import ChannelWidth, RadioCapabilities
from beacon_live.parser import TSHARK_FRAME_FIELD_NAMES
from test_live import _FakeTsharkProcess, _prepare_one_interval_live_run


@pytest.fixture
def prepared(monkeypatch):
    _prepare_one_interval_live_run(monkeypatch)
    monkeypatch.setattr(core_capture, 'check_core_dependency', lambda: None)
    monkeypatch.setattr(live, 'available_tshark_fields', lambda: frozenset(TSHARK_FRAME_FIELD_NAMES))
    monkeypatch.setattr(live, 'discover_capture_target', lambda freq, **kw: CaptureTarget('wlanpi0', 'lab'))
    monkeypatch.setattr(live, '_read_radio_capabilities_safely', lambda iface: RadioCapabilities(
        frozenset({ChannelWidth.MHZ20}), frozenset({2412}), supports_monitor=True, source_complete=True))
    events = []
    class Radio:
        original_mode = 'monitor'
        def __enter__(self): return self
        def prepare(self): events.append('prepare')
        def __exit__(self, *args): events.append('restore')
    def radio(*args, **kw):
        assert kw['require_idle_before_restore'] is True
        return Radio()
    monkeypatch.setattr(live, 'RadioSession', radio)
    monkeypatch.setattr(live, 'configure_monitor_interface', lambda *a, **kw: pytest.fail('Core must perform tuning'))
    monkeypatch.setattr(live, 'start_tshark_process', lambda *a, **kw: pytest.fail('No independent local capture'))
    monkeypatch.setattr(live, 'run_checked_command', lambda command: events.append(('up', command)))
    monkeypatch.setattr(live, '_read_actual_channel_safely', lambda iface, requested: (events.append('verify') or (requested, True)))
    class Process(_FakeTsharkProcess):
        capture_error = ''
        session_id = 'cap_test'
        stop_confirmed = False
        def wait_phase(self, phase): events.append(phase)
        def start_capture(self): events.append('core-start')
        def close(self):
            events.append('core-stop')
            self.stop_confirmed = True
    process = Process()
    def start(target, definition, **kwargs):
        assert target == CaptureTarget('wlanpi0', 'lab')
        events.append('connect')
        return process
    monkeypatch.setattr(live, 'start_core_capture_process', start)
    return events, process


def test_core_runs_shared_metrics_once_and_records_backend_provenance(prepared, tmp_path):
    events, process = prepared
    stats, beacons, raw = tmp_path / 'stats.csv', tmp_path / 'beacons.jsonl', tmp_path / 'raw.pcapng'
    assert live.run_live(capture_backend='core', band='2.4', channel='1', interval_seconds=.1,
                         stats_csv=stats, beacons_jsonl=beacons, raw_capture_path=raw, min_free_bytes=0) == 0
    assert events[:6] == ['connect', 'ready', 'prepare', ('up', ['ip','netns','exec','lab','ip','link','set','wlanpi0','up']), 'core-start', 'verify']
    assert events[-2:] == ['core-stop', 'restore']
    rows = list(csv.DictReader(stats.open()))
    assert rows and all(row['interface'] == 'wlanpi0' and row['network_namespace'] == 'lab' for row in rows)
    assert any(row['retry_eligible_frame_count'] == '2' and row['retry_frame_count'] == '1' for row in rows)
    metadata = json.loads(raw.with_suffix('.pcapng.json').read_text())
    assert metadata['capture_backend'] == 'core' and metadata['core_role'] == 'owner'
    assert metadata['core_session_id'] == 'cap_test' and metadata['core_stop_confirmed']
    assert metadata['actual_channel_definition_verified'] is True
    assert events.count('core-start') == events.count('core-stop') == 1


@pytest.mark.parametrize('verified', [True, False])
def test_wrong_or_unverified_actual_tuning_stops_core_before_restoration(prepared, monkeypatch, verified):
    events, _ = prepared
    monkeypatch.setattr(live, '_read_actual_channel_safely', lambda iface, requested: (
        live.ChannelDefinition(2437, ChannelWidth.MHZ20, 2437) if verified else requested, verified))
    monkeypatch.setattr(live, '_start_live_aggregation_process', lambda *a, **kw: pytest.fail('Must not analyze unverified tuning'))
    with pytest.raises(ValueError, match='actual tuning could not be verified'):
        live.run_live(capture_backend='core', band='2.4', channel='1', interval_seconds=.1)
    assert events[-2:] == ['core-stop', 'restore']


def test_ready_failure_does_not_prepare_radio(prepared, monkeypatch):
    events, process = prepared
    def wait(phase): raise ValueError('Core inventory conflict')
    monkeypatch.setattr(process, 'wait_phase', wait)
    with pytest.raises(ValueError, match='inventory conflict'):
        live.run_live(capture_backend='core', band='2.4', channel='1')
    assert 'prepare' not in events and 'core-start' not in events
    assert events[-2:] == ['core-stop', 'restore']


def test_missing_dependency_fails_before_radio_discovery(monkeypatch):
    monkeypatch.setattr(core_capture.importlib.util, 'find_spec', lambda name: None)
    monkeypatch.setattr(live, 'discover_capture_target', lambda *a, **kw: pytest.fail('No hardware may be inspected'))
    with pytest.raises(ValueError, match='prototype needs websockets'):
        live.run_live(capture_backend='core')


def test_stream_command_retains_decoder_preferences_without_capture_arguments():
    command = live.build_tshark_stream_command(field_names=TSHARK_FRAME_FIELD_NAMES)
    assert '-i' not in command and '-s' not in command and '-B' not in command and '-w' not in command
    assert command[command.index('-r') + 1] == '-'
    assert '-l' in command and command[command.index('-Y') + 1] == 'wlan'
    assert 'wlan.defragment:FALSE' in command and 'wlan.enable_decryption:FALSE' in command
