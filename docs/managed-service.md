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

`--managed` atomically opens the active session or starts one if idle. An
existing session wins over the command's requested source/tuning: the CLI
reports **Session already running** and displays the actual owner configuration,
session ID, logs and history. Source changes still require an explicit stop
and new start. Logging paths and limits belong to the initial owner;
display-only attachment cannot replace them or disable logging. An explicit
logging request can enable logging. `--attach` only reads the current owner
and never starts one. A plain `sudo wlanpi-utilization` also attaches when the
managed broker has an active session; otherwise it retains foreground capture.
Use `--standalone` to request foreground capture explicitly. Radio exclusion
still prevents that mode from acquiring a radio already owned by a session.

The broker's strict `start`/compatible `ensure` APIs continue rejecting
conflicting source requests. The consumer `join` operation preserves an active
owner atomically, including concurrent opens with different requested channels.

Exiting a managed CLI view with Q/Ctrl+C or leaving the FPMS display detaches
that renderer. Capture and active logging continue with zero renderers. FPMS
managed mode adds **Open Running Session** above band selection and **Stop
Session** to each channel menu. Opening a display through a different channel
choice still loads the current source and reports that it is already running.
The resume-only entry cannot create a replacement session. **Stop Logging**
disables logging without ending capture. CLI controls require the actual
`session_id` value from status, not the literal placeholder `SESSION_ID`.
While a session is active, read its ID automatically before using the desired
control below. Repeat the assignment after a new session starts.

```bash
UTILIZATION_SESSION_ID="$(sudo wlanpi-utilization session status --summary \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["session_id"] or "")')"
sudo wlanpi-utilization session logging --session-id "$UTILIZATION_SESSION_ID" --enabled yes --summary
sudo wlanpi-utilization session logging --session-id "$UTILIZATION_SESSION_ID" --enabled no --summary
sudo wlanpi-utilization session stop --session-id "$UTILIZATION_SESSION_ID" --summary
```

`Stale Utilization session ID` means the supplied value does not match the
current owner. This check prevents an old command from stopping a newer
capture. Read status again and use its current ID; no reinstall is needed to
correct a placeholder.

Stopping is asynchronous. Poll status until stopped/failed before starting
another owner. The final publication can precede process reaping by a short
interval; a new start remains blocked until cleanup and reaping finish. Final
history stays readable until a new explicit start. Stale control IDs fail.

Foreground capture stops on exit; managed attachment only detaches. Select
`--fpms-runtime standalone` to
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
the existing shared logging policy. Closed CSV/JSONL segments now enter a
bounded download catalog; beacon pcapng export remains a later milestone.

## Web boundary and device acceptance

The web renderer now uses **Utilization** at `/utilization/`, with a separate
Flask/Waitress backend, local assets, and one-second JSON polling shared across
HTTP threads. Read handlers consume this local service; explicit controls use
the broker. Browser startup, reconnect, and navigation have no implicit
start/stop behavior. `sudo wlanpi-utilization web` accesses the private broker
as root and listens on loopback, suitable for a local browser or SSH tunnel.
See [web installation and browser checks](web-interface.md). SSE remains
optional if measured polling overhead justifies it. The optional platform login
adapter and private Unix backend are described in [protected deployment](web-deployment.md).
Page loads embed the current cached publication before the first status poll,
without a start/control operation. Publications now include capture tuning and
its verification flag so consumers can show actual width/center frequencies.

On the target Pi, record image/package versions, adapter, source namespace,
backend, band/width, revision, and diagnostic bundle. Check:

1. Fresh install and repeat install supply both preferred commands and legacy
   aliases, preserve FPMS settings, and start an idle service after reboot.
2. Start through managed CLI, open FPMS on the same source, and attach a second
   CLI. Verify one TShark/decoder/owner, matching current data/history, FPMS
   navigation/rotation, and logging status. Exit all displays and verify capture
   and logging remain active; reconnect and verify retained history.
3. Open CLI/managed FPMS with another channel while active. Verify the
   already-running message, actual current tuning and no retune. Explicit
   conflicting API starts must still fail. Toggle logging and verify the
   session ID/history persist. Stop
   capture through FPMS and CLI; verify owner exit and physical radio restoration.
4. Test SIGTERM/service restart, startup failure, source EOF, unavailable
   socket, optional Core owner shutdown, and low disk using the existing safe
   test procedures. Verify cleanup and error/status evidence.
5. Compare managed versus standalone under controlled high utilization. Record
   capture/decoder drops, publication lag, CPU/RAM, retry totals, and survey
   availability. Include reconnect and slow-client cases. Synthetic fixture
   cleanup markers establish process behavior, not physical radio restoration.
