"""Core-owner lifecycle and fail-closed transport tests without Pi hardware."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from beacon_live import core_capture as core
from beacon_live.capture_target import CaptureTarget
from beacon_live.channel import ChannelDefinition, ChannelWidth


def request(frequency=2412, width='20', center=None, interface='wlanpi0', namespace=None):
    return core.CoreRequest(CaptureTarget(interface, namespace), ChannelDefinition(frequency, ChannelWidth(width), center or frequency))


def event(code, data=None, kind='status'):
    return json.dumps({'type': 'event', 'event': kind, 'code': code, 'data': data or {}})


class Socket:
    def __init__(self, spec, *, conflict=False, tune_error=False, corrupt_start=False, stream_error=False):
        self.spec = spec
        self.queue = asyncio.Queue()
        self.sent = []
        self.conflict, self.tune_error = conflict, tune_error
        self.corrupt_start, self.stream_error = corrupt_start, stream_error

    async def send(self, raw):
        command = json.loads(raw)
        self.sent.append(command)
        name = command['command']
        data = {}
        if name == 'auth':
            code = 'AUTH_OK'
        elif name == 'list_sessions':
            code, data = 'SESSIONS', {'sessions': [{'interfaces': ['wlanpi0']}] if self.conflict else []}
        elif name == 'get_supported_frequencies':
            code, data = 'SUPPORTED_FREQUENCIES', {'wlanpi0': [2412]}
        elif name == 'configure':
            code = 'CONFIG_APPLIED'
        elif name == 'start':
            if self.tune_error:
                self.queue.put_nowait(event('CHANNEL_SET_FAILED', {'message': 'Device or resource busy (-16)'}, 'error'))
            # Core may deliver bytes before CAPTURE_STARTED; preserve ordering.
            self.queue.put_nowait(b'first-block')
            code, data = 'CAPTURE_STARTED', {
                'session_id': 'cap_test', 'interfaces': ['wlanpi0'], 'namespace': None,
                'config': self.spec.configuration if not self.corrupt_start else {'pcap_filter': 'beacons'},
            }
        elif name == 'stop':
            code = 'CAPTURE_STOPPED'
        else:
            raise AssertionError(name)
        self.queue.put_nowait(event(code, data))
        if name == 'start':
            self.queue.put_nowait(event('CAPTURE_ENDED') if self.stream_error else b'second-block')

    async def recv(self):
        return await self.queue.get()


async def exercise(*, conflict=False, tune_error=False, corrupt_start=False, stream_error=False, layout=None):
    spec = request()
    socket = Socket(spec, conflict=conflict, tune_error=tune_error, corrupt_start=corrupt_start, stream_error=stream_error)
    controls = asyncio.Queue()
    controls.put_nowait('start')
    statuses, chunks = [], []
    async def layout_reader():
        return layout if layout is not None else {'root': {'wlanpi0': {'type': 'monitor'}}}
    async def write(data):
        chunks.append(data)
        if len(chunks) == 2:
            controls.put_nowait('stop')
    error = None
    try:
        await core.run_owner(socket, 'private-token', spec, layout_reader=layout_reader,
                             send_status=statuses.append, next_control=controls.get, write_bytes=write)
    except core.CoreCaptureError as exc:
        error = exc
    return socket, statuses, chunks, error


def test_owner_forwards_each_chunk_once_and_stops_its_own_session():
    socket, statuses, chunks, error = asyncio.run(exercise())
    assert error is None
    assert chunks == [b'first-block', b'second-block']
    assert [s['phase'] for s in statuses] == ['ready', 'started', 'stopped']
    assert statuses[-1]['confirmed'] is True
    assert [c['command'] for c in socket.sent] == ['auth', 'list_sessions', 'get_supported_frequencies', 'configure', 'start', 'stop']
    assert socket.sent[-2]['pcap_filter'] == ''
    assert 'private-token' not in json.dumps(statuses)


def test_initial_tune_failure_stops_core_even_if_it_continues_capturing():
    socket, statuses, chunks, error = asyncio.run(exercise(tune_error=True))
    assert 'CHANNEL_SET_FAILED' in str(error)
    assert 'busy (-16)' in str(error)
    assert chunks == []
    assert socket.sent[-1]['command'] == 'stop'
    assert statuses[-1]['confirmed'] is True


def test_existing_owner_is_refused_without_mutating_or_stopping_it():
    socket, statuses, chunks, error = asyncio.run(exercise(conflict=True))
    assert 'already has an owner' in str(error)
    assert [c['command'] for c in socket.sent] == ['auth', 'list_sessions']
    assert statuses[-1]['attempted'] is False
    assert chunks == []


def test_namespace_mismatch_is_rejected_before_configuration():
    socket, _, chunks, error = asyncio.run(exercise(layout={'lab': {'wlanpi0': {'type': 'monitor'}}}))
    assert 'different namespace' in str(error)
    assert [c['command'] for c in socket.sent] == ['auth']
    assert chunks == []


def test_started_configuration_is_verified_before_forwarding_packet_bytes():
    socket, statuses, chunks, error = asyncio.run(exercise(corrupt_start=True))
    assert 'configuration differs' in str(error)
    assert chunks == [] and socket.sent[-1]['command'] == 'stop'
    assert statuses[-1]['confirmed'] is True


def test_unexpected_end_is_an_error_and_still_cleans_up():
    socket, statuses, chunks, error = asyncio.run(exercise(stream_error=True))
    assert 'ended unexpectedly' in str(error)
    assert chunks == [b'first-block']
    assert socket.sent[-1]['command'] == 'stop' and statuses[-1]['confirmed']


@pytest.mark.parametrize('freq,width,center', [(2412,'20',2412),(5180,'80',5210),(5745,'80',5775),(5975,'80',5985)])
def test_supported_core_prototype_definitions(freq, width, center):
    core.validate_core_request(request(freq, width, center))


@pytest.mark.parametrize('spec', [request(5765,'80',5775), request(5785,'80',5775), request(5805,'80',5775),
                                request(2412,'40',2422), request(5180,'160',5250), request(interface='renamed'),
                                request(5180,'80',5290)])
def test_unrepresentable_requests_fail_before_connecting(spec):
    with pytest.raises(core.CoreCaptureError):
        core.validate_core_request(spec)


def test_decoder_backpressure_is_bounded_and_never_silently_discards(monkeypatch):
    read, write = os.pipe()
    os.set_blocking(write, False)
    monkeypatch.setattr(core, 'WRITE_TIMEOUT', 0.02)
    try:
        with pytest.raises(core.CoreCaptureError, match='cannot keep up'):
            asyncio.run(core._write_pipe(write, b'x' * 1024 * 1024))
    finally:
        os.close(read)
        os.close(write)


def test_layout_requires_unique_monitor_source():
    with pytest.raises(core.CoreCaptureError, match='ambiguous'):
        core.validate_core_layout({'root': {'wlanpi0': {'type':'monitor'}}, 'lab': {'wlanpi0': {'type':'monitor'}}}, CaptureTarget('wlanpi0'))
    with pytest.raises(core.CoreCaptureError, match='already be a monitor'):
        core.validate_core_layout({'root': {'wlanpi0': {'type':'managed'}}}, CaptureTarget('wlanpi0'))


def test_missing_core_certificate_has_actionable_error(tmp_path):
    spec = core.CoreRequest(CaptureTarget('wlanpi0'), ChannelDefinition(2412),
                            ca_cert=tmp_path / 'missing.cert')
    with pytest.raises(core.CoreCaptureError, match='Cannot load Core certificate'):
        core._tls_context(spec)


def test_closed_decoder_pipe_is_reported_as_decoder_failure():
    read, write = os.pipe()
    os.close(read)
    try:
        with pytest.raises(core.CoreCaptureError, match='decoder closed its input'):
            asyncio.run(core._write_pipe(write, b'pcapng'))
    finally:
        os.close(write)


def test_close_stops_transport_before_decoder_even_after_decoder_exit():
    calls = []
    process = core.CoreCaptureProcess.__new__(core.CoreCaptureProcess)
    process.closed = False
    process.attempted = True
    process.stop_confirmed = True
    process.events = []
    process.capture_error = ''
    class Worker:
        alive = True
        def is_alive(self): return self.alive
        def join(self, timeout):
            calls.append('core-stopped')
            self.alive = False
        def terminate(self): pytest.fail('confirmed stop must not kill worker')
    class Connection:
        def send(self, command): calls.append(command)
        def poll(self): return False
        def close(self): calls.append('ipc-closed')
    class Decoder:
        def poll(self): return None
        def terminate(self): calls.append('decoder-stopped')
        def wait(self, timeout): return 0
    process.worker, process.connection, process.decoder = Worker(), Connection(), Decoder()
    process.close()
    assert calls == ['stop', 'core-stopped', 'ipc-closed', 'decoder-stopped']
    process.close()
    assert len(calls) == 4


def test_unconfirmed_stop_is_reported_and_decoder_still_closes():
    process = core.CoreCaptureProcess.__new__(core.CoreCaptureProcess)
    process.closed, process.attempted, process.stop_confirmed = False, True, False
    process.events, process.capture_error = [], ''
    class Worker:
        def is_alive(self): return False
    class Connection:
        def poll(self): return False
        def close(self): pass
    class Decoder:
        stopped = False
        def poll(self): return None
        def terminate(self): self.stopped = True
        def wait(self, timeout): return 0
    process.worker, process.connection, process.decoder = Worker(), Connection(), Decoder()
    with pytest.raises(core.CoreCaptureError, match='stop was not confirmed'):
        process.close()
    assert process.decoder.stopped


def test_worker_death_is_an_error_even_before_decoder_exits():
    process = core.CoreCaptureProcess.__new__(core.CoreCaptureProcess)
    process.closed, process.capture_error, process.events = False, '', []
    class Connection:
        def poll(self): return False
    class Decoder:
        def poll(self): return None
    class Worker:
        def is_alive(self): return False
    process.connection, process.decoder, process.worker = Connection(), Decoder(), Worker()
    assert process.poll() == 1
    assert 'transport exited' in process.capture_error
