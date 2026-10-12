#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  echo "Activate a Python 3.13+ virtual environment before running this development setup."
  echo "  python3 -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  ./scripts/pi_setup.sh"
  exit 1
fi

PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "The active virtual environment has no executable Python: ${PYTHON_BIN}"
  exit 1
fi

"${PYTHON_BIN}" - <<'PY'
import os
import sys
from pathlib import Path

if sys.prefix == sys.base_prefix:
    raise SystemExit("The selected Python is not in a venv; system Python will not be modified.")
if Path(sys.prefix).resolve() != Path(os.environ["VIRTUAL_ENV"]).resolve():
    raise SystemExit("VIRTUAL_ENV does not match the selected Python's environment.")
if sys.version_info < (3, 13):
    raise SystemExit(f"Python 3.13+ is required; found {sys.version.split()[0]}. Create a new venv with Python 3.13.")
try:
    config = (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
except OSError as error:
    raise SystemExit(f"Cannot verify the active virtual environment: {error}") from error
for line in config.splitlines():
    key, separator, value = line.partition("=")
    if separator and key.strip().lower() == "include-system-site-packages" and value.strip().lower() == "true":
        raise SystemExit("Use a venv without system site-packages.")
print(f"Using virtual environment: {sys.prefix} (Python {sys.version.split()[0]})")
PY

missing=()

check_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    missing+=("${command_name}")
  fi
}

check_command ip
check_command iw
check_command tshark
check_command dumpcap
check_command git

if ((${#missing[@]} > 0)); then
  echo "Missing required command(s): ${missing[*]}"
  echo
  echo "On Raspberry Pi OS or Debian, install missing system packages manually, for example:"
  echo "  sudo apt update"
  echo "  sudo apt install python3 python3-venv git iproute2 iw tshark wireshark-common"
  echo
  echo "This script does not run apt install automatically."
  exit 1
fi

if ! "${PYTHON_BIN}" -m pip --version >/dev/null 2>&1; then
  echo "pip is missing from the active virtual environment; trying ensurepip..."
  if ! "${PYTHON_BIN}" -m ensurepip --upgrade >/dev/null 2>&1; then
    echo "Missing pip for ${PYTHON_BIN}, and ensurepip could not repair it."
    echo "Install python3-venv, then create and activate a fresh Python 3.13 venv."
    exit 1
  fi
fi

cd "${REPO_ROOT}"

echo "Updating virtual environment packaging tools..."
if ! "${PYTHON_BIN}" -m pip install --upgrade "pip>=24.3" "setuptools>=75.3" wheel; then
  echo "Unable to update packaging tools in ${VIRTUAL_ENV}. Check connectivity and pip output."
  exit 1
fi

echo "Installing local package in editable mode with dev dependencies..."
"${PYTHON_BIN}" -m pip install -e ".[dev]"

echo "Checking CLI import..."
"${PYTHON_BIN}" -m beacon_live.cli --help >/dev/null
"${PYTHON_BIN}" -m beacon_live.device --help >/dev/null

echo "Checking hardware-free replay..."
"${PYTHON_BIN}" -m beacon_live.cli replay --input samples/tshark_qbss_sample.tsv >/dev/null

echo "Running pytest..."
"${PYTHON_BIN}" -m pytest

echo "Pi setup checks completed."
echo
echo "For replay without sudo, use:"
echo "  ${PYTHON_BIN} -m beacon_live.cli replay --input samples/tshark_qbss_sample.tsv"
echo
LIVE_LAUNCHER="${VIRTUAL_ENV}/bin/wlanpi-beacon-live"

echo "For the recommended on-device foreground launcher, use:"
echo "  sudo ${LIVE_LAUNCHER} --band 5 --channel 36"
echo "  sudo ${LIVE_LAUNCHER} --frequency-mhz 5975"
echo "  sudo ${LIVE_LAUNCHER} --band 6 --channel 5"
echo
echo "For optional local survey CU diagnostics, use:"
echo "  sudo ${LIVE_LAUNCHER} --band 5 --channel 36 --survey-debug"
echo
echo "For WLAN Pi R4 front-panel integration, run:"
echo "  sudo ./scripts/install_wlanpi_fpms.sh"
