#!/usr/bin/env python3
"""Install Utilization's protected WLAN Pi Apps route without restarting capture."""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.patch_wlanpi_fpms import FileState, _atomic_write

ROOT = Path(__file__).resolve().parents[1]
WEB_SERVICE = "wlanpi-utilization-web.service"
AUTH_SHA256 = "0928f7c1094ed6c65bd173bacbd17a274d125c02d6298d729d29aa572e0a087c"
NGINX_SHA256 = "8d4704f848e77777ef1220ecbb442beb9220f7cde3a71735968d2b6517f0382f"
APPLICATION_SHA256 = {
    "573e60fb1a4af250c118888e4d318c824c870a2cec3a14889f49b78e88e1b1bb",  # reviewed upstream
    "b90723ca640079f8e7354fd0757c4d85b0d4e591ccc51d99147cb247bf0af3f9",  # wlanpi-r5 preflight
    "02f2915fc819ce479805dc0975d4490cf5a4d72cfdb237d68b65dcd6212f0330",  # Pi 4: upstream dc39a7e
}
# Relevant before_request functions at reviewed upstream 2b112f5. Other WebUI
# supported application revision also has its original session gates intact.
AUTH_FUNCTIONS = {
    "enforce_session_freshness": "13dae8ddfe7542fbf91049559381605a2abc586fbb60378b85605737d0a81e88",
    "require_login": "20569c051037a1587be988661356bd384178e48b666c850970d355e4bb47dad1",
}
GRID = '<div class="uk-grid-small uk-child-width-1-1 uk-child-width-1-2@m" uk-grid>'
ANCHOR = "    keepalive_timeout 5;\n"


class IntegrationError(RuntimeError):
    pass


@dataclass
class Plan:
    files: dict[Path, FileState]
    socket_path: Path
    token: str


def strip_managed(text, area):
    if area == "card":
        start, end = "    <!-- BEGIN Utilization managed app card -->", "    <!-- END Utilization managed app card -->"
        prefix = "\n"
    else:
        start, end = "    # BEGIN Utilization managed web route", "    # END Utilization managed web route"
        prefix = ""
    if start not in text and end not in text:
        return text
    if text.count(start) != 1 or text.count(end) != 1:
        raise IntegrationError(f"Ambiguous Utilization {area} markers")
    pattern = re.escape(prefix + start) + r"\n.*?" + re.escape(end) + r"\n"
    result, count = re.subn(pattern, "", text, flags=re.S)
    if count != 1:
        raise IntegrationError(f"Malformed Utilization {area} block")
    return result


def validate_platform(package, nginx_text):
    auth = (package / "auth/auth.py").read_bytes()
    if hashlib.sha256(auth).hexdigest() != AUTH_SHA256:
        raise IntegrationError("Unsupported WebUI auth module; run pi_web_preflight.py for review")
    application = (package / "app.py").read_bytes()
    if hashlib.sha256(application).hexdigest() not in APPLICATION_SHA256:
        raise IntegrationError("Unsupported WebUI application revision; run pi_web_preflight.py for review")
    found = {}
    for node in ast.walk(ast.parse(application)):
        if isinstance(node, ast.FunctionDef) and node.name in AUTH_FUNCTIONS:
            if node.name in found:
                raise IntegrationError("Duplicate WebUI authentication hook")
            found[node.name] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
    if found != AUTH_FUNCTIONS:
        raise IntegrationError("Unsupported WebUI session freshness/login gates; run pi_web_preflight.py for review")
    if hashlib.sha256(nginx_text.encode()).hexdigest() != NGINX_SHA256:
        raise IntegrationError("Unsupported Nginx configuration; custom changes need integration review")


def proxy_headers(token):
    return ("# Utilization private authenticated proxy headers. Do not publish.\n"
            "proxy_set_header Host $http_host;\n"
            f'proxy_set_header X-Utilization-Proxy-Token "{token}";\n'
            'proxy_set_header Forwarded "";\n'
            'proxy_set_header X-Forwarded-For "";\n'
            'proxy_set_header X-Forwarded-Host "";\n'
            'proxy_set_header X-Forwarded-Proto "";\n'
            'proxy_set_header X-Forwarded-Port "";\n')


def prepare(package, nginx_config, config_dir, unit_path, *, remove=False):
    package = package.resolve(strict=True)
    if not (package / "app.py").is_file() and (package / "wlanpi_webui/app.py").is_file():
        package = package / "wlanpi_webui"
    nginx_config = nginx_config.resolve(strict=True)  # Preserve any symlink used by Nginx.
    card_path = (package / "templates/partials/apps_cards.html").resolve(strict=True)
    nginx_original, card_original = FileState.read(nginx_config), FileState.read(card_path)
    nginx_base = strip_managed(nginx_original.content.decode(), "route")
    card_base = strip_managed(card_original.content.decode(), "card")
    if unit_path.exists() and unit_path.read_bytes() != (ROOT / "integration/systemd" / WEB_SERVICE).read_bytes():
        raise IntegrationError("Web unit was customized; review before changing integration")
    if remove:
        if nginx_base == nginx_original.content.decode() or card_base == card_original.content.decode():
            raise IntegrationError("No complete managed Utilization installation found")
        return Plan({nginx_config: FileState(nginx_base.encode(), nginx_original.mode),
                     card_path: FileState(card_base.encode(), card_original.mode),
                     unit_path: FileState(None)}, Path("/run/wlanpi-utilization-web/web.sock"), "")
    validate_platform(package, nginx_base)
    if card_base.count(GRID) != 1 or 'href="/utilization/' in card_base:
        raise IntegrationError("Unsupported or already customized Apps-card layout")
    if nginx_base.count(ANCHOR) != 1 or re.search(r"location\s+[^\n{]*utilization", nginx_base):
        raise IntegrationError("Unsupported or conflicting Utilization Nginx location")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(config_dir)) or ".." in config_dir.parts:
        raise IntegrationError("Use an absolute config directory without spaces or parent traversal")
    if config_dir.exists():
        info = config_dir.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise IntegrationError("Proxy configuration directory must be owned by the installer user and not writable by others")
    token_path = config_dir / "web-proxy.token"
    if token_path.exists() or token_path.is_symlink():
        info = token_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise IntegrationError("Existing proxy token must be an owner-only regular file owned by the installer user")
        token = token_path.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise IntegrationError("Invalid existing proxy token")
    else:
        token = secrets.token_hex(32)
    fragment = (ROOT / "integration/webui/utilization-nginx.conf").read_text().replace(
        "/etc/wlanpi-beacon-live", str(config_dir))
    card = (ROOT / "integration/webui/utilization-card.html").read_text()
    headers = proxy_headers(token)
    return Plan({
        token_path: FileState((token + "\n").encode(), 0o600),
        config_dir / "web-proxy.headers": FileState(headers.encode(), 0o600),
        unit_path: FileState((ROOT / "integration/systemd" / WEB_SERVICE).read_bytes()),
        card_path: FileState(card_base.replace(GRID, GRID + card, 1).encode(), card_original.mode),
        nginx_config: FileState(nginx_base.replace(ANCHOR, fragment + ANCHOR, 1).encode(), nginx_original.mode),
    }, Path("/run/wlanpi-utilization-web/web.sock"), token)


def wait_ready(path, token):
    deadline = time.monotonic() + 10
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(.5)
                client.connect(str(path))
                client.sendall(("GET /utilization/ HTTP/1.0\r\nHost: localhost\r\n"
                                f"X-Utilization-Proxy-Token: {token}\r\n\r\n").encode())
                if re.match(rb"HTTP/1\.[01] 200 ", client.recv(128)):
                    return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise IntegrationError("Private web backend did not become ready; inspect its service journal")
        time.sleep(.2)


def apply(plan, *, webui_service, nginx="nginx", remove=False, runner=subprocess.run, ready=wait_ready):
    def command(*args, check=True):
        return runner(list(args), check=check, capture_output=True, text=True)

    # Check the existing platform before writes. No capture service is stopped
    # or restarted anywhere in this installer.
    command(nginx, "-t")
    configuration = command(nginx, "-T").stdout
    if not re.search(r"^\s*user\s+www-data(?:\s+www-data)?\s*;", configuration, re.M):
        raise IntegrationError("Protected web socket requires Nginx workers running as www-data; review this platform configuration")
    command("systemctl", "is-active", "--quiet", "nginx")
    command("systemctl", "is-active", "--quiet", webui_service)
    if command("systemctl", "show", webui_service, "-p", "CanReload", "--value").stdout.strip() != "yes":
        raise IntegrationError("The WebUI unit must support graceful reload; review --webui-service")
    enabled = command("systemctl", "is-enabled", "--quiet", WEB_SERVICE, check=False).returncode == 0
    active = command("systemctl", "is-active", "--quiet", WEB_SERVICE, check=False).returncode == 0
    originals = {path: FileState.read(path) for path in plan.files}
    owners = {path: (path.stat().st_uid, path.stat().st_gid) for path in plan.files if path.exists()}
    writes = dict(plan.files)
    for path, original in list(originals.items()):
        if original.content is not None and path.suffix in {".html", ".conf"}:
            backup = path.with_name(path.name + ".utilization-web.bak")
            if not backup.exists():
                originals[backup] = FileState(None)
                writes[backup] = original
    attempted, service_attempted = [], False

    def write(path, state):
        _atomic_write(path, state)
        if state.content is not None and path in owners:
            info = path.stat()
            if (info.st_uid, info.st_gid) != owners[path]:
                os.chown(path, *owners[path])

    try:
        if remove:
            service_attempted = True
            command("systemctl", "disable", "--now", WEB_SERVICE)
        for path, state in writes.items():
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            attempted.append(path)
            write(path, state)
        command(nginx, "-t")
        command("systemctl", "daemon-reload")
        service_attempted = True
        if not remove:
            command("systemctl", "enable", WEB_SERVICE)
            command("systemctl", "restart", WEB_SERVICE)
            command("systemctl", "is-active", "--quiet", WEB_SERVICE)
            ready(plan.socket_path, plan.token)
        command("systemctl", "reload", webui_service)
        command("systemctl", "reload", "nginx")
    except BaseException as error:
        failures = []
        # Undo first-time enablement while the new unit file still exists.
        # After restoring an absent unit, systemctl disable would fail even
        # though the desired pre-install state was simply "not installed".
        if service_attempted and not remove:
            early = []
            if not enabled:
                early.append(("systemctl", "disable", WEB_SERVICE))
            if not active:
                early.append(("systemctl", "stop", WEB_SERVICE))
            for args in early:
                try:
                    command(*args)
                except (OSError, subprocess.CalledProcessError) as failure:
                    failures.append(f"{' '.join(args)}: {failure}")
        for path in reversed(attempted):
            try:
                write(path, originals[path])
            except OSError as failure:
                failures.append(f"restore {path}: {failure}")
        if not failures:
            recovery = [("systemctl", "daemon-reload")]
            if service_attempted:
                if active or remove:
                    recovery += [("systemctl", "restart" if active else "stop", WEB_SERVICE)]
                if enabled or remove:
                    recovery += [("systemctl", "enable" if enabled else "disable", WEB_SERVICE)]
            recovery += [(nginx, "-t"), ("systemctl", "reload", webui_service), ("systemctl", "reload", "nginx")]
            for args in recovery:
                try:
                    command(*args)
                except (OSError, subprocess.CalledProcessError) as failure:
                    failures.append(f"{' '.join(args)}: {failure}")
        detail = "; recovery needs attention: " + "; ".join(failures) if failures else "; restored pre-install files and web service state"
        if isinstance(error, (KeyboardInterrupt, SystemExit)) and not failures:
            raise
        raise IntegrationError(str(error) + detail) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webui-root", type=Path, default=Path("/opt/wlanpi-webui/workingdirectory"))
    parser.add_argument("--nginx-config", type=Path, default=Path("/etc/wlanpi-webui/nginx/sites-enabled/wlanpi_webui.conf"))
    parser.add_argument("--webui-service", default="wlanpi-webui.service")
    parser.add_argument("--check", action="store_true", help="Validate proposed integration without writes or service operations")
    parser.add_argument("--remove", action="store_true", help="Remove managed route/card/unit; retain logs and private token for reinstall")
    args = parser.parse_args()
    if not args.check and os.geteuid() != 0:
        parser.error("Install/remove requires root; --check is read-only")
    try:
        plan = prepare(args.webui_root, args.nginx_config, Path("/etc/wlanpi-beacon-live"),
                       Path("/etc/systemd/system") / WEB_SERVICE, remove=args.remove)
        print("Validated protected Utilization integration. Files:")
        for path in plan.files:
            print(f"  {path}")
        if args.check:
            print("Read-only check complete; no files or services changed.")
            return 0
        if not args.remove:
            subprocess.run(["/opt/wlanpi-beacon-live/bin/python", "-I", "-c",
                "import flask, waitress; from beacon_live.web import create_app, read_proxy_token; "
                "create_app(proxy_token='0'*64)"], check=True, capture_output=True, text=True)
        apply(plan, webui_service=args.webui_service, remove=args.remove)
    except (IntegrationError, OSError, SyntaxError, UnicodeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Utilization web integration failed: {error}\n")
    print("Utilization web integration removed." if args.remove else "Utilization is available in WLAN Pi Apps and at https://<your-pi>/utilization/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
