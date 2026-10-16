# Utilization managed service

The v2.1.0 development service gives CLI and FPMS consumers one capture owner
and the same analyzer publications. Managed mode is opt-in pending Pi
acceptance. Direct capture remains the default provider; Core remains an
explicit optional provider. The preferred commands and every app title use
**Utilization**. `wlanpi-beacon-live` and `beacon-live` remain executable
compatibility aliases. New generated logs use the `utilization_` filename
prefix. Existing package, installation, configuration, and log directories
retain their historical names, and existing files are not renamed.

## Install and operate

```bash
sudo ./scripts/install_wlanpi_fpms.sh --fpms-runtime managed
sudo systemctl status wlanpi-utilization.service
sudo wlanpi-utilization --managed --band 5 --channel 36 \
  --start-only --log-dir /var/log/wlanpi-beacon-live
sudo wlanpi-utilization --attach
sudo wlanpi-utilization session status --summary
```

For an already installed application, `sudo bash
scripts/install_utilization_service.sh` installs the service independently of
FPMS configuration. Service installation/reinstallation restarts the broker
and ends its current capture gracefully. The service starts idle, including
after reboot or service restart; it never resumes capture automatically.

`--managed` starts or joins a compatible session. Compatible requests preserve
the existing session ID, log paths, capture, and history. Compatibility checks
cover source/namespace, backend, tuning, survey, sampling, and raw diagnostic
capture choices. A different source requires an explicit stop and then a new
start. Logging paths and limits belong to the initial owner configuration;
joining consumers cannot replace them. `--attach` reads the current owner
configuration without requesting a start or reconfiguration.

Exiting a managed CLI view with Q/Ctrl+C or leaving the FPMS display detaches
that renderer. Capture and active logging continue with zero renderers. FPMS
managed mode adds **Stop Capture** to each channel menu. **Stop Logging**
disables logging without ending capture. CLI controls require the current
session ID from status:

```bash
sudo wlanpi-utilization session logging --session-id SESSION_ID --enabled yes --summary
sudo wlanpi-utilization session logging --session-id SESSION_ID --enabled no --summary
sudo wlanpi-utilization session stop --session-id SESSION_ID --summary
```

Stopping is asynchronous. Poll status until stopped/failed before starting
another owner. The final publication can precede process reaping by a short
interval; a new start remains blocked until cleanup and reaping finish. Final
history stays readable until a new explicit start. Stale control IDs fail.

The normal live command without `--managed` keeps standalone behavior,
including stopping capture on exit. Select `--fpms-runtime standalone` to
restore that FPMS behavior. Reinstallation without a runtime flag preserves
the saved selection in `/etc/wlanpi-beacon-live/fpms.json`. The
`WLANPI_UTILIZATION_RUNTIME_MODE=managed|standalone` environment variable can
override that setting. Backend selection remains independent.

## Process and IPC contract

```text
CLI or FPMS renderer → private Unix socket → Utilization broker
                                                │
                                      isolated capture owner
                                                │
                                run_live → existing analyzer worker
                                                │
                         immutable snapshots and completed statistics
```

The broker starts the capture owner through a fresh Python process. Capture
and the existing aggregation fork run from the owner's main thread, without
an HTTP handler or service publication thread. Existing radio locking,
preflight, TShark/Core decoding, survey, and CSV/JSONL logging remain in
`run_live`. The broker never calculates wireless metrics.

Local IPC uses protocol version 1, length-prefixed JSON, and declared
dataclass types. Enums, paths, nulls, tuples, completed rows, and graph-gap
flags round-trip without changing metric definitions. There is no pickle
deserialization. Each message is limited to 8 MiB. The nonblocking owner link
keeps at most one partial message and one newest pending message. Intermediate
publications can be skipped; the newest snapshot and bounded completed-row
history carry current analyzer state. Capture never waits for a renderer's
socket write. Messages above the limit fail explicitly rather than truncating
metrics.

The broker allows 16 simultaneous client connections, each with a five-second
deadline and one request/response. A slow or incomplete client does not block
the accept loop. Consumers poll publications once per second and handle local
navigation every 100 ms. A renderer may keep its own screen index; navigation
never reaches the owner or resets the shared snapshot/history.

The default socket is `/run/wlanpi-utilization/service.sock`, mode 0600 in a
0700 directory, with one broker lock. The systemd unit runs as root for radio
operations; installed CLI controls therefore use sudo. The broker refuses a
public directory, unrelated socket path, or duplicate service. A stale socket
is removed only when it belongs to the service user and no listener exists.
The local API trusts callers who can access this private socket, including
their requested capture/log paths. It is not a browser authentication layer.

## Shutdown and failures

An explicit stop interrupts the owner with SIGINT, including during hardware
startup, using existing `run_live` and `RadioSession` cleanup. Service SIGTERM
stops accepting requests, requests owner cleanup, drains the final publication,
and reaps the owner before removing its socket. The broker allows 95 seconds
for cleanup, and systemd allows 105 seconds for the service. A timeout forces
exit with an explicit error; restoration cannot be guaranteed after forced
termination, power loss, or driver failure.

Source EOF retains stopped state. Startup/decoder errors and unexpected owner
death retain failed state and an error; no replacement starts while that owner
is still running. Broker restart creates a fresh idle state. Loss of the broker
connection causes the owner to exit through its normal cleanup when it next
polls, rather than leaving an unattended capture. Logging-control errors are
reported without replacing the analyzer. Low-disk/rotation behavior remains
the existing shared logging policy. File export and download inventory are
later milestones.

## Web boundary and device acceptance

The next renderer will use **Utilization** at `/utilization/`. Use a separate
Flask backend with locally served assets and one-second JSON polling initially;
every handler calls this local service instead of constructing an analyzer.
Flask matches the existing WLAN Pi WebUI stack. Its
[production deployment guidance](https://flask.palletsprojects.com/en/stable/deploying/)
requires a dedicated server even for local production use. Capture remains in
the owner service, consistent with Flask's
[background task guidance](https://flask.palletsprojects.com/en/stable/async-await/#background-tasks).
SSE can follow if measured
polling overhead or update needs justify it. Browser startup, reconnect, and
navigation will have no implicit start/stop behavior. The backend's access to
the root-owned socket and the platform login adapter need a concrete
deployment decision before remote controls are exposed.

On the target Pi, record image/package versions, adapter, source namespace,
backend, band/width, revision, and diagnostic bundle. Check:

1. Fresh install and repeat install supply both preferred commands and legacy
   aliases, preserve FPMS settings, and start an idle service after reboot.
2. Start through managed CLI, open FPMS on the same source, and attach a second
   CLI. Verify one TShark/decoder/owner, matching current data/history, FPMS
   navigation/rotation, and logging status. Exit all displays and verify capture
   and logging remain active; reconnect and verify retained history.
3. Try another channel/backend while active. Verify a useful conflict and no
   retune. Toggle logging and verify the session ID/history persist. Stop
   capture through FPMS and CLI; verify owner exit and physical radio restoration.
4. Test SIGTERM/service restart, startup failure, source EOF, unavailable
   socket, optional Core owner shutdown, and low disk using the existing safe
   test procedures. Verify cleanup and error/status evidence.
5. Compare managed versus standalone under controlled high utilization. Record
   capture/decoder drops, publication lag, CPU/RAM, retry totals, and survey
   availability. Include reconnect and slow-client cases. Synthetic fixture
   cleanup markers establish process behavior, not physical radio restoration.
