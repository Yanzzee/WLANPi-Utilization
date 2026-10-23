"""Actual TLS Nginx auth_request + private Waitress + managed broker.

The platform session checker is a controlled fixture, not PAM/device acceptance.
Set UTILIZATION_TEST_NGINX to a binary built with SSL and auth_request.
"""
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time

import pytest

pytest.importorskip("flask")
pytest.importorskip("waitress")

from flask import Flask, request
from waitress import create_server
from beacon_live.runtime import SessionPhase
from beacon_live.web import create_app
from scripts.install_web_integration import proxy_headers
from test_service import service, until

NGINX = os.environ.get("UTILIZATION_TEST_NGINX") or shutil.which("nginx")
pytestmark = pytest.mark.skipif(not NGINX or not shutil.which("openssl"), reason="Optional real Nginx TLS/auth_request integration")
TOKEN = "a" * 64
ROOT = Path(__file__).resolve().parents[1]


def serve_unix(app, path):
    server = create_server(app, unix_socket=str(path), unix_socket_perms="600")
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def close_server(pair):
    server, thread = pair
    server.trigger.pull_trigger(server.close)
    thread.join(3)
    server.task_dispatcher.shutdown()
    assert not thread.is_alive()


def test_authenticated_tls_route_controls_downloads_expiry_logout_and_reconnect(service, tmp_path):
    owner, _ = service
    # Keep Unix paths short on macOS and Linux, independent of a long TMPDIR.
    with tempfile.TemporaryDirectory(prefix="ut-web-", dir="/tmp") as folder:
        root = Path(folder)
        front, backend, auth_socket = (root / name for name in ("front.sock", "backend.sock", "auth.sock"))
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=wlanpi-r5.local", "-addext", "subjectAltName=DNS:wlanpi-r5.local",
            "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        trust = ssl.create_default_context(cafile=str(cert))
        auth = Flask("fixture_auth")
        sessions = {"valid": 100}
        clock = [10]
        unavailable = [False]

        @auth.get("/auth/check")
        def check():
            if unavailable[0]:
                return "", 503
            return "", 200 if sessions.get(request.cookies.get("sid"), 0) > clock[0] else 401

        auth_server = serve_unix(auth, auth_socket)
        app_server = serve_unix(create_app(client=owner, log_directory=tmp_path, proxy_token=TOKEN), backend)
        headers_file = root / "headers.conf"
        headers_file.write_text(proxy_headers(TOKEN))
        headers_file.chmod(0o600)
        fragment = (ROOT / "integration/webui/utilization-nginx.conf").read_text().replace(
            "/etc/wlanpi-beacon-live/web-proxy.headers", str(headers_file)).replace(
            "/run/wlanpi-utilization-web/web.sock", str(backend))
        configuration = root / "nginx.conf"
        configuration.write_text(f'''daemon off;
master_process off;
pid {root}/nginx.pid;
error_log {root}/error.log info;
events {{ worker_connections 64; }}
http {{
    access_log off;
    client_body_temp_path {root}/client_body;
    proxy_temp_path {root}/proxy;
    server {{
        listen unix:{front} ssl;
        ssl_certificate {cert};
        ssl_certificate_key {key};
        error_page 401 = @login_redirect;
        location @login_redirect {{ return 302 /login; }}
        location = /login {{ return 200 "fixture sign-in"; }}
        location = /auth/check {{
            internal;
            proxy_pass http://unix:{auth_socket};
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
        }}
{fragment}
    }}
}}
''')
        subprocess.run([NGINX, "-t", "-p", folder, "-c", str(configuration)], check=True, capture_output=True)
        process = subprocess.Popen([NGINX, "-p", folder, "-c", str(configuration)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        class Connection(http.client.HTTPConnection):
            def connect(self):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(3)
                sock.connect(str(front))
                self.sock = trust.wrap_socket(sock, server_hostname="wlanpi-r5.local")

        def get(path, *, cookie=None, method="GET", body=None, headers=None):
            client = Connection("wlanpi-r5.local", timeout=3)
            values = dict(headers or {})
            if cookie:
                values["Cookie"] = "sid=" + cookie
            if body is not None:
                values["Content-Type"] = "application/json"
            try:
                client.request(method, path, json.dumps(body) if body is not None else None, values)
                response = client.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                client.close()

        try:
            deadline = time.monotonic() + 5
            while not front.exists():
                assert process.poll() is None, process.communicate()[1].decode()
                assert time.monotonic() < deadline
                time.sleep(.02)
            assert get("/utilization")[0] == 308
            assert get("/auth/check")[0] == 404  # internal only
            assert get("/utilization/")[0] == 302
            for path in ("status", "files", "files/" + "c" * 32):
                status, _, data = get("/utilization/api/" + path)
                assert status == 401 and json.loads(data)["login_url"] == "/login"
            assert owner.read().phase is SessionPhase.IDLE
            status, _, html = get("/utilization/", cookie="valid")
            assert status == 200 and b"<title>Utilization</title>" in html
            csrf = re.search(rb'name="utilization-csrf" content="([^"]+)"', html)[1].decode()
            controls = {"Origin": "https://wlanpi-r5.local", "X-Utilization-CSRF": csrf}
            assert get("/utilization/assets/utilization.js", cookie="valid")[0] == 200
            assert get("/utilization/api/session/start", cookie="valid", method="POST",
                       body={"band": "2.4", "channel": "1"}, headers={**controls, "Origin": "https://evil.example"})[0] == 403
            status, _, data = get("/utilization/api/session/start", cookie="valid", method="POST",
                body={"band": "2.4", "channel": "1", "logging_enabled": True}, headers=controls)
            assert status == 202, data
            identifier = json.loads(data)["session_id"]
            initial = until(owner, lambda pub: pub.logging.active and bool(pub.completed_stats))
            for _ in range(3):
                assert get("/utilization/", cookie="valid")[0] == 200
                status, _, data = get("/utilization/api/status", cookie="valid")
                assert status == 200 and json.loads(data)["publication"]["session_id"] == identifier
            assert get("/utilization/api/logging", cookie="valid", method="POST",
                body={"session_id": identifier, "enabled": False}, headers=controls)[0] == 202
            until(owner, lambda pub: not pub.logging.active and bool(pub.closed_logs))
            status, _, data = get("/utilization/api/files", cookie="valid")
            assert status == 200
            file_id = next(item["id"] for item in json.loads(data)["files"] if item["name"].endswith(".csv"))
            status, headers, content = get("/utilization/api/files/" + file_id, cookie="valid")
            assert status == 200 and "attachment;" in headers["Content-Disposition"] and b"retry_percent" in content
            clock[0] = 101  # Expired platform cookie; all API endpoints deny.
            assert get("/utilization/api/status", cookie="valid")[0] == 401
            assert get("/utilization/api/files/" + file_id, cookie="valid")[0] == 401
            assert get("/utilization/api/session/stop", cookie="valid", method="POST",
                body={"session_id": identifier}, headers={**controls, "X-Utilization-Proxy-Token": TOKEN})[0] == 401
            assert owner.read().phase is SessionPhase.RUNNING
            unavailable[0] = True
            assert get("/utilization/api/status", cookie="valid")[0] == 500
            unavailable[0] = False
            sessions["renewed"] = 1000
            status, _, data = get("/utilization/api/status", cookie="renewed")
            assert status == 200 and json.loads(data)["publication"]["session_id"] == identifier
            assert owner.read().snapshot == initial.snapshot
            del sessions["renewed"]  # Logout/revocation must not be cached.
            assert get("/utilization/api/status", cookie="renewed")[0] == 401
            sessions["final"] = 1000
            assert get("/utilization/api/session/stop", cookie="final", method="POST",
                body={"session_id": identifier}, headers=controls)[0] == 202
            until(owner, lambda pub: pub.phase is SessionPhase.STOPPED)
        finally:
            process.terminate()
            process.communicate(timeout=5)
            close_server(app_server)
            close_server(auth_server)
