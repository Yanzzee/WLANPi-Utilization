"""Optional loopback mock: real spawn worker, PCAPNG pipe, decoder, and stop."""
import asyncio
import json
from http import HTTPStatus
import os
from pathlib import Path
import shutil
import subprocess

import pytest

websockets = pytest.importorskip('websockets', reason='Optional Core transport integration needs websockets')
serve = pytest.importorskip('websockets.asyncio.server').serve

from beacon_live import core_capture as core
from beacon_live.capture_target import CaptureTarget
from beacon_live.channel import ChannelDefinition, ChannelWidth, RadioCapabilities
from beacon_live.live import (available_tshark_fields, build_tshark_stream_command,
    select_tshark_frame_fields, _start_live_aggregation_process, _stop_live_aggregation)
from beacon_live.parser import parse_tshark_live_record


@pytest.mark.parametrize('tune_failure,aggregate', [(False, False), (True, False), (False, True)])
def test_spawn_transport_with_mock_core_and_real_decoder(tmp_path, monkeypatch, tune_failure, aggregate):
    tshark = os.environ.get('WLANPI_TEST_TSHARK') or shutil.which('tshark')
    if not tshark:
        pytest.skip('TShark is required for transport integration')
    getjwt = tmp_path / 'getjwt'
    getjwt.write_text('#!/bin/sh\nprintf \'{"access_token":"private-test-token"}\\n\'\n')
    getjwt.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    fixture = Path(__file__).parent / 'fixtures/tshark_4_4/he80.pcap'
    capture = subprocess.run([tshark, '-r', str(fixture), '-F', 'pcapng', '-w', '-'], capture_output=True, check=True, timeout=15).stdout
    fields = select_tshark_frame_fields(available_tshark_fields(tshark=tshark))
    commands = []

    async def scenario():
        config = None
        stopped = asyncio.Event()
        streamed = asyncio.Event()
        async def http(connection, request):
            if request.path == '/api/v1/network/config/status':
                assert request.headers['Authorization'] == 'Bearer private-test-token'
                return connection.respond(HTTPStatus.OK, json.dumps({'root': {'wlanpi0': {'type':'monitor'}}}))
        async def handler(ws):
            nonlocal config
            async for raw in ws:
                msg = json.loads(raw)
                name = msg['command']
                commands.append(name)
                data = {}
                if name == 'auth': code = 'AUTH_OK'
                elif name == 'list_sessions': code, data = 'SESSIONS', {'sessions':[]}
                elif name == 'get_supported_frequencies': code, data = 'SUPPORTED_FREQUENCIES', {'wlanpi0':[5975]}
                elif name == 'configure':
                    config = {'interfaces':msg['interfaces'], 'pcap_filter':''}
                    code = 'CONFIG_APPLIED'
                elif name == 'start':
                    if tune_failure:
                        await ws.send(json.dumps({'type':'event','event':'error','code':'CHANNEL_SET_FAILED','data':{'message':'simulated tune refusal'}}))
                    await ws.send(capture[:16])
                    code, data = 'CAPTURE_STARTED', {'session_id':'cap_mock','interfaces':['wlanpi0'],'namespace':None,'config':config}
                elif name == 'stop':
                    code = 'CAPTURE_STOPPED'
                    stopped.set()
                else: raise AssertionError(name)
                await ws.send(json.dumps({'type':'event','event':'status','code':code,'data':data}))
                if name == 'start':
                    for offset in range(16, len(capture), 17):
                        await ws.send(capture[offset:offset+17])
                        if aggregate:
                            await asyncio.sleep(.01)
                    streamed.set()
        async with serve(handler, '127.0.0.1', 0, process_request=http) as server:
            port = server.sockets[0].getsockname()[1]
            spec = core.CoreRequest(CaptureTarget('wlanpi0'), ChannelDefinition(5975, ChannelWidth.MHZ80, 5985),
                                    url=f'ws://127.0.0.1:{port}/api/v1/streaming/capture', raw_path=tmp_path/'core.pcapng')
            command = build_tshark_stream_command(field_names=fields)
            command[0] = tshark
            decoder = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            process = core.CoreCaptureProcess(decoder, spec)
            aggregation = None
            try:
                await asyncio.to_thread(process.wait_phase, 'ready')
                if tune_failure:
                    with pytest.raises(core.CoreCaptureError, match='CHANNEL_SET_FAILED'):
                        await asyncio.to_thread(process.start_capture)
                else:
                    await asyncio.to_thread(process.start_capture)
                    if aggregate:
                        aggregation = _start_live_aggregation_process(decoder.stdout, field_names=fields,
                            band='6', target_primary_frequency_mhz=5975, actual_definition=spec.definition,
                            capabilities=RadioCapabilities(frozenset({ChannelWidth.MHZ80}), frozenset({5975})))
                        receiver, worker = aggregation
                        assert await asyncio.to_thread(receiver.poll, 10), 'Shared analyzer published no live frames'
                        message = receiver.recv()
                        assert message.capture_record_count > 0 and message.normalized_frame_count > 0
                    else:
                        rows = []
                        for _ in range(9):
                            row = await asyncio.wait_for(asyncio.to_thread(decoder.stdout.readline), 10)
                            assert row
                            rows.append(row.decode())
                        frames = tuple(f for row in rows for f in parse_tshark_live_record(row, field_names=fields))
                        assert len(frames) == 9 and frames[0].qbss_cu_raw == 128
                        assert frames[0].channel_definition.width is ChannelWidth.MHZ80
                    await asyncio.wait_for(streamed.wait(), 10)
            finally:
                await asyncio.to_thread(process.close)
                if aggregation is not None:
                    await asyncio.to_thread(_stop_live_aggregation, *aggregation)
            assert process.stop_confirmed
            assert not process.worker.is_alive() and decoder.poll() is not None
            assert stopped.is_set()
            assert commands.count('start') == commands.count('stop') == 1
            if not tune_failure:
                assert (tmp_path/'core.pcapng').read_bytes() == capture
                assert process.received_bytes == process.forwarded_bytes == len(capture)
                assert process.chunks > 0
    asyncio.run(scenario())
