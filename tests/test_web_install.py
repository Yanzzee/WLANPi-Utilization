"""Reversible integration changes, including failure after service operations."""
import ast
import hashlib
from pathlib import Path
import subprocess

import pytest

from scripts import install_web_integration as install


@pytest.fixture
def platform(tmp_path, monkeypatch):
    package = tmp_path / "webui"
    (package / "auth").mkdir(parents=True)
    (package / "templates/partials").mkdir(parents=True)
    auth = b'@bp.route("/auth/check")\ndef auth_check(): return "", 401\n'
    (package / "auth/auth.py").write_bytes(auth)
    application = '@app.before_request\ndef enforce_session_freshness():\n    pass\n@app.before_request\ndef require_login():\n    pass\n'
    (package / "app.py").write_text(application)
    monkeypatch.setattr(install, "APPLICATION_SHA256", {hashlib.sha256(application.encode()).hexdigest()})
    monkeypatch.setattr(install, "AUTH_SHA256", hashlib.sha256(auth).hexdigest())
    monkeypatch.setattr(install, "AUTH_FUNCTIONS", {node.name: hashlib.sha256(
        ast.dump(node, include_attributes=False).encode()).hexdigest()
        for node in ast.walk(ast.parse(application)) if isinstance(node, ast.FunctionDef)})
    cards = install.GRID + '\n    <div>Existing custom card</div>\n</div>\n'
    (package / "templates/partials/apps_cards.html").write_text(cards)
    nginx = tmp_path / "webui.conf"
    base = 'server {\n    location = /auth/check { internal; }\n' + install.ANCHOR + '}\n'
    nginx.write_text(base)
    monkeypatch.setattr(install, "NGINX_SHA256", hashlib.sha256(base.encode()).hexdigest())
    config = tmp_path / "config"
    unit = tmp_path / "systemd" / install.WEB_SERVICE
    return package, nginx, config, unit


class System:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.failed = False
        self.active = self.enabled = False

    def __call__(self, command, *, check, **options):
        self.calls.append(command)
        if self.fail and not self.failed and self.fail(command, self.calls):
            self.failed = True
            raise subprocess.CalledProcessError(1, command)
        if command == ["nginx", "-T"]:
            return subprocess.CompletedProcess(command, 0, "user www-data;\n", "")
        if command[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(command, 0, "yes\n", "")
        if command[:3] == ["systemctl", "is-enabled", "--quiet"]:
            return subprocess.CompletedProcess(command, 0 if self.enabled else 1, "", "")
        if command[:3] == ["systemctl", "is-active", "--quiet"] and command[-1] == install.WEB_SERVICE:
            return subprocess.CompletedProcess(command, 0 if self.active else 1, "", "")
        if command[:2] == ["systemctl", "enable"]:
            self.enabled = True
        if command[:2] == ["systemctl", "disable"]:
            self.enabled = False
            if "--now" in command:
                self.active = False
        if command[:2] == ["systemctl", "restart"]:
            self.active = True
        if command[:2] == ["systemctl", "stop"]:
            self.active = False
        return subprocess.CompletedProcess(command, 0, "", "")


def test_plan_is_read_only_repeat_install_is_idempotent_and_removal_preserves_custom_cards(platform):
    package, nginx, config, unit = platform
    original_nginx = nginx.read_bytes()
    card_path = package / "templates/partials/apps_cards.html"
    original_card = card_path.read_bytes()
    plan = install.prepare(*platform)
    assert not config.exists() and not unit.exists()
    assert nginx.read_bytes() == original_nginx and card_path.read_bytes() == original_card
    system = System()
    install.apply(plan, webui_service="wlanpi-webui.service", runner=system, ready=lambda *args: None)
    assert system.enabled and system.active
    assert nginx.read_text().count("auth_request /auth/check;") == 2
    assert plan.token not in nginx.read_text()
    assert (config / "web-proxy.token").stat().st_mode & 0o777 == 0o600
    assert (config / "web-proxy.headers").stat().st_mode & 0o777 == 0o600
    assert card_path.read_text().count('href="/utilization/"') == 1
    assert "Existing custom card" in card_path.read_text()
    again = install.prepare(*platform)
    assert again.token == plan.token
    assert all(state.content == path.read_bytes() for path, state in again.files.items())
    install.apply(again, webui_service="wlanpi-webui.service", runner=system, ready=lambda *args: None)
    remove = install.prepare(*platform, remove=True)
    install.apply(remove, webui_service="wlanpi-webui.service", remove=True, runner=system)
    assert not system.enabled and not system.active and not unit.exists()
    assert nginx.read_bytes() == original_nginx and card_path.read_bytes() == original_card
    assert (config / "web-proxy.token").exists()
    assert not any(call[:2] in (["systemctl", "restart"], ["systemctl", "stop"])
                   and call[-1] == "wlanpi-utilization.service" for call in system.calls)


@pytest.mark.parametrize("change", ["auth", "freshness", "nginx", "markers", "token", "unit"])
def test_unsupported_or_insecure_layout_fails_before_writes(platform, change):
    package, nginx, config, unit = platform
    if change == "auth":
        (package / "auth/auth.py").write_text("modified authentication\n")
    elif change == "freshness":
        p = package / "app.py"
        p.write_text(p.read_text().replace("    pass", "    return True", 1))
    elif change == "nginx":
        nginx.write_text(nginx.read_text() + "# custom TLS changes\n")
    elif change == "markers":
        nginx.write_text(nginx.read_text() + "    # BEGIN Utilization managed web route\n")
    elif change == "token":
        config.mkdir()
        (config / "web-proxy.token").write_text("a" * 64)
        (config / "web-proxy.token").chmod(0o644)
    else:
        unit.parent.mkdir()
        unit.write_text("custom service\n")
    before = nginx.read_bytes()
    with pytest.raises(install.IntegrationError):
        install.prepare(*platform)
    assert nginx.read_bytes() == before


def test_allowlisted_application_still_requires_reviewed_session_gates(platform, monkeypatch):
    package, _, _, _ = platform
    path = package / "app.py"
    changed = path.read_text().replace("    pass", "    return True", 1)
    path.write_text(changed)
    monkeypatch.setattr(install, "APPLICATION_SHA256", {hashlib.sha256(changed.encode()).hexdigest()})
    with pytest.raises(install.IntegrationError, match="session freshness/login gates"):
        install.prepare(*platform)


@pytest.mark.parametrize("phase", ["syntax", "restart", "readiness", "webui_reload", "nginx_reload"])
def test_failed_install_restores_files_token_and_original_service_state(platform, phase):
    package, nginx, config, unit = platform
    cards = package / "templates/partials/apps_cards.html"
    before = {nginx: nginx.read_bytes(), cards: cards.read_bytes()}
    plan = install.prepare(*platform)
    def fail(command, calls):
        return ((phase == "syntax" and command == ["nginx", "-t"] and calls.count(command) == 2)
            or (phase == "restart" and command == ["systemctl", "restart", install.WEB_SERVICE])
            or (phase == "webui_reload" and command == ["systemctl", "reload", "wlanpi-webui.service"])
            or (phase == "nginx_reload" and command == ["systemctl", "reload", "nginx"]))
    system = System(fail)
    def ready(*args):
        if phase == "readiness":
            raise install.IntegrationError("fixture backend did not start")
    with pytest.raises(install.IntegrationError, match="restored pre-install"):
        install.apply(plan, webui_service="wlanpi-webui.service", runner=system, ready=ready)
    assert all(path.read_bytes() == data for path, data in before.items())
    assert not unit.exists() and not (config / "web-proxy.token").exists()
    assert not list(nginx.parent.glob("*.utilization-web.bak"))
    assert not system.active and not system.enabled


def test_reinstall_failure_retains_existing_token_and_running_web_service(platform):
    plan = install.prepare(*platform)
    system = System()
    install.apply(plan, webui_service="wlanpi-webui.service", runner=system, ready=lambda *args: None)
    old = {path: path.read_bytes() for path in plan.files}
    system.fail = lambda command, calls: command == ["systemctl", "reload", "nginx"]
    with pytest.raises(install.IntegrationError, match="restored pre-install"):
        install.apply(install.prepare(*platform), webui_service="wlanpi-webui.service", runner=system,
                      ready=lambda *args: None)
    assert all(path.read_bytes() == data for path, data in old.items())
    assert system.enabled and system.active


@pytest.mark.parametrize("check", ["worker_group", "graceful_reload"])
def test_platform_service_requirements_fail_before_file_changes(platform, check):
    plan = install.prepare(*platform)
    system = System()
    before = {path: install.FileState.read(path) for path in plan.files}

    def runner(command, **options):
        if command == ["nginx", "-T"] and check == "worker_group":
            return subprocess.CompletedProcess(command, 0, "user nginx;\n", "")
        if command[:2] == ["systemctl", "show"] and check == "graceful_reload":
            return subprocess.CompletedProcess(command, 0, "no\n", "")
        return system(command, **options)

    with pytest.raises(install.IntegrationError):
        install.apply(plan, webui_service="wlanpi-webui.service", runner=runner)
    assert all(install.FileState.read(path) == state for path, state in before.items())
    assert not any(call[:2] in (["systemctl", "restart"], ["systemctl", "reload"])
                   for call in system.calls)


def test_failed_removal_restores_installed_route_and_running_service(platform):
    system = System()
    plan = install.prepare(*platform)
    install.apply(plan, webui_service="wlanpi-webui.service", runner=system, ready=lambda *args: None)
    old = {path: path.read_bytes() for path in plan.files}
    system.fail = lambda command, calls: command == ["systemctl", "reload", "nginx"]
    with pytest.raises(install.IntegrationError, match="restored pre-install"):
        install.apply(install.prepare(*platform, remove=True), webui_service="wlanpi-webui.service",
                      remove=True, runner=system)
    assert all(path.read_bytes() == data for path, data in old.items())
    assert system.enabled and system.active
