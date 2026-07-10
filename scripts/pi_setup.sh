#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

missing=()

check_command() {
  local command_name="$1"

  if ! command -v "${command_name}" >/dev/null 2>&1; then
    missing+=("${command_name}")
  fi
}

check_command python3
check_command iw
check_command tshark
check_command dumpcap
check_command git

if ((${#missing[@]} > 0)); then
  echo "Missing required command(s): ${missing[*]}"
  echo
  echo "On Raspberry Pi OS or Debian, install missing system packages manually, for example:"
  echo "  sudo apt update"
  echo "  sudo apt install python3 python3-pip python3-venv git iw tshark wireshark-common"
  echo
  echo "This script does not run apt install automatically."
  exit 1
fi

if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
  echo "Using active virtual environment: ${VIRTUAL_ENV}"
else
  PYTHON_BIN="python3"
  echo "No active virtual environment detected; using python3."
fi

if ! "${PYTHON_BIN}" -m pip --version >/dev/null 2>&1; then
  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    echo "pip is missing from the active virtual environment; trying ensurepip..."
    if ! "${PYTHON_BIN}" -m ensurepip --upgrade >/dev/null 2>&1; then
      echo "Missing pip for ${PYTHON_BIN}, and ensurepip could not repair it."
      echo
      echo "On Raspberry Pi OS or Debian, install venv/pip support manually, for example:"
      echo "  sudo apt update"
      echo "  sudo apt install python3-pip python3-venv"
      echo
      echo "Then recreate the virtual environment:"
      echo "  rm -rf .venv"
      echo "  python3 -m venv .venv"
      echo "  source .venv/bin/activate"
      exit 1
    fi
  else
    echo "Missing pip for ${PYTHON_BIN}."
    echo
    echo "On Raspberry Pi OS or Debian, install pip manually, for example:"
    echo "  sudo apt update"
    echo "  sudo apt install python3-pip python3-venv"
    exit 1
  fi
fi

"${PYTHON_BIN}" - <<'PY'
import sys

if sys.version_info < (3, 9):
    raise SystemExit(
        f"Python 3.9+ is required; found {sys.version.split()[0]}"
    )
PY

cd "${REPO_ROOT}"

if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  echo "Updating virtual environment packaging tools..."
  echo "Current pip:"
  "${PYTHON_BIN}" -m pip --version
  if ! "${PYTHON_BIN}" -m pip install --upgrade "pip>=23.1" "setuptools>=64" wheel; then
    echo "Unable to update packaging tools in the active virtual environment."
    echo
    echo "Try running this manually after activating the venv:"
    echo "  ${PYTHON_BIN} -m pip install --upgrade 'pip>=23.1' 'setuptools>=64' wheel"
    exit 1
  fi
  echo "Updated pip:"
  "${PYTHON_BIN}" -m pip --version
else
  echo "No active virtual environment; leaving system packaging tools unchanged."
fi

echo "Installing local package in editable mode with dev dependencies..."
if ! "${PYTHON_BIN}" -m pip install -e ".[dev]"; then
  echo
  echo "Editable install failed."
  echo "This usually means the Pi is using an older pip/setuptools editable-install path."
  echo "Falling back to a standard local install so CLI and tests can still run."
  echo "Code changes on the Pi will require rerunning this setup script after fallback."
  "${PYTHON_BIN}" -m pip install ".[dev]"
fi

echo "Checking CLI import..."
"${PYTHON_BIN}" -m beacon_live.cli --help >/dev/null

echo "Running pytest..."
"${PYTHON_BIN}" -m pytest

echo "Pi setup checks completed."
echo
echo "For replay without sudo, use:"
echo "  ${PYTHON_BIN} -m beacon_live.cli replay --input samples/tshark_qbss_sample.tsv"
echo
echo "For live mode with sudo, use:"
echo "  sudo ${PYTHON_BIN} -m beacon_live.cli live --iface wlan0 --channel 36"
echo "  sudo ${PYTHON_BIN} -m beacon_live.cli live --iface wlan0 --frequency-mhz 5975"
echo "  sudo ${PYTHON_BIN} -m beacon_live.cli live --iface wlan0 --band 6 --channel 5"
echo
echo "For optional local survey CU diagnostics, use:"
echo "  sudo ${PYTHON_BIN} -m beacon_live.cli live --iface wlan0 --channel 36 --local-cu --survey-debug"
