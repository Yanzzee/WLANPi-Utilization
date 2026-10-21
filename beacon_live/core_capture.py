"""Explicit Core-owner prototype: isolated PCAPNG transport, shared decoder."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.util
import json
import logging
import multiprocessing
from multiprocessing.reduction import DupFd
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from beacon_live.capture_target import CaptureTarget
from beacon_live.channel import ChannelDefinition, ChannelWidth

DEFAULT_CORE_URL = 'wss://localhost:31415/api/v1/streaming/capture'
DEFAULT_CORE_CERT = Path('/etc/nginx/ssl/self-signed-wlanpi.cert')
COMMAND_TIMEOUT = 15.0
WRITE_TIMEOUT = 5.0


class CoreCaptureError(ValueError):
    """Core capture failed; no credentials belong in this exception."""


@dataclass(frozen=True)
class CoreRequest:
    target: CaptureTarget
    definition: ChannelDefinition
    url: str = DEFAULT_CORE_URL
    ca_cert: Path = DEFAULT_CORE_CERT
    raw_path: Path | None = None

    @property
    def configuration(self) -> dict:
        return {
            'interfaces': {self.target.interface: {
                'channels': [{'freq': self.definition.primary_frequency_mhz,
                              'width': int(self.definition.width.value)}],
                'dwell_time': 250,
            }},
            'pcap_filter': '',
        }


def validate_core_request(request: CoreRequest) -> None:
    parsed = urlsplit(request.url)
    if (parsed.scheme not in {'ws', 'wss'} or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}
            or parsed.path != '/api/v1/streaming/capture' or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise CoreCaptureError('Core prototype requires the local capture endpoint without URL credentials')
    if not re.fullmatch(r'wlanpi[0-9]{1,3}', request.target.interface):
        raise CoreCaptureError('Core capture requires an existing wlanpiN monitor interface')
    definition = request.definition
    if definition.width not in {ChannelWidth.MHZ20, ChannelWidth.MHZ80}:
        raise CoreCaptureError('Core owner prototype supports 20 and 80 MHz only')
    if definition.puncturing_bitmap or definition.center_frequency2_mhz is not None:
        raise CoreCaptureError('Core owner prototype does not accept punctured or split channel definitions')
    freq = definition.primary_frequency_mhz
    center = freq
    if definition.width is ChannelWidth.MHZ80:
        # Explicitly represent the shipped API's supported mappings. This is a
        # client acceptance boundary, not a replacement tune implementation.
        if 5180 <= freq <= 5720 and (freq - 5180) % 20 == 0:
            center = 5210 + ((freq - 5180) // 80) * 80
        elif freq in {5745, 5825}:
            center = freq + 30
        elif 5955 <= freq <= 7055 and (freq - 5955) % 20 == 0:
            center = 5985 + ((freq - 5955) // 80) * 80
        else:
            raise CoreCaptureError(f'Core 2.4.8 has no supported 80 MHz mapping for primary {freq} MHz; use an explicit 20 MHz request')
    if center != definition.center_frequency1_mhz:
        raise CoreCaptureError('Requested center cannot be represented by the shipped Core capture API')


def check_core_dependency() -> None:
    if importlib.util.find_spec('websockets') is None:
        raise CoreCaptureError('Core capture needs websockets in this Python environment; rerun the FPMS installer or install the project core extra')


def _tls_context(request: CoreRequest):
    if urlsplit(request.url).scheme != 'wss':
        return None
    try:
        return ssl.create_default_context(cafile=str(request.ca_cert))
    except (OSError, ssl.SSLError) as exc:
        raise CoreCaptureError(f'Cannot load Core certificate {request.ca_cert}; check the Core installation') from exc


def validate_core_layout(layout: dict, target: CaptureTarget) -> None:
    matches = []
    for namespace, interfaces in layout.items():
        if not isinstance(interfaces, dict) or 'error' in interfaces:
            continue
        info = interfaces.get(target.interface)
        if isinstance(info, dict):
            matches.append((None if namespace == 'root' else namespace, info))
    if len(matches) != 1 or matches[0][0] != target.namespace:
        raise CoreCaptureError('Core source is missing, ambiguous, or in a different namespace')
    if (matches[0][1].get('type') or matches[0][1].get('mode')) != 'monitor':
        raise CoreCaptureError('Core source must already be a monitor interface')


def validate_started(data: dict, request: CoreRequest) -> None:
    if (not isinstance(data, dict) or not isinstance(data.get('session_id'), str)
            or not data['session_id'] or data.get('interfaces') != [request.target.interface]
            or data.get('namespace') != request.target.namespace
            or data.get('config') != request.configuration):
        raise CoreCaptureError('Core started a session whose source or configuration differs from the request')


def _get_token() -> str:
    try:
        result = subprocess.run(['getjwt', 'wlanpi-utilization-owner', '--no-color'],
                                capture_output=True, text=True, check=False, timeout=15)
        token = json.loads(result.stdout).get('access_token') if result.returncode == 0 else None
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired) as exc:
        raise CoreCaptureError('Unable to obtain Core JWT; check getjwt, TLS, and root access') from exc
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise CoreCaptureError('Core JWT issuance failed; authentication output was withheld')
    return token


def _get_layout(request: CoreRequest, token: str, context) -> dict:
    parsed = urlsplit(request.url)
    url = f'{"https" if parsed.scheme == "wss" else "http"}://{parsed.netloc}/api/v1/network/config/status'
    query = Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urlopen(query, context=context, timeout=10) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError('invalid layout')
        return data
    except Exception as exc:
        raise CoreCaptureError('Cannot verify Core source layout before capture') from exc


def _event(raw: str) -> dict:
    try:
        event = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise CoreCaptureError('Invalid Core JSON event') from exc
    if not isinstance(event, dict):
        raise CoreCaptureError('Invalid Core event shape')
    if event.get('event') == 'error':
        code = event.get('code')
        code = code if isinstance(code, str) and re.fullmatch(r'[A-Z_]{1,64}', code) else 'UNKNOWN_ERROR'
        detail = ''
        if code == 'CHANNEL_SET_FAILED' and isinstance(event.get('data'), dict):
            detail = ': ' + str(event['data'].get('message', ''))[:500]
        raise CoreCaptureError(f'Core rejected capture ({code}){detail}')
    return event


async def _wait_event(ws, code: str, *, early: list[bytes] | None = None) -> dict:
    async def read():
        size = 0
        while True:
            raw = await ws.recv()
            if isinstance(raw, bytes):
                size += len(raw)
                if early is None or size > 2 * 1024 * 1024:
                    raise CoreCaptureError('Unexpected or excessive packet data during Core startup')
                early.append(raw)
                continue
            event = _event(raw)
            if event.get('code') == code:
                return event.get('data')
    try:
        return await asyncio.wait_for(read(), COMMAND_TIMEOUT)
    except TimeoutError as exc:
        raise CoreCaptureError(f'Timed out waiting for Core {code}') from exc


async def _write_pipe(fd: int, data: bytes) -> None:
    async def write():
        view = memoryview(data)
        while view:
            try:
                sent = os.write(fd, view)
                view = view[sent:]
            except BlockingIOError:
                loop = asyncio.get_running_loop()
                ready = loop.create_future()
                loop.add_writer(fd, lambda: None if ready.done() else ready.set_result(None))
                try:
                    await ready
                finally:
                    loop.remove_writer(fd)
    try:
        await asyncio.wait_for(write(), WRITE_TIMEOUT)
    except TimeoutError as exc:
        raise CoreCaptureError('Core decoder cannot keep up; capture stopped instead of dropping stream bytes') from exc
    except BrokenPipeError as exc:
        raise CoreCaptureError('Core decoder closed its input stream; capture stopped') from exc


async def run_owner(ws, token: str, request: CoreRequest, *, layout_reader, send_status,
                    next_control, write_bytes) -> None:
    """Protocol lifecycle; dependencies are injectable for hardware-free tests."""
    attempted = False
    stopped = False
    try:
        await ws.send(json.dumps({'command': 'auth', 'token': token}))
        await _wait_event(ws, 'AUTH_OK')
        validate_core_layout(await layout_reader(), request.target)
        await ws.send(json.dumps({'command': 'list_sessions'}))
        listed = await _wait_event(ws, 'SESSIONS')
        sessions = listed.get('sessions') if isinstance(listed, dict) else None
        if not isinstance(sessions, list):
            raise CoreCaptureError('Invalid Core session inventory')
        if any(request.target.interface in s.get('interfaces', []) for s in sessions if isinstance(s, dict)):
            raise CoreCaptureError('Core interface already has an owner; this prototype does not subscribe')
        await ws.send(json.dumps({'command': 'get_supported_frequencies'}))
        frequencies = await _wait_event(ws, 'SUPPORTED_FREQUENCIES')
        if not isinstance(frequencies, dict) or request.definition.primary_frequency_mhz not in frequencies.get(request.target.interface, []):
            raise CoreCaptureError('Core does not report the requested source/frequency as available')
        send_status({'phase': 'ready'})
        if await next_control() != 'start':
            return
        await ws.send(json.dumps({'command': 'configure', 'interfaces': request.configuration['interfaces']}))
        await _wait_event(ws, 'CONFIG_APPLIED')
        attempted = True
        await ws.send(json.dumps({'command': 'start', 'interfaces': [request.target.interface], 'pcap_filter': ''}))
        early: list[bytes] = []
        started = await _wait_event(ws, 'CAPTURE_STARTED', early=early)
        validate_started(started, request)
        send_status({'phase': 'started', 'session_id': started['session_id'], 'config': started['config']})
        stop_requested = False

        async def receive():
            nonlocal stopped
            for chunk in early:
                await write_bytes(chunk)
            while True:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    await write_bytes(raw)
                    continue
                event = _event(raw)
                if event.get('code') == 'CAPTURE_STOPPED' and stop_requested:
                    stopped = True
                    return
                if event.get('code') in {'CAPTURE_ENDED', 'CAPTURE_STOPPED'}:
                    raise CoreCaptureError('Core capture ended unexpectedly')

        async def control():
            nonlocal stop_requested
            await next_control()
            stop_requested = True
            await ws.send(json.dumps({'command': 'stop'}))

        reader = asyncio.create_task(receive())
        controller = asyncio.create_task(control())
        try:
            # Stop can be requested while the reader drains a partial pipe write;
            # do not cancel that write and silently corrupt the PCAPNG stream.
            done, _ = await asyncio.wait({reader, controller}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if controller in done and not reader.done():
                try:
                    await asyncio.wait_for(reader, 20)
                except TimeoutError as exc:
                    raise CoreCaptureError('Core did not confirm capture stop') from exc
        finally:
            for task in (reader, controller):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, controller, return_exceptions=True)
    finally:
        if attempted and not stopped:
            try:
                await ws.send(json.dumps({'command': 'stop'}))
                # A failure may precede CAPTURE_STARTED. Ignore stale error
                # events and packet bytes while awaiting our stop completion.
                async def confirm():
                    while True:
                        raw = await ws.recv()
                        if isinstance(raw, str):
                            data = json.loads(raw)
                            if isinstance(data, dict) and data.get('code') == 'CAPTURE_STOPPED':
                                return
                await asyncio.wait_for(confirm(), 15)
                stopped = True
            except Exception:
                pass
        send_status({'phase': 'stopped', 'attempted': attempted, 'confirmed': stopped or not attempted})


def _transport_worker(connection, descriptor, request: CoreRequest, parent_pid: int) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    fd = descriptor.detach()
    os.set_blocking(fd, False)
    raw = None
    received = forwarded = chunks = 0
    last_progress = 0.0

    def progress():
        connection.send({'phase': 'progress', 'received_bytes': received,
                         'forwarded_bytes': forwarded, 'chunks': chunks})

    async def worker():
        import websockets
        context = _tls_context(request)
        token = await asyncio.to_thread(_get_token)
        async with websockets.connect(request.url, ssl=context, open_timeout=15, close_timeout=5,
                                      compression=None, max_size=2 * 1024 * 1024, max_queue=8) as ws:
            async def next_control():
                while True:
                    if os.getppid() != parent_pid:
                        return 'stop'
                    if connection.poll():
                        try:
                            return connection.recv()
                        except EOFError:
                            return 'stop'
                    await asyncio.sleep(0.05)
            async def layout_reader():
                return await asyncio.to_thread(_get_layout, request, token, context)
            async def write(chunk):
                nonlocal received, forwarded, chunks, last_progress
                received += len(chunk)
                chunks += 1
                if raw is not None:
                    raw.write(chunk)
                await _write_pipe(fd, chunk)
                forwarded += len(chunk)
                if time.monotonic() - last_progress >= 1:
                    if raw is not None:
                        raw.flush()
                    progress()
                    last_progress = time.monotonic()
            await run_owner(ws, token, request, layout_reader=layout_reader,
                            send_status=connection.send, next_control=next_control, write_bytes=write)
    try:
        if request.raw_path is not None:
            raw = request.raw_path.open('wb')
        asyncio.run(worker())
    except BaseException as exc:
        if isinstance(exc, CoreCaptureError):
            message = str(exc)
        elif isinstance(exc, ssl.SSLError):
            message = 'Core TLS verification failed; check the device certificate and Core service'
        elif isinstance(exc, (ConnectionError, TimeoutError)):
            message = 'Core capture endpoint unavailable; check wlanpi-core service and local connectivity'
        else:
            message = f'Core transport failed ({type(exc).__name__})'
        try:
            connection.send({'phase': 'error', 'message': message})
        except (OSError, EOFError):
            pass
    finally:
        try:
            progress()
        except (OSError, EOFError):
            pass
        if raw is not None:
            raw.close()
        os.close(fd)
        connection.close()


class CoreCaptureProcess:
    """Decoder process facade; Core TCP/JWT state exists only in a spawn worker."""
    def __init__(self, decoder, request: CoreRequest, *, exporter=None):
        self.decoder = decoder
        self.stdout, self.stderr = decoder.stdout, decoder.stderr
        self.request = request
        self.session_id = None
        self.capture_error = ''
        self.stop_confirmed = False
        self.attempted = False
        self.closed = False
        self.received_bytes = self.forwarded_bytes = self.chunks = 0
        self.events: list[dict] = []
        self.fanout = None
        sink_fd = decoder.stdin.fileno()
        if exporter is not None:
            from beacon_live.packet_source import core_fanout
            self.fanout, sink_fd = core_fanout(decoder, exporter)
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.worker = context.Process(target=_transport_worker,
            args=(child, DupFd(sink_fd), request, os.getpid()), name='beacon-live-core', daemon=True)
        try:
            self.worker.start()
        finally:
            child.close()
            if self.fanout is None:
                decoder.stdin.close()
                decoder.stdin = None
            else:
                os.close(sink_fd)

    def _drain(self):
        try:
            while self.connection.poll():
                event = self.connection.recv()
                if event['phase'] == 'error':
                    self.capture_error = event['message']
                elif event['phase'] == 'stopped':
                    self.stop_confirmed = event['confirmed']
                    self.attempted = event['attempted']
                elif event['phase'] == 'progress':
                    self.received_bytes = event['received_bytes']
                    self.forwarded_bytes = event['forwarded_bytes']
                    self.chunks = event['chunks']
                else:
                    self.events.append(event)
        except (EOFError, OSError):
            pass

    def wait_phase(self, phase: str, timeout: float = 45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain()
            if self.capture_error:
                raise CoreCaptureError(self.capture_error)
            for event in self.events:
                if event['phase'] == phase:
                    self.events.remove(event)
                    return event
            if not self.worker.is_alive():
                raise CoreCaptureError(f'Core transport ended before {phase}')
            time.sleep(0.05)
        raise CoreCaptureError(f'Core transport timed out before {phase}')

    def start_capture(self):
        self.attempted = True
        try:
            self.connection.send('start')
        except (OSError, EOFError) as exc:
            self._drain()
            raise CoreCaptureError(self.capture_error or 'Core transport closed before capture start') from exc
        event = self.wait_phase('started')
        self.session_id = event['session_id']
        logging.getLogger(__name__).info('Core capture owner: %s (%s)', self.session_id, self.request.target.label)

    def poll(self):
        self._drain()
        if getattr(self, "fanout", None) is not None and self.fanout.error:
            self.capture_error = self.fanout.error
        result = self.decoder.poll()
        if self.capture_error:
            return 1
        if not self.closed and not self.worker.is_alive():
            self.capture_error = 'Core transport exited before capture was stopped'
            return 1
        return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        old_mask = None
        if threading.current_thread() is threading.main_thread():
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            if self.worker.is_alive():
                try:
                    self.connection.send('stop')
                except (OSError, EOFError):
                    pass
                self.worker.join(timeout=25)
            self._drain()
            if self.worker.is_alive():
                self.worker.terminate()
                self.worker.join(timeout=5)
            if self.attempted and not self.stop_confirmed:
                raise CoreCaptureError('Core stop was not confirmed; radio restoration must verify that the radio is idle')
        finally:
            try:
                self.connection.close()
                if getattr(self, "fanout", None) is not None:
                    self.fanout.join(timeout=0.5)
                if self.decoder.poll() is None:
                    self.decoder.terminate()
                    try:
                        self.decoder.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.decoder.kill()
                        self.decoder.wait(timeout=5)
                if getattr(self, "fanout", None) is not None:
                    self.fanout.join(timeout=1)
            finally:
                if old_mask is not None:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
