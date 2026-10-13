"""Core evaluation issues only metadata commands and preserves credentials."""
import asyncio
import json
from pathlib import Path
import subprocess

import pytest

from scripts import check_core_capture as probe


def session(**changes):
    result = {
        'session_id': 'cap_example', 'owner': 'other-tool', 'namespace': None,
        'interfaces': ['wlanpi0'],
        'config': {'interfaces': {'wlanpi0': {'channels': [{'freq': 2412, 'width': 20}], 'dwell_time': 250}}, 'pcap_filter': ''},
    }
    result.update(changes)
    return result


class FakeSocket:
    def __init__(self, events):
        self.events = iter(events)
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        return next(self.events)


def event(code, data, kind='status'):
    return json.dumps({'type': 'event', 'event': kind, 'code': code, 'data': data})


def test_metadata_probe_does_not_start_stop_or_subscribe_to_existing_capture():
    ws = FakeSocket([event('AUTH_OK', {'did': 'evaluation'}), event('SESSIONS', {'sessions': [session()]}),
                     event('SUPPORTED_FREQUENCIES', {'wlanpi0': [2412, 5180, 5975]}, 'frequencies')])
    result = asyncio.run(probe.inspect_connection(ws, 'private-token', 'wlanpi0', 2412, 20, 1))
    assert [msg['command'] for msg in ws.sent] == ['auth', 'list_sessions', 'get_supported_frequencies']
    assert result['sessions'][0]['configured_candidate'] is True
    assert result['sessions'][0]['actual_tuning_verified'] is False
    assert 'private-token' not in json.dumps(result)


@pytest.mark.parametrize('config', [
    {},
    {'interfaces': {'wlanpi0': {'channels': []}}, 'pcap_filter': ''},
    {'interfaces': {'wlanpi0': {'channels': [{'freq': 2412, 'width': 20}, {'freq': 2437, 'width': 20}]}}, 'pcap_filter': ''},
    {'interfaces': {'wlanpi0': {'channels': [{'freq': 2412, 'width': 20}]}}, 'pcap_filter': 'type mgt subtype beacon'},
    {'interfaces': {'wlanpi0': {'channels': [{'freq': 5180, 'width': 80}]}}, 'pcap_filter': ''},
    {'interfaces': {'wlanpi0': 'invalid'}, 'pcap_filter': ''},
])
def test_incompatible_owner_configuration_is_not_a_candidate(config):
    result = probe.session_assessment(session(config=config), 'wlanpi0', 2412, 20)
    assert result['configured_candidate'] is False and result['reasons']


def test_multiple_sources_are_not_silently_combined():
    result = probe.session_assessment(session(interfaces=['wlanpi0', 'wlanpi1']), 'wlanpi0', 2412, 20)
    assert result['configured_candidate'] is False


def test_auth_failure_stops_before_session_commands_and_hides_server_payload():
    ws = FakeSocket([event('AUTH_FAILED', {'message': 'do not print private-token'}, 'error')])
    with pytest.raises(ValueError, match='AUTH_FAILED') as error:
        asyncio.run(probe.inspect_connection(ws, 'private-token', 'wlanpi0', 2412, 20, 1))
    assert 'private-token' not in str(error.value)
    assert len(ws.sent) == 1


def test_unexpected_binary_metadata_is_rejected():
    with pytest.raises(ValueError, match='Unexpected packet data'):
        asyncio.run(probe.receive_event(FakeSocket([b'packet']), 'AUTH_OK', 1))


def test_auth_token_is_captured_in_memory_not_in_arguments():
    calls = []
    def run(command, **options):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '{"access_token":"private-token"}', '')
    assert probe.obtain_token(None, runner=run) == 'private-token'
    assert calls == [['getjwt', probe.IDENTITY, '--no-color']]


def test_failed_token_command_does_not_print_raw_auth_output():
    runner = lambda command, **kw: subprocess.CompletedProcess(command, 1, 'private-token', 'private-token')
    with pytest.raises(ValueError) as error:
        probe.obtain_token(None, runner=runner)
    assert 'private-token' not in str(error.value)


def test_existing_token_file_avoids_token_issuance(tmp_path):
    path = tmp_path / 'token'
    path.write_text('private-token\n')
    assert probe.obtain_token(path, runner=lambda *a, **kw: pytest.fail('getjwt must not run')) == 'private-token'


@pytest.mark.parametrize('url', [
    'wss://localhost:31415/api/v1/streaming/capture?token=private-token',
    'ws://remote-device:8000/api/v1/streaming/capture',
    'wss://user:password@localhost:31415/api/v1/streaming/capture',
    'https://localhost:31415/api/v1/streaming/capture',
])
def test_invalid_or_credential_bearing_urls_are_rejected(url):
    with pytest.raises(ValueError):
        probe.validate_url(url)


def test_help_works_without_websocket_dependency():
    script = Path(__file__).parents[1] / 'scripts/check_core_capture.py'
    result = subprocess.run([__import__('sys').executable, str(script), '--help'], capture_output=True, text=True, check=True)
    assert '--token-file' in result.stdout and '--width' in result.stdout
