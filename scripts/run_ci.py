#!/usr/bin/env python3
"""Run the complete hardware-free baseline; missing tools/skipped tests fail CI."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
# These integrations must be collected as well as pass. An importorskip during
# collection otherwise looks like success to pytest when other modules pass.
REQUIRED_MODULES = {
    "test_parser": 1, "test_analyzer": 1, "test_service": 1,
    "test_web": 1, "test_core_capture": 1, "test_core_live": 1,
    "test_core_stream": 5, "test_core_transport_integration": 4,
    "test_web_nginx": 1,
}


def check_junit(path: Path) -> int:
    """Require actual passing cases, including the real decoder/socket tests."""
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("pytest reported no test cases")
    counts = dict.fromkeys(REQUIRED_MODULES, 0)
    for case in cases:
        if any(case.find(kind) is not None for kind in ("skipped", "failure", "error")):
            raise ValueError(f"CI requires passing tests without skips: {case.get('classname')}.{case.get('name')}")
        module = case.get("classname", "").removeprefix("tests.")
        if module in counts:
            counts[module] += 1
    missing = [f"{module} ({counts[module]}/{minimum})"
               for module, minimum in REQUIRED_MODULES.items() if counts[module] < minimum]
    if missing:
        raise ValueError("Required integration coverage missing: " + ", ".join(missing))
    return len(cases)


def environment_report() -> dict:
    if sys.version_info < (3, 13):
        raise ValueError("CI requires Python 3.13 or newer")
    report = {"python": sys.version, "platform": platform.platform(),
              "packages": {}, "tools": {}}
    for package in ("pytest", "Flask", "waitress", "websockets"):
        report["packages"][package] = importlib.metadata.version(package)
    if report["packages"]["websockets"] != "15.0.1":
        raise ValueError("CI requires the project's pinned websockets==15.0.1")
    for name, override, args in (
        ("tshark", "WLANPI_TEST_TSHARK", ["--version"]),
        ("nginx", "UTILIZATION_TEST_NGINX", ["-V"]),
        ("openssl", "", ["version"]), ("node", "", ["--version"]),
    ):
        executable = os.environ.get(override) if override else None
        executable = executable or shutil.which(name)
        if not executable:
            raise ValueError(f"CI requires {name}; install the tools listed in docs/development.md")
        result = subprocess.run([executable, *args], capture_output=True, text=True,
                                check=True, timeout=20)
        output = (result.stdout + result.stderr).strip()
        report["tools"][name] = {"executable": executable, "version": output}
        if name == "tshark":
            version = re.search(r"Wireshark\)\s+(\d+)\.(\d+)", output)
            if not version or tuple(map(int, version.groups())) < (4, 4):
                raise ValueError("CI requires TShark 4.4 or newer, including HE/EHT fixture support")
        if name == "nginx" and not all(flag in output for flag in (
                "--with-http_auth_request_module", "--with-http_ssl_module")):
            raise ValueError("CI Nginx requires http_auth_request and TLS support")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/ci")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        report = environment_report()
        (output / "environment.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
        tshark = report["tools"]["tshark"]["executable"]
        subprocess.run([sys.executable, "scripts/check_tshark.py", "--tshark", tshark],
                       cwd=ROOT, check=True, timeout=120)
        junit = output / "pytest.xml"
        # Remove an earlier report so failed collection cannot reuse old success.
        junit.unlink(missing_ok=True)
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-rs", "--tb=short",
                        f"--junitxml={junit}"], cwd=ROOT, check=True, timeout=600)
        passed = check_junit(junit)
        subprocess.run([sys.executable, "tests/fixtures/web_preview.py", "--export",
                        str(output / "web")], cwd=ROOT, check=True, timeout=30)
        subprocess.run([report["tools"]["node"]["executable"], "tests/web_ui_check.mjs",
                        str(output / "web")], cwd=ROOT, check=True, timeout=60)
        print(f"CI baseline passed: {passed} pytest cases, no skips, and frontend logic checks.")
    except (ValueError, OSError, ET.ParseError, importlib.metadata.PackageNotFoundError,
            subprocess.SubprocessError) as exc:
        print(f"CI baseline failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
