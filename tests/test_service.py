import signal
import json
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

from beacon_live.ipc import MAX_MESSAGE_BYTES, FramedSocket, encode, from_wire, to_wire
from beacon_live.lcd_dashboard import LcdDashboard
from beacon_live.runtime import LiveSessionConfig, ManagedRuntime, RuntimePublication, SessionPhase
from beacon_live.service import serve
from beacon_live.service_client import ServiceClient, ServiceError, follow

FIXTURE = Path(__file__).parent / "fixtures/managed_owner.py"


def until(client, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        publication = client.read()
        if predicate(publication):
            return publication
        time.sleep(0.02)
    pytest.fail(f"Publication condition timed out: {publication}")


@pytest.fixture
def service(tmp_path):
    holder = tempfile.TemporaryDirectory(prefix="util-", dir="/private/tmp")
    directory = Path(holder.name)
    path = directory / "service.sock"
    process = subprocess.Popen([sys.executable, str(FIXTURE), "--socket", str(path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    client = ServiceClient(path)
    deadline = time.monotonic() + 5
    while True:
        try:
            client.read()
            break
        except ServiceError:
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode())
            if time.monotonic() >= deadline:
                process.kill()
                pytest.fail(process.communicate()[1].decode())
            time.sleep(0.02)
    try:
        yield client, process
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        _, error = process.communicate(timeout=10)
        holder.cleanup()
        assert process.returncode == 0, error.decode()


def config(tmp_path, **changes):
    return replace(LiveSessionConfig(band="2.4", channel="1", stats_csv=tmp_path / "stats.csv",
                                    initial_logging_enabled=False), **changes)


def test_typed_publication_roundtrip_and_invalid_messages():
    publication = ManagedRuntime().read()
    assert from_wire(RuntimePublication, to_wire(publication)) == publication
    for value in ({"unknown": 1}, {"interval_seconds": float("inf")},
                  {"min_free_bytes": True}, {"channel_width": "invalid"}, {"local_cu": "yes"}):
        with pytest.raises((ValueError, TypeError)):
            from_wire(LiveSessionConfig, value)
    with pytest.raises(ValueError):
        encode({"value": float("nan")})


def test_partial_framing_latest_queue_and_oversized_header():
    sender, receiver = socket.socketpair()
    try:
        reader = FramedSocket(receiver)
        data = encode({"op": "read"})
        sender.sendall(data[:3])
        assert reader.receive() == []
        sender.sendall(data[3:])
        assert reader.receive() == [{"op": "read"}]
        writer = FramedSocket(sender)
        writer.queue({"revision": 1})
        writer.queue({"revision": 100})
        writer.flush()
        assert reader.receive() == [{"revision": 100}]
        sender.sendall(struct.pack("!I", MAX_MESSAGE_BYTES + 1))
        with pytest.raises(ValueError, match="message size"):
            reader.receive()
    finally:
        sender.close()
        receiver.close()


def test_multiple_consumers_navigation_logging_and_explicit_stop(service, tmp_path):
    client, _ = service
    assert client.read().phase is SessionPhase.IDLE
    assert stat.S_IMODE(client.socket_path.stat().st_mode) == 0o600
    first = client.ensure(config(tmp_path))
    publication = until(client, lambda pub: pub.phase is SessionPhase.RUNNING and bool(pub.completed_stats))
    assert publication.session_id == first.session_id
    assert publication.completed_stats[0].retry_percent == 50.0
    assert publication.completed_stats[0].local_cu_percent is None
    second = ServiceClient(client.socket_path)
    assert second.read().snapshot == publication.snapshot
    assert second.ensure(config(tmp_path, stats_csv=tmp_path / "other.csv")).session_id == first.session_id
    assert not (tmp_path / "other.owners").exists()
    assert len((tmp_path / "stats.owners").read_text().splitlines()) == 1
    dashboard = LcdDashboard(tmp_path / "frame.ppm", band="2.4", channel="1")
    reads = 0

    def navigate_then_detach():
        nonlocal reads
        reads += 1
        dashboard.navigate_down(now=float(reads))
        assert dashboard.snapshot == publication.snapshot
        return reads == 4

    dashboard.poll_input = navigate_then_detach
    assert follow(second, dashboard, initial=publication, wait=lambda _: None) == 0
    assert client.read().session_id == first.session_id
    assert client.read().snapshot == publication.snapshot
    client.set_logging(first.session_id, True)
    logged = until(client, lambda pub: pub.logging.active)
    assert logged.snapshot == publication.snapshot
    client.set_logging(first.session_id, False)
    until(client, lambda pub: not pub.logging.active)
    with pytest.raises(ServiceError, match="stop it before changing source"):
        client.ensure(config(tmp_path, channel="6"))
    with pytest.raises(ServiceError, match="Stale"):
        client.stop("previous-session")
    client.stop(first.session_id)
    final = until(client, lambda pub: pub.phase is SessionPhase.STOPPED)
    assert final.snapshot == publication.snapshot
    assert final.logging.active is False
    assert (tmp_path / "stats.restored").exists()
    # The final publication can precede reaping; wait for a new owner admission.
    fresh = restart(client, config(tmp_path, channel="6"))
    assert fresh.session_id != first.session_id
    assert fresh.revision > final.revision
    with pytest.raises(ServiceError, match="Stale"):
        client.set_logging(first.session_id, True)
    until(client, lambda pub: pub.phase is SessionPhase.RUNNING)
    client.stop(fresh.session_id)


def restart(client, settings):
    deadline = time.monotonic() + 5
    while True:
        try:
            return client.ensure(settings)
        except ServiceError:
            assert time.monotonic() < deadline
            time.sleep(0.02)


@pytest.mark.parametrize("iface", ["fail", "crash"])
def test_owner_failure_is_visible_and_service_allows_a_new_session(service, tmp_path, iface):
    client, process = service
    started = client.ensure(config(tmp_path, iface=iface))
    failed = until(client, lambda pub: pub.phase is SessionPhase.FAILED)
    assert failed.session_id == started.session_id
    assert failed.error
    assert process.poll() is None
    fresh = restart(client, config(tmp_path))
    assert fresh.session_id != failed.session_id
    until(client, lambda pub: pub.phase is SessionPhase.RUNNING)
    client.stop(fresh.session_id)


def test_service_shutdown_cleans_owner_and_removes_socket(service, tmp_path):
    client, process = service
    client.ensure(config(tmp_path))
    until(client, lambda pub: pub.phase is SessionPhase.RUNNING)
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)
    assert (tmp_path / "stats.restored").exists()
    assert not client.socket_path.exists()


def test_slow_client_and_duplicate_service_do_not_restart_owner(service, tmp_path):
    client, _ = service
    first = client.ensure(config(tmp_path))
    until(client, lambda pub: pub.phase is SessionPhase.RUNNING)
    with socket.socket(socket.AF_UNIX) as slow:
        slow.connect(str(client.socket_path))
        slow.sendall(b"\x00")
        assert client.read().session_id == first.session_id
        with pytest.raises(BlockingIOError):
            serve(client.socket_path)
        assert client.read().phase is SessionPhase.RUNNING
    client.stop(first.session_id)


def test_unrelated_socket_file_and_public_directory_are_rejected(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "service.sock"
    path.write_text("unrelated data")
    with pytest.raises(ValueError, match="unrelated"):
        serve(path)
    assert path.read_text() == "unrelated data"
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        serve(path)


def test_attach_does_not_apply_initial_false_logging_control(service, tmp_path):
    client, _ = service
    client.ensure(config(tmp_path, initial_logging_enabled=True))
    publication = until(client, lambda pub: pub.logging.active)
    control = tmp_path / "logging.json"
    control.write_text('{"logging_enabled":false}')
    dashboard = LcdDashboard(tmp_path / "frame.ppm", band="2.4", channel="1")
    dashboard.poll_input = lambda: True
    assert follow(client, dashboard, initial=publication, logging_control_path=control) == 0
    assert client.read().logging.active
    client.stop(publication.session_id)


def test_real_cli_attach_and_navigation_leave_shared_owner_running(service, tmp_path):
    client, _ = service
    command = [sys.executable, "-m", "beacon_live.device", "--managed", "--band", "2.4",
               "--channel", "1", "--start-only", "--log-dir", str(tmp_path),
               "--service-socket", str(client.socket_path)]
    started = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert started.returncode == 0, started.stderr
    assert "Utilization session:" in started.stdout
    original = until(client, lambda pub: pub.phase is SessionPhase.RUNNING and bool(pub.completed_stats))
    frame = tmp_path / "attached.ppm"
    renderer = subprocess.Popen(
        [sys.executable, "-m", "beacon_live.device", "--attach", "--lcd-frame", str(frame),
         "--service-socket", str(client.socket_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not frame.exists():
            assert renderer.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        frame.with_suffix(".control.json").write_text('{"active_screen_offset":2}')
        while json.loads(frame.with_suffix(".json").read_text())["screen_index"] != 2:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        renderer.send_signal(signal.SIGINT)
        _, error = renderer.communicate(timeout=5)
        assert renderer.returncode == 0, error.decode()
        current = client.read()
        assert current.session_id == original.session_id
        assert current.snapshot == original.snapshot
        assert current.phase is SessionPhase.RUNNING
        incompatible = subprocess.run(
            [*command[:command.index("--channel") + 1], "6",
             *command[command.index("--channel") + 2:]],
            capture_output=True, text=True, timeout=5,
        )
        assert incompatible.returncode == 1
        assert client.read().session_id == original.session_id
    finally:
        if renderer.poll() is None:
            renderer.send_signal(signal.SIGINT)
            renderer.communicate(timeout=5)
        client.stop(original.session_id)


def test_large_partial_write_retains_only_one_pending_latest_publication():
    sender, receiver = socket.socketpair()
    try:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        writer, reader = FramedSocket(sender), FramedSocket(receiver)
        writer.queue({"revision": 1, "data": "x" * 262144})
        writer.flush()
        assert writer.offset > 0 and writer.writing
        for revision in range(2, 100):
            writer.queue({"revision": revision, "data": "x" * 262144})
        assert len(writer.output) + len(writer.next_output) < 600000
        messages = []
        deadline = time.monotonic() + 5
        while len(messages) < 2:
            messages.extend(reader.receive())
            writer.flush()
            assert time.monotonic() < deadline
        assert [message["revision"] for message in messages] == [1, 99]
    finally:
        sender.close()
        receiver.close()
