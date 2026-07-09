#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEBUG_ROOT="${REPO_ROOT}/debug"
TIMESTAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
DEBUG_DIR="${DEBUG_ROOT}/pi_debug_${TIMESTAMP}"
ARCHIVE_PATH="${DEBUG_ROOT}/pi_debug_${TIMESTAMP}.tar.gz"

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

capture_command() {
  local output_file="$1"
  shift

  {
    echo "$ $*"
    "$@"
  } >"${output_file}" 2>&1 || true
}

capture_missing() {
  local output_file="$1"
  local command_name="$2"

  echo "${command_name} not found" >"${output_file}"
}

mkdir -p "${DEBUG_DIR}"

capture_command "${DEBUG_DIR}/uname.txt" uname -a

if command -v python3 >/dev/null 2>&1; then
  capture_command "${DEBUG_DIR}/python_version.txt" python3 --version
else
  capture_missing "${DEBUG_DIR}/python_version.txt" python3
fi

if command -v iw >/dev/null 2>&1; then
  capture_command "${DEBUG_DIR}/iw_dev.txt" iw dev
  capture_command "${DEBUG_DIR}/iw_phy.txt" iw phy
else
  capture_missing "${DEBUG_DIR}/iw_dev.txt" iw
  capture_missing "${DEBUG_DIR}/iw_phy.txt" iw
fi

if command -v tshark >/dev/null 2>&1; then
  capture_command "${DEBUG_DIR}/tshark_version.txt" tshark --version
else
  capture_missing "${DEBUG_DIR}/tshark_version.txt" tshark
fi

if command -v ip >/dev/null 2>&1; then
  capture_command "${DEBUG_DIR}/ip_addr.txt" ip addr
else
  capture_missing "${DEBUG_DIR}/ip_addr.txt" ip
fi

if command -v rfkill >/dev/null 2>&1; then
  capture_command "${DEBUG_DIR}/rfkill.txt" rfkill list
else
  capture_missing "${DEBUG_DIR}/rfkill.txt" rfkill
fi

if command -v dmesg >/dev/null 2>&1; then
  {
    echo "$ dmesg | tail -n 200"
    run_root dmesg | tail -n 200
  } >"${DEBUG_DIR}/dmesg_tail.txt" 2>&1 || true
else
  capture_missing "${DEBUG_DIR}/dmesg_tail.txt" dmesg
fi

tar -czf "${ARCHIVE_PATH}" -C "${DEBUG_ROOT}" "$(basename "${DEBUG_DIR}")"

echo "Debug archive written to:"
echo "${ARCHIVE_PATH}"
