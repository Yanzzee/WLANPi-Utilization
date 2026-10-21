"""Web rendering/control boundaries; no radio or frame aggregation in handlers."""
import re
import json
from dataclasses import replace

import pytest

pytest.importorskip("flask", reason="Install the optional web extra for web tests")

from beacon_live.channel import ChannelWidth
from beacon_live.models import MetricsSnapshot
from beacon_live.runtime import LiveSessionConfig, RuntimePublication, SessionPhase
from beacon_live.service_client import ServiceError
from beacon_live.web import PublicationReader, create_app, open_closed_log
from beacon_live.web import read_proxy_token

SESSION = "a" * 32
PROXY_TOKEN = "b" * 64


class FakeService:
    def __init__(self):
        self.publication = RuntimePublication(
            SESSION, 10, SessionPhase.RUNNING,
            LiveSessionConfig(band="2.4", channel="1"), MetricsSnapshot.empty(),
        )
        self.reads = 0
        self.controls = []
        self.records = []
        self.failed = False

    def read(self):
        self.reads += 1
        if self.failed:
            raise ServiceError("Service unavailable")
        return self.publication

    def request(self, op, **values):
        self.controls.append((op, values))
        return self.publication

    def stop(self, identifier):
        if identifier != SESSION:
            raise ServiceError("Stale Utilization session ID")
        return self.request("stop", session_id=identifier)

    def set_logging(self, identifier, enabled):
        if identifier != SESSION:
            raise ServiceError("Stale Utilization session ID")
        return self.request("logging", session_id=identifier, enabled=enabled)

    def set_export(self, identifier, enabled):
        if identifier != SESSION:
            raise ServiceError("Stale Utilization session ID")
        return self.request("export", session_id=identifier, enabled=enabled)

    def files(self):
        return self.records


def test_protected_proxy_requires_private_token_for_every_route_and_checks_public_origin(tmp_path):
    service = FakeService()
    app = create_app(client=service, log_directory=tmp_path, proxy_token=PROXY_TOKEN)
    client = app.test_client()
    base = "https://wlanpi-r5.local"
    for path in ("", "api/status", "api/interfaces", "api/files", "assets/utilization.js", "api/files/" + "c" * 32):
        assert client.get("/utilization/" + path, base_url=base).status_code == 403
    headers = {"X-Utilization-Proxy-Token": PROXY_TOKEN}
    page = client.get("/utilization/", base_url=base, headers=headers)
    assert page.status_code == 200
    token = re.search(r'name="utilization-csrf" content="([^"]+)"', page.text)[1]
    body = {"session_id": SESSION}
    assert not service.controls
    assert client.post("/utilization/api/session/stop", base_url=base, json=body, headers={
        **headers, "Origin": base, "X-Utilization-CSRF": token}).status_code == 202
    for origin in ("http://wlanpi-r5.local", "https://foreign.example", "https://wlanpi-r5.local.evil.example"):
        assert client.post("/utilization/api/session/stop", base_url=base, json=body, headers={
            **headers, "Origin": origin, "X-Forwarded-Proto": "https", "X-Forwarded-Host": "foreign.example",
            "X-Utilization-CSRF": token}).status_code == 403
    assert len(service.controls) == 1
    assert client.post("/utilization/api/session/stop", base_url=base, json=body, headers={
        **headers, "Origin": base}).status_code == 403
    assert client.get("/utilization/", base_url=base, headers={
        **headers, "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_private_proxy_token_file_permissions_symlinks_and_cli_pairing(tmp_path):
    from beacon_live.web import main
    path = tmp_path / "proxy.token"
    path.write_text(PROXY_TOKEN + "\n")
    path.chmod(0o600)
    assert read_proxy_token(path) == PROXY_TOKEN
    path.chmod(0o640)
    with pytest.raises(ValueError, match="owner-only"):
        read_proxy_token(path)
    path.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        read_proxy_token(alias)
    for args in (["--unix-socket", str(tmp_path / "web.sock")], ["--proxy-token-file", str(path)]):
        with pytest.raises(SystemExit):
            main(args)


def test_protected_cli_creates_only_a_private_unix_listener(tmp_path, monkeypatch):
    import waitress
    from beacon_live.web import main
    path = tmp_path / "proxy.token"
    path.write_text(PROXY_TOKEN)
    path.chmod(0o600)
    calls = []
    monkeypatch.setattr(waitress, "serve", lambda app, **options: calls.append(options))
    assert main(["--unix-socket", str(tmp_path / "web.sock"), "--proxy-token-file", str(path)]) == 0
    assert len(calls) == 1 and "host" not in calls[0] and "port" not in calls[0]
    assert calls[0]["unix_socket_perms"] == "660" and calls[0]["url_scheme"] == "https"


@pytest.fixture
def web(tmp_path):
    service = FakeService()
    app = create_app(client=service, log_directory=tmp_path)
    app.testing = True
    client = app.test_client()
    page = client.get("/utilization/")
    token = re.search(r'name="utilization-csrf" content="([^"]+)"', page.text)[1]
    return service, app, client, {"X-Utilization-CSRF": token, "Origin": "http://localhost"}


def test_page_tabs_assets_and_multiple_clients_are_read_only(web):
    service, app, client, _ = web
    for other in (client, app.test_client(), app.test_client()):
        for _ in range(2):
            page = other.get("/utilization/")
            assert page.status_code == 200
            assert "<title>Utilization</title>" in page.text
            assert "BSSIDs" in page.text and "Log files" in page.text
            assert "script-src 'self'" in page.headers["Content-Security-Policy"]
            assert page.headers["Cache-Control"] == "no-store"
        for asset in ("utilization.css", "utilization.js"):
            response = other.get("/utilization/assets/" + asset)
            assert response.status_code == 200
            response.close()
        assert other.get("/utilization/api/status").status_code == 200
    assert service.reads == 1  # One cached publication, shared across HTTP clients.
    assert service.controls == []
    assert service.publication.session_id == SESSION


def test_page_bootstraps_the_running_publication_and_preserves_data_without_a_control(web):
    service, app, client, _ = web
    sample = replace(MetricsSnapshot.empty().current, second=1000, retry_percent=.25)
    service.publication = replace(service.publication, snapshot=replace(
        MetricsSnapshot.empty(), generated_at=1001., current=sample, history=(sample,)))
    app.extensions["utilization_reader"].deadline = 0
    page = client.get("/utilization/").text
    data = json.loads(re.search(r'<script id="initial-status" type="application/json">(.*?)</script>', page, re.S)[1])
    assert data["publication"]["session_id"] == SESSION
    assert data["publication"]["phase"] == "running"
    assert data["publication"]["snapshot"]["current"]["retry_percent"] == .25
    assert not service.controls
    assert page.index('id="composition-title"') < page.index('On this channel')
    assert 'data-sort="latest_ap_name"' in page
    assert 'id="session-toggle"' in page and "Stop capture" not in page
    assert 'id="stop"' not in page and 'id="new-session"' not in page
    assert '<button id="logging-toggle"' in page
    assert re.search(r'<input id="start-logging"[^>]*type="checkbox"', page)
    assert page.index('id="start-form"') < page.index('id="start-logging"')
    assert page.count('name="logging_enabled"') == 1
    assert 'name="local_cu"' not in page


def test_protected_page_reuses_platform_favicons_and_actual_capture_tuning(tmp_path):
    from beacon_live.channel import ChannelDefinition
    service = FakeService()
    actual = ChannelDefinition(5180, ChannelWidth.MHZ20, 5180, primary_channel=36)
    service.publication = replace(service.publication, config=LiveSessionConfig(
        band="5", channel="36", channel_width=ChannelWidth.MHZ80, center_frequency1_mhz=5210),
        capture_definition=actual, capture_definition_verified=True, capture_width="20")
    client = create_app(client=service, proxy_token=PROXY_TOKEN).test_client()
    headers = {"X-Utilization-Proxy-Token": PROXY_TOKEN}
    page = client.get("/utilization/", headers=headers).text
    assert 'href="/static/img/favicon-32x32.png"' in page
    assert 'href="/static/img/favicon-16x16.png"' in page
    data = client.get("/utilization/api/status", headers=headers).json
    assert data["publication"]["capture_definition"]["width"] == "20"
    assert data["publication"]["capture_definition"]["center_frequency1_mhz"] == 5180
    assert data["publication"]["config"]["center_frequency1_mhz"] == 5210


def test_page_still_loads_and_can_reconnect_when_broker_is_absent(tmp_path):
    service = FakeService()
    service.failed = True
    client = create_app(client=service, log_directory=tmp_path).test_client()
    page = client.get("/utilization/")
    assert page.status_code == 200
    assert '<script id="initial-status" type="application/json">null</script>' in page.text
    assert client.get("/utilization/api/status").status_code == 503
    service.failed = False
    assert client.get("/utilization/api/status").json["publication"]["session_id"] == SESSION
    assert not service.controls


def test_embedded_status_escapes_untrusted_text_without_creating_script_tags(tmp_path):
    service = FakeService()
    text = '</script><script src="/unexpected.js"></script>'
    service.publication = replace(service.publication, notice=text)
    page = create_app(client=service, log_directory=tmp_path).test_client().get("/utilization/").text
    assert text not in page
    embedded = re.search(r'<script id="initial-status" type="application/json">(.*?)</script>', page, re.S)[1]
    assert json.loads(embedded)["publication"]["notice"] == text


def test_channel_choices_and_server_validation_use_primary_band_grid(web):
    from html import unescape
    import json
    from beacon_live.channel import PRIMARY_CHANNELS
    service, _, client, headers = web
    page = client.get("/utilization/").text
    choices = json.loads(unescape(re.search(r"data-channels='([^']+)'", page)[1]))
    assert choices == {band: list(channels) for band, channels in PRIMARY_CHANNELS.items()}
    assert 'id="channel" data-channels=' in page and 'type="number" min="1" max="233"' not in page
    for band, channel in (("2.4", "15"), ("5", "37"), ("5", "42"), ("6", "2"), ("6", "234")):
        response = client.post("/utilization/api/session/start", json={"band": band, "channel": channel}, headers=headers)
        assert response.status_code == 400
    assert not service.controls
    for band, channel in (("2.4", "14"), ("5", "177"), ("6", "233")):
        assert client.post("/utilization/api/session/start", json={"band": band, "channel": channel}, headers=headers).status_code == 202


def test_capture_quality_preserves_owner_counts_and_reports_server_time(web):
    from beacon_live.models import CaptureHealth
    service, app, client, _ = web
    service.publication = replace(service.publication,
        capture_health=CaptureHealth(2000, 1900, 100, 300, 1000.25, True))
    app.extensions["utilization_reader"].deadline = 0
    response = client.get("/utilization/api/status").json
    assert response['publication']['capture_health'] == {
        'capture_records': 2000, 'decoded_frames': 1900, 'rejected_records': 100,
        'bad_fcs_frames': 300, 'latest_timestamp': 1000.25, 'fcs_metadata_available': True,
    }
    assert response['server_time'] > 1000.25


def test_status_preserves_null_zero_gap_and_sub_one_retry_percent(web):
    service, app, client, _ = web
    base = service.publication.snapshot
    zero = replace(base.current, second=1000, retry_percent=0.0)
    small = replace(base.current, second=1001, retry_percent=0.25)
    gap = replace(base.current, second=1002, sample_available=False)
    snapshot = replace(base, generated_at=1003.0, current=small, history=(zero, small, gap))
    service.publication = replace(service.publication, snapshot=snapshot, completed_stats=(zero, small))
    app.extensions["utilization_reader"].deadline = 0
    value = client.get("/utilization/api/status").json
    assert value["publication"]["snapshot"]["current"]["retry_percent"] == 0.25
    assert value["publication"]["snapshot"]["history"][0]["retry_percent"] == 0
    assert value["publication"]["snapshot"]["history"][2]["sample_available"] is False
    assert value["publication"]["snapshot"]["current"]["local_cu_percent"] is None
    assert len(value["publication"]["completed_stats"]) == 2
    retry = next(screen for screen in value["screens"] if screen["screen_id"] == "retry")
    assert "<1%" in retry["summary"]
    assert "closed_logs" not in value["publication"]
    assert "logging_control_path" not in value["publication"]["config"]


def test_start_validates_configuration_without_exposing_paths(web, tmp_path):
    service, _, client, headers = web
    body = {"band": "5", "channel": "36", "channel_width": "80", "center_frequency1_mhz": 5210,
            "local_cu": True, "logging_enabled": False}
    response = client.post("/utilization/api/session/start", json=body, headers=headers)
    assert response.status_code == 202
    operation, values = service.controls[-1]
    assert operation == "start"
    config = values["config"]
    assert config.capture_backend == "direct"
    assert config.channel_width is ChannelWidth.MHZ80
    assert config.stats_csv.parent == tmp_path
    assert config.beacons_jsonl.parent == tmp_path
    assert not config.initial_logging_enabled
    for changed in ({"stats_csv": "/etc/passwd"}, {"bssid": "a"}, {"band": "invalid"},
                    {"channel": "0"}, {"local_cu": "yes"}, {"logging_enabled": 1},
                    {"channel_width": "40", "center_frequency1_mhz": None}):
        assert client.post("/utilization/api/session/start", json=body | changed, headers=headers).status_code == 400
    assert len(service.controls) == 1


def test_interfaces_are_cached_read_only_and_preserve_partial_inventory(tmp_path):
    from beacon_live.capture_target import CaptureTarget, InterfaceInventory, WirelessInterface
    service, reads = FakeService(), []
    def provider():
        reads.append(True)
        return InterfaceInventory((WirelessInterface(CaptureTarget("mon0", "lab"), "phy1", "monitor"),), ("One namespace unavailable",))
    app = create_app(client=service, log_directory=tmp_path, interface_provider=provider)
    client = app.test_client()
    expected = {"interfaces": [{"id": "lab:mon0", "interface": "mon0", "namespace": "lab", "phy": "phy1", "mode": "monitor"}],
                "errors": ["One namespace unavailable"]}
    assert client.get("/utilization/api/interfaces").json == expected
    assert client.get("/utilization/api/interfaces").json == expected
    assert reads == [True]
    assert not service.controls and service.reads == 0


def test_actual_capture_target_is_published_without_replacing_requested_auto_config(web):
    from beacon_live.capture_target import CaptureTarget
    service, app, client, _ = web
    service.publication = replace(service.publication, capture_target=CaptureTarget("sniffer", "capture"))
    app.extensions["utilization_reader"].deadline = 0
    value = client.get("/utilization/api/status").json["publication"]
    assert value["capture_target"] == {"interface": "sniffer", "namespace": "capture"}
    assert value["config"]["iface"] == "auto"


@pytest.mark.parametrize("body", [
    {"band": "2.4", "channel": "1", "channel_width": "80", "center_frequency1_mhz": 2442},
    {"band": "5", "channel": "36", "channel_width": "320", "center_frequency1_mhz": 5330},
    {"band": "5", "channel": "44", "channel_width": "80", "center_frequency1_mhz": 5230},
    {"band": "5", "channel": "36", "channel_width": "80+80", "center_frequency1_mhz": 5210, "center_frequency2_mhz": 5290},
])
def test_web_rejects_unsupported_or_off_grid_bonding_before_start(web, body):
    service, _, client, headers = web
    assert client.post("/utilization/api/session/start", json=body, headers=headers).status_code == 400
    assert not service.controls


def test_mutations_require_same_origin_and_csrf_and_current_id(web):
    service, _, client, headers = web
    path = "/utilization/api/session/stop"
    body = {"session_id": SESSION}
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers=headers | {"Origin": "https://other.example"}).status_code == 403
    assert client.post(path, json=body, headers=headers | {"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(path, json=body, headers={"X-Utilization-CSRF": "é"}).status_code == 403
    assert client.post(path, json={"session_id": "SESSION_ID"}, headers=headers).status_code == 400
    assert client.post(path, json={"session_id": "b" * 32}, headers=headers).status_code == 409
    assert service.controls == []
    assert client.post(path, json=body, headers=headers).status_code == 202
    assert service.controls[-1][0] == "stop"
    assert client.get(path).status_code == 405


def test_logging_control_preserves_capture_and_invalidates_cache(web):
    service, _, client, headers = web
    client.get("/utilization/api/status")
    assert client.post("/utilization/api/logging", json={"session_id": SESSION, "enabled": True}, headers=headers).status_code == 202
    client.get("/utilization/api/status")
    assert service.reads == 2
    assert service.publication.phase is SessionPhase.RUNNING
    assert service.publication.session_id == SESSION
    assert service.controls == [("logging", {"session_id": SESSION, "enabled": True})]
    assert client.post("/utilization/api/logging", json={"session_id": SESSION, "enabled": "yes"}, headers=headers).status_code == 400


def record_for(path, identifier="c" * 32):
    info = path.stat()
    return {"id": identifier, "path": str(path), "device": info.st_dev, "inode": info.st_ino,
            "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns, "closed_at": 1000.0, "session_id": SESSION}


def test_download_inventory_is_registered_scoped_and_closed(web, tmp_path):
    service, _, client, _ = web
    path = tmp_path / "utilization_stats.csv"
    path.write_text("second,retry_percent\n1000,0.25\n")
    record = record_for(path)
    service.records.append(record)
    inventory = client.get("/utilization/api/files").json["files"]
    assert inventory[0]["available"] and inventory[0]["name"] == path.name
    assert "path" not in inventory[0]
    response = client.get("/utilization/api/files/" + record["id"])
    assert response.status_code == 200 and response.data == path.read_bytes()
    assert response.headers["Content-Disposition"].startswith("attachment;")
    response.close()
    assert client.get("/utilization/api/files/" + "d" * 32).status_code == 404
    assert client.get("/utilization/api/files/../../etc/passwd").status_code == 404
    outside = tmp_path.parent / "outside.csv"
    outside.write_text("outside")
    service.records.append(record_for(outside, "e" * 32))
    # A lexical root prefix must not expose a parent-traversal record's name.
    service.records.append(record_for(tmp_path / ".." / outside.name, "f" * 32))
    assert len(client.get("/utilization/api/files").json["files"]) == 1
    assert client.get("/utilization/api/files/" + "e" * 32).status_code == 404
    assert client.get("/utilization/api/files/" + "f" * 32).status_code == 404


@pytest.mark.parametrize("mutation", ["delete", "replace", "truncate", "symlink"])
def test_download_rejects_deleted_changed_or_replaced_segments(web, tmp_path, mutation):
    service, _, client, _ = web
    path = tmp_path / "segment.csv"
    path.write_text("original log\n")
    record = record_for(path)
    service.records.append(record)
    if mutation == "truncate":
        path.write_text("")
    else:
        path.unlink()
        if mutation == "replace":
            path.write_text("replacement")
        elif mutation == "symlink":
            target = tmp_path / "unrelated.csv"
            target.write_text("private")
            path.symlink_to(target)
    assert client.get("/utilization/api/files").json["files"][0]["available"] is False
    assert client.get("/utilization/api/files/" + record["id"]).status_code == 404


def test_open_download_survives_retention_unlink_and_rejects_parent_symlink(tmp_path):
    path = tmp_path / "segment.jsonl"
    path.write_text('{"ssid":"Example"}\n')
    stream = open_closed_log(record_for(path), tmp_path)
    path.unlink()
    assert stream.read() == b'{"ssid":"Example"}\n'
    stream.close()
    real = tmp_path / "real"
    real.mkdir()
    (real / "segment.csv").write_text("private")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        open_closed_log(record_for(alias / "segment.csv"), tmp_path)


def test_unavailable_service_is_explicit_and_page_does_not_start_it(web):
    service, app, client, _ = web
    service.failed = True
    app.extensions["utilization_reader"].deadline = 0
    assert client.get("/utilization/").status_code == 200
    assert client.get("/utilization/api/status").status_code == 503
    assert service.controls == []
    service.failed = False
    assert client.get("/utilization/api/status").status_code == 200


def test_prefix_host_body_limits_and_no_external_asset_dependencies(web):
    service, app, client, headers = web
    other = create_app(client=service, url_prefix="/local/utilization/").test_client()
    assert other.get("/local/utilization/").status_code == 200
    assert other.get("/local/utilization/api/status").status_code == 200
    assert client.get("/utilization/", headers={"Host": "attacker.example"}).status_code == 400
    assert client.get("/utilization/api/status", headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.get("/utilization/", base_url="http://127.0.0.1").status_code == 200
    assert client.post("/utilization/api/logging", data="x" * 17000, content_type="application/json", headers=headers).status_code == 413
    page = client.get("/utilization/").text
    assert not re.search(r'(?:src|href)="https?://', page)


def test_export_controls_require_csrf_valid_id_and_preserve_session(web):
    service, app, client, headers = web
    body = {"session_id": SESSION, "enabled": True}
    path = "/utilization/api/export"
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers=headers | {"Origin": "https://other.example"}).status_code == 403
    assert client.post(path, json=body | {"session_id": "b" * 32}, headers=headers).status_code == 409
    assert client.post(path, json=body | {"enabled": "yes"}, headers=headers).status_code == 400
    assert not service.controls
    assert client.post(path, json=body, headers=headers).status_code == 202
    assert service.controls == [("export", body)]
    assert service.publication.phase is SessionPhase.RUNNING
    assert service.publication.session_id == SESSION


def test_web_start_can_enable_export_and_status_hides_internal_paths(web, tmp_path):
    from beacon_live.beacon_export import BeaconExportStatus
    service, app, client, headers = web
    assert client.post("/utilization/api/session/start", json={
        "band": "2.4", "channel": "6", "export_enabled": True}, headers=headers).status_code == 202
    config = service.controls[-1][1]["config"]
    assert config.initial_export_enabled and config.export_directory == tmp_path / "beacons"
    assert not config.initial_logging_enabled
    assert client.post("/utilization/api/session/start", json={
        "band": "2.4", "channel": "6", "export_enabled": 1}, headers=headers).status_code == 400
    service.publication = replace(service.publication, config=config, beacon_export=BeaconExportStatus(
        requested=True, active=True, exported_beacons=17, skipped_beacons=2, queue_full=2))
    app.extensions["utilization_reader"].deadline = 0
    status = client.get("/utilization/api/status").json
    assert status["export_available"]
    assert status["publication"]["beacon_export"]["exported_beacons"] == 17
    assert status["publication"]["beacon_export"]["skipped_beacons"] == 2
    assert "closed_exports" not in status["publication"]
    assert "export_directory" not in status["publication"]["config"]


def test_closed_pcapng_download_preserves_bytes_and_handles_retention(web, tmp_path):
    from test_pcapng import section, interface, packet
    service, _, client, _ = web
    directory = tmp_path / "beacons"
    directory.mkdir()
    path = directory / "utilization_beacons.pcapng"
    path.write_bytes(section() + interface() + packet())
    record = record_for(path)
    service.records.append(record)
    assert client.get("/utilization/api/files").json["files"][0]["available"]
    response = client.get("/utilization/api/files/" + record["id"])
    assert response.status_code == 200 and response.data == path.read_bytes()
    assert response.mimetype == "application/octet-stream"
    response.close()
    path.unlink()
    assert not client.get("/utilization/api/files").json["files"][0]["available"]
    assert client.get("/utilization/api/files/" + record["id"]).status_code == 404
    partial = directory / "active.pcapng.partial"
    partial.write_bytes(b"incomplete")
    with pytest.raises(ValueError, match="Unsupported"):
        open_closed_log(record_for(partial), tmp_path)
