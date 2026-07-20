# Screen Reference

This document explains the five WLANPi Beacon Live screens and their metric
selection rules. For a short operational overview, start with the
[README](../README.md). For analyzer invariants, see
[architecture.md](architecture.md).

## Shared behavior

All screens render from the same immutable analyzer snapshot and the same
120-second history.

- Up/down changes only the active screen.
- Navigation wraps through all five screens and is debounced.
- Capture, analysis, logging, and inactive-screen history continue while a
  different screen is visible.
- Returning to a screen immediately shows the newest shared state.
- Missing values appear as `--`, an unavailable message, or a graph gap.
- Footer identities are selected automatically. Users never choose an SSID or
  BSSID.

The first two rows show the tuned frequency/screen name and a metric summary.
Graph screens use the middle 120×64 region. The bottom rows show the selected
SSID/RSSI and BSSID/channel. Long SSIDs are truncated; BSSIDs are retained.

## 1. Utilization

The Utilization screen graphs AP-advertised QBSS channel utilization for the
automatically selected QBSS BSSID.

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

Summary fields:

- `ADC`: current admission capacity as a percentage of 31,250;
- `AVG`: average available percentage in the rolling history;
- `MIN`: minimum available percentage in the rolling history.

Admission capacity is beacon-advertised information, not a local capacity test.
Unavailable values remain gaps and do not become zero.

## 3. Composition

Composition is a text screen describing the channel rather than a graph.

It shows:

- `BSSIDs`: BSSIDs with beacons currently retained in the rolling window;
- `QBSS`: retained BSSIDs advertising QBSS information;
- `Est Radios`: best-effort estimated physical-radio count;
- `Radio BSSIDs`: BSSIDs grouped with the strongest estimated radio;
- `AP Name`: supported vendor AP-name information when present;
- `Vendor`: supported vendor identification when present.

The footer cycles every two seconds through BSSIDs grouped with the strongest
estimated radio. Radio grouping is an estimate: BSSIDs are compared using OUI,
AP-name, RSSI, and related-MAC clues. Missing vendor information or unusual
BSSID allocation can prevent accurate grouping.

See [Radio grouping](architecture.md#radio-grouping) for the implemented
heuristics.

## 4. Stations

Stations sums the latest QBSS station count advertised by every retained BSSID.
It does not count client MAC addresses observed over the air.

Summary fields:

- `SUM`: current sum of latest advertised station counts;
- `MAX`: highest channel-wide sum in the rolling history;
- `TOP`: latest advertised count from the current highest-count BSSID.

The footer identifies the same BSSID represented by `TOP`. If multiple BSSIDs
have the same highest count, deterministic BSSID ordering is used.

The graph uses a fixed 0–64 station scale. Values above 64 reach the graph
ceiling and use the overflow color. Counts above 999 display as `∞` in the
compact text layout, while snapshots and logs retain the uncapped number.

## 5. Retries

Retries graphs one independent percentage for each completed capture second:

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
- **Observed clients** are not currently tracked.
- **Retry percentage** is computed locally from eligible monitor-mode frames
  heard on the tuned channel.

These values describe different observations and must not be presented as
interchangeable measurements.
