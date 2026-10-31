"""The release baseline cannot pass by skipping or omitting required coverage."""
import xml.etree.ElementTree as ET

import pytest

from scripts import run_ci


def report(tmp_path, *, omit=None, outcome=None):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for module, count in run_ci.REQUIRED_MODULES.items():
        if module == omit:
            continue
        for index in range(count):
            case = ET.SubElement(suite, "testcase", classname=f"tests.{module}", name=f"case_{index}")
            if module == "test_core_transport_integration" and index == 0 and outcome:
                ET.SubElement(case, outcome)
    path = tmp_path / "pytest.xml"
    ET.ElementTree(root).write(path)
    return path


def test_complete_baseline_reports_passing_case_count(tmp_path):
    assert run_ci.check_junit(report(tmp_path)) == sum(run_ci.REQUIRED_MODULES.values())


@pytest.mark.parametrize("module", ["test_core_transport_integration", "test_web_nginx"])
def test_collection_time_import_skip_cannot_hide_missing_integration(tmp_path, module):
    with pytest.raises(ValueError, match="coverage missing"):
        run_ci.check_junit(report(tmp_path, omit=module))


@pytest.mark.parametrize("outcome", ["skipped", "failure", "error"])
def test_skipped_or_unsuccessful_cases_fail_baseline(tmp_path, outcome):
    with pytest.raises(ValueError, match="without skips"):
        run_ci.check_junit(report(tmp_path, outcome=outcome))


def test_partial_core_transport_matrix_cannot_pass(tmp_path):
    path = report(tmp_path)
    tree = ET.parse(path)
    suite = tree.getroot().find("testsuite")
    suite.remove(next(case for case in suite if case.get("classname").endswith("test_core_transport_integration")))
    tree.write(path)
    with pytest.raises(ValueError, match=r"test_core_transport_integration \(3/4\)"):
        run_ci.check_junit(path)


def test_empty_or_malformed_results_cannot_pass(tmp_path):
    path = tmp_path / "pytest.xml"
    path.write_text("<testsuites/>")
    with pytest.raises(ValueError, match="no test cases"):
        run_ci.check_junit(path)
    path.write_text("not XML")
    with pytest.raises(ET.ParseError):
        run_ci.check_junit(path)


def test_missing_tool_fails_before_pytest(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_ci.platform, "platform", lambda: "test platform")
    monkeypatch.setattr(run_ci.importlib.metadata, "version", lambda name: "15.0.1" if name == "websockets" else "test")
    monkeypatch.setattr(run_ci.shutil, "which", lambda name: None)
    monkeypatch.delenv("WLANPI_TEST_TSHARK", raising=False)
    monkeypatch.setattr(run_ci.subprocess, "run", lambda *a, **kw: pytest.fail("must fail before subprocesses"))
    assert run_ci.main(["--output", str(tmp_path)]) == 1
    assert "requires tshark" in capsys.readouterr().err
    assert not (tmp_path / "pytest.xml").exists()


@pytest.mark.parametrize("tool_output,message", [
    ({"tshark": "TShark (Wireshark) 4.2.0"}, "TShark 4.4"),
    ({"nginx": "nginx version: nginx/1.28.3 --with-http_ssl_module"}, "http_auth_request"),
])
def test_present_but_incompatible_tools_are_rejected(monkeypatch, tool_output, message):
    import subprocess
    monkeypatch.setattr(run_ci.platform, "platform", lambda: "test platform")
    monkeypatch.setattr(run_ci.importlib.metadata, "version", lambda name: "15.0.1" if name == "websockets" else "test")
    monkeypatch.setattr(run_ci.shutil, "which", lambda name: name)
    monkeypatch.delenv("WLANPI_TEST_TSHARK", raising=False)
    monkeypatch.delenv("UTILIZATION_TEST_NGINX", raising=False)
    outputs = {"tshark": "TShark (Wireshark) 4.4.19",
               "nginx": "nginx --with-http_ssl_module --with-http_auth_request_module"}
    outputs.update(tool_output)
    monkeypatch.setattr(run_ci.subprocess, "run", lambda command, **kw:
                        subprocess.CompletedProcess(command, 0, outputs[command[0]], ""))
    with pytest.raises(ValueError, match=message):
        run_ci.environment_report()
