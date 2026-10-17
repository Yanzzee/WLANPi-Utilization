from scripts.pi_web_preflight import inspect


def test_missing_platform_is_reported_without_importing_or_mutating_it(tmp_path):
    report = inspect(tmp_path / "missing", tmp_path / "nginx.conf")
    assert report["read_only"] and not report["integration_candidate"]
    assert not any(report["candidate_hooks"].values())
    assert list(tmp_path.iterdir()) == []


def test_platform_candidate_records_hashes_and_not_configuration_contents(tmp_path):
    package = tmp_path / "wlanpi_webui"
    (package / "auth").mkdir(parents=True)
    (package / "templates/partials").mkdir(parents=True)
    (package / "auth/auth.py").write_text('@bp.route("/auth/check")\ndef auth_check(): pass\n')
    (package / "app.py").write_text('raise RuntimeError("never import platform code")\n'
        'session_is_valid(); session.get("last_seen"); app.config["IDLE_TIMEOUT"]; end_session()')
    (package / "templates/partials/apps_cards.html").write_text('<div class="app-card"></div>')
    nginx = tmp_path / "nginx.conf"
    nginx.write_text('location = /auth/check { internal; }\nlocation / { auth_request /auth/check; }')
    before = nginx.read_bytes()
    report = inspect(tmp_path, nginx)
    assert report["integration_candidate"]
    assert all(len(item["sha256"]) == 64 for item in report["files"].values())
    assert "never import" not in str(report)
    assert nginx.read_bytes() == before
