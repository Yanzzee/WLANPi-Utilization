#!/usr/bin/env python3
"""Read-only evidence for the protected Utilization launcher adapter.

Reads source/configuration as text; never imports the installed WebUI, reads
session stores/keys, changes services, or enables a listener. Hook detection
identifies an integration candidate, not an authentication security audit.
"""
import argparse
import hashlib
import json
from pathlib import Path


def inspect(webui_root: Path, nginx_config: Path):
    if not (webui_root / "app.py").exists() and (webui_root / "wlanpi_webui/app.py").exists():
        webui_root = webui_root / "wlanpi_webui"
    paths = {"auth": webui_root / "auth/auth.py", "application": webui_root / "app.py",
             "apps_cards": webui_root / "templates/partials/apps_cards.html", "nginx": nginx_config}
    files, texts = {}, {}
    for name, path in paths.items():
        try:
            content = path.read_bytes()
            texts[name] = content.decode("utf-8")
            files[name] = {"path": str(path), "sha256": hashlib.sha256(content).hexdigest()}
        except (OSError, UnicodeError) as error:
            files[name] = {"path": str(path), "error": str(error)}
            texts[name] = ""
    checks = {
        "session_check_route": '@bp.route("/auth/check")' in texts["auth"] and 'def auth_check(' in texts["auth"],
        "session_revocation_and_expiry": all(token in texts["application"] for token in (
            "session_is_valid()", 'session.get("last_seen")', 'app.config["IDLE_TIMEOUT"]', 'end_session()')),
        "internal_nginx_auth_check": 'location = /auth/check' in texts["nginx"]
            and 'internal;' in texts["nginx"] and 'auth_request /auth/check;' in texts["nginx"],
        "apps_card_template": 'app-card' in texts["apps_cards"],
    }
    return {"read_only": True, "files": files, "candidate_hooks": checks,
            "integration_candidate": all(checks.values()),
            "next": "Verify installed nginx routing, login/logout/expiry and proxy boundary before enabling /utilization/."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webui-root", type=Path,
                        default=Path("/opt/wlanpi-webui/workingdirectory"))
    parser.add_argument("--nginx-config", type=Path,
                        default=Path("/etc/wlanpi-webui/nginx/sites-enabled/wlanpi_webui.conf"))
    args = parser.parse_args()
    print(json.dumps(inspect(args.webui_root, args.nginx_config), indent=2))


if __name__ == "__main__":
    main()
