# Core capture support contract

This is the implementation baseline for the 2.2.0 Core expansion phase,
reviewed October 30, 2026. Core remains opt-in until the
[roadmap acceptance gates](roadmap.md#ordered-core-milestones) pass. Utilization
still owns one decoder/analyzer, shared history, renderers, logging and export.

## Version and evidence policy

The minimum device-tested Core baseline is **2.4.8-1**. Earlier packages are
outside this integration's declared support contract. This is a documentation
boundary, not a new package-version check in the live runtime. Support also
requires the protocol, source and tuning checks below to pass; a package number
alone is insufficient.

| Core baseline | Evidence | Acceptance boundary |
| --- | --- | --- |
| 2.4.8-1, revision `7e858ff` | Original source review; device owner captures at 2412/20 and 5200/20 with confirmed stop/restoration | Existing optional owner baseline; broader widths/failures remain pending |
| Changelog 2.4.13, revision `63c5e7e8675b81e8571bd7647d7119c64aa9972d` | Current upstream source reviewed October 30 | Source-compatible candidate; installed-image capture/lifecycle acceptance pending |
| Other revisions | Require protocol/capability checks and a recorded device run | Do not infer acceptance from a higher version |

The current review covers the
[WebSocket endpoint](https://github.com/WLAN-Pi/wlanpi-core/blob/63c5e7e8675b81e8571bd7647d7119c64aa9972d/wlanpi_core/api/api_v1/endpoints/streaming_api.py),
[capture models](https://github.com/WLAN-Pi/wlanpi-core/blob/63c5e7e8675b81e8571bd7647d7119c64aa9972d/wlanpi_core/streaming/models.py),
[connection manager](https://github.com/WLAN-Pi/wlanpi-core/blob/63c5e7e8675b81e8571bd7647d7119c64aa9972d/wlanpi_core/streaming/connection_manager.py),
[getjwt client](https://github.com/WLAN-Pi/wlanpi-core/blob/63c5e7e8675b81e8571bd7647d7119c64aa9972d/wlanpi_core/cli/getjwt.py)
and [layout schema](https://github.com/WLAN-Pi/wlanpi-core/blob/63c5e7e8675b81e8571bd7647d7119c64aa9972d/wlanpi_core/schemas/network/config_status.py).
Core's proposed REST capture API is
[still marked design, not implemented](https://github.com/WLAN-Pi/wlanpi-core/blob/63c5e7e8675b81e8571bd7647d7119c64aa9972d/docs/api/wifi-capture-consumer-guide.md).

The capture protocol, interface-name restriction, width schema and center-mapping
limitations described below remain in this reviewed revision. Local Core
installation details must be recorded in the [device matrix](core-device-matrix.md).

## Protocol and authentication

- Production endpoint: `wss://localhost:31415/api/v1/streaming/capture`. The
  current capture backend accepts local endpoints only; remote capture is outside
  this contract. Loopback `ws` is a development/test option.
- Trust `/etc/nginx/ssl/self-signed-wlanpi.cert` with hostname and validity checks.
  Missing/untrusted/expired certificates fail; no verification bypass or
  cleartext fallback is allowed in production.
- Obtain a JWT through `getjwt wlanpi-utilization-owner --no-color`, capturing
  its output in memory. Core's getjwt performs local HMAC authentication; the
  app does not read the shared secret itself. The first WebSocket message is
  `auth` with that JWT. Do not put credentials in URLs, command arguments, logs
  or evidence bundles. The metadata probe uses a separate evaluation identity.
- The app installs `websockets==15.0.1` in its isolated environment. Core's own
  Python environment and dependency version are independent.
- Authenticate, validate layout, list sessions and request supported frequencies
  before local preparation. Existing owners are refused in this milestone.
  Configure one source/channel, start with an explicitly empty `pcap_filter`,
  and verify returned source, namespace and configuration.
- Preserve arbitrary binary PCAPNG chunk boundaries and byte order through the
  single decoder. An initial `CHANNEL_SET_FAILED`, configuration mismatch,
  unexpected end or bounded pipe-write failure stops the owned capture.
- Core capture belongs to its owning socket. Send `stop` and confirm
  `CAPTURE_STOPPED` before guarded local restoration; disconnect is not evidence
  that saved radio state was restored. No automatic reconnect or direct fallback.

## Source and channel boundary

| Property | Current Utilization Core contract | Upstream or future work |
| --- | --- | --- |
| Interface | One existing monitor named `wlanpi` plus 1–3 digits | Arbitrary interface names need Core API support |
| Namespace | Root or one named namespace; local target and Core layout must agree uniquely | No combining multiple source namespaces |
| Channel | One fixed primary frequency; no hopping; all decoded frame types delivered | Filtered/hopping/multi-source streams are incompatible |
| Bands | 2.4, 5 and 6 GHz subject to device frequencies, driver and regulatory permission | Tri-band schema availability does not establish reception quality |
| Width | App accepts 20 or 80 MHz, with verified actual centers | Core schema accepts 40/160 too, but app acceptance is not implemented |
| Split/punctured/wider capture | Rejected | Explicit centers/direction, 80+80, 320 and puncturing need appropriate API support and acceptance |
| Existing owner | Refused without stopping or reconfiguring it | Compatible read-only subscription is milestone 4 |

For 80 MHz, the current app accepts representable primaries in 5180–5720 MHz
at 20 MHz steps, 5745 and 5825 MHz, and 5955–7055 MHz at 20 MHz steps, subject
to the standard channel definition and exact center match. Core's upper-5-GHz
lookup still lacks 5765/5785/5805 MHz (channels 153/157/161) at 80 MHz. Explicit
20 MHz remains an option there. Do not silently retune directly to bypass Core.

Both supported-frequency enumeration and actual local `iw` readback must pass.
`CAPTURE_STARTED` echoes requested configuration and is insufficient proof of
actual tuning. Adapter/driver limits can still omit frames, notably on 6 GHz;
an unavailable loss counter is not zero. Core transport does not improve RF
reception or change the retry/QBSS definitions.

## Local radio responsibilities

Retain `RadioSession` and local capability, conflict and identity checks. Save
original state, prepare only idle siblings, verify actual tuning and restore
known modes, administrative UP flags and monitor definitions after Core releases
capture. Keep local survey reads in the selected namespace and separate from
AP QBSS CU. Do not move a radio between namespaces for capture.

The reviewed Core interface registry is not a whole-phy lease shared by all
controllers. It provides neither a saved-state restoration transaction nor a
survey-counter API. Its layout channel string can inform future readback but
does not contain every identity/administrative-state field the guard needs.
SIGKILL, service failure, power loss and adapter removal have explicit recovery
limits; retain evidence rather than claim guaranteed restoration.

## App work and upstream work

App milestones cover backend-aware setup/validation, consistent defaults,
installed-version evidence, compatible subscriptions, lifecycle/metric testing
and migration. Upstream dependencies cover missing channel geometry and fuller
radio reservation/preparation/restoration/survey services. The latter can proceed
alongside Core-default capture while the app retains its local safeguards.

Use `scripts/run_ci.py` for the hardware-free baseline and the
[device matrix](core-device-matrix.md) for physical evidence. A passing mock
transport or source review does not complete the installed-device gate.

## Milestone 1 validation

The strict baseline runs locally with Python 3.13.3, TShark 4.6.0, Nginx 1.28.3
with TLS/auth_request, websockets 15.0.1 and Node 22.23.4. All four decoder
fixtures, **987 pytest cases in 43.47 seconds** including real loopback Core
transport and protected Nginx, and both frontend logic modes pass without skips.
The two existing direct-worker fork deprecation warnings remain.

The GitHub workflow runs this same gate in Debian Trixie and retains versions
and JUnit evidence. Its first hosted run awaits a push/PR. Current-image Pi
inventory, authentication and physical capture acceptance remain pending in the
device matrix; the local run uses mocks and synthetic packets without a radio.
