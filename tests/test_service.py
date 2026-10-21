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


@pytest.mark.parametrize("mode", [[], ["--attach"], ["--managed", "--channel", "6"]])
def test_real_cli_attach_and_navigation_leave_shared_owner_running(service, tmp_path, mode):
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
        [sys.executable, "-m", "beacon_live.device", *mode, "--lcd-frame", str(frame),
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
        assert incompatible.returncode == 0, incompatible.stderr
        assert "Session already running" in incompatible.stderr
        assert client.read().session_id == original.session_id
        assert client.read().config.channel == "1"
    finally:
        if renderer.poll() is None:
            renderer.send_signal(signal.SIGINT)
            renderer.communicate(timeout=5)
        client.stop(original.session_id)


def test_atomic_start_or_attach_uses_one_owner_and_keeps_existing_source_and_logging(service, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    client, _ = service
    def open_session(channel):
        return ServiceClient(client.socket_path).start_or_attach(config(tmp_path, channel=channel))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(open_session, ["1", "6", "11", "1"]))
    assert len({pub.session_id for pub, _ in results}) == 1
    assert sum(not attached for _, attached in results) == 1
    original = until(client, lambda pub: pub.phase is SessionPhase.RUNNING and bool(pub.completed_stats))
    assert all(pub.config.channel == original.config.channel for pub, _ in results)
    assert len((tmp_path / "stats.owners").read_text().splitlines()) == 1
    assert not original.logging.active
    with pytest.raises(ServiceError, match="owns the radio"):
        client.request("start", config=config(tmp_path))
    client.stop(original.session_id)


def test_native_fpms_menu_stop_ends_shared_owner_and_logging_but_keeps_broker_and_history(service, tmp_path, monkeypatch):
    import ast
    import types
    from integration.wlanpi_fpms import channel_utilization as fpms
    client, broker = service
    client.ensure(config(tmp_path, initial_logging_enabled=True))
    original = until(client, lambda pub: pub.logging.active and bool(pub.completed_stats))
    settings = tmp_path / "fpms.json"
    settings.write_text('{"runtime_mode":"managed"}')
    monkeypatch.delenv("WLANPI_UTILIZATION_RUNTIME_MODE", raising=False)
    monkeypatch.setattr(fpms, "BACKEND_CONFIG", settings)
    real_run = subprocess.run
    def installed_cli(command, **options):
        assert command[0] == fpms.EXECUTABLE
        return real_run([sys.executable, "-m", "beacon_live.device", *command[1:],
                         "--socket", str(client.socket_path)], **options)
    monkeypatch.setattr(fpms.subprocess, "run", installed_cli)
    messages = []
    monkeypatch.setattr(fpms, "_display_status", lambda state, text: messages.append(text))
    monkeypatch.setattr(fpms, "_display_error", lambda state, text: pytest.fail(text))
    menu_state = {}
    menu = fpms.build_channel_utilization_menu(menu_state)
    menu_state["option_selected"] = menu["action"]
    menu_state["channel_utilization_menu_enter"]()
    stop = next(item["action"] for item in menu["action"] if item["name"] == "Stop Session")
    # Execute the actual FunctionType gate from the pinned native dispatcher.
    fixture = Path(__file__).parent / "fixtures/fpms_1_4_24/fpms.py"
    tree = ast.parse(fixture.read_text())
    gates = [node for node in ast.walk(tree) if isinstance(node, ast.If)
             and any(isinstance(child, ast.Attribute) and child.attr == "FunctionType" for child in ast.walk(node.test))]
    gate = max(gates, key=lambda node: node.lineno)
    exec(compile(ast.Module(body=[gate], type_ignores=[]), str(fixture), "exec"),
         {"types": types, "g_vars": {"option_selected": stop}})
    stopped = until(client, lambda pub: pub.phase is SessionPhase.STOPPED)
    assert messages == ["Session stopping"]
    assert stopped.session_id == original.session_id and stopped.snapshot == original.snapshot
    assert not stopped.logging.active and stopped.closed_logs
    assert (tmp_path / "stats.restored").exists()
    assert broker.poll() is None


def test_native_fpms_left_detaches_new_session_and_returns_to_stop_menu(service, tmp_path, monkeypatch):
    import ast
    import types
    from integration.wlanpi_fpms import channel_utilization as fpms
    from scripts.patch_wlanpi_fpms import _patch_buttons
    client, broker = service
    settings = tmp_path / "fpms.json"
    settings.write_text('{"runtime_mode":"managed"}')
    monkeypatch.delenv("WLANPI_UTILIZATION_RUNTIME_MODE", raising=False)
    monkeypatch.setattr(fpms, "BACKEND_CONFIG", settings)
    monkeypatch.setattr(fpms, "FRAME_PATH", tmp_path / "display.ppm")
    monkeypatch.setattr(fpms, "LOG_DIR", tmp_path)
    monkeypatch.setattr(fpms._DisplaySession, "start_display", lambda self: None)
    real_run, real_popen = subprocess.run, subprocess.Popen
    def cli(command):
        return [sys.executable, "-m", "beacon_live.device", *command[1:],
                "--service-socket", str(client.socket_path)]
    def installed_cli(command, **options):
        # session controls use --socket rather than the device option.
        return real_run([sys.executable, "-m", "beacon_live.device", *command[1:],
                         "--socket", str(client.socket_path)], **options)
    monkeypatch.setattr(fpms.subprocess, "run", installed_cli)
    state = {"display_state": "menu", "current_menu_location": [0, 0, 0, 0, 1]}
    controller = fpms.ChannelUtilizationApp(state, popen=lambda command: real_popen(
        cli(command), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    monkeypatch.setattr(fpms, "ChannelUtilizationApp", lambda _: controller)
    app_menu = fpms.build_channel_utilization_menu(state)
    menu = [{"name": "Apps", "action": [app_menu]}]
    buttons_path = Path(__file__).parent / "fixtures/fpms_1_4_24/buttons.py"
    tree = ast.parse(_patch_buttons(buttons_path.read_text(), buttons_path))
    button_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    namespace = {"types": types}
    exec(compile(ast.Module(body=[button_class], type_ignores=[]), "native_buttons", "exec"), namespace)
    button = object.__new__(namespace["Button"])
    class Page:
        def draw_page(self, g_vars, menu):
            level = menu
            for index in g_vars["current_menu_location"][:-1]:
                level = level[index]["action"]
            g_vars["option_selected"] = level[g_vars["current_menu_location"][-1]]["action"]
    button.page_obj = Page()
    button.page_obj.draw_page(state, menu)
    button.menu_center(state, menu)  # Start + Log from a channel submenu.
    try:
        original = until(client, lambda pub: pub.logging.active and bool(pub.completed_stats))
        session = state["channel_utilization_session"]
        button.menu_left(state, menu)
        current = client.read()
        assert current.session_id == original.session_id
        assert current.phase is SessionPhase.RUNNING and current.logging.active
        assert current.logging.paths == original.logging.paths
        assert current.snapshot == original.snapshot
        assert session.process.poll() == 0
        assert state["display_state"] == "menu"
        assert state["current_menu_location"] == [0, 0, 0]
        assert [item["name"] for item in app_menu["action"]][1:3] == ["Connect", "Stop Session"]
        # Repeating Start Logging while active must preserve the same files.
        controller.start_logging_only(band="5", channel=36)
        assert client.read().logging.paths == original.logging.paths
        state["current_menu_location"][-1] = 2
        button.page_obj.draw_page(state, menu)
        button.menu_center(state, menu)
        stopped = until(client, lambda pub: pub.phase is SessionPhase.STOPPED)
        assert not stopped.logging.active and stopped.closed_logs
        assert stopped.session_id == original.session_id
        assert broker.poll() is None
    finally:
        if state.get("channel_utilization_session"):
            state["channel_utilization_session"].stop()


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


def test_web_clients_share_real_service_and_download_closed_logs(service, tmp_path):
    pytest.importorskip("flask", reason="Install the optional web extra")
    import re
    from beacon_live.web import create_app

    owner, _ = service
    app = create_app(client=owner, log_directory=tmp_path)
    first, second = app.test_client(), app.test_client()
    page = first.get("/utilization/")
    token = re.search(r'name="utilization-csrf" content="([^"]+)"', page.text)[1]
    headers = {"X-Utilization-CSRF": token, "Origin": "http://localhost"}
    assert owner.read().phase is SessionPhase.IDLE
    result = first.post("/utilization/api/session/start", json={"band": "2.4", "channel": "1", "logging_enabled": True}, headers=headers)
    assert result.status_code == 202, result.json
    identifier = result.json["session_id"]
    initial = until(owner, lambda pub: pub.logging.active and bool(pub.completed_stats))
    for client in (first, second, app.test_client()):
        assert client.get("/utilization/").status_code == 200
        assert client.get("/utilization/api/status").json["publication"]["session_id"] == identifier
    assert owner.files() == []
    assert first.post("/utilization/api/logging", json={"session_id": identifier, "enabled": False}, headers=headers).status_code == 202
    until(owner, lambda pub: not pub.logging.active and bool(pub.closed_logs))
    inventory = first.get("/utilization/api/files").json["files"]
    assert len(inventory) == 2
    assert all(item["available"] for item in inventory)
    csv = next(item for item in inventory if item["name"].endswith(".csv"))
    response = second.get("/utilization/api/files/" + csv["id"])
    assert response.status_code == 200
    assert b"retry_percent" in response.data and b"50.00" in response.data
    response.close()
    assert owner.read().phase is SessionPhase.RUNNING
    assert owner.read().snapshot == initial.snapshot
    assert first.post("/utilization/api/session/stop", json={"session_id": identifier}, headers=headers).status_code == 202
    until(owner, lambda pub: pub.phase is SessionPhase.STOPPED)
    # Closed segments survive an explicit new session without exposing its
    # currently written files or assigning the old files to the new session.
    deadline = time.monotonic() + 5
    while True:
        restarted = first.post("/utilization/api/session/start", json={"band": "2.4", "channel": "1"}, headers=headers)
        if restarted.status_code != 409:
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert restarted.status_code == 202
    next_identifier = restarted.json["session_id"]
    assert next_identifier != identifier
    until(owner, lambda pub: pub.phase is SessionPhase.RUNNING)
    retained = second.get("/utilization/api/files").json["files"]
    assert {item["id"] for item in retained} == {item["id"] for item in inventory}
    assert all(item["session_id"] == identifier for item in retained)
    owner.stop(next_identifier)
