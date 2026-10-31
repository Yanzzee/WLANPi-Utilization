# Core device acceptance matrix

Record installed-image evidence for the [Core support contract](core-support.md).
Each run must identify its actual versions and hardware; source review and
automated mocks are separate evidence. Unknown fields stay unknown. This matrix
is established for milestone 1; full lifecycle acceptance belongs to milestone 3.

## Existing device evidence

| Report | Core | Requested tuning | Result | Remaining detail |
| --- | --- | --- | --- | --- |
| October 13 owner run, wlanpi-bd2 | 2.4.8-1 | 2.4 GHz channel 1, 2412/20 | 899 normalized frames, no malformed rows, confirmed stop/restoration | Exact OS/kernel/adapter/driver and component versions not recorded in the report |
| Subsequent owner run before October 14 | 2.4.8-1 baseline | 5 GHz channel 40, 5200/20 | 518 normalized frames, confirmed stop, restored to prior 2412 MHz channel | Exact device inventory and namespace not recorded in the report |
| October 14 installed-path feedback | Version not separately restated | 6 GHz CLI; FPMS starts on each band | Basic display/start feedback accepted | Exact widths, backend-by-band combinations, versions and failure results unavailable |

The [original evidence](core-capture-evaluation.md#controlled-owner-prototype-on-the-pi)
contains byte/row counts and context. These rows do not establish a complete
2.2.0 device matrix. No new device run is claimed by the October 30 source review.

## Pending acceptance coverage

| Case | Required coverage | Status |
| --- | --- | --- |
| Inventory and metadata | Current installed Core/API; source/namespace/frequency agreement, trusted TLS and authentication | Pending on current image |
| Basic owner | 2.4 GHz 20 MHz; 5 and 6 GHz 20/80 MHz on supported configurations | Prior limited evidence above; current-build matrix pending |
| Channel limits | Known 80 MHz mapping gap; unsupported width/center; no mutation on rejection | Pending device confirmation |
| Shared application session | Web/CLI/FPMS attach/detach, navigation and history continuity | Core-specific current-build evidence pending |
| Storage and load | Independent logging/export; rotations; disk pressure; sustained traffic and consumer load on Pi 4/5 | Pending Core comparison |
| Normal cleanup | Repeated start/stop, Ctrl+C/SIGTERM/service stop; restore a different original channel and sibling state | Prior limited evidence above; broader matrix pending |
| Failures | Tune/start failure, busy radio, Core restart/disconnect, auth/TLS failure, decoder stall, adapter removal | Pending |
| Subscription | Compatible owner, incompatible/hopping/filtered source, owner exit, slow consumer; detach without owner changes | Pending milestone 4 implementation |
| Default migration | Fresh install, explicit saved direct/Core, upgrade/reinstall/reboot | Pending milestone 5 implementation |

## Record for each run

Copy this record into a dated acceptance report or retained test bundle. Store
raw captures/debug bundles outside Git unless sanitized for a regression fixture.

```text
Run ID and date/time/timezone:
Utilization checkout commit and installed version:
Pi model / WLAN Pi OS image / Debian version / kernel:
Core dpkg version / source revision if known:
FPMS / WebUI / Nginx versions:
Python / TShark / Dumpcap / websockets versions:
Adapter make/model / USB or PCI ID / driver / firmware:
Interface / phy / namespace / regulatory domain:
Core role / Core session ID / Utilization session ID:
Requested band / primary / width / center1 / center2:
Verified actual tuning and evidence path:
Original radio/sibling state and after-stop comparison:
Controls and attached consumers exercised:
Traffic/load duration / CPU / RAM / capture-stream delay:
Received/forwarded bytes / decoded rows / normalized frames / rejects:
Available drop evidence (or unavailable):
Logging/export rotations / closure / skips / output hashes:
Failure/recovery case / Core stop acknowledgement / restoration result:
Saved-packet metric comparison and completed interval range:
Outcome (pass/fail/partial) / limitations / evidence paths:
```

## Collect the inventory without starting capture

From the checkout on the Pi, run:

```bash
dpkg-query -W wlanpi-core wlanpi-fpms wlanpi-webui tshark wireshark-common nginx
/opt/wlanpi-beacon-live/bin/python --version
/opt/wlanpi-beacon-live/bin/python -I -c 'import websockets; print(websockets.__version__)'
sudo /opt/wlanpi-beacon-live/bin/python scripts/check_core_capture.py
sudo ./scripts/pi_collect_debug.sh --since "5 minutes ago"
```

The debug collector records versions, root/named namespaces, adapter/USB evidence
where available, radio state, deployment fingerprints and service health. The
metadata probe authenticates, lists sessions and reads supported frequencies;
it does not configure/start/stop/subscribe to capture. Without `--token-file`
it issues an evaluation JWT, which updates Core's token store. Do not add raw
getjwt output to the evidence. Treat metadata matches as configuration candidates,
not proof of actual tuning or packet delivery.

After inventory, use the [installed Core procedure](core-capture-evaluation.md#test-the-installed-core-path)
and [additional owner checks](core-capture-evaluation.md#additional-owner-acceptance)
for captures. Use actual supported channels and retain the diagnostic sidecar,
before/after snapshots and same-source packet evidence for each case.
