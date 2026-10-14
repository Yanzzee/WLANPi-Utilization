# Development Guide

This guide is for contributors and future Codex runs. Read
[AGENTS.md](../AGENTS.md) and [architecture.md](architecture.md) before changing
application behavior.

## Local setup

WLANPi Beacon Live requires Python 3.13 or newer; the tested Trixie target is
Python 3.13. Direct capture/replay use the standard library; Core capture adds
the pinned `websockets==15.0.1` client through the `core` extra.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade 'pip>=24.3' 'setuptools>=75.3' wheel
python -m pip install -e '.[dev]'
python -m pytest
```

The development extra installs `pytest>=8.3.5,<9`. Build requirements are
`setuptools>=75.3` and `wheel`.
For Core transport development/tests, install `python -m pip install -e '.[dev,core]'`.

On a Pi, `scripts/pi_setup.sh` requires an activated, genuine venv without
system site-packages. It verifies the interpreter/version before running pip,
checks system commands, installs the package in editable mode, exercises both
CLI entrypoints and hardware-free replay, and runs the test suite. Missing pip
can be repaired with ensurepip inside that venv. Failed installs stop without
falling back to system Python or a non-editable installation. It prints package
suggestions but does not run `apt install` itself. Run it without `sudo`.

Create a fresh Python 3.13 venv when replacing an older development environment.
The production FPMS installer retains `/opt/wlanpi-beacon-live`, uses its
interpreter for application installation, and verifies genuine venv isolation
and Python 3.13+ before package writes. FPMS keeps its separate package-managed
Python environment; development setup does not install into either production
environment.

The FPMS installer installs Core runtime wheels in the app venv without building
the application package; `--wheel-dir` supports offline runtime installation.
Copied CLI entrypoints have a main guard so Core's spawn worker can import them
without relaunching capture. The FPMS backend choice is independent of the
standard CLI's direct default; see [Core installed-path testing](core-capture-evaluation.md#test-the-installed-core-path).
Direct capture remains the recommended CLI/FPMS default. Core is an optional
integration path, and passing its tests does not schedule a default migration.
Use Core's device information/control APIs where they simplify integration;
subscription and broader Core acceptance are separate follow-ups. See
[the backend decision](core-capture-evaluation.md).

## Repository map

| Path | Purpose |
| --- | --- |
| `beacon_live/models.py` | Canonical records, rolling state, immutable snapshots. |
| `beacon_live/parser.py` | TShark row normalization. |
| `beacon_live/analyzer.py` | Single-pass rolling analysis and selection. |
| `beacon_live/aggregator.py` | Replay aggregation. |
| `beacon_live/radio_grouping.py` | Best-effort physical-radio estimation. |
| `beacon_live/screens.py` | Pure screen definitions. |
| `beacon_live/screen_manager.py` | UI-only screen index and debounce. |
| `beacon_live/dashboard.py` | Terminal rendering. |
| `beacon_live/tui.py` | Interactive curses dashboard, layout, and table navigation. |
| `beacon_live/lcd_dashboard.py` | PPM/JSON LCD artifact rendering. |
| `beacon_live/live.py` | Live hardware boundary and runtime loop. |
| `beacon_live/channel.py` | Pure channel-operation parsing, capability, and coverage policy. |
| `beacon_live/capture_target.py` | Read-only interface discovery and namespace context for hardware commands. |
| `beacon_live/radio_session.py` | Radio locking, active-user checks, saved interface state, and restoration. |
| `beacon_live/log_writer.py` | Log formats, logging service, disk safety, rollover. |
| `beacon_live/survey.py` | Optional local survey parsing/calculation. |
| `beacon_live/retry_debug.py` | Offline retry audit. |
| `beacon_live/cli.py` | Replay/live/retry-debug CLI. |
| `beacon_live/device.py` | WLAN Pi foreground entrypoint. |
| `integration/wlanpi_fpms/` | Thin FPMS menu/display/process adapter. |
| `scripts/` | Installation, smoke, debug, and setup helpers. |
| `tests/` | Unit and integration-style pytest coverage. |

## Architectural expectations

- One captured frame enters one analyzer.
- All screens and logging modes share that analyzer state.
- Screen or logging changes must not restart capture or history.
- UI renderers consume snapshots; they do not calculate independent metrics.
- The newest beacon supplies beacon-derived values within each retained BSSID.
- BSSID selection is automatic and never uses timing as a tie-breaker.
- Keep AP QBSS CU separate from local survey CU.
- Keep advertised station counts separate from locally observed unique client
  MAC counts.
- Keep hardware calls at the live/survey/integration boundaries.
- Do not add pandas to the live capture path.

See [architecture.md](architecture.md) for the complete invariants and phase
history.

## Test strategy

Run the full suite before handing off a behavior change:

```bash
.venv/bin/python -m pytest -q
```

Useful focused groups:

```bash
.venv/bin/python -m pytest -q tests/test_parser.py tests/test_analyzer.py
.venv/bin/python -m pytest -q tests/test_screens.py tests/test_lcd_dashboard.py
.venv/bin/python -m pytest -q tests/test_log_writer.py tests/test_live.py
.venv/bin/python -m pytest -q tests/test_channel.py tests/test_channel_retry_scope.py
.venv/bin/python -m pytest -q tests/test_fpms_integration.py tests/test_fpms_patch.py
```

Parser, selection, aggregation, client-MAC eligibility, retry eligibility,
grouping, logging safety, screen navigation, and FPMS command wiring are
expected to have pytest coverage. Most tests must run without Wi-Fi or display
hardware.

## Hardware-free replay

Replay the included sample:

```bash
.venv/bin/beacon-live replay --input samples/tshark_qbss_sample.tsv
```

Replay can combine copied smoke-test beacon data with before/after survey
dumps:

```bash
.venv/bin/beacon-live replay \
  --beacons-tsv samples/pi_tshark_qbss_sample.tsv \
  --survey-before samples/pi_survey_before.txt \
  --survey-after samples/pi_survey_after.txt
```

Malformed rows are counted/skipped. Replay reports record counts, time range,
duration, unique BSSIDs, and optional survey CU.

## Pi and WLAN Pi verification

Use [pi_testing.md](pi_testing.md) for:

- system/setup checks;
- monitor-mode smoke capture;
- copying and replaying hardware samples;
- reproducible retry PCAP capture/audit;
- FPMS installation; and
- on-device acceptance checks.

The most common helpers are:

```bash
./scripts/pi_setup.sh
./scripts/pi_smoke.sh wlanpi0 5180
sudo ./scripts/pi_collect_debug.sh --since "15 minutes ago"
sudo ./scripts/install_wlanpi_fpms.sh
```

The legacy smoke script is only for an idle test adapter: it does not use the
radio ownership guard and attempts to restore only the interface's original
type. Prefer an application-owned PCAPNG for Trixie testing. The normal live runtime uses a radio session that restores saved modes, up/down flags,
and known monitor tuning after capture stops, including startup failures.
See [the radio acceptance checks](pi_testing.md#radio-ownership-and-restoration-acceptance).

## Adding or changing a screen

Prefer this sequence:

1. Add shared data to models/analyzer only if the current snapshot cannot
   represent the metric.
2. Add analyzer tests for the metric and selection semantics.
3. Implement a pure screen definition over `MetricsSnapshot`.
4. Add renderer/screen tests.
5. Verify up/down navigation retains one capture process and shared history.

A new screen should not own frame buffers, select its own capture source, or
restart the analyzer.

## Changing logging

Live display and logging-only modes must continue using the same
`LoggingService`. Disk checks belong in the logging/runtime layer, not a screen
refresh callback. Test at least:

- inactive/start/stop transitions;
- display and logging-only operation;
- periodic deadline behavior;
- low-disk closure and marker contents;
- rollover filenames; and
- capture continuation when display logging stops.

See [logging.md](logging.md) for the public contract.

## Documentation ownership

- `README.md`: concise user overview, installation, operation.
- `docs/architecture.md`: source-of-truth constraints and phase history.
- `docs/screens.md`: metric and selection reference.
- `docs/logging.md`: logging contract and safety policy.
- `docs/troubleshooting.md`: user/operator diagnosis.
- `docs/pi_testing.md`: detailed hardware verification procedures.
- `docs/development.md`: contributor workflow and repository map.
- `docs/to_do.md`: future ideas/status; do not treat unimplemented items as
  current behavior.

Update the narrowest owning document and link to it instead of copying the same
technical explanation into multiple files.

## Generated and sensitive artifacts

Do not commit these by default:

- `samples/pi_*` smoke outputs;
- `debug/pi_debug_*` bundles/archives;
- generated `logs/` files; or
- raw PCAP/PCAPNG captures.

They can contain SSIDs, BSSIDs, client/interface MAC addresses, IP addresses,
kernel logs, and environment details. If a capture is valuable as a regression
fixture, create a small sanitized file with a descriptive name and tests.
