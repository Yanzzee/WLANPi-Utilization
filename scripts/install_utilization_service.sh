#!/usr/bin/env bash
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./scripts/install_utilization_service.sh" >&2
  exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ ! -x /opt/wlanpi-beacon-live/bin/wlanpi-utilization ]]; then
  echo "Install Utilization with install_wlanpi_fpms.sh first." >&2
  exit 1
fi
install -m 0644 "${REPO_ROOT}/integration/systemd/wlanpi-utilization.service" \
  /etc/systemd/system/wlanpi-utilization.service
systemctl daemon-reload
systemctl enable wlanpi-utilization.service
systemctl restart wlanpi-utilization.service
systemctl is-active --quiet wlanpi-utilization.service
for attempt in {1..30}; do
  if /opt/wlanpi-beacon-live/bin/wlanpi-utilization session status --summary >/dev/null 2>&1; then
    echo "Utilization service is ready; capture starts only on an explicit managed request."
    exit 0
  fi
  sleep 0.1
done
echo "Utilization service did not become ready. Check journalctl -u wlanpi-utilization." >&2
exit 1
