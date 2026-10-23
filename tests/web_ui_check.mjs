// Hardware-free frontend contract checks. This simulates a DOM; it is not
// rendered-browser acceptance. Inputs come from fixtures/web_preview.py --export.
import assert from "node:assert/strict";
import vm from "node:vm";
import {readFile} from "node:fs/promises";
import {pathToFileURL} from "node:url";

export async function checkWebUi({html, source, status}) {
  const data = structuredClone(status);
  const [qbss, retry, station] = data.publication.snapshot.bssids;
  assert.equal(data.publication.snapshot.selected_bssid, qbss.bssid);
  assert.equal(data.publication.snapshot.top_station_bssid, station.bssid);
  assert.equal(data.publication.snapshot.top_retry_bssid, retry.bssid);
  qbss.latest_ap_name = "Zulu"; retry.latest_ap_name = "Alpha"; station.latest_ap_name = null;
  const text = element => element.textContent + element.children.map(text).join(" ");

  async function boot(hosted) {
    const elements = new Map(), docEvents = {}, calls = [], timers = new Map();
    let sequence = 0, now = 100000, value = structuredClone(data), statusCode = 200, headCode = 200;
    const availableInterfaces = [
      {id: "root:wlan0", interface: "wlan0", namespace: null, phy: "phy0", mode: "managed"},
      {id: "lab:mon0", interface: "mon0", namespace: "lab", phy: "phy1", mode: "monitor"},
    ];
    let interfaceInventory = availableInterfaces;
    class Element {
      constructor(id = "") {
        this.id = id; this.dataset = {}; this.events = {}; this.children = [];
        this.textContent = ""; this.hidden = false; this.disabled = false;
        this.attributes = {}; this.classList = {toggle() {}};
      }
      append(...nodes) {this.children.push(...nodes); for (const child of nodes) if (child.id) elements.set(child.id, child);}
      replaceChildren(...nodes) {this.children = []; this.append(...nodes);}
      setAttribute(key, val) {this.attributes[key] = val;}
      removeAttribute(key) {delete this.attributes[key];}
      addEventListener(key, fn) {this.events[key] = fn;}
      showModal() {this.open = true;}
      close() {this.open = false;}
    }
    for (const match of html.matchAll(/id="([^"]+)"/g)) elements.set(match[1], new Element(match[1]));
    elements.get("initial-status").textContent = JSON.stringify(value);
    elements.get("capture-options").textContent = html.match(/<script id="capture-options" type="application\/json">(.*?)<\/script>/s)[1];
    elements.get("band").value = "2.4"; elements.get("channel").value = "1";
    elements.get("channel-width").value = ""; elements.get("iface").value = "auto"; elements.get("netns").value = "";
    for (const id of ["bssids", "hosts", "logs"]) elements.get(id).hidden = true;
    elements.get("channel").dataset.channels = html.match(/data-channels='([^']+)'/)[1];
    const sortButtons = Array.from(html.matchAll(/data-sort="([^"]+)"/g), match => {
      const button = new Element(); button.dataset.sort = match[1]; button.parentElement = new Element(); return button;
    });
    const hostSortButtons = Array.from(html.matchAll(/data-host-sort="([^"]+)"/g), match => {
      const button = new Element(); button.dataset.hostSort = match[1]; button.parentElement = new Element(); return button;
    });
    const tabs = Array.from(html.matchAll(/data-tab="([^"]+)"/g), match => {
      const button = new Element(); button.dataset.tab = match[1]; return button;
    });
    const panels = ["overview", "bssids", "hosts", "logs"].map(id => elements.get(id));
    const metas = {"utilization-api": "/utilization/api/", "utilization-hosted": hosted ? "yes" : "no",
      "utilization-csrf": html.match(/name="utilization-csrf" content="([^"]+)"/)[1]};
    const sandbox = {
      document: {
        getElementById: id => elements.get(id),
        querySelector: selector => ({content: metas[selector.match(/name="([^"]+)"/)?.[1]]}),
        querySelectorAll: selector => selector === "[data-sort]" ? sortButtons : selector === "[data-host-sort]" ? hostSortButtons : selector === "[data-tab]" ? tabs : selector === ".panel" ? panels : [],
        createElement: () => new Element(), createElementNS: () => new Element(),
        createTextNode: content => Object.assign(new Element(), {textContent: content}),
        addEventListener: (event, fn) => docEvents[event] = fn,
      },
      Intl, Map, Set, AbortController,
      FormData: class {
        get(key) {
          const id = {channel_width: "channel-width", center_frequency1_mhz: "center-one", center_frequency2_mhz: "center-two-choice"}[key] || key;
          return key === "capture_backend" ? "direct" : elements.get(id)?.value;
        }
        has(key) {return key === "logging_enabled" ? elements.get("start-logging").checked : key === "export_enabled" && elements.get("start-export").checked;}
      },
      Date: class extends Date {
        constructor(...args) {super(...(args.length ? args : [Date.UTC(2026, 9, 21, 19, 4, 5)]));}
        static now() {return now;}
        toLocaleTimeString(locales, options) {return super.toLocaleTimeString("en-US", {...options, timeZone: "UTC"});}
      },
      setTimeout: (fn, ms) => {timers.set(++sequence, {fn, ms}); return sequence;},
      clearTimeout: id => timers.delete(id),
      fetch: async (url, options) => {
        calls.push({url, options});
        const code = url === "/apps" ? headCode : statusCode;
        if (code === 200 && options.method === "POST" && url.endsWith("session/start")) {
          value.publication.phase = "starting"; value.publication.revision++;
          value.publication.logging.active = JSON.parse(options.body).logging_enabled;
          value.publication.beacon_export.requested = JSON.parse(options.body).export_enabled;
        }
        if (code === 200 && options.method === "POST" && url.endsWith("/export")) {
          value.publication.beacon_export.requested = JSON.parse(options.body).enabled;
          value.publication.beacon_export.active = JSON.parse(options.body).enabled;
          value.publication.revision++;
        }
        if (code === 200 && options.method === "POST" && url.endsWith("session/stop")) {
          value.publication.phase = "stopping"; value.publication.revision++;
        }
        return {status: code, type: code === 302 ? "opaqueredirect" : "basic", ok: code === 200,
          json: async () => code !== 200 ? {error: "Fixture service error"}
            : url.endsWith("files") ? {files: []} : url.endsWith("interfaces") ? {interfaces: interfaceInventory, errors: []} : structuredClone(value)};
      },
    };
    const settle = async () => {for (let i = 0; i < 20; i++) await Promise.resolve();};
    vm.runInNewContext(source, sandbox);
    // Embedded state must render before the first asynchronous status response.
    assert.equal(elements.get("phase").textContent, "Running");
    assert.equal(elements.get("updated").textContent, "Received 19:04:05");
    assert.match(elements.get("session-detail").textContent, /Session already running/);
    assert.match(elements.get("session-id").textContent, new RegExp(value.publication.session_id));
    assert.equal(elements.get("retries-value").textContent, "<1%");
    assert.equal(elements.get("session-toggle").disabled, false);
    assert.equal(elements.get("session-toggle").textContent, "Stop session");
    assert.equal(elements.get("session-toggle").className, "button danger");
    assert.equal(elements.get("logging-toggle").textContent, "Stop logging");
    assert.equal(elements.has("stop"), false);
    assert.equal(elements.has("new-session"), false);
    assert.equal(elements.get("graphs").children.length, 6);
    assert.equal(elements.has("local-cu-value"), false);
    assert.equal(elements.has("bssids-value"), false);
    assert.match(text(elements.get("graphs")), /Observed active clients/);
    assert.doesNotMatch(text(elements.get("graphs")), /Local survey/);
    assert.match(elements.get("composition-current").textContent, new RegExp(value.publication.snapshot.composition.displayed_bssid));
    assert.match(elements.get("composition-current").textContent, new RegExp(value.publication.snapshot.composition.displayed_ssid));
    assert.equal(elements.get("radio-bssids").children.length, 2);
    assert.equal(elements.get("radio-bssids").children[0].className, "qbss-source");
    const selections = elements.get("selected-bssids").children;
    assert.match(text(selections[0]), new RegExp(qbss.bssid));
    assert.match(text(selections[1]), new RegExp(station.bssid));
    assert.match(text(selections[2]), new RegExp(retry.bssid));
    assert.match(text(selections[1]), /Advertised QBSS/);
    await settle();
    assert.equal(calls.some(call => call.url === "/apps" || call.options.method === "POST"), false);
    assert.equal(calls.some(call => call.url.endsWith("interfaces")), false);
    assert.match(elements.get("export-policy").textContent, /no file-count limit.*no total-size limit/);
    assert.match(elements.get("export-policy").textContent, /1 hour or 256 MiB/);
    assert.ok(html.includes("Strongest radio"));
    assert.ok(!html.includes("Strongest estimated radio"));
    assert.equal(elements.get("channel-width").children[0].textContent, "Band default (20 MHz)");
    assert.deepEqual(elements.get("channel-width").children.map(option => option.value), ["", "20", "40"]);

    // Hosts are rendered on demand from the same publication; navigation sends
    // no control, performs no capture work and does not alter graph history.
    const history = structuredClone(value.publication.snapshot.history);
    const originalSession = value.publication.session_id;
    assert.equal(elements.get("host-rows").children.length, 0);
    const beforeTabs = calls.length;
    tabs.find(button => button.dataset.tab === "hosts").events.click();
    assert.equal(elements.get("hosts").hidden, false);
    assert.equal(elements.get("overview").hidden, true);
    assert.equal(elements.get("host-rows").children.length, value.publication.snapshot.hosts.length);
    assert.match(text(elements.get("host-rows")), /Example Client/);
    assert.match(text(elements.get("host-rows")), /Locally administered/);
    const clientRow = mac => elements.get("host-rows").children.find(row => row.children[0].textContent === mac);
    const rxOnly = clientRow("00:10:00:00:00:99");
    assert.equal(rxOnly.children[1].textContent, "Unknown");
    assert.equal(rxOnly.children[2].textContent, "Classroom East");
    assert.equal(rxOnly.children[3].textContent, qbss.bssid);
    assert.match(rxOnly.children[4].textContent, /^\d\d:\d\d:\d\d$/);
    assert.equal(rxOnly.children[5].textContent, "—");
    assert.equal(rxOnly.children[6].textContent, "0");
    assert.equal(rxOnly.children[8].textContent, "—");
    const transmitting = clientRow("00:10:00:00:00:01");
    assert.equal(transmitting.children[5].textContent, "-55 dBm");
    assert.equal(transmitting.children.length, 9);
    const hostMacSort = hostSortButtons.find(button => button.dataset.hostSort === "mac");
    hostMacSort.events.click();
    assert.equal(elements.get("host-rows").children[0].children[0].textContent, "00:10:00:00:00:01");
    assert.equal(hostMacSort.parentElement.attributes["aria-sort"], "ascending");
    hostMacSort.events.click();
    assert.equal(hostMacSort.parentElement.attributes["aria-sort"], "descending");
    tabs.find(button => button.dataset.tab === "overview").events.click();
    assert.equal(elements.get("hosts").hidden, true);
    assert.equal(calls.length, beforeTabs);
    assert.equal(value.publication.session_id, originalSession);
    assert.deepEqual(value.publication.snapshot.history, history);

    const apSort = sortButtons.find(button => button.dataset.sort === "latest_ap_name");
    apSort.events.click();
    assert.match(text(elements.get("bssid-rows").children[0]), new RegExp(retry.bssid));
    assert.match(text(elements.get("bssid-rows").children.at(-1)), new RegExp(station.bssid));
    apSort.events.click();
    assert.match(text(elements.get("bssid-rows").children[0]), new RegExp(qbss.bssid));
    assert.equal(value.publication.snapshot.selected_bssid, qbss.bssid);
    elements.get("band").value = "6"; elements.get("band").events.change();
    elements.get("channel-width").value = "20"; elements.get("channel-width").events.change();
    const choices = elements.get("channel").children;
    assert.equal(choices.length, 59);
    assert.equal(choices.find(option => option.value === "5").textContent, "5 (PSC)");
    assert.equal(choices.find(option => option.value === "1").textContent, "1");
    assert.equal(choices.filter(option => option.textContent.includes("PSC")).length, 15);
    elements.get("band").value = "2.4"; elements.get("band").events.change();
    assert.equal(elements.get("channel").children.some(option => option.textContent.includes("PSC")), false);
    elements.get("channel").value = "6"; elements.get("channel").events.change();
    elements.get("channel-width").value = "40"; elements.get("channel-width").events.change();
    assert.equal(elements.get("channel").value, "6:2427");
    assert.equal(elements.get("channel-label").textContent, "Primary + secondary channel");
    assert.equal(elements.get("channel").children.some(option => option.value === "14"), false);
    assert.equal(elements.get("channel").children.find(option => option.value === "6:2447").textContent, "6 + 10 · center 2447 MHz (Ch 8)");
    assert.equal(elements.get("centers").hidden, true);
    assert.equal(elements.get("center-one").value, "2427");
    elements.get("channel").value = "6:2447"; elements.get("channel").events.change();
    assert.equal(elements.get("center-one").value, "2447");
    assert.equal(elements.get("center-one-field").hidden, true);
    elements.get("channel-width").value = "20"; elements.get("channel-width").events.change();
    assert.equal(elements.get("channel").value, "6");
    elements.get("channel").value = "14"; elements.get("channel").events.change();
    elements.get("channel-width").value = "40"; elements.get("channel-width").events.change();
    assert.equal(elements.get("channel").value, "1:2422");
    elements.get("band").value = "5"; elements.get("band").events.change();
    assert.equal(elements.get("channel-width").children[0].textContent, "Band default (80 MHz)");
    assert.equal(elements.get("channel-width").children.some(option => option.value === "320"), false);
    elements.get("channel-width").value = "80"; elements.get("channel-width").events.change();
    assert.deepEqual(elements.get("center-one").children.map(option => option.value), ["5210"]);
    assert.equal(elements.get("centers").hidden, true);
    assert.equal(elements.get("channel-label").textContent, "Primary channel");
    elements.get("band").value = "6"; elements.get("band").events.change();
    elements.get("channel-width").value = "320"; elements.get("channel-width").events.change();
    elements.get("channel").value = "65"; elements.get("channel").events.change();
    assert.deepEqual(elements.get("center-one").children.map(option => option.value), ["6265", "6425"]);
    assert.equal(elements.get("centers").hidden, false);
    assert.equal(elements.get("center-one-field").hidden, false);
    elements.get("channel").value = "1"; elements.get("channel").events.change();
    assert.equal(elements.get("centers").hidden, true);
    elements.get("channel-width").value = "80+80"; elements.get("channel-width").events.change();
    assert.equal(elements.get("center-one-field").hidden, true);
    assert.equal(elements.get("center-two").hidden, false);
    assert.equal(elements.get("centers").hidden, false);
    assert.ok(elements.get("center-two-choice").children.every(option => Math.abs(Number(option.value) - Number(elements.get("center-one").value)) > 80));
    elements.get("channel-width").value = "20"; elements.get("channel-width").events.change();
    elements.get("channel").value = "233"; elements.get("channel").events.change();
    elements.get("channel-width").value = ""; elements.get("channel-width").events.change();
    assert.equal(elements.get("channel-width").children[0].textContent, "Band default (80 MHz)");
    assert.equal(elements.get("channel").children.some(option => option.value === "233"), false);
    assert.equal(elements.get("channel").value, "5");
    elements.get("band").value = "2.4"; elements.get("band").events.change();
    elements.get("channel-width").value = "20"; elements.get("channel-width").events.change();

    async function poll() {
      const entry = [...timers].find(([, timer]) => timer.ms !== 6000);
      assert.ok(entry, "next status poll scheduled");
      timers.delete(entry[0]); await entry[1].fn(); await settle();
    }
    tabs.find(button => button.dataset.tab === "hosts").events.click();
    value.publication.snapshot.hosts[0].tx_retry_percent = .25;
    value.publication.snapshot.host_details_limited = true;
    value.publication.revision++; await poll();
    assert.equal(elements.get("host-limit").hidden, false);
    assert.match(text(elements.get("host-rows")), /<1%/);
    value.publication.snapshot.hosts = [];
    value.publication.snapshot.window_unique_client_mac_count = 0;
    value.publication.snapshot.host_details_limited = false;
    value.publication.revision++; await poll();
    assert.equal(elements.get("host-empty").hidden, false);
    assert.equal(elements.get("host-rows").children.length, 0);
    assert.equal(elements.get("host-limit").hidden, true);
    assert.equal(elements.get("host-count").textContent, "0");
    tabs.find(button => button.dataset.tab === "overview").events.click();
    value.publication.revision++;
    value.publication.capture_definition = {...value.publication.capture_definition, width: "80+80", center_frequency2_mhz: 5530};
    await poll();
    assert.match(elements.get("tuning").textContent, /80\+80 MHz/);
    assert.match(elements.get("session-detail").textContent, /Center 5210 \/ 5530 MHz/);
    assert.doesNotMatch(elements.get("tuning").textContent, /MHz MHz/);
    value.publication.capture_target = {interface: "mon0", namespace: "capture"};
    value.publication.revision++; await poll();
    assert.match(elements.get("session-detail").textContent, /mon0 · Namespace capture/);
    assert.doesNotMatch(elements.get("session-detail").textContent, /Automatic interface/);
    const metrics = elements.get("ap-cu-value").textContent;
    statusCode = 503; await poll();
    assert.equal(elements.get("session-toggle").disabled, true);
    assert.equal(elements.get("logging-toggle").disabled, true);
    assert.equal(elements.get("export-toggle").disabled, true);
    assert.equal(elements.get("export-toggle-files").disabled, true);
    assert.equal(elements.get("ap-cu-value").textContent, metrics);
    statusCode = 401; await poll();
    assert.equal(elements.get("login-required").hidden, false);
    statusCode = 200; await poll();
    assert.equal(elements.get("login-required").hidden, true);
    assert.equal(elements.get("session-toggle").disabled, false);
    assert.equal(value.publication.session_id, data.publication.session_id);

    await elements.get("logging-toggle").events.click(); await settle();
    const post = calls.find(call => call.options.method === "POST");
    assert.equal(JSON.parse(post.options.body).session_id, value.publication.session_id);
    assert.equal(JSON.parse(post.options.body).enabled, false);
    assert.equal(post.options.headers["X-Utilization-CSRF"], metas["utilization-csrf"]);
    assert.equal(calls.filter(call => call.url === "/apps").length, hosted ? 1 : 0);
    value.publication.logging.active = false; value.publication.revision++;
    await poll();
    assert.equal(elements.get("logging-toggle").disabled, false);
    assert.equal(elements.get("logging-toggle").textContent, "Start logging");
    await elements.get("logging-toggle").events.click(); await settle();
    assert.equal(JSON.parse(calls.filter(call => call.url.endsWith("logging")).at(-1).options.body).enabled, true);
    value.publication.logging.active = true; value.publication.revision++;
    await poll();
    assert.equal(elements.get("logging-toggle").textContent, "Stop logging");
    statusCode = 409;
    await elements.get("logging-toggle").events.click(); await settle();
    assert.equal(elements.get("logging-toggle").textContent, "Stop logging");
    assert.match(elements.get("error").textContent, /Fixture service error/);
    statusCode = 200;
    assert.equal(elements.get("export-toggle").disabled, false);
    assert.equal(elements.get("export-toggle").textContent, "Start beacon export");
    assert.equal(elements.get("export-toggle-files").textContent, "Start beacon export");
    const beforeExport = value.publication.session_id;
    await elements.get("export-toggle").events.click(); await settle();
    assert.equal(elements.get("export-toggle").textContent, "Stop beacon export");
    assert.equal(elements.get("export-toggle-files").textContent, "Stop beacon export");
    assert.match(elements.get("export-status").textContent, /Export active/);
    assert.equal(value.publication.session_id, beforeExport);
    await elements.get("export-toggle-files").events.click(); await settle();
    assert.equal(elements.get("export-toggle").textContent, "Start beacon export");
    assert.equal(elements.get("export-toggle-files").textContent, "Start beacon export");
    value.logging_available = false; await poll();
    assert.equal(elements.get("logging-toggle").disabled, true);
    value.logging_available = true;
    value.publication.phase = "stopping"; value.publication.revision++; await poll();
    assert.equal(elements.get("session-toggle").textContent, "Stopping…");
    assert.equal(elements.get("session-toggle").disabled, true);
    assert.equal(elements.get("logging-toggle").disabled, true);
    assert.equal(elements.get("export-toggle").disabled, true);
    assert.equal(elements.get("export-toggle-files").disabled, true);
    value.publication.phase = "stopped"; value.publication.logging.active = false; value.publication.revision++;
    await poll();
    assert.equal(elements.get("session-toggle").textContent, "Start session");
    assert.equal(elements.get("session-toggle").className, "button primary");
    assert.equal(elements.get("logging-toggle").disabled, true);
    const countBeforePreference = calls.filter(call => call.options.method === "POST").length;
    elements.get("session-toggle").events.click();
    assert.equal(elements.get("start-dialog").open, true);
    await settle();
    assert.deepEqual(elements.get("iface").children.map(option => option.value), ["auto", "root:wlan0", "lab:mon0"]);
    elements.get("iface").value = "lab:mon0"; elements.get("iface").events.change();
    assert.equal(elements.get("netns").value, "lab");
    assert.equal(elements.get("netns").readOnly, true);
    elements.get("start-logging").checked = true;
    elements.get("start-export").checked = true;
    await poll();
    assert.equal(elements.get("start-logging").checked, true);
    elements.get("cancel-start").events.click();
    assert.equal(calls.filter(call => call.options.method === "POST").length, countBeforePreference);
    elements.get("session-toggle").events.click();
    await settle();
    interfaceInventory = availableInterfaces.slice(0, 1);
    await elements.get("refresh-interfaces").events.click();
    assert.equal(elements.get("iface").value, "lab:mon0");
    assert.match(elements.get("iface").children.at(-1).textContent, /Unavailable/);
    await elements.get("start-form").events.submit({preventDefault() {}, currentTarget: elements.get("start-form")});
    assert.equal(calls.filter(call => call.options.method === "POST").length, countBeforePreference);
    assert.match(elements.get("form-error").textContent, /Refresh interfaces/);
    interfaceInventory = availableInterfaces;
    await elements.get("refresh-interfaces").events.click();
    for (const [band, width, channel, primary, center] of [
      ["2.4", "40", "6:2427", "6", 2427],
      ["2.4", "40", "6:2447", "6", 2447],
      ["5", "80", "44", "44", 5210],
    ]) {
      elements.get("band").value = band; elements.get("band").events.change();
      elements.get("channel-width").value = width; elements.get("channel-width").events.change();
      elements.get("channel").value = channel; elements.get("channel").events.change();
      assert.equal(elements.get("centers").hidden, true);
      await elements.get("start-form").events.submit({preventDefault() {}, currentTarget: elements.get("start-form")});
      const payload = JSON.parse(calls.findLast(call => call.url.endsWith("session/start")).options.body);
      assert.equal(payload.channel, primary);
      assert.equal(payload.center_frequency1_mhz, center);
      assert.equal(payload.channel_width, width);
      assert.equal(payload.center_frequency2_mhz, undefined);
      value.publication.phase = "stopped"; value.publication.revision++; await poll();
      elements.get("session-toggle").events.click(); await settle();
    }
    elements.get("band").value = "6"; elements.get("band").events.change();
    elements.get("channel").value = "65"; elements.get("channel").events.change();
    elements.get("channel-width").value = "320"; elements.get("channel-width").events.change();
    elements.get("center-one").value = "6425"; elements.get("center-one").events.change();
    await elements.get("start-form").events.submit({preventDefault() {}, currentTarget: elements.get("start-form")});
    await settle();
    const start = calls.findLast(call => call.url.endsWith("session/start"));
    assert.equal(JSON.parse(start.options.body).logging_enabled, true);
    assert.equal(JSON.parse(start.options.body).export_enabled, true);
    assert.equal(JSON.parse(start.options.body).iface, "mon0");
    assert.equal(JSON.parse(start.options.body).netns, "lab");
    assert.equal(JSON.parse(start.options.body).center_frequency1_mhz, 6425);
    assert.equal(JSON.parse(start.options.body).channel_width, "320");
    assert.equal(JSON.parse(start.options.body).center_frequency2_mhz, undefined);
    assert.equal(elements.get("start-dialog").open, false);
    assert.equal(elements.get("session-toggle").textContent, "Stop session");
    elements.get("session-toggle").events.click(); await settle();
    assert.equal(elements.get("session-toggle").textContent, "Stopping…");
    assert.equal(elements.get("session-toggle").disabled, true);
    const stop = calls.find(call => call.url.endsWith("session/stop"));
    assert.equal(JSON.parse(stop.options.body).session_id, value.publication.session_id);
    value.publication.phase = "running"; value.publication.revision++; await poll();
    if (hosted) {
      docEvents.pointerdown(); docEvents.keydown(); await settle();
      assert.equal(calls.filter(call => call.url === "/apps").length, 1);
      now += 61000; headCode = 302;
      const count = calls.filter(call => call.options.method === "POST").length;
      elements.get("session-toggle").events.click(); await settle();
      assert.equal(calls.filter(call => call.options.method === "POST").length, count);
      assert.equal(elements.get("login-required").hidden, false);
    }
    return {hosted, passed: true};
  }
  return [await boot(true), await boot(false)];
}

if (typeof process !== "undefined" && process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const folder = process.argv[2];
  assert.ok(folder, "Usage: node tests/web_ui_check.mjs <exported-fixture-directory>");
  console.log(await checkWebUi({
    html: await readFile(`${folder}/preview.html`, "utf8"),
    status: JSON.parse(await readFile(`${folder}/status.json`, "utf8")),
    source: await readFile(new URL("../beacon_live/web_static/utilization.js", import.meta.url), "utf8"),
  }));
}
