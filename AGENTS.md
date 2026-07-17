# WLANPi Beacon Live

This project builds a Python live 802.11 beacon and channel-utilization analyzer for Raspberry Pi / WLAN Pi hardware.

## Architecture

The application is designed around a single-pass analysis pipeline.

Frames are captured once, analyzed once, and stored in a rolling two-minute window. All display screens render from the same shared metrics snapshot. Display navigation must never restart capture, analysis, or graph history.

The user never selects a BSSID or SSID. The application automatically chooses the reporting BSSID for display using the selection rules below.

## Design rules

- Keep hardware-specific code isolated.
- Most code must be testable without WLAN Pi hardware.
- Do not use pandas in the live capture path.
- Prefer dataclasses, pure functions, immutable snapshots, and small modules.
- Use pytest for all parser and aggregation behavior.
- Treat AP QBSS channel utilization and local survey channel utilization as separate metrics.
- Treat QBSS station count and observed client count as separate metrics.
- Do not call observed clients "associated station count."
- Capture each frame exactly once.
- Do not create separate analysis pipelines for different display screens.
- Display code must consume shared analyzer state and must not compute metrics independently.
- UI code should remain separate from capture and analysis logic.

## Rolling window

- Maintain a continuous two-minute rolling history.
- All graphs display the same rolling history regardless of which screen is active.
- Changing display screens must not reset history or restart processing.
- The newest data should always be available immediately when changing screens.

## BSSID selection

The application automatically selects the BSSID used for display.

Selection rules:

1. Prefer the strongest RSSI.
2. Do not switch if another candidate is within approximately 2–3 dB (hysteresis).
3. When candidates have similar RSSI, prefer the BSSID advertising the highest station count.
4. Otherwise keep the current selection to avoid unnecessary display changes.

Users never manually choose a BSSID.

## Beacon data rules

For every BSSID, the newest beacon within the rolling window is authoritative.

QBSS channel utilization, station count, admission capacity, and similar beacon-derived values should always come from the latest beacon observed for that BSSID.

Older beacon contents should not overwrite newer values.

If no beacon was received for a BSSID within the last interval, do not include any data for that BSSID.

## Radio grouping

A BSSID is not necessarily a unique radio.

Where possible, estimate physical radios using available information such as:

- AP Name information element (vendor-specific)
- BSSID MAC
- RSSI
- Other vendor-specific identifiers

Radio grouping is best-effort and should not rely on any single vendor implementation.

## Important fields

TShark beacon fields:

- frame.time_epoch
- wlan.ssid
- wlan.bssid
- wlan.qbss.cu
- wlan.qbss.scount
- wlan.qbss.adc

Additional fields when available:

- wlan_radio.signal_dbm
- wlan.fc.type
- wlan.fc.subtype
- wlan.fc.retry
- wlan.ta
- wlan.ra
- wlan.sa
- wlan.da
- AP Name (vendor-specific)
- Beacon interval

## Retry data rules

- Retry graph samples are independent one-second ratios retained in the shared
  two-minute history.
- The denominator contains only frames eligible for Retry-bit retransmission:
  unicast data frames and retry-capable unicast management frames.
- Exclude beacons, probe requests, Action No Ack, group-addressed frames,
  control frames, extension frames, and frames without a readable Retry bit.
- Count every captured frame with the Retry bit set, including multiple retry
  transmissions of the same original frame.
- Display nonzero retry percentages below 1% as `<1%`, not `0%`.
- Associate a frame with a known BSSID when that BSSID appears in `wlan.bssid`,
  `wlan.ta`, `wlan.ra`, `wlan.sa`, or `wlan.da`.
- Select the retry footer BSSID by highest one-second retry percentage. Keep the
  currently displayed BSSID when the highest percentages tie. When no retries
  occurred, show the strongest-RSSI beacon BSSID.

Local survey source:

- iw dev <iface> survey dump

## Development targets

- Laptop replay mode must work without Wi-Fi hardware.
- Pi live mode can require sudo, tshark, iw, and a monitor-mode adapter.
- The same analysis engine should be used for replay, live capture, logging, and display modes.
- Terminal and WLAN Pi display interfaces should consume the same shared metrics snapshot.
- New display screens should primarily be new renderers, not new analysis code.
