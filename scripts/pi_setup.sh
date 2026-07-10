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
  echo "Missing pip for ${PYTHON_BIN}."
  echo
  echo "On Raspberry Pi OS or Debian, install pip manually, for example:"
  echo "  sudo apt update"
  echo "  sudo apt install python3-pip python3-venv"
  exit 1
fi

"${PYTHON_BIN}" - <<'PY'
import sys

if sys.version_info < (3, 9):
    raise SystemExit(
        f"Python 3.9+ is required; found {sys.version.split()[0]}"
    )
PY

cd "${REPO_ROOT}"

echo "Installing local package in editable mode with dev dependencies..."
"${PYTHON_BIN}" -m pip install -e ".[dev]"

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
