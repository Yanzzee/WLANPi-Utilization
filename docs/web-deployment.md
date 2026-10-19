# WLAN Pi Apps and authenticated Utilization hosting

The optional integration adds **Utilization** to WLAN Pi's Apps menu and serves
the existing application at `https://<your-pi>/utilization/`. It uses WLAN Pi's
existing login, session revocation, idle expiry and maximum session age.
Utilization remains a separate backend and managed capture consumer; no custom
Core endpoint or additional analyzer is introduced.

Implementation and local regression checks are complete. Installed Pi login,
systemd, reboot and browser acceptance below remain required before release.

## Install on the Pi

From the updated `~/WLANPi-Utilization` checkout, update the application and its
optional web dependencies first:

```bash
sudo ./scripts/install_wlanpi_fpms.sh --fpms-runtime managed --with-web
```

That application installer restarts the capture broker and FPMS. Stop any
active capture normally before this application update. If a manual
`sudo wlanpi-utilization web` process is running, stop it with Ctrl-C.
The protected web service replaces that manual process for direct hosting.

Validate the installed WebUI sources and proposed changes, then install:

```bash
sudo python3 scripts/install_web_integration.py --check
sudo python3 scripts/install_web_integration.py
```

`--check` reads files and prepares the proposal without writing files or
operating services. Installation also checks the app dependencies, existing
Nginx syntax, the `www-data` Nginx worker identity and WebUI graceful-reload
support before changing files. An unsupported platform revision stops with an
error; collect `sudo python3 scripts/pi_web_preflight.py` for review rather
than editing the authentication checks.

From another computer on the same reachable network, open
`https://wlanpi-r5.local/`, sign in normally, and choose **Apps → Utilization**.
The direct address is `https://wlanpi-r5.local/utilization/`; substitute the Pi's
hostname or IP address as appropriate. This uses the platform's existing TLS
certificate and requires no SSH tunnel. An on-device browser can use the same
application through the Pi's HTTPS address.

The integration installer starts/enables `wlanpi-utilization-web.service` and
gracefully reloads WebUI and Nginx. It does not stop/restart the capture broker,
FPMS or an active capture, and never retunes the radio. Its web unit can start
the broker if it is absent; the broker stays idle until an explicit capture
start. Reinstallation updates the managed route/card without duplicating them
and preserves the private proxy token and custom Apps cards outside the
managed block. Initial copies of modified template/config files use the suffix
`.utilization-web.bak`. Failed syntax, startup, readiness or reload checks
restore pre-install files and web service state; a recovery failure is reported
separately if manual attention is required.

Default paths match the supplied Pi 5 preflight:

| Component | Path |
| --- | --- |
| WebUI package | `/opt/wlanpi-webui/workingdirectory` |
| Nginx site | `/etc/wlanpi-webui/nginx/sites-enabled/wlanpi_webui.conf` |
| Platform service | `wlanpi-webui.service` |
| Utilization web service | `/etc/systemd/system/wlanpi-utilization-web.service` |
| Private web listener | `/run/wlanpi-utilization-web/web.sock` |
| Proxy token and header include | `/etc/wlanpi-beacon-live/web-proxy.token`, `web-proxy.headers` |

`--webui-root`, `--nginx-config` and `--webui-service` support path/unit
differences, including an older `wlanpi_webui.service` name when appropriate.
They do not bypass source validation. The supported auth-module and base Nginx
hashes match the user's Pi preflight. Both the supplied Pi `app.py` hash and
reviewed upstream `2b112f5a739e4647de4f5fd48948cd16e4d07d94` are accepted,
with additional AST checks on the session-freshness and login gates. A later
WebUI package upgrade may require review before reapplying this integration.

## Authentication and process boundary

Every page, asset, API control/status response and download passes Nginx's
existing internal `/auth/check` subrequest. API expiry/revocation returns 401
JSON; the page disables controls, retains the last received values and offers
the normal WLAN Pi sign-in link. After signing in, polling reconnects to the
same capture session. Signing out or closing all browsers leaves capture and
logging running.

Passive polling does not refresh the platform's idle timeout. Actual pointer
or keyboard activity refreshes the normal platform cookie through `HEAD /apps`,
at most once per minute; controls also check this activity path. The existing
platform gates still enforce revocation, idle expiry and maximum age. No
Utilization login store, password handler or session lifetime is added.

The privileged broker socket stays root-only. The root web service accesses
that broker and registered log files, with an empty capability set, no network
address families other than Unix sockets, and a restricted filesystem view.
Its web socket is root:`www-data`, mode 0660 inside a 0750 runtime directory.
Nginx supplies a private token from root-only files on every proxied request;
the backend rejects missing/incorrect tokens even for assets/downloads.
Browser-provided token and forwarded headers are overwritten/cleared. Controls
also require the current per-process CSRF token and matching HTTPS origin.
Restarting the web service requires reloading open pages to renew CSRF tokens;
it does not restart capture. No TCP backend listener is enabled in this mode.

The original `sudo wlanpi-utilization web` loopback mode and SSH access remain
available separately; see [web operation](web-interface.md). Direct capture
remains the default in both modes, with Core optional.

## Remove or diagnose

```bash
sudo systemctl status wlanpi-utilization-web --no-pager
sudo journalctl -u wlanpi-utilization-web -u wlanpi-webui -u nginx \
  --since "15 minutes ago" --no-pager
sudo python3 scripts/pi_web_preflight.py
```

The preflight reports paths/hashes without private tokens or session contents.
The private Nginx header include contains a credential and should not be shared
as diagnostic output.

To remove the integration:

```bash
sudo python3 scripts/install_web_integration.py --remove --check
sudo python3 scripts/install_web_integration.py --remove
```

Removal deletes the managed route/card/web unit, disables/stops only the web
service, and reloads the platform services. It preserves capture, logs, original
backups, and the private token/header files for reinstallation. Custom Apps
cards outside the managed block remain intact.

## Installed Pi acceptance

Record checkout revision, Pi image, WebUI/Nginx versions, adapter/driver,
browser, direct/Core backend and session ID alongside a diagnostic bundle.

1. Run `--check`, install, and confirm `systemctl is-active
   wlanpi-utilization-web` succeeds. Open through normal HTTPS login and Apps
   with no SSH tunnel. An idle broker must remain idle on page load.
2. Start or attach to one managed session through web, CLI and FPMS. Reload,
   open another tab, navigate back to Apps, close all browsers and reconnect.
   Session ID/history/logging and the single capture owner must persist.
3. Verify a signed-out browser cannot retrieve metrics, control capture or
   download a closed CSV/JSONL file. While capturing, sign out in another tab:
   the existing page must show sign-in required and disabled controls. A
   revoked copied cookie must also fail; logging/capture must continue.
4. Test actual idle and maximum-age expiry using the installed platform's
   policy. Passive polling must not prolong login. Normal interaction before
   expiry should refresh the idle cookie; signing in again must recover the
   same session without reissuing controls. Verify phone/desktop layouts and
   the sign-in link in the actual browsers.
5. Reinstall the integration while capturing: confirm one Apps card and route,
   unchanged capture ID and working downloads. Restart only the web service;
   reload the page to renew CSRF and confirm retained history. Verify missing
   backend/auth service fails closed, then recovers without capture restart.
6. Check enabled web service and Apps access after reboot; radio restoration
   and broker session persistence follow the existing managed-service policy
   (an in-memory capture session does not survive reboot). Test removal and
   reinstallation, preserving other Apps cards and capture when applicable.

Local tests exercise the production Nginx fragment over verified TLS, real
Waitress Unix listeners and the real managed broker with a synthetic capture
owner. A controlled auth checker supplies login/revocation/expiry outcomes;
this is not a test of installed PAM, WebUI cookies or systemd on the Pi.
Installer tests cover repeat installs, custom cards, preconditions and rollback
after syntax/start/readiness/reload failures, including failed removal.
