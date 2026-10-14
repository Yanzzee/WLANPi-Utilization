#!/usr/bin/env python3
"""Evaluate Core capture metadata without starting, tuning, or joining a capture.

Run with Core's interpreter (which already provides websockets). Unless an
existing token file is supplied, getjwt issues an evaluation JWT in memory.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import re
import ssl
import subprocess
import sys
from urllib.parse import urlsplit

DEFAULT_URL = 'wss://localhost:31415/api/v1/streaming/capture'
DEFAULT_CERT = Path('/etc/nginx/ssl/self-signed-wlanpi.cert')
IDENTITY = 'wlanpi-utilization-evaluation'


def session_assessment(session: dict, interface: str, frequency: int, width: int) -> dict:
    """Match requested configuration, never infer actual tuning from it."""
    reasons = []
    config = session.get('config')
    if not isinstance(config, dict):
        reasons.append('running configuration missing')
        config = {}
    if session.get('interfaces') != [interface]:
        reasons.append('session must contain exactly the requested interface')
    if config.get('pcap_filter') != '':
        reasons.append('capture must explicitly have an empty filter')
    interfaces = config.get('interfaces', {})
    iface_config = interfaces.get(interface, {}) if isinstance(interfaces, dict) else {}
    channels = iface_config.get('channels') if isinstance(iface_config, dict) else None
    if not isinstance(channels, list) or len(channels) != 1:
        reasons.append('capture must have one fixed channel')
    elif not isinstance(channels[0], dict) or channels[0].get('freq') != frequency or channels[0].get('width') != width:
        reasons.append('configured frequency/width differ from the request')
    return {
        'session_id': session.get('session_id'), 'owner': session.get('owner'),
        'namespace': session.get('namespace'), 'interfaces': session.get('interfaces'),
        'config': config, 'configured_candidate': not reasons, 'reasons': reasons,
        'actual_tuning_verified': False,
    }


async def receive_event(ws, expected: str, timeout: float):
    async def receive():
        while True:
            raw = await ws.recv()
            if not isinstance(raw, str):
                raise ValueError('Unexpected packet data during metadata-only evaluation')
            try:
                event = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError('Core returned an invalid JSON event') from exc
            if not isinstance(event, dict):
                raise ValueError('Core returned a non-object event')
            if event.get('event') == 'error':
                code = event.get('code')
                code = code if isinstance(code, str) and re.fullmatch(r'[A-Z_]{1,64}', code) else 'UNKNOWN_ERROR'
                raise ValueError(f'Core rejected the request ({code})')
            if event.get('code') == expected:
                return event.get('data')
    return await asyncio.wait_for(receive(), timeout=timeout)


async def inspect_connection(ws, token: str, interface: str, frequency: int, width: int, timeout: float):
    await asyncio.wait_for(ws.send(json.dumps({'command': 'auth', 'token': token})), timeout)
    await receive_event(ws, 'AUTH_OK', timeout)
    await asyncio.wait_for(ws.send(json.dumps({'command': 'list_sessions'})), timeout)
    data = await receive_event(ws, 'SESSIONS', timeout)
    sessions = data.get('sessions') if isinstance(data, dict) else None
    if not isinstance(sessions, list) or not all(isinstance(s, dict) for s in sessions):
        raise ValueError('Core returned invalid session metadata')
    await asyncio.wait_for(ws.send(json.dumps({'command': 'get_supported_frequencies'})), timeout)
    frequencies = await receive_event(ws, 'SUPPORTED_FREQUENCIES', timeout)
    if not isinstance(frequencies, dict):
        raise ValueError('Core returned invalid frequency metadata')
    return {
        'authenticated': True, 'requested': {'interface': interface, 'frequency_mhz': frequency, 'width_mhz': width},
        'supported_frequencies': frequencies,
        'sessions': [session_assessment(s, interface, frequency, width) for s in sessions],
        'note': 'Matching session configuration is only a candidate; actual tuning and frame delivery are not verified.',
    }


def obtain_token(token_file: Path | None, runner=subprocess.run) -> str:
    if token_file is None:
        result = runner(['getjwt', IDENTITY, '--no-color'], capture_output=True, text=True, check=False, timeout=15)
        if result.returncode:
            # Do not print raw auth responses or credential-bearing diagnostics.
            raise ValueError(f'getjwt failed ({result.returncode}); check Core service, TLS, and root access')
        value = result.stdout
    else:
        value = token_file.read_text().strip()
    try:
        response = json.loads(value)
    except ValueError:
        response = value if token_file is not None else None
    token = response.get('access_token') if isinstance(response, dict) else response
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise ValueError('No valid token was supplied by getjwt or the token file')
    return token


def validate_url(url: str):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use a capture URL without credentials, query parameters, or fragments')
    if parsed.path != '/api/v1/streaming/capture' or not parsed.hostname:
        raise ValueError('URL must identify /api/v1/streaming/capture')
    if parsed.scheme == 'wss':
        return
    if parsed.scheme != 'ws' or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Use wss for production; ws is allowed only on a local development server')


async def evaluate(args):
    try:
        import websockets
    except ImportError as exc:
        raise ValueError('Run this probe with /opt/wlanpi-core/bin/python; its websockets dependency is required') from exc
    context = ssl.create_default_context(cafile=str(args.ca_cert)) if args.url.startswith('wss:') else None
    token = obtain_token(args.token_file)
    async with websockets.connect(args.url, ssl=context, open_timeout=args.timeout_seconds,
                                  close_timeout=5, max_size=2 * 1024 * 1024, max_queue=8) as ws:
        return await inspect_connection(ws, token, args.interface, args.frequency_mhz, args.width, args.timeout_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=DEFAULT_URL)
    parser.add_argument('--ca-cert', type=Path, default=DEFAULT_CERT)
    parser.add_argument('--token-file', type=Path, help='Existing JWT or getjwt JSON; otherwise issue an evaluation JWT')
    parser.add_argument('--interface', default='wlanpi0')
    parser.add_argument('--frequency-mhz', type=int, default=2412)
    parser.add_argument('--width', type=int, choices=(20, 40, 80, 160), default=20)
    parser.add_argument('--timeout-seconds', type=float, default=15)
    args = parser.parse_args()
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error('--timeout-seconds must be finite and positive')
    if not re.fullmatch(r'wlanpi[0-9]{1,3}', args.interface):
        parser.error('Core capture accepts wlanpiN interfaces')
    if args.frequency_mhz <= 0:
        parser.error('--frequency-mhz must be positive')
    try:
        validate_url(args.url)
        report = asyncio.run(evaluate(args))
    except (ValueError, OSError, TimeoutError, subprocess.SubprocessError) as exc:
        # WebSocket libraries may include connection details in exceptions;
        # auth token values never belong in this output.
        print(f'Core evaluation failed: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'Core connection failed ({type(exc).__name__}); check service/TLS and journal', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    print('Metadata evaluation passed. No capture was configured, started, stopped, or subscribed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
