#!/usr/bin/env bash
set -euo pipefail

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

python3 -m venv "${APP_VENV}"
"${APP_VENV}/bin/python" "${SCRIPT_DIR}/install_app_venv.py" \
  --source-root "${REPO_ROOT}"
"${APP_VENV}/bin/python" "${SCRIPT_DIR}/install_cli_launchers.py" \
  --app-bin "${APP_VENV}/bin" \
  --launcher-dir /usr/local/bin
"${APP_VENV}/bin/python" "${SCRIPT_DIR}/patch_wlanpi_fpms.py" \
  --adapter "${ADAPTER}"

mkdir -p /run/wlanpi-beacon-live /var/log/wlanpi-beacon-live
systemctl restart wlanpi-fpms

echo "Utilization is installed under FPMS Apps."
echo "CLI commands installed: wlanpi-beacon-live and beacon-live"
echo "Select a band, channel, then Display or Display + Log."
