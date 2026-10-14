#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to collect WLAN Pi diagnostics." >&2
  exit 1
fi
exec python3 "${SCRIPT_DIR}/collect_pi_debug.py" "$@"
