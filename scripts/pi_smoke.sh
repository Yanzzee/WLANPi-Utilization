#!/usr/bin/env bash
set -euo pipefail

IFACE="${1:-wlan1}"
CHANNEL="${2:-36}"
CAPTURE_SECONDS=15

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SAMPLES_DIR="${REPO_ROOT}/samples"

PHY_OUT="${SAMPLES_DIR}/pi_iw_phy.txt"
QBSS_OUT="${SAMPLES_DIR}/pi_tshark_qbss_sample.tsv"
MAC_OUT="${SAMPLES_DIR}/pi_tshark_mac_sample.tsv"
SURVEY_BEFORE_OUT="${SAMPLES_DIR}/pi_survey_before.txt"
SURVEY_AFTER_OUT="${SAMPLES_DIR}/pi_survey_after.txt"
TMP_PCAP=""
ORIGINAL_TYPE=""

die() {
  echo "error: $*" >&2
  exit 1
}

require_command() {
  local command_name="$1"

  command -v "${command_name}" >/dev/null 2>&1 || die "missing command: ${command_name}"
}

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

cleanup() {
  if [[ -n "${ORIGINAL_TYPE}" && "${ORIGINAL_TYPE}" != "monitor" ]]; then
    echo "Restoring ${IFACE} to ${ORIGINAL_TYPE} mode..."
    run_root ip link set "${IFACE}" down >/dev/null 2>&1 || true
    run_root iw dev "${IFACE}" set type "${ORIGINAL_TYPE}" >/dev/null 2>&1 || true
    run_root ip link set "${IFACE}" up >/dev/null 2>&1 || true
  fi

  if [[ -n "${TMP_PCAP}" && -f "${TMP_PCAP}" ]]; then
    rm -f "${TMP_PCAP}"
  fi
}

trap cleanup EXIT

require_command uname
require_command python3
require_command iw
require_command tshark
require_command dumpcap
require_command ip
require_command awk
require_command sed
require_command wc
require_command mktemp

if [[ "${EUID}" -ne 0 ]]; then
  require_command sudo
fi

mkdir -p "${SAMPLES_DIR}"

echo "System info"
echo "==========="
uname -a
python3 --version
iw --version
tshark --version | sed -n '1,2p'
echo

echo "Wi-Fi interfaces"
echo "==============="
iw dev
echo

if ! iw dev "${IFACE}" info >/dev/null 2>&1; then
  die "interface ${IFACE} was not found by iw dev"
fi

echo "Saving adapter capabilities to ${PHY_OUT}"
if ! iw phy >"${PHY_OUT}" 2>&1; then
  echo "warning: iw phy failed; see ${PHY_OUT}" >&2
fi

ORIGINAL_TYPE="$(iw dev "${IFACE}" info | awk '/type/ {print $2; exit}')"

echo "Configuring ${IFACE} for monitor capture on channel ${CHANNEL} HT20..."
run_root ip link set "${IFACE}" down
run_root iw dev "${IFACE}" set type monitor
run_root ip link set "${IFACE}" up
run_root iw dev "${IFACE}" set channel "${CHANNEL}" HT20

echo "Saving survey snapshot before capture to ${SURVEY_BEFORE_OUT}"
if ! run_root iw dev "${IFACE}" survey dump >"${SURVEY_BEFORE_OUT}" 2>&1; then
  echo "warning: survey dump before capture failed; see ${SURVEY_BEFORE_OUT}" >&2
fi

TMP_PCAP="$(mktemp "${TMPDIR:-/tmp}/beacon-live-smoke.XXXXXX.pcapng")"
rm -f "${TMP_PCAP}"

echo "Capturing ${CAPTURE_SECONDS} seconds of 802.11 traffic to a temporary pcap..."
run_root dumpcap -i "${IFACE}" -a "duration:${CAPTURE_SECONDS}" -w "${TMP_PCAP}"

echo "Extracting beacon QBSS TSV to ${QBSS_OUT}"
run_root tshark -r "${TMP_PCAP}" \
  -Y "wlan.fc.type_subtype == 8" \
  -T fields \
  -E separator=$'\t' \
  -E occurrence=f \
  -e frame.time_epoch \
  -e wlan.ssid \
  -e wlan.bssid \
  -e wlan.qbss.cu \
  -e wlan.qbss.scount \
  -e wlan.qbss.adc \
  >"${QBSS_OUT}"

echo "Extracting general 802.11 MAC TSV to ${MAC_OUT}"
run_root tshark -r "${TMP_PCAP}" \
  -Y "wlan" \
  -T fields \
  -E separator=$'\t' \
  -E occurrence=f \
  -e frame.time_epoch \
  -e wlan.fc.type \
  -e wlan.fc.subtype \
  -e wlan.fc.type_subtype \
  -e wlan.sa \
  -e wlan.da \
  -e wlan.ta \
  -e wlan.ra \
  -e wlan.bssid \
  -e wlan.ssid \
  >"${MAC_OUT}"

echo "Saving survey snapshot after capture to ${SURVEY_AFTER_OUT}"
if ! run_root iw dev "${IFACE}" survey dump >"${SURVEY_AFTER_OUT}" 2>&1; then
  echo "warning: survey dump after capture failed; see ${SURVEY_AFTER_OUT}" >&2
fi

line_count() {
  local path="$1"

  wc -l <"${path}"
}

echo
echo "Smoke capture summary"
echo "====================="
for path in \
  "${PHY_OUT}" \
  "${QBSS_OUT}" \
  "${MAC_OUT}" \
  "${SURVEY_BEFORE_OUT}" \
  "${SURVEY_AFTER_OUT}"; do
  echo "$(line_count "${path}") lines  ${path}"
done

if [[ ! -s "${QBSS_OUT}" ]]; then
  echo
  echo "No beacon rows were written to ${QBSS_OUT}."
  echo "That can happen if no beacons were heard during the capture window."
elif ! awk -F '\t' 'NF >= 4 && $4 != "" { found = 1 } END { exit found ? 0 : 1 }' "${QBSS_OUT}"; then
  echo
  echo "Beacon rows were captured, but no QBSS channel-utilization fields were present."
  echo "The output file is still valid for parser replay and fixture review."
fi
