"""Local Utilization web consumer. Capture belongs exclusively to the service."""

from __future__ import annotations

import argparse
import hmac
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from beacon_live.channel import PRIMARY_CHANNELS
from beacon_live.capture_options import validate_selection, web_channel_plan
from beacon_live.capture_target import list_wireless_interfaces

from beacon_live.ipc import DEFAULT_SOCKET, to_wire
from beacon_live.log_writer import build_live_log_paths
from beacon_live.models import QBSS_ADMISSION_CAPACITY_MAX
from beacon_live.runtime import LiveSessionConfig
from beacon_live.screens import DEFAULT_SCREENS
from beacon_live.service import validate_config
from beacon_live.service_client import ServiceClient, ServiceError

DEFAULT_LOG_DIRECTORY = Path("/var/log/wlanpi-beacon-live")


class PublicationReader:
    """Share a short read cache across HTTP threads without owning an analyzer."""

    def __init__(self, client, *, clock=time.monotonic):
        self.client, self.clock = client, clock
        self.lock = threading.RLock()
        self.cached = None
        self.deadline = 0.0

    def read(self):
        with self.lock:
            if self.cached is None or self.clock() >= self.deadline:
                publication = self.client.read()
                value = to_wire(publication)
                # Only expose useful consumer configuration, not internal IPC
                # control files or the closed-file descriptors used for downloads.
                value.pop("closed_logs", None)
                if value["config"] is not None:
                    value["config"] = {key: item for key, item in value["config"].items() if key in {
                        "iface", "netns", "band", "channel", "frequency_mhz", "channel_width",
                        "center_frequency1_mhz", "center_frequency2_mhz", "capture_backend", "local_cu",
                    }}
                self.cached = {"publication": value, "logging_available": bool(
                    publication.config and (publication.config.stats_csv or publication.config.beacons_jsonl)
                ), "screens": [
                    to_wire(screen.render(publication.snapshot)) for screen in DEFAULT_SCREENS
                ], "units": {"admission_capacity_max": QBSS_ADMISSION_CAPACITY_MAX}}
                self.deadline = self.clock() + 1.0
            return self.cached

    def control(self, operation):
        with self.lock:
            try:
                return operation()
            finally:
                self.cached = None
                self.deadline = 0.0


def open_closed_log(record: dict, directory: Path):
    """Open a registered identity under the log root, refusing symlinks and races.

    An already opened descriptor remains usable if retention removes its name.
    Replacement, truncation, and disappearance before open are explicit errors.
    """
    path = Path(record["path"])
    relative = path.relative_to(directory.absolute())
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("Invalid registered log path")
    if path.suffix not in {".csv", ".jsonl"}:
        raise ValueError("Unsupported log format")
    folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            os.close(folder)
            folder = child
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        ) != (record["device"], record["inode"], record["size_bytes"], record["mtime_ns"]):
            raise ValueError("The closed log was replaced or changed")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        return stream
    finally:
        os.close(folder)
        if descriptor is not None:
            os.close(descriptor)


def create_app(*, client=None, socket_path=DEFAULT_SOCKET, log_directory=DEFAULT_LOG_DIRECTORY,
               url_prefix="/utilization/", proxy_token=None, interface_provider=None):
    from flask import Flask, abort, jsonify, render_template, request, send_file
    from werkzeug.exceptions import HTTPException

    if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+", url_prefix):
        raise ValueError("URL prefix must contain path segments and end in a slash")
    app = Flask(__name__, static_folder="web_static", static_url_path=url_prefix + "assets")
    if proxy_token is not None and not re.fullmatch(r"[0-9a-f]{64}", proxy_token):
        raise ValueError("Invalid private proxy token")
    app.config.update(MAX_CONTENT_LENGTH=16 * 1024,
                      TRUSTED_HOSTS=None if proxy_token else ["localhost", "127.0.0.1"])
    service = client if client is not None else ServiceClient(socket_path)
    reader = PublicationReader(service)
    directory = Path(log_directory).absolute()
    csrf = secrets.token_urlsafe(32)
    app.extensions["utilization_reader"] = reader
    interface_lock = threading.Lock()
    interface_cache, interface_deadline = None, 0.0
    interface_provider = interface_provider or list_wireless_interfaces

    @app.before_request
    def local_origin():
        # The protected deployment uses a private Unix socket and a token set
        # only by the authenticated TLS location in Nginx. Neither browser
        # identity headers nor forwarded scheme/host headers confer authority.
        if proxy_token is not None:
            supplied = request.headers.get("X-Utilization-Proxy-Token", "")
            if not hmac.compare_digest(supplied.encode(), proxy_token.encode()):
                abort(403, "Use the authenticated WLAN Pi Utilization route")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403, "Cross-origin requests are not allowed")
        origin = request.headers.get("Origin")
        expected_origin = "https://" + request.host if proxy_token else request.host_url.rstrip("/")
        if origin is not None and origin.rstrip("/") != expected_origin:
            abort(403, "Cross-origin requests are not allowed")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("X-Utilization-CSRF", "")
            if not hmac.compare_digest(supplied.encode(), csrf.encode()):
                abort(403, "Reload the Utilization page before sending a control")

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=str(error)), 503

    @app.get(url_prefix)
    def index():
        try:
            initial = reader.read() | {"server_time": time.time()}
        except ServiceError:
            # Keep the page/reconnect UI available when the broker is absent.
            initial = None
        return render_template("utilization.html", prefix=url_prefix, csrf=csrf,
                               channel_choices=PRIMARY_CHANNELS, platform_hosted=bool(proxy_token),
                               initial=initial, capture_options=web_channel_plan())

    @app.get(url_prefix + "api/interfaces")
    def interfaces():
        nonlocal interface_cache, interface_deadline
        with interface_lock:
            if interface_cache is None or time.monotonic() >= interface_deadline:
                inventory = interface_provider()
                interface_cache = {"interfaces": [dict(id=item.target.label, interface=item.target.interface,
                    namespace=item.target.namespace, mode=item.mode, phy=item.phy) for item in inventory.interfaces],
                    "errors": inventory.errors}
                interface_deadline = time.monotonic() + 5
            return jsonify(interface_cache)

    @app.get(url_prefix + "api/status")
    def status():
        return jsonify(reader.read() | {"server_time": time.time()})

    def payload(allowed):
        value = request.get_json()
        if not isinstance(value, dict) or set(value) - set(allowed):
            abort(400, "Invalid control fields")
        return value

    @app.post(url_prefix + "api/session/start")
    def start():
        value = payload({"band", "channel", "channel_width", "center_frequency1_mhz",
                         "center_frequency2_mhz", "iface", "netns", "capture_backend",
                         "local_cu", "logging_enabled"})
        try:
            enabled = value.pop("logging_enabled", False)
            if type(enabled) is not bool:
                raise ValueError("Logging enabled must be boolean")
            from beacon_live.ipc import from_wire
            paths = build_live_log_paths(directory, write_stats_csv=True, write_beacons_jsonl=True)
            config = from_wire(LiveSessionConfig, value)
            if config.band not in PRIMARY_CHANNELS or int(config.channel) not in PRIMARY_CHANNELS[config.band]:
                raise ValueError("Select a valid primary channel for the band")
            validate_selection(config.band, int(config.channel), config.channel_width,
                               config.center_frequency1_mhz, config.center_frequency2_mhz)
            # Defaults match CLI/FPMS; the web form always supplies band/channel.
            validate_config(config)
            from dataclasses import replace
            config = replace(config, stats_csv=paths.stats_csv, beacons_jsonl=paths.beacons_jsonl,
                             initial_logging_enabled=enabled)
            publication = reader.control(lambda: service.request("start", config=config))
        except (ValueError, TypeError) as error:
            abort(400, str(error))
        except ServiceError as error:
            if "owns the radio" in str(error):
                abort(409, str(error))
            raise
        return jsonify(session_id=publication.session_id, phase=publication.phase.value), 202

    def current_session(value):
        identifier = value.get("session_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            abort(400, "A current session ID is required")
        return identifier

    @app.post(url_prefix + "api/session/stop")
    def stop():
        value = payload({"session_id"})
        identifier = current_session(value)
        try:
            publication = reader.control(lambda: service.stop(identifier))
        except ServiceError as error:
            if "Stale" in str(error) or "No active" in str(error):
                abort(409, str(error))
            raise
        return jsonify(phase=publication.phase.value), 202

    @app.post(url_prefix + "api/logging")
    def logging_control():
        value = payload({"session_id", "enabled"})
        identifier = current_session(value)
        if type(value.get("enabled")) is not bool:
            abort(400, "Logging enabled must be boolean")
        try:
            reader.control(lambda: service.set_logging(identifier, value["enabled"]))
        except ServiceError as error:
            if any(text in str(error) for text in ("Stale", "No active", "stopping", "no logging formats")):
                abort(409, str(error))
            raise
        return jsonify(requested=value["enabled"]), 202

    def registered_files():
        return service.files()

    @app.get(url_prefix + "api/files")
    def files():
        result = []
        for record in registered_files():
            try:
                relative = Path(record["path"]).relative_to(directory)
            except ValueError:
                # A CLI session can use a custom log root. Keep those names
                # outside this web instance's download scope.
                continue
            if not relative.parts or ".." in relative.parts:
                continue
            try:
                stream = open_closed_log(record, directory)
                stream.close()
                available = True
            except (OSError, ValueError):
                available = False
            result.append({key: record[key] for key in ("id", "session_id", "size_bytes", "closed_at")}
                          | {"name": Path(record["path"]).name, "available": available})
        return jsonify(files=list(reversed(result)))

    @app.get(url_prefix + "api/files/<file_id>")
    def download(file_id):
        if not re.fullmatch(r"[0-9a-f]{32}", file_id):
            abort(404, "Unknown closed log")
        record = next((item for item in registered_files() if item["id"] == file_id), None)
        if record is None:
            abort(404, "Unknown closed log")
        try:
            stream = open_closed_log(record, directory)
        except (OSError, ValueError):
            abort(404, "This log is no longer available")
        try:
            response = send_file(stream, as_attachment=True, download_name=Path(record["path"]).name,
                                 mimetype="text/csv" if record["path"].endswith(".csv") else "application/x-ndjson",
                                 conditional=False, etag=False)
            response.content_length = record["size_bytes"]
            response.call_on_close(stream.close)
            return response
        except BaseException:
            stream.close()
            raise

    return app


def read_proxy_token(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="ascii") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError("Proxy token must be an owner-only regular file owned by the web service user")
        token = stream.read(128).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("Invalid private proxy token file")
    return token


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wlanpi-utilization web", description="Serve the local Utilization web interface.")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--url-prefix", default="/utilization/")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIRECTORY)
    parser.add_argument("--unix-socket", type=Path, help="Private socket for authenticated WLAN Pi hosting.")
    parser.add_argument("--proxy-token-file", type=Path, help="Owner-only shared token for the protected Nginx route.")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if bool(args.unix_socket) != bool(args.proxy_token_file):
        parser.error("Private hosting requires both --unix-socket and --proxy-token-file")
    if args.unix_socket and (args.host != "127.0.0.1" or args.port != 8765):
        parser.error("Use either the private socket or loopback host/port options")
    try:
        from waitress import serve
        token = read_proxy_token(args.proxy_token_file) if args.proxy_token_file else None
        app = create_app(socket_path=args.socket, log_directory=args.log_dir, url_prefix=args.url_prefix,
                         proxy_token=token)
    except ImportError:
        parser.error("Install the web extra: pip install -e '.[web]' or use the Pi installer with --with-web")
    except (ValueError, OSError, UnicodeError) as error:
        parser.error(str(error))
    listener = {"unix_socket": str(args.unix_socket), "unix_socket_perms": "660", "url_scheme": "https"} if args.unix_socket else {"host": args.host, "port": args.port}
    print(f"Utilization: private WLAN Pi socket {args.unix_socket}" if args.unix_socket
          else f"Utilization: http://{args.host}:{args.port}{args.url_prefix}", flush=True)
    serve(app, **listener, threads=4, connection_limit=32,
          max_request_body_size=16 * 1024, channel_timeout=15, clear_untrusted_proxy_headers=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
