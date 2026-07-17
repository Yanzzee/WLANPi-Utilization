WLANPi Utilization 1.1 Technical Plan

Objective

Keep capture and analysis single-pass and continuous, while making the display layer switchable. The app should collect frames once, compute metrics once, retain a rolling two-minute history, and let the user scroll through views without restarting any analysis.

Core design rules

1. The user never selects a BSSID or SSID.
2. The application chooses the reporting BSSID automatically for each view.
3. Switching views must not restart capture, metric accumulation, or graph history.
4. Every screen should render from the same current snapshot plus the same two-minute rolling history.
5. For any chosen BSSID, the latest beacon in the window is the authoritative beacon for that BSSID.
6. Use a 2–3 dB hysteresis threshold when deciding whether to switch to a different strongest BSSID.
7. AP name IEs should be treated as a best-effort clue for radio grouping, not as a standard identity field.

⸻

Recommended architecture

1) Acquisition layer

A single capture worker reads frames continuously and emits normalized frame records into the analysis pipeline.

This layer should do only the minimum necessary work:

* timestamp
* channel / frequency
* RSSI
* BSSID
* SSID
* beacon/probe-response/frame type
* Retry bit
* sequence-related fields if useful
* station count / QBSS fields if present
* AP name or vendor-specific name field if present

Do not make this layer aware of screen logic.

2) Analysis layer

A single analyzer maintains the rolling window and updates derived state continuously.

It should:

* keep a two-minute rolling buffer of per-frame observations
* maintain a latest-record per BSSID
* maintain per-BSSID rolling metrics
* maintain per-channel aggregates
* maintain best-effort radio groups
* expose a read-only snapshot object to the UI

3) Display layer

Each screen is a pure renderer over the same snapshot.

The display system should:

* hold a list of screen definitions
* switch the active screen index on up/down
* never reset the analyzer on navigation
* never rebuild the rolling history on navigation
* only redraw what is currently active

If the current front panel expects a single “Display” start mode, that mode should initialize the shared analyzer once, then attach the screen selector on top of it.

⸻

Data model

Raw frame record

A normalized record for every captured frame.

Suggested fields:

* ts
* channel
* frequency
* bssid
* ssid
* rssi
* frame_type
* subtype
* retry_flag
* frame_len
* seq_ctrl if available
* beacon_interval if available
* qbss_cu
* qbss_admission_capacity
* station_count
* ap_name
* vendor_ie_source
* is_management_frame
* is_beacon
* is_probe_response

This is the minimal canonical input for all later metrics.

Per-BSSID state

One record per BSSID, updated from the rolling window.

Suggested fields:

* bssid
* ssid
* last_seen_ts
* latest_beacon_ts
* latest_beacon_record
* window_frames
* window_beacons
* window_management_frames
* window_retry_frames
* window_total_frames
* window_station_count_latest
* window_qbss_present
* window_cu_latest
* window_admission_capacity_latest
* window_rssi_latest
* window_rssi_peak
* window_ap_name
* radio_group_id
* selection_score

Important rule: latest_beacon_record must be the newest beacon for that BSSID inside the current window, even if another older beacon has a different station count or QBSS value.

Per-radio group state

This is the best-effort deduplicated “actual radio” abstraction.

Suggested fields:

* radio_group_id
* representative_bssid
* candidate_bssids
* ap_name_set
* ssid_set
* channel
* best_rssi
* best_station_count
* last_updated_ts

This is where AP name can help collapse multiple BSSIDs that belong to the same physical radio.

Channel snapshot (if needed)

One object per channel, derived from all BSSID states on that channel.

Suggested fields:

* channel
* active_bssid_count
* qbss_reporting_bssid_count
* radio_group_count
* unique_ap_count_estimate
* total_station_count
* total_retry_rate
* selected_display_bssid
* selected_display_radio_group
* selected_cu
* selected_admission_capacity
* selected_retry_rate
* selected_beacon_rate
* top_station_bssid
* top_retry_bssid

UI state

Keep UI state separate from analytics.

Suggested fields:

* active_screen_id
* screen_order
* scroll_locked if needed for brief navigation debounce
* last_user_input_ts

Do not store metric history in UI state.

⸻

BSSID selection logic

Primary rule

Choose the reporting BSSID using the strongest RSSI beacon in the current window.

Hysteresis rule

Do not switch to a different BSSID unless it is meaningfully stronger, using a 2-3 dB threshold.

Tie-breakers

When BSSIDs are close enough to be considered the same radio or near-equal candidates:

1. Prefer the one with the most stations.
2. If still tied, keep the current selected BSSID to avoid display churn.

“Same radio” heuristic

Treat BSSIDs as probably the same radio when one or more of these are true:

* AP name matches
* similar RSSI and adjacent BSSID MACs

This should be a heuristic, not a hard identity rule. The goal is stable display behavior, not perfect RF identity resolution.

Latest beacon rule

For the selected BSSID and for any metric shown on a screen, always use the latest beacon from the two-minute window as the displayed value source for that BSSID.

That avoids stale QBSS values from beacons that have not yet refreshed.

If a no beacon is received from a BSSID during the latest window, do not include any metrics for that BSSID in the current window.

Never change the data or graph for anything older than the current window.

⸻

Screen architecture

Each screen should be a pure function of the shared snapshot.

Screen 1: CU

Show channel utilization for the selected BSSID.

Recommended behavior:

* display the selected BSSID identity
* display CU from the latest beacon of that BSSID
* show a two-minute graph of CU for the selected BSSID(s)
* indicate when the selected BSSID changed with a vertical line in the graph

Screen 2: Admission capacity

Show admission capacity instead for the selected BSSID.

Recommended behavior:

* use the same selected BSSID logic as CU
* display admission capacity as a percentage of the maximum ADC value, 31,250,
  from the latest beacon in the window
* show ADC, average ADC, and minimum ADC percentages without CU values
* show a two-minute graph of ADC percentage for the selected BSSID(s)
* indicate when the selected BSSID changed with a vertical line in the graph
* keep the same graph time base as the CU screen

Screen 3: Channel composition

Show:

* number of BSSIDs on the channel
* number reporting QBSS IEs
* estimated number of distinct radios
* estimated number of unique APs
* the strongest SSID & BSSID by signal
* the AP name associated with the strongest signal BSSID
* the AP vendor if known

Recommended behavior:

* provide a “best effort” distinction between BSSID count and radio count
* include a detail line for the current grouping confidence if useful
* optionally list BSSIDs sorted by station count on a secondary detail screen

Screen 4: Total station count

Show total station count sum of all received BSSID beacons

Recommended behavior:

* make the total station count the primary value
* optionally swap the CU/SUM layout if that improves readability
* display the SSID/BSSID with the highest station count
* graph the two-minute trend on a fixed 0–100 scale
* truncate graph bars above 100 at the graph ceiling and distinguish them as
  overflow bars
* label the highest advertised station count as `TOP STA`; do not show a BSSID
  count on this screen
* ensure `TOP STA` and the footer SSID/BSSID describe the same BSSID

Display styling

* use the same Scanner font for all screens and prefer size 10
* color the current graph-associated metric to match its graph
* render all secondary summary fields, metadata, and footer text in white
* use a distinct graph/primary-metric color for each screen

Screen 5: Retry percentage

Show retries as a percentage of total frames received on the channel.

Recommended behavior:

* compute retry percentage from the rolling window
* ensure the capture path retains enough header information to read the Retry bit
* display the SSID/BSSID with the highest retry rate
* include beacon rate of the strongest signal as a percentage of expected
* graph the two-minute trend


⸻


Navigation

The control stick up/down should only change active_screen_id.

Requirements:

* no analyzer reset
* no graph reset
* no capture restart
* no per-screen reinitialization

Add a small debounce so one press does not accidentally skip multiple screens.


⸻


Logging-only mode

Add a menu item that starts capture and logging without rendering the display screens.

This mode should:

* use the same acquisition and analysis engine
* write logs to disk
* keep screen rendering disabled
* still enforce disk-space safety checks


⸻


Documentation

Split documentation into:

* user-facing README
* developer/technical docs in docs/

The README should stay user-centered and brief. The deeper implementation notes belong in docs/.

⸻

Rolling history and graph behavior

All graphs should use the same two-minute rolling window.

That means:

* graphs do not restart when the user scrolls
* the newest visible sample should always be current
* the history for the inactive screens continues updating in the background
* when the user returns to a screen, it shows the current state immediately

Implementation-wise, the simplest reliable pattern is a time-bucketed ring buffer keyed by metric and BSSID/radio/channel.

⸻

Suggested internal update cycle

1. Receive frame.
2. Normalize it into a canonical frame record.
3. Update per-BSSID state.
4. Update radio-group heuristics.
5. Update channel aggregates.
6. Expire records older than two minutes.
7. Recompute selected BSSID and selected radio.
8. Publish a new immutable snapshot.
9. The display reads the latest snapshot on its own refresh cadence.

⸻

Logging and disk safety

Long-term logging needs guardrails.

Recommended behavior:

* periodically check free disk space
* stop cleanly before the disk is nearly full
* append a clear end-of-log marker that explains logging stopped because disk space ran low
* rotate to a new file at fixed intervals for long-term logging

Suggested log policy:

* one active file at a time
* periodic rotation by time or size
* final “disk full / logging stopped” record in the file when the safety threshold is hit

⸻

Practical implementation order

Phase 1

* create the shared rolling snapshot model
* keep the existing CU screen working through the new snapshot
* confirm the latest-beacon rule

Phase 2

* add screen navigation without reset behavior
* add admission capacity and total station count views

Phase 3

* add retry percentage
* confirm the capture path supports Retry-bit extraction reliably

Phase 4

* add radio grouping using AP name and other vendor clues
* add channel composition metrics

Phase 5

* add logging-only mode and disk-space guardrails
* tighten docs and split user/developer content
