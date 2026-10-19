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
    elements.get("logs").hidden = true;
    elements.get("channel").dataset.channels = html.match(/data-channels='([^']+)'/)[1];
    const sortButtons = Array.from(html.matchAll(/data-sort="([^"]+)"/g), match => {
      const button = new Element(); button.dataset.sort = match[1]; button.parentElement = new Element(); return button;
    });
    const panels = ["overview", "bssids", "logs"].map(id => elements.get(id));
    const metas = {"utilization-api": "/utilization/api/", "utilization-hosted": hosted ? "yes" : "no",
      "utilization-csrf": html.match(/name="utilization-csrf" content="([^"]+)"/)[1]};
    const sandbox = {
      document: {
        getElementById: id => elements.get(id),
        querySelector: selector => ({content: metas[selector.match(/name="([^"]+)"/)?.[1]]}),
        querySelectorAll: selector => selector === "[data-sort]" ? sortButtons : selector === ".panel" ? panels : [],
        createElement: () => new Element(), createElementNS: () => new Element(),
        createTextNode: content => Object.assign(new Element(), {textContent: content}),
        addEventListener: (event, fn) => docEvents[event] = fn,
      },
      Intl, Map, Set, AbortController,
      Date: class extends Date {static now() {return now;}},
      setTimeout: (fn, ms) => {timers.set(++sequence, {fn, ms}); return sequence;},
      clearTimeout: id => timers.delete(id),
      fetch: async (url, options) => {
        calls.push({url, options});
        const code = url === "/apps" ? headCode : statusCode;
        return {status: code, type: code === 302 ? "opaqueredirect" : "basic", ok: code === 200,
          json: async () => code !== 200 ? {error: "Fixture service error"}
            : url.endsWith("files") ? {files: []} : structuredClone(value)};
      },
    };
    const settle = async () => {for (let i = 0; i < 20; i++) await Promise.resolve();};
    vm.runInNewContext(source, sandbox);
    // Embedded state must render before the first asynchronous status response.
    assert.equal(elements.get("phase").textContent, "Running");
    assert.match(elements.get("session-detail").textContent, /Session already running/);
    assert.match(elements.get("session-id").textContent, new RegExp(value.publication.session_id));
    assert.equal(elements.get("retries-value").textContent, "<1%");
    assert.equal(elements.get("stop").disabled, false);
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

    const apSort = sortButtons.find(button => button.dataset.sort === "latest_ap_name");
    apSort.events.click();
    assert.match(text(elements.get("bssid-rows").children[0]), new RegExp(retry.bssid));
    assert.match(text(elements.get("bssid-rows").children.at(-1)), new RegExp(station.bssid));
    apSort.events.click();
    assert.match(text(elements.get("bssid-rows").children[0]), new RegExp(qbss.bssid));
    assert.equal(value.publication.snapshot.selected_bssid, qbss.bssid);
    elements.get("band").value = "6"; elements.get("band").events.change();
    const choices = elements.get("channel").children;
    assert.equal(choices.length, 59);
    assert.equal(choices.find(option => option.value === "5").textContent, "5 (PSC)");
    assert.equal(choices.find(option => option.value === "1").textContent, "1");
    assert.equal(choices.filter(option => option.textContent.includes("PSC")).length, 15);
    elements.get("band").value = "2.4"; elements.get("band").events.change();
    assert.equal(elements.get("channel").children.some(option => option.textContent.includes("PSC")), false);

    async function poll() {
      const entry = [...timers].find(([, timer]) => timer.ms !== 6000);
      assert.ok(entry, "next status poll scheduled");
      timers.delete(entry[0]); await entry[1].fn(); await settle();
    }
    value.publication.revision++;
    value.publication.capture_definition = {...value.publication.capture_definition, width: "80+80", center_frequency2_mhz: 5530};
    await poll();
    assert.match(elements.get("tuning").textContent, /80\+80 MHz/);
    assert.match(elements.get("session-detail").textContent, /Center 5210 \/ 5530 MHz/);
    assert.doesNotMatch(elements.get("tuning").textContent, /MHz MHz/);
    const metrics = elements.get("ap-cu-value").textContent;
    statusCode = 503; await poll();
    assert.equal(elements.get("stop").disabled, true);
    assert.equal(elements.get("ap-cu-value").textContent, metrics);
    statusCode = 401; await poll();
    assert.equal(elements.get("login-required").hidden, false);
    statusCode = 200; await poll();
    assert.equal(elements.get("login-required").hidden, true);
    assert.equal(elements.get("stop").disabled, false);
    assert.equal(value.publication.session_id, data.publication.session_id);

    elements.get("logging-toggle").events.click(); await settle();
    const post = calls.find(call => call.options.method === "POST");
    assert.equal(JSON.parse(post.options.body).session_id, value.publication.session_id);
    assert.equal(post.options.headers["X-Utilization-CSRF"], metas["utilization-csrf"]);
    assert.equal(calls.filter(call => call.url === "/apps").length, hosted ? 1 : 0);
    if (hosted) {
      docEvents.pointerdown(); docEvents.keydown(); await settle();
      assert.equal(calls.filter(call => call.url === "/apps").length, 1);
      now += 61000; headCode = 302;
      const count = calls.filter(call => call.options.method === "POST").length;
      elements.get("stop").events.click(); await settle();
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
