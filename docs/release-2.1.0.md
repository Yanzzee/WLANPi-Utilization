# Utilization 2.1.0

These notes describe the 2.1.0 release. Package metadata is aligned at `2.1.0`.
Device smoke acceptance is complete for the agreed scope; external offline
analysis remains deferred. Repository merge/tag/publication are separate steps.

## What changes

- One managed capture/analyzer session shared by web, CLI and FPMS. Opening,
  closing, reconnecting or changing screens preserves capture and the shared
  two-minute history. Left/Q/Ctrl+C in an attached display only detaches it.
- WLAN Pi **Apps → Utilization** and `https://<your-pi>/utilization/`, protected
  by the existing platform login. The backend stays separate from Core.
- Responsive metrics, desktop graphs, sortable BSSID and Hosts tables,
  actual interface/tuning information, session/logging/export controls and
  authenticated downloads. Hosts includes vendor/local indication, observed
  SSID/BSSID context, last activity, client-transmitted RSSI, Tx/Rx counts and
  Tx retry percentage; it does not discover hostnames or estimate throughput.
- Beacon-only pcapng export from the same original packet source used for
  live analysis. All required frame types still reach the analyzer. Original
  beacon bytes, timestamp metadata and radiotap are preserved.
- Independent CSV/JSONL logging and beacon export, with dynamic FPMS actions
  that follow changes from any consumer. Stop Session explicitly ends capture.
- Better corrupt-frame identity guards and capture-health reporting. Metric
  definitions, automatic BSSID selection, nulls, zeros, gaps and `<1%` retry
  formatting retain their existing meanings.

## Requirements and upgrade

Use WLAN Pi OS 26.10 DeadEye / Debian 13, Python 3.13+, supported FPMS 1.4.24
and WebUI/Nginx, and a monitor-mode adapter supported by `iw` and TShark/Dumpcap.
Pi 4 and Pi 5 have been exercised. Adapter/regulatory limits still apply.
Bookworm/Python 3.9 installations are outside this release target.

Stop any active session normally before updating. In an existing checkout:

```bash
git pull
sudo ./scripts/install_wlanpi_fpms.sh
```

This updates the application, CLI launchers, FPMS, broker and protected web
hosting together. It restarts services and ends active capture. Reapply it
after FPMS/WebUI package updates, which can replace integration files. An
unsupported platform revision is rejected; do not bypass the auth checks.

New FPMS installations default to managed/direct capture; saved explicit
backend/runtime choices are preserved. To migrate a saved standalone FPMS
installation, pass `--fpms-runtime managed`. Core remains optional and is never
selected as an automatic fallback. `--without-web` skips web installation;
it does not remove an already installed route.

Preferred commands are `wlanpi-utilization` and `utilization`; legacy
`wlanpi-beacon-live` / `beacon-live` aliases remain. Existing installation,
configuration and log paths under `wlanpi-beacon-live` are preserved. New log
filenames start with `utilization_`; existing files are not renamed.

After reboot, the web page is available after normal login, but capture starts
idle. Sessions and the download catalog are in memory and do not resume after
broker restart/reboot. Closed files remain on disk.

## Common operations

```bash
# Persistent shared capture; browser/FPMS can attach later.
sudo wlanpi-utilization --managed --band 5 --channel 36 --start-only
sudo wlanpi-utilization --attach
sudo wlanpi-utilization session status --summary

# Read the current ID once and request normal shutdown; poll status for completion.
sudo wlanpi-utilization session stop --summary

# Start another session with beacon pcapng export enabled.
sudo wlanpi-utilization --managed --band 5 --channel 36 --start-only --beacon-pcapng
```

The plain live CLI adopts an active managed session; otherwise it starts
foreground capture. Use `--standalone` for explicit foreground behavior.
Source changes require Stop Session and a new start. Explicit ID-scoped
logging/export controls are in the [service guide](managed-service.md).

## Output and limitations

- Beacon export rotates hourly or at 256 MiB per file. By default there is no
  file-count/total-size cap and no automatic deletion. At 256 MiB free space,
  export stops and closes buffered output while analysis continues. Positive
  retention options opt into a ring buffer. Stop export closes the current file.
- Only registered closed files appear for download. Broker restart clears its
  bounded catalog, not saved files. A segment that fails to close is not offered.
- Host detail is limited to 1,024 published rows, with an explicit notice if
  details are omitted. The observed-client total is uncapped. Host context does
  not prove association, and receive-only clients have no transmitted RSSI.
- Monitor-mode losses and adapter/driver limitations can make observations
  incomplete even when tuning succeeds. In particular, earlier MT7921U 6 GHz
  testing missed payload data at 80 MHz. Missing QBSS or metadata stays missing.
- Local survey reporting is hidden in the web UI until adapter validation;
  CLI/API/log definitions remain available. Survey CU is separate from AP QBSS CU.
- Core capture retains its documented source/width/lifecycle restrictions.
  Broader Core sharing, Grafana, touchscreen hardware and additional platform
  support are outside the 2.1.0 scope. A future local browser uses the same app.
- **WiFi-Beacon-analysis testing is deferred.** Export/download and Wireshark
  readability have been tested; results in that external analysis project are
  not yet validated. Later testing needs supported AP names, AP/classroom and
  schedule files, a pinned analysis revision, and explicit date/timezone context.

## Device acceptance so far

The user reports successful output rotation and buffered closure, use of web,
CLI and FPMS with one session, and working start/stop. Reboot and authentication
also appear to work as designed. The user subsequently confirmed no differences
between the radio's before-session and after-stop snapshots, completing the
physical radio-restoration check. This accepts the reported smoke-test areas;
exact adapter/build details and untested hardware combinations are not inferred.
Earlier Pi 4 login/logout/expiry tests and Pi 4/5 display/performance observations
remain recorded in the roadmap. External offline analysis remains deferred.

## Final release checks

No additional feature or device smoke check is required by the agreed scope.
The [restoration procedure](pi_testing.md#quick-final-radio-restoration-check)
remains available for future regressions. Final preparation verifies tests,
distribution contents, fresh installation/replay and version consistency.

For publication, merge the accepted release commit from `dev` into `main`,
then create `v2.1.0` on that exact commit and use these notes for the release.
Remote review found only `v1.0.0` and `v2.0.0`; `main` still points to v2.0.0.
Recheck remote state before publishing. This preparation does not push a branch,
create a remote tag or publish a GitHub/PyPI release.

## Distribution artifacts

The Python wheel supplies the package, CLI entrypoints and local web assets.
It does not install FPMS/systemd/Nginx integration by itself. The source archive
also includes the Pi installer, integration files, documentation, sample inputs
and hardware-free tests. For a Pi installation, use the repository or extracted
source archive and run `sudo ./scripts/install_wlanpi_fpms.sh` there.

Release preparation builds both distributions under `dist/` and generates
`dist/SHA256SUMS`. Contributor reproduction in an isolated development venv:

```bash
python -m pip install 'build>=1.2,<2' 'setuptools>=75.3' wheel
python -m build --no-isolation
```

The final 2.1.0 regression run passed **976 tests in 43.66 seconds** with two
existing fork deprecation warnings; hosted/local frontend logic checks also
passed. Wheel/source contents and versions were checked against the working
tree. Fresh isolated wheel installation, all four CLI entrypoints and sample
replay passed. The source archive includes all tracked project files except
the development `.gitignore`, plus its manifest and generated package metadata.
Packaging-tool notices about duplicate legacy `setup.py` extras are benign:
`pyproject.toml` is authoritative and their declarations agree. Reported
Pi 4/5 speedtests produced no significant display/graph delay; `top` showed
about 8% CPU on a lightly used Pi 5 channel and similar Pi 4 CPU. Multiple
beacon exports and Wireshark opening were reported successful. These are
acceptance observations, not exhaustive adapter/load coverage.

See [web operation](web-interface.md), [export policy](beacon-export.md),
[troubleshooting](troubleshooting.md) and the
[development/evidence roadmap](managed-runtime-roadmap.md) for details.
