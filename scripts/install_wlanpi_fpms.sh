#!/usr/bin/env bash
set -euo pipefail

FPMS_OPTIONS=()
CLIENT_OPTIONS=(--with-core)
FPMS_RUNTIME=""
WITH_WEB=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-web)
      WITH_WEB=true
      shift
      ;;
    --without-web)
      WITH_WEB=false
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
      echo "Usage: sudo $0 [--fpms-backend direct|core] [--fpms-runtime standalone|managed] [--without-web] [--wheel-dir DIRECTORY]"
      echo "Without --fpms-backend, the saved FPMS choice is preserved (initial default: direct)."
      echo "Without --fpms-runtime, the saved runtime is preserved (initial default: managed)."
      echo "Installs/updates CLI, FPMS, the managed service and protected web integration by default."
      echo "--without-web skips web dependencies and route installation; existing web integration is retained."
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

if [[ "${WITH_WEB}" == true ]]; then
  # Reject an unsupported platform before installing files or restarting capture.
  python3 "${SCRIPT_DIR}/install_web_integration.py" --check
  CLIENT_OPTIONS+=(--with-web)
fi

python3 -m venv "${APP_VENV}"
"${APP_VENV}/bin/python" "${SCRIPT_DIR}/install_app_venv.py" \
  --source-root "${REPO_ROOT}" "${CLIENT_OPTIONS[@]}"
"${APP_VENV}/bin/python" "${SCRIPT_DIR}/install_cli_launchers.py" \
  --app-bin "${APP_VENV}/bin" \
  --launcher-dir /usr/local/bin
mkdir -p /run/wlanpi-beacon-live /var/log/wlanpi-beacon-live
if [[ -z "${FPMS_RUNTIME}" ]]; then
  FPMS_RUNTIME="$(python3 - <<'PY'
import json
from pathlib import Path
path = Path('/etc/wlanpi-beacon-live/fpms.json')
try:
    settings = json.loads(path.read_text())
    mode = settings.get('runtime_mode', 'managed')
except FileNotFoundError:
    mode = 'managed'
if mode not in ('managed', 'standalone'):
    raise SystemExit('Invalid saved FPMS runtime; use --fpms-runtime managed|standalone')
print(mode)
PY
)"
fi
FPMS_OPTIONS+=(--runtime-mode "${FPMS_RUNTIME}")
if [[ "${FPMS_RUNTIME}" == "managed" || "${WITH_WEB}" == true ]]; then
  bash "${SCRIPT_DIR}/install_utilization_service.sh"
fi
"${APP_VENV}/bin/python" "${SCRIPT_DIR}/patch_wlanpi_fpms.py" \
  --adapter "${ADAPTER}" \
  --restart-service "${FPMS_OPTIONS[@]}"

if [[ "${WITH_WEB}" == true ]]; then
  "${APP_VENV}/bin/python" "${SCRIPT_DIR}/install_web_integration.py"
fi

echo "Utilization is installed under FPMS Apps."
echo "CLI commands installed: wlanpi-utilization and utilization (legacy aliases retained)."
echo "Standard CLI default: direct; select --capture-backend core explicitly."
echo "FPMS backend configuration: /etc/wlanpi-beacon-live/fpms.json (absent means direct)."
echo "FPMS runtime: ${FPMS_RUNTIME}. Managed mode: Left detaches; Stop Session ends capture."
echo "Select a band, channel, then Start Session or Start + Log; Connect opens an existing session."
if [[ "${WITH_WEB}" == true ]]; then
  echo "Utilization web integration installed: Apps -> Utilization or https://<your-pi>/utilization/"
fi
