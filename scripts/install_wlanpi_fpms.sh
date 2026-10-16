#!/usr/bin/env bash
set -euo pipefail

FPMS_OPTIONS=()
CLIENT_OPTIONS=(--with-core)
FPMS_RUNTIME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-web)
      CLIENT_OPTIONS+=(--with-web)
      shift
      ;;
    --fpms-runtime)
      if [[ $# -lt 2 || ( "$2" != "standalone" && "$2" != "managed" ) ]]; then
        echo "--fpms-runtime requires standalone or managed" >&2
        exit 2
      fi
      FPMS_RUNTIME="$2"
      shift 2
      ;;
    --fpms-backend)
      if [[ $# -lt 2 || ( "$2" != "direct" && "$2" != "core" ) ]]; then
        echo "--fpms-backend requires direct or core" >&2
        exit 2
      fi
      FPMS_OPTIONS+=(--capture-backend "$2")
      shift 2
      ;;
    --wheel-dir)
      if [[ $# -lt 2 ]]; then
        echo "--wheel-dir requires a directory" >&2
        exit 2
      fi
      CLIENT_OPTIONS+=(--wheel-dir "$2")
      shift 2
      ;;
    --help|-h)
      echo "Usage: sudo $0 [--fpms-backend direct|core] [--fpms-runtime standalone|managed] [--with-web] [--wheel-dir DIRECTORY]"
      echo "Without --fpms-backend, the saved FPMS choice is preserved (initial default: direct)."
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root: sudo ./scripts/install_wlanpi_fpms.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_VENV="/opt/wlanpi-beacon-live"
ADAPTER="${REPO_ROOT}/integration/wlanpi_fpms/channel_utilization.py"

if [[ ! -x /usr/sbin/fpms ]]; then
  echo "WLAN Pi FPMS was not found at /usr/sbin/fpms."
  exit 1
fi

python3 - <<'PY'
import sys

if sys.version_info < (3, 13):
    raise SystemExit(f"Python 3.13+ is required for Trixie; found {sys.version.split()[0]}")
PY

python3 -m venv "${APP_VENV}"
"${APP_VENV}/bin/python" "${SCRIPT_DIR}/install_app_venv.py" \
  --source-root "${REPO_ROOT}" "${CLIENT_OPTIONS[@]}"
"${APP_VENV}/bin/python" "${SCRIPT_DIR}/install_cli_launchers.py" \
  --app-bin "${APP_VENV}/bin" \
  --launcher-dir /usr/local/bin
mkdir -p /run/wlanpi-beacon-live /var/log/wlanpi-beacon-live
if [[ -n "${FPMS_RUNTIME}" ]]; then
  FPMS_OPTIONS+=(--runtime-mode "${FPMS_RUNTIME}")
fi
if [[ "${FPMS_RUNTIME}" == "managed" ]]; then
  bash "${SCRIPT_DIR}/install_utilization_service.sh"
fi
"${APP_VENV}/bin/python" "${SCRIPT_DIR}/patch_wlanpi_fpms.py" \
  --adapter "${ADAPTER}" \
  --restart-service "${FPMS_OPTIONS[@]}"

echo "Utilization is installed under FPMS Apps."
echo "CLI commands installed: wlanpi-utilization and utilization (legacy aliases retained)."
echo "Standard CLI default: direct; select --capture-backend core explicitly."
echo "FPMS backend configuration: /etc/wlanpi-beacon-live/fpms.json (absent means direct)."
echo "Select a band, channel, then Display or Display + Log."
