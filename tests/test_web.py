"""Web rendering/control boundaries; no radio or frame aggregation in handlers."""
import re
from dataclasses import replace

import pytest

pytest.importorskip("flask", reason="Install the optional web extra for web tests")

from beacon_live.channel import ChannelWidth
from beacon_live.models import MetricsSnapshot
from beacon_live.runtime import LiveSessionConfig, RuntimePublication, SessionPhase
from beacon_live.service_client import ServiceError
from beacon_live.web import PublicationReader, create_app, open_closed_log

SESSION = "a" * 32


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

    def files(self):
        return self.records


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
            assert other.get("/utilization/assets/" + asset).status_code == 200
        assert other.get("/utilization/api/status").status_code == 200
    assert service.reads == 1  # One cached publication, shared across HTTP clients.
    assert service.controls == []
    assert service.publication.session_id == SESSION


def test_status_preserves_null_zero_gap_and_sub_one_retry_percent(web):
    service, _, client, _ = web
    base = service.publication.snapshot
    zero = replace(base.current, second=1000, retry_percent=0.0)
    small = replace(base.current, second=1001, retry_percent=0.25)
    gap = replace(base.current, second=1002, sample_available=False)
    snapshot = replace(base, generated_at=1003.0, current=small, history=(zero, small, gap))
    service.publication = replace(service.publication, snapshot=snapshot, completed_stats=(zero, small))
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
