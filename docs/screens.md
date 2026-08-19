# Screen Reference

This document explains the six WLANPi Beacon Live screens and their metric
selection rules. For a short operational overview, start with the
[README](../README.md). For analyzer invariants, see
[architecture.md](architecture.md).

## Shared behavior

All screens render from the same immutable analyzer snapshot and the same
120-second history.

- Up/down changes only the active screen.
- Navigation wraps through all six screens and is debounced.
- Capture, analysis, logging, and inactive-screen history continue while a
  different screen is visible.
- Returning to a screen immediately shows the newest shared state.
- Missing values appear as `--`, an unavailable message, or a graph gap.
- Footer identities are selected automatically. Users never choose an SSID or
  BSSID.
- The first two auxiliary buttons are disabled while a display is active. The
  third saves a PNG of the current screen and briefly confirms the destination
  folder without changing the active screen or navigation offset.

The first two rows show the tuned frequency/screen name and a metric summary.
Graph screens use the middle 120×64 region. The bottom rows show the selected
SSID/RSSI and BSSID/channel. Long SSIDs are truncated; BSSIDs are retained.

## 1. Utilization

The Utilization screen graphs AP-advertised QBSS channel utilization for the
automatically selected QBSS BSSID.

![WLANPi Beacon Live utilization screen](docs/images/channel-utilization_screen.png)

Summary fields:

- `CU`: current advertised utilization percentage;
- `AVG`: average of available samples in the rolling history;
- `MAX`: maximum available sample in the rolling history.

QBSS utilization is transmitted as a raw value from 0 through 255 and shown as
a percentage. The graph retains the raw scale so all 256 values map cleanly to
the 64-pixel graph height.

The footer identifies the selected QBSS source. `<No QBSS Beacons>` means no
currently retained BSSID advertises usable QBSS information.

## 2. Admission

The Admission screen uses the same selected QBSS BSSID as Utilization and shows
its latest advertised admission capacity.

![WLANPi Beacon Live admission capacity screen](docs/images/admission-capacity_screen.png)

Summary fields:

- `ADC`: current admission capacity as a percentage of 31,250;
- `AVG`: average available percentage in the rolling history;
- `MIN`: minimum available percentage in the rolling history.

Admission capacity is beacon-advertised information, not a local capacity test.
Unavailable values remain gaps and do not become zero.

## 3. Stations

Stations sums the latest QBSS station count advertised by every retained BSSID.
It also reports unique client MAC addresses observed in eligible data frames.
These remain separate metrics: the displayed QBSS sum is AP-advertised, while
the MAC count is locally observed.

![WLANPi Beacon Live station count screen](docs/images/total-station-count_screen.png)

Summary fields:

- `SUM`: current sum of latest advertised station counts;
- `MAC`: unique client MAC addresses detected anywhere on the channel during
  the current 120-second window;
- `TOP`: highest per-BSSID count found either in the latest advertised QBSS
  station count or in unique client MACs observed for that BSSID during the
  current 120-second window.

The footer identifies the same SSID/BSSID represented by `TOP`. An advertised
QBSS count wins a tie with an observed client-MAC count. Ties between counts
from the same source use deterministic BSSID ordering.

Two per-second bar series share the fixed 0–64 station scale and are overlaid:
the amber series is the summed advertised QBSS count and the cyan series is
the number of unique eligible client MACs detected in that second. The `MAC`
text uses the same cyan as its graph series. Advertised sums above 64 reach the
graph ceiling and use the overflow color. Counts above 999 display as `∞` in
the compact text layout, while snapshots and logs retain the uncapped number.

### Client-MAC eligibility

A MAC is counted only when it is the unicast non-BSSID TA/RA endpoint of a data
frame linked to a BSSID whose beacon is retained on the monitored channel.
Repeated frames, retries, and traffic in both directions count that client only
once in each one-second sample and once in the rolling-window total.

The analyzer excludes:

- all known BSSID MAC addresses;
- authentication, probe, and every other management frame;
- control and extension frames;
- broadcast and multicast addresses;
- data frames that cannot be linked to a retained on-channel BSSID; and
- SA/DA-only addresses, which may identify hosts behind the distribution
  system rather than wireless clients.

## 4. Retries

Retries graphs one independent percentage for each completed capture second:

![WLANPi Beacon Live retry screen](docs/images/retry-percentage_screen.png)

```text
frames with Retry bit set / retry-eligible frames × 100
```

Summary fields:

- `RET`: current one-second channel retry percentage;
- `AVG`: average of available one-second samples in the rolling history;
- `MAX`: maximum available one-second sample.

A nonzero value below 1% displays as `<1%`. A missing value means the second
contained no frame eligible for the denominator; a displayed `0%` means
eligible frames existed but none had the Retry bit set.

### Eligible frames

Include:

- unicast data frames with a readable Retry bit;
- retry-capable unicast management frames with a readable Retry bit.

Exclude:

- beacons and probe requests;
- Action No Ack;
- group-addressed frames;
- control and extension frames; and
- frames without a readable Retry bit.

Every retry transmission is counted. Multiple retries of one original frame
are not deduplicated.

The live analyzer completes a second only after a later capture timestamp is
ingested. This avoids finalizing a bucket while TShark still has older rows
buffered on stdout.

### Retry footer

Known BSSIDs are associated with eligible frames through BSSID, TA, RA, SA, or
DA fields. The footer chooses the BSSID with the highest retry percentage for
the second. It keeps the prior footer if percentages tie and uses the strongest
beacon RSSI when no retry occurred. Frame timing is never a tie-breaker.

## 5. Beacon Loss

Beacon Loss graphs the percentage of expected beacons not received during each
completed capture second for the strongest estimated radio.

![WLANPi Beacon Live beacon loss screen](docs/images/beacon-loss_screen.png)

Summary fields:

- `BL`: aggregate loss percentage;
- `AVG`: average available beacon-loss percentage in the 120-second history;
  and
- `MAX`: maximum available beacon-loss percentage in that history.

The analyzer applies the existing best-effort radio grouping to every retained
BSSID, including hidden-SSID BSSIDs and BSSIDs without QBSS information. It
sums `REC` and `EXP` for all BSSIDs in the strongest group and calculates
`BL = (EXP - REC) / EXP × 100`. The footer uses the same two-second member
rotation as Composition.

Expected timing assumes a 102.4 ms interval for every BSSID. Retained capture
timestamps establish the latest schedule phase consistent with no more than
102.4 ms of delay, so a wall-clock second may contain nine or ten expected
transmissions. For a newly observed BSSID, accounting begins with its first
received beacon. Gaps in the inferred schedule increase `EXP` without
increasing `REC`; capture jitter cannot make the displayed percentage exceed
100%.

`REC` and `EXP` apply only to the BSSIDs grouped with the selected strongest
radio, not every beacon on the channel. A BSSID remains eligible for that
selection for one reporting second plus the 102.4 ms delayed-beacon allowance.
This permits a complete missed second to register as loss, but prevents a stale
high-RSSI BSSID retained elsewhere in the rolling window from producing a
continuous `REC 0` while another radio is actively beaconing.

A completed second remains open for one additional 102.4 ms of ordered capture
time. The analyzer matches each received beacon to at most one inferred
transmission slot and accepts arrival delays from zero through 102.4 ms. A
beacon scheduled before the wall-clock boundary can therefore arrive just
after it and still count in the earlier second. An arrival beyond that grace
does not fill the earlier slot.

## 6. Composition

Composition is a text screen describing the channel rather than a graph.

![WLANPi Beacon Live composition screen](docs/images/composition_screen.png)

It shows:

- `BSSIDs`: BSSIDs with beacons currently retained in the rolling window;
- `QBSS`: retained BSSIDs advertising QBSS information;
- `Est Radios`: best-effort estimated physical-radio count;
- `Radio BSSIDs`: BSSIDs grouped with the strongest estimated radio;
- `Radio Stations` in the interactive CLI: sum of the newest advertised QBSS
  station counts for those strongest-radio BSSIDs; missing counts are zero and
  client MACs are not deduplicated across BSSIDs;
- `AP`: supported vendor AP-name information when present;
- `Vendor`: supported vendor identification when present.

The footer cycles every two seconds through BSSIDs grouped with the strongest
estimated radio. Radio grouping is an estimate: BSSIDs are compared using OUI,
AP-name, RSSI, and related-MAC clues. Missing vendor information or unusual
BSSID allocation can prevent accurate grouping.

Only BSSIDs whose latest beacon is no more than 1.1024 seconds old participate
in strongest-radio selection. The headline BSSID and estimated-radio counts
still describe all beacon state retained in the rolling window.

See [Radio grouping](architecture.md#radio-grouping) for the implemented
heuristics.

## QBSS source selection

Utilization and Admission share one stable QBSS selection:

1. Consider retained BSSIDs whose latest beacon contains usable QBSS channel
   utilization.
2. Find the strongest peak RSSI.
3. Treat candidates within 3 dB as near-equal.
4. Prefer the near-equal candidate with the highest latest advertised station
   count.
5. Keep the current selection if it remains tied.

The selected BSSID's newest beacon in the 120-second window supplies its
displayed beacon-derived values. The newest-beacon rule does not choose among
BSSIDs and timing is not used as a selection tie-breaker.

## Metric distinctions

- **QBSS channel utilization** is advertised by an AP in beacon frames.
- **Local survey channel utilization** is derived from the local adapter's
  `iw dev <iface> survey dump` counter deltas and is optional/driver-dependent.
- **QBSS station count** is advertised independently by each BSSID.
- **Observed clients** are locally inferred from eligible BSSID-linked data
  frames and are not an associated-station count.
- **Retry percentage** is computed locally from eligible monitor-mode frames
  heard on the tuned channel.

These values describe different observations and must not be presented as
interchangeable measurements.
