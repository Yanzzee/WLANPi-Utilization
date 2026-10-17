# Busy-channel identity and missing-data validation

On October 16, the user reported that the web application works on Raspberry
Pi 5 with **direct capture**, but download speedtests sometimes raise observed
client MACs above 1,000 or produce hundreds of unexpected/hidden BSSIDs and
missing metrics. This was not consistently reproducible, and was not observed
on the user's Pi 4. Adapter/driver, raw packets, and comparable load evidence
were not available for this review. The infrastructure is not established as
the cause.

## Confirmed software paths and fixes

Live capture did not request FCS quality fields in its normal configuration.
The full parser validated the radiotap bad-FCS boolean syntax but discarded its
value; the fast data parser ignored it entirely. Consequently, damaged data
headers could create observed clients, and damaged beacon headers could create
BSSIDs or replace authoritative beacon contents. The existing client definition
already uses TA/RA endpoints, so remote internet SA/DA hosts are not the cause
of that counter's growth.

Both live decoders now negotiate `radiotap.flags.badfcs` and `wlan.fcs.status`,
and request TShark checksum validation when captured FCS bytes are available.
Frames explicitly marked bad by radiotap or TShark remain decoded diagnostic
records and advance the ordered capture watermark, but do not supply beacon
identities, QBSS values, client observations, or retry numerator/denominator.
Missing, unverified, or absent FCS is accepted. No station-count cap, MAC
deduplication interval, association assumption, or valid-frame metric formula
was changed. Exported diagnostic packet bytes are unchanged.

The multi-occurrence parser also incorrectly used repeated address-field
counts as evidence of extra WLAN frames and copied singleton address/Retry
fields into those frames. It now uses actual frame-type occurrences. For
multiple headers, ambiguous missing address/Retry occurrences stay unavailable.
A repeated information/address field cannot create extra beacon observations.
Genuinely aligned multiple WLAN headers retain every decoded frame/Retry bit.
Nonfinite timestamps are rejected before they can poison the capture watermark.

`tests/test_capture_quality.py` builds a real radiotap PCAP without hardware.
With one valid AP/client pair, 1,100 damaged download headers, and 200 damaged
beacons, TShark 4.6.0 and the previous parser produced **1,101 clients and 201
BSSIDs**. The corrected pipeline produces **one client and one BSSID**, retains
the valid QBSS values, and excludes damaged retry inputs. The tests cover both
driver-reported bad FCS and incorrect actual CRC bytes with no driver bad flag.
Streaming and replay aggregation are checked. This proves a relevant failure
path; it does not prove the user's particular Pi 5 packets had bad FCS.

## New diagnostic evidence

The managed owner publishes cumulative capture-record, decoded-frame,
rejected-row and bad-FCS counts, latest decoded capture timestamp, and whether
FCS fields were negotiated. The web page and
`wlanpi-utilization session status --summary` expose these counters. The web
page measures stream age using server time, independently of browser clock
skew and the HTTP receive time. This does not complete buckets or move graphs.

A growing stream age can mean quiet traffic, decoder buffering, or capture
loss. Rejected rows are parser/non-WLAN records, not a hardware drop counter.
Negotiated FCS support does not certify every frame: adapters may omit FCS,
snapshots may be truncated, and some checks remain unverified. Primary-channel
scope/coverage and absent QBSS IEs can also leave metrics unavailable. Preserve
those distinctions when interpreting missing data.

The debug collector now includes managed-service journal/status, WebUI/Nginx
package versions, and `web_integration_preflight.json`. It remains read-only.
Process output includes CPU/RSS/elapsed time; optional USB inventory/topology
records adapter IDs, drivers and bus speed when `lsusb` is installed.

## Repeat on the Pi

Reinstall the updated app and restart its broker/web process using the
[web instructions](web-interface.md). Keep direct capture as the default.
Repeat the same band, primary channel, width, adapter and speedtest target on
Pi 4/5 where possible. Record idle, download, upload and recovery periods.
Watch client/BSSID counts, bad FCS, rejected rows, latest completed sample time,
and capture-stream age. Confirm the session ID/history survive browser changes.

If the spike persists, use the existing same-source full-packet diagnostic
capture for a short 15–30 second interval. Stop the current session explicitly
first. Substitute the actual test band/channel:

```bash
sudo wlanpi-utilization --managed --band 5 --channel 36 --start-only \
  --raw-pcapng /tmp/utilization-speedtest.pcapng \
  --log-dir /var/log/wlanpi-beacon-live
sudo wlanpi-utilization session status --summary
# Run the short speedtest interval, then request normal capture cleanup.
UTILIZATION_SESSION_ID="$(sudo wlanpi-utilization session status --summary \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"] or "")')"
sudo wlanpi-utilization session stop --session-id "$UTILIZATION_SESSION_ID" --summary
sudo ./scripts/pi_collect_debug.sh --since "10 minutes ago"
```

Keep the raw PCAPNG, its sidecar, CSV/JSONL, debug archive, adapter/driver and
TShark versions, and the times when the display changed. This diagnostic PCAPNG
contains all captured frame types; beacon-only export is a later milestone.
Compare headers and FCS in Wireshark and the existing `retry-debug` packet CSV;
its new `bad_fcs_excluded_count` and `radiotap_bad_fcs` fields expose this guard.

## Parser/analyzer performance baseline

Run without root or Wi-Fi hardware from the checkout:

```bash
.venv/bin/python scripts/benchmark_live_replay.py --seconds 120 --frames-per-second 2000
```

The same command with `/opt/wlanpi-beacon-live/bin/python` can use the installed
Pi interpreter. The script generates 240,121 normalized rows, verifies 120
completed seconds with one BSSID/eight clients/exactly 20% retries, and reports
elapsed/CPU time and peak RSS. It retains the same 120-second history.

On Apple M4/Python 3.13.3, the corrected run took **2.618 seconds**, about
**91,735 frames/second**, with **23.4 MiB** peak RSS. A previous-code run using
the same generator took 2.415 seconds (99,437 frames/second, 25.8 MiB). These
single runs are not a Pi capacity claim or evidence of a speedup; FCS validation
adds work. The benchmark includes row generation, parsing, streaming analysis
and publications, and excludes TShark/checksum calculation, radio/USB, IPC,
logging, HTTP and rendering. Measure those stages and capture/decoder drops on
the Pi before changing queues, sampling valid analyzer input, or claiming the
remaining missing-data problem is resolved.
