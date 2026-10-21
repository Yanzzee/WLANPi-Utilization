"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const api = document.querySelector('meta[name="utilization-api"]').content;
  const csrf = document.querySelector('meta[name="utilization-csrf"]').content;
  const platformHosted = document.querySelector('meta[name="utilization-hosted"]').content === "yes";
  const initialStatus = JSON.parse($("initial-status").textContent);
  const attachedSession = initialStatus && activeSession(initialStatus.publication) ? initialStatus.publication.session_id : null;
  const ns = "http://www.w3.org/2000/svg";
  const activePhases = new Set(["starting", "running", "stopping"]);
  let publication = null, screens = new Map(), online = false, busy = false;
  let failures = 0, filesAt = 0, chosenGraph = "cu", sortKey = "latest_rssi_dbm", descending = true;
  let admissionMaximum = 31250;
  let loggingAvailable = false;
  let statusRequest = null;
  let interfacesRequest = null;
  const interfacesById = new Map();
  let loginRequired = false;
  let activityAt = 0, activityRequest = null;
  const number = value => value == null ? "—" : new Intl.NumberFormat(undefined, {maximumFractionDigits: 1}).format(value);
  const percent = (value, retry = false) => value == null ? "—" : retry && value > 0 && value < 1 ? "<1%" : `${number(value)}%`;
  const date = seconds => seconds == null ? "—" : new Date(seconds * 1000).toLocaleString();
  const timeLabel = seconds => new Date(seconds * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  const node = (tag, className, text) => {const element = document.createElement(tag); if (className) element.className = className; if (text != null) element.textContent = text; return element;};
  const svgNode = (tag, attributes = {}, text) => {const element = document.createElementNS(ns, tag); Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value)); if (text != null) element.textContent = text; return element;};
  const message = (id, text) => {$(id).textContent = text || ""; $(id).hidden = !text;};
  const reporting = identity => {
    if (!identity?.bssid) return identity?.unavailable_text || "No reporting BSSID available";
    const state = publication.snapshot.bssids.find(row => row.bssid === identity.bssid);
    return `${state?.latest_ap_name ? `${state.latest_ap_name} · ` : ""}${identity.ssid || "<Hidden SSID>"} · ${identity.bssid} · ${number(identity.rssi_dbm)} dBm`;
  };
  function activeSession(value) {return ["starting", "running", "stopping"].includes(value.phase);}
  function stationSource() {return publication.snapshot.top_station_source === "mac" ? "Observed clients · 120s" : "Advertised QBSS";}
  function sourceLabel(key) {
    if (["cu", "admission_capacity"].includes(key)) return "QBSS source";
    if (key === "total_station_count") return `Station maximum: ${number(publication.snapshot.top_station_count)} · ${stationSource()}`;
    if (key === "retry") return `Retry maximum: ${percent(publication.snapshot.retry_bssids.find(row => row.bssid === publication.snapshot.top_retry_bssid)?.window_retry_percent, true)}`;
    return "Strongest eligible BSSID";
  }

  function requireLogin() {
    loginRequired = true; $("login-required").hidden = false; connection(false);
    return new Error("Sign in to WLAN Pi to reconnect.");
  }

  async function recordActivity() {
    if (!platformHosted || Date.now() - activityAt < 60000) return;
    if (!activityRequest) activityRequest = (async () => {
      const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 6000);
      try {
        // The normal Apps endpoint updates the platform's sliding idle cookie.
        // HEAD avoids transferring HTML. Status polls never keep a login alive.
        const response = await fetch("/apps", {method: "HEAD", credentials: "same-origin", cache: "no-store", redirect: "manual", signal: controller.signal});
        if (response.status === 401 || response.type === "opaqueredirect" || response.status === 302) throw requireLogin();
        if (!response.ok) throw new Error("WLAN Pi session activity check failed.");
        activityAt = Date.now();
      } finally {clearTimeout(timer);}
    })().finally(() => {activityRequest = null;});
    return activityRequest;
  }
  if (platformHosted) for (const event of ["pointerdown", "keydown"]) {
    document.addEventListener(event, () => {recordActivity().catch(() => {});}, {passive: true});
  }

  async function request(path, body) {
    if (body !== undefined) await recordActivity();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 6000);
    try {
      const options = {signal: controller.signal, cache: "no-store", credentials: "same-origin"};
      if (body !== undefined) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Utilization-CSRF": csrf}, body: JSON.stringify(body)});
      const response = await fetch(api + path, options);
      if (response.status === 401) {
        throw requireLogin();
      }
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
      return value;
    } finally {clearTimeout(timer);}
  }

  function setControls() {
    const active = publication && activePhases.has(publication.phase);
    const stopping = publication?.phase === "stopping";
    $("session-toggle").disabled = !online || busy || stopping;
    $("session-toggle").textContent = stopping ? "Stopping…" : active ? "Stop session" : "Start session";
    $("session-toggle").className = `button ${active ? "danger" : "primary"}`;
    $("logging-toggle").disabled = !online || busy || !active || !loggingAvailable || stopping;
    $("logging-toggle").title = loggingAvailable ? "Toggle log files while capture continues" : "This session has no logging formats configured";
    $("logging-toggle").textContent = publication?.logging.active ? "Stop logging" : "Start logging";
    $("start-submit").disabled = !online || busy || active;
    $("refresh-files").disabled = !online || busy;
    $("refresh-interfaces").disabled = !online || busy || Boolean(interfacesRequest);
  }

  function connection(connected) {
    online = connected;
    $("connection").className = `status ${connected ? "" : "bad"}`;
    $("connection").textContent = connected ? "Connected" : "Disconnected";
    if (connected) $("updated").textContent = `Received ${new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"})}`;
    setControls();
  }

  const metricDefinitions = [
    ["ap-cu", "AP QBSS utilization", "teal"],
    ["retries", "Retries · one second", "purple"], ["admission", "Admission capacity", "blue"],
    ["stations", "QBSS station count · sum", "orange"], ["clients", "Observed active clients · one second", "teal"],
    ["loss", "Beacon loss · one second", "orange"],
  ];
  for (const [id, label, color] of metricDefinitions) {
    const card = node("article", `metric-card accent-${color}`);
    card.append(node("h3", "metric-label", label));
    const value = node("div", "metric-value unavailable", "—"); value.id = `${id}-value`;
    const detail = node("div", "metric-detail", "Waiting for completed data"); detail.id = `${id}-detail`;
    card.append(value, detail); $("metrics").append(card);
  }

  function metric(id, value, detail) {
    $(`${id}-value`).textContent = value;
    $(`${id}-value`).classList.toggle("unavailable", value === "—");
    $(`${id}-detail`).textContent = detail;
    $(`${id}-detail`).title = detail;
  }

  function renderMetrics() {
    const snapshot = publication.snapshot, c = snapshot.current;
    const available = snapshot.generated_at != null && snapshot.history.some(row => row.sample_available) && c.sample_available;
    const value = key => available ? c[key] : null;
    const selected = reporting(screens.get("cu")?.identity);
    metric("ap-cu", percent(value("selected_qbss_cu_percent")), selected);
    metric("retries", percent(value("retry_percent"), true), available ? `${number(c.retry_frame_count)} retries / ${number(c.retry_eligible_frame_count)} eligible frames` : "Eligible unicast frames only");
    metric("admission", percent(available && c.selected_qbss_admission_capacity != null ? c.selected_qbss_admission_capacity / admissionMaximum * 100 : null), selected);
    metric("stations", number(value("qbss_station_count_sum")), available ? `${sourceLabel("total_station_count")} · ${reporting(screens.get("total_station_count")?.identity)}` : "Advertised QBSS counts");
    metric("clients", number(value("unique_client_mac_count")), `${number(available ? snapshot.window_unique_client_mac_count : null)} distinct client MACs in the rolling window`);
    metric("loss", percent(value("beacon_loss_percent")), available ? `${number(c.beacon_received_count)} received / ${number(c.beacon_expected_count)} expected · above −75 dBm` : "RSSI-qualified beacon reception");
    const collecting = publication.collecting && ["starting", "running"].includes(publication.phase);
    $("sample-time").textContent = available ? `Last completed sample · ${timeLabel(c.second)}${collecting ? " · Collecting" : ""}` : collecting ? "Collecting the first complete sample" : "No completed sample available";
    $("window-label").textContent = number(snapshot.window_seconds);
    $("bssid-count").textContent = number(snapshot.bssids.length);
  }

  function renderComposition() {
    const snapshot = publication.snapshot, comp = snapshot.composition;
    const available = snapshot.history.some(row => row.sample_available);
    $("composition-detail").textContent = comp.displayed_bssid ? `AP: ${comp.strongest_radio_ap_name || "<no AP name>"} · Vendor: ${comp.strongest_radio_vendor || "<unknown>"}` : "No radio identity available";
    $("composition-current").textContent = `Displayed: ${reporting(screens.get("composition")?.identity)}`;
    $("radio-counts").textContent = `Radio BSSIDs: ${number(available ? comp.strongest_radio_bssid_count : null)} · Radio QBSS stations: ${number(available ? comp.strongest_radio_station_count_sum : null)}`;
    $("composition-counts").replaceChildren(...[[comp.bssid_count, "BSSIDs · 120s"], [comp.qbss_bssid_count, "With QBSS"], [comp.estimated_radio_count, "Estimated radios"]].map(([count, label]) => {const item = node("div", "composition-stat"); item.append(node("strong", "", number(available ? count : null)), node("span", "", label)); return item;}));
    $("radio-bssids").replaceChildren(...comp.strongest_radio_bssids.map(bssid => {
      const state = snapshot.bssids.find(row => row.bssid === bssid);
      const row = node("tr"), identity = node("td"), ssid = node("div", "ssid", state?.ssid || "<Hidden SSID>");
      ssid.title = state?.ssid || "<Hidden SSID>";
      if (bssid === snapshot.selected_bssid) {row.className = "qbss-source"; ssid.append(node("span", "reporting", "QBSS source"));}
      identity.append(ssid, node("div", "mac", bssid));
      row.append(identity, node("td", "", `${number(state?.peak_rssi_dbm ?? state?.latest_rssi_dbm)} dBm`), node("td", "", number(state?.latest_station_count)));
      return row;
    }));
    $("selected-bssids").replaceChildren(...["cu", "total_station_count", "retry", "beacons"].map(key => {
      const item = node("div", "selected-bssid"); item.append(node("strong", "", sourceLabel(key)), node("p", "", reporting(screens.get(key)?.identity))); return item;
    }));
  }

  const graphDefinitions = [
    {id: "cu", title: "Channel utilization", series: [["AP QBSS", "teal", "cu"]]},
    {id: "total_station_count", title: "Stations & observed clients", series: [["QBSS stations · sum", "orange", "total_station_count"], ["Observed active clients", "teal", "unique_client_mac_count"]]},
    {id: "retry", title: "Retry percentage", series: [["Retries / eligible frames", "purple", "retry"]]},
    {id: "beacons", title: "Beacon loss", series: [["Above −75 dBm", "orange", "beacons"]]},
    {id: "admission_capacity", title: "Admission capacity", series: [["Selected AP", "blue", "admission_capacity"]]},
    {id: "bssid_history", title: "BSSIDs heard", series: [["Per completed second", "teal", "unique_bssid_count"]]},
  ];

  function graphSeries(key) {
    const rows = publication.snapshot.history;
    const valid = new Map(rows.map(row => [row.second, row.sample_available]));
    const view = screens.get(key);
    return view ? view.graph_points.map(point => ({second: point.second, value: valid.get(point.second) ? point.display_value : null}))
      : rows.map(row => ({second: row.second, value: row.sample_available ? row[key] : null}));
  }

  function drawGraph(definition) {
    const snapshot = publication.snapshot, rows = snapshot.history;
    const newest = rows.length ? rows[rows.length - 1].second : snapshot.current.second;
    const oldest = newest - snapshot.window_seconds + 1;
    const datasets = definition.series.map(([, , key]) => graphSeries(key));
    const counts = ["total_station_count", "bssid_history"].includes(definition.id);
    const max = counts ? Math.max(definition.id === "total_station_count" ? 64 : 4, ...datasets.flat().map(point => point.value ?? 0)) : 100;
    const x = second => 38 + (second - oldest) / Math.max(1, snapshot.window_seconds - 1) * 518;
    const y = value => 153 - Math.min(max, Math.max(0, value)) / max * 128;
    const card = node("article", `graph-card${definition.id === chosenGraph ? " mobile-selected" : ""}`); card.dataset.graph = definition.id;
    const top = node("div", "graph-top"); top.append(node("h3", "", definition.title));
    const last = datasets[0].find(point => point.second === snapshot.current.second)?.value ?? null;
    top.append(node("span", "graph-summary", counts ? `${number(last)} in the last sample` : percent(last, definition.id === "retry")));
    card.append(top);
    const legend = node("div", "graph-legend");
    for (const [label, color] of definition.series) {const item = node("span", "legend-item"); item.append(node("span", `swatch ${color}`), document.createTextNode(label)); legend.append(item);}
    card.append(legend);
    const chart = svgNode("svg", {viewBox: "0 0 570 185", class: "chart", role: "img", "aria-label": `${definition.title}, shared ${snapshot.window_seconds}-second history. Gaps are unavailable samples.`});
    chart.append(svgNode("title", {}, `${definition.title} · ${snapshot.window_seconds} seconds`));
    for (let step = 0; step <= 4; step++) {const value = max * step / 4, lineY = y(value); chart.append(svgNode("line", {x1: 38, y1: lineY, x2: 556, y2: lineY, class: "gridline"}), svgNode("text", {x: 29, y: lineY + 4, "text-anchor": "end"}, number(value)));}
    chart.append(svgNode("text", {x: 38, y: 178}, `−${snapshot.window_seconds}s`), svgNode("text", {x: 297, y: 178, "text-anchor": "middle"}, `−${Math.round(snapshot.window_seconds / 2)}s`), svgNode("text", {x: 556, y: 178, "text-anchor": "end"}, "Latest"));
    datasets.forEach((points, seriesIndex) => {
      let path = "", previous = null;
      for (const point of points) {
        if (point.value == null || !Number.isFinite(point.value)) {previous = null; continue;}
        const contiguous = previous != null && point.second === previous + 1;
        path += `${contiguous ? "L" : "M"}${x(point.second).toFixed(2)},${y(point.value).toFixed(2)} `;
        if (!contiguous) chart.append(svgNode("circle", {cx: x(point.second), cy: y(point.value), r: 2, class: `point ${definition.series[seriesIndex][1]}`}));
        previous = point.second;
      }
      chart.append(svgNode("path", {d: path, class: `series ${definition.series[seriesIndex][1]}`}));
    });
    rows.forEach((row, index) => {
      const hit = svgNode("rect", {x: x(row.second) - 2, y: 20, width: 518 / snapshot.window_seconds + 2, height: 136, fill: "transparent"});
      hit.append(svgNode("title", {}, `${timeLabel(row.second)} · ${row.sample_available ? definition.series.map(([label], s) => `${label}: ${counts ? number(datasets[s][index]?.value) : percent(datasets[s][index]?.value, definition.id === "retry")}`).join(" · ") : "Unavailable sample"}`)); chart.append(hit);
    });
    card.append(chart);
    const identity = screens.get(definition.id)?.identity;
    const caption = node("p", "graph-identity", identity ? `${sourceLabel(definition.id)} · ${reporting(identity)}` : "Counts from the shared completed statistics"); caption.title = caption.textContent;
    card.append(caption);
    return card;
  }

  function renderTable() {
    const rows = [...publication.snapshot.bssids];
    rows.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (av == null) return bv == null ? a.bssid.localeCompare(b.bssid) : 1;
      if (bv == null) return -1;
      return (typeof av === "string" ? av.localeCompare(bv) : av - bv) * (descending ? -1 : 1) || a.bssid.localeCompare(b.bssid);
    });
    $("bssid-rows").replaceChildren(...rows.map(row => {
      const tr = node("tr"), identity = node("td"), ssid = node("div", "ssid", row.ssid ?? "<Hidden SSID>"); ssid.title = row.ssid ?? "<Hidden SSID>";
      if (row.bssid === publication.snapshot.selected_bssid) ssid.append(node("span", "reporting", "QBSS reporting"));
      identity.append(ssid, node("div", "mac", row.bssid)); tr.append(identity);
      [row.latest_ap_name ?? "—", row.latest_rssi_dbm == null ? "—" : `${number(row.latest_rssi_dbm)} dBm`, percent(row.latest_qbss_cu_percent), number(row.latest_station_count), number(row.window_unique_client_mac_count), percent(row.window_retry_percent, true), number(row.latest_admission_capacity), timeLabel(row.latest_beacon_ts)].forEach(value => tr.append(node("td", "", value)));
      return tr;
    }));
    $("bssid-empty").hidden = rows.length > 0;
    document.querySelectorAll("[data-sort]").forEach(button => {
      button.parentElement.setAttribute("aria-sort", button.dataset.sort === sortKey ? descending ? "descending" : "ascending" : "none");
      const label = button.textContent.replace(/ [↑↓]$/, ""); button.textContent = label + (button.dataset.sort === sortKey ? descending ? " ↓" : " ↑" : "");
    });
  }

  function renderSession() {
    const cfg = publication.config, phase = publication.phase;
    $("phase").textContent = phase.charAt(0).toUpperCase() + phase.slice(1);
    $("phase").className = `status ${phase === "failed" ? "bad" : ["starting", "stopping"].includes(phase) ? "warning" : phase === "running" ? "" : "neutral"}`;
    const actual = publication.capture_definition;
    const width = actual?.width || publication.capture_width || cfg?.channel_width;
    const widthLabel = width ? `${String(width).replace(/\s*MHz$/i, "")} MHz` : "Width pending";
    const primary = actual?.primary_frequency_mhz || cfg?.frequency_mhz;
    const centers = actual ? [actual.center_frequency1_mhz, actual.center_frequency2_mhz] : [cfg?.center_frequency1_mhz, cfg?.center_frequency2_mhz];
    const centerLabel = centers.filter(value => value != null).join(" / ");
    const target = publication.capture_target;
    const iface = target?.interface || (cfg?.iface === "auto" ? "Automatic interface" : cfg?.iface);
    const netns = target ? target.namespace : cfg?.netns;
    $("tuning").textContent = cfg ? `${cfg.band ? `${cfg.band} GHz · ` : ""}Channel ${actual?.primary_channel || cfg.channel}${primary ? ` · ${primary} MHz primary` : ""} · ${widthLabel}` : "No session";
    $("session-detail").textContent = cfg ? `${cfg.capture_backend === "core" ? "Core" : "Direct"} capture · ${iface}${netns ? ` · Namespace ${netns}` : ""} · ${centerLabel ? `Center ${centerLabel} MHz${actual ? publication.capture_definition_verified ? "" : " (unverified)" : " requested"}` : "Center frequency pending"}${activePhases.has(phase) ? `${publication.session_id === attachedSession ? " · Session already running" : ""} · Capture continues when views close` : " · Retained final history"}` : "Start a session to collect live channel metrics.";
    $("session-id").textContent = publication.session_id ? `Session ${publication.session_id}` : "No capture session";
    $("logging-state").textContent = publication.logging.active ? "● Logging active" : "Logging off";
    $("logging-state").classList.toggle("active", publication.logging.active);
    $("log-heading").textContent = publication.logging.active ? "Logging is active" : "Logging is off";
    $("log-detail").textContent = publication.logging.message || "CSV statistics and beacon JSONL use the same capture session.";
    $("active-files").replaceChildren(...publication.logging.paths.map(path => node("li", "", `${path.split("/").pop()}${publication.logging.active ? " · Writing" : " · Closed"}`)));
    message("notice", [...new Set([publication.error, publication.notice, publication.logging.message].filter(Boolean))].join(" · "));
    setControls();
  }

  async function files() {
    try {
      const value = await request("files"); filesAt = Date.now(); message("file-error", null);
      $("file-count").textContent = number(value.files.length);
      $("file-empty").hidden = value.files.length > 0;
      $("file-list").replaceChildren(...value.files.map(file => {
        const row = node("div", "file-row"), detail = node("div");
        detail.append(node("div", "file-name", file.name), node("div", "file-meta", `${date(file.closed_at)} · ${number(file.size_bytes / 1024)} KiB · Closed`));
        row.append(detail);
        if (file.available) {const link = node("a", "button secondary small download", "Download"); link.href = api + "files/" + encodeURIComponent(file.id); link.setAttribute("download", file.name); link.setAttribute("aria-label", `Download ${file.name}`); row.append(link);}
        else row.append(node("span", "muted", "Unavailable"));
        return row;
      }));
    } catch (error) {message("file-error", error.name === "AbortError" ? "Log file request timed out. Try again." : error.message);}
  }

  function showTab(name) {
    document.querySelectorAll(".panel").forEach(panel => panel.hidden = panel.id !== name);
    document.querySelectorAll("[data-tab]").forEach(button => {button.classList.toggle("selected", button.dataset.tab === name); if (button.dataset.tab === name) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");});
    if (name === "logs") files();
  }
  document.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => showTab(button.dataset.tab)));
  document.querySelectorAll("[data-sort]").forEach(button => button.addEventListener("click", () => {descending = sortKey === button.dataset.sort ? !descending : !["ssid", "latest_ap_name"].includes(button.dataset.sort); sortKey = button.dataset.sort; if (publication) renderTable();}));
  $("graph-choice").addEventListener("change", () => {chosenGraph = $("graph-choice").value; document.querySelectorAll("[data-graph]").forEach(card => card.classList.toggle("mobile-selected", card.dataset.graph === chosenGraph));});
  $("refresh-files").addEventListener("click", files);

  async function control(path, body) {
    busy = true; setControls(); message("error", null);
    try {await request(path, body); await pollOnce(); return true;}
    catch (error) {message("error", error.name === "AbortError" ? "Control timed out. Check the session status before retrying." : error.message); return false;}
    finally {busy = false; setControls();}
  }
  $("session-toggle").addEventListener("click", () => {
    if (!online || busy || publication?.phase === "stopping") return;
    if (publication && activeSession(publication)) control("session/stop", {session_id: publication.session_id});
    else {message("form-error", null); $("start-dialog").showModal(); loadInterfaces();}
  });
  $("logging-toggle").addEventListener("click", async () => {
    if (!online || busy || !publication || !activeSession(publication) || !loggingAvailable || publication.phase === "stopping") return;
    await control("logging", {session_id: publication.session_id, enabled: !publication.logging.active});
    filesAt = 0;
  });
  ["cancel-start", "cancel-bottom"].forEach(id => $(id).addEventListener("click", () => $("start-dialog").close()));
  const captureOptions = JSON.parse($("capture-options").textContent);
  const channelPlan = () => captureOptions[$("band").value];
  const selectedChannel = () => channelPlan().channels[$("channel-width").value].find(choice => choice.value === $("channel").value);
  function optionsFor(select, entries, fallback) {
    const previous = select.value;
    select.replaceChildren(...entries.map(([value, label]) => {const option = node("option", "", label); option.value = String(value); return option;}));
    select.value = entries.some(([value]) => String(value) === previous) ? previous : String(fallback ?? entries[0]?.[0] ?? "");
  }
  function primaryCenters(width) {
    const plan = channelPlan(), primary = plan.defaults[selectedChannel()?.primary]?.frequency;
    const size = width === "80+80" ? 80 : Number(width);
    return (plan.centers[width === "80+80" ? "80" : width] || []).filter(center => {
      const offset = primary - (center.frequency - size / 2 + 10);
      return offset >= 0 && offset < size && offset % 20 === 0;
    });
  }
  function syncCenters() {
    const width = $("channel-width").value, wide = width && width !== "20";
    const entries = centers => centers.map(c => [c.frequency, `${c.frequency} MHz (Ch ${c.channel})`]);
    const combinedCenter = selectedChannel()?.center;
    const first = wide ? primaryCenters(width).filter(c => !combinedCenter || c.frequency === combinedCenter) : [];
    optionsFor($("center-one"), entries(first));
    const second = width === "80+80" ? channelPlan().centers["80"].filter(c => Math.abs(c.frequency - Number($("center-one").value)) > 80) : [];
    optionsFor($("center-two-choice"), entries(second));
    $("center-one-field").hidden = first.length <= 1;
    $("center-two").hidden = second.length <= 1;
    $("centers").hidden = first.length <= 1 && second.length <= 1;
    $("center-one").required = first.length > 1;
    $("center-two-choice").required = second.length > 1;
  }
  function syncChannels() {
    const previousPrimary = Number($("channel").value.split(":")[0]);
    const choices = channelPlan().channels[$("channel-width").value];
    const preferred = choices.find(choice => choice.primary === previousPrimary)
      || choices.find(choice => choice.primary === {"2.4": 1, "5": 36, "6": 5}[$("band").value]);
    optionsFor($("channel"), choices.map(choice => [choice.value, choice.label]), preferred?.value);
    $("channel-label").textContent = $("band").value === "2.4" && $("channel-width").value === "40" ? "Primary + secondary channel" : "Primary channel";
    syncCenters();
  }
  function syncBand() {
    const plan = channelPlan();
    optionsFor($("channel-width"), [["", `Band default (${plan.default_width} MHz)`], ...plan.widths.map(width => [width, `${width === "80+80" ? "80 + 80" : width} MHz`])], "");
    $("channel").value = "";
    syncChannels();
  }
  $("band").addEventListener("change", syncBand);
  $("channel").addEventListener("change", syncCenters);
  $("channel-width").addEventListener("change", syncChannels);
  $("center-one").addEventListener("change", syncCenters);
  syncBand();
  function syncInterface() {
    const target = interfacesById.get($("iface").value);
    $("netns").readOnly = Boolean(target);
    if (target) $("netns").value = target.namespace || "";
  }
  async function loadInterfaces() {
    if (interfacesRequest) return interfacesRequest;
    interfacesRequest = (async () => {
      try {
        const inventory = await request("interfaces");
        interfacesById.clear(); inventory.interfaces.forEach(item => interfacesById.set(item.id, item));
        const previous = $("iface").value;
        const entries = [["auto", "Automatic discovery"], ...inventory.interfaces.map(item => [item.id, `${item.id} · ${item.mode}`])];
        if (previous && previous !== "auto" && !interfacesById.has(previous)) entries.push([previous, `${previous} · Unavailable`]);
        optionsFor($("iface"), entries, "auto");
        syncInterface();
        $("interface-note").textContent = inventory.errors.length ? inventory.errors.join(" · ") : inventory.interfaces.length ? "Interfaces include root and named network namespaces." : "No wireless interfaces found. Connect an adapter and refresh.";
      } catch (error) {$("interface-note").textContent = error.message;}
    })().finally(() => {interfacesRequest = null; setControls();});
    setControls();
    return interfacesRequest;
  }
  $("iface").addEventListener("change", syncInterface);
  $("refresh-interfaces").addEventListener("click", loadInterfaces);
  $("start-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!online || busy || (publication && activeSession(publication))) return;
    const data = new FormData(event.currentTarget), width = data.get("channel_width");
    const choice = selectedChannel();
    if (!choice) {message("form-error", "Select a valid primary channel for this capture width."); return;}
    const body = {band: data.get("band"), channel: String(choice.primary), channel_width: width || null, capture_backend: data.get("capture_backend"), iface: data.get("iface"), netns: data.get("netns") || null, logging_enabled: data.has("logging_enabled")};
    const target = interfacesById.get(body.iface);
    if (body.iface !== "auto" && !target) {message("form-error", "Refresh interfaces and select an available interface."); return;}
    if (target) {body.iface = target.interface; body.netns = target.namespace;}
    if (width && width !== "20") body.center_frequency1_mhz = Number(data.get("center_frequency1_mhz"));
    if (width === "80+80") body.center_frequency2_mhz = Number(data.get("center_frequency2_mhz"));
    busy = true; setControls(); message("form-error", null);
    try {await request("session/start", body); $("start-dialog").close(); message("error", null); await pollOnce();}
    catch (error) {message("form-error", error.name === "AbortError" ? "Start request timed out. Close this dialog and check session status before retrying." : error.message);}
    finally {busy = false; setControls();}
  });

  function pollOnce() {
    if (!statusRequest) statusRequest = readStatus().finally(() => {statusRequest = null;});
    return statusRequest;
  }

  function applyStatus(value) {
    const changed = !publication || value.publication.revision !== publication.revision || value.publication.session_id !== publication.session_id;
    publication = value.publication; screens = new Map(value.screens.map(screen => [screen.screen_id, screen]));
    loggingAvailable = value.logging_available;
    admissionMaximum = value.units?.admission_capacity_max || admissionMaximum;
    connection(true);
    loginRequired = false; $("login-required").hidden = true;
    const health = publication.capture_health;
    if (health) {
      const age = health.latest_timestamp == null ? null : Math.max(0, value.server_time - health.latest_timestamp);
      $("capture-health").textContent = `Capture quality · ${number(health.decoded_frames)} decoded frames · ${number(health.bad_fcs_frames)} bad FCS · ${number(health.rejected_records)} rejected rows · ${age == null ? "Waiting for capture timestamps" : `Latest capture ${number(age)}s ago`}${health.fcs_metadata_available ? "" : " · FCS metadata unavailable"}${age > 3 && publication.phase === "running" ? " · Quiet traffic, buffering or capture loss can leave gaps" : ""}`;
    }
    if (changed) {renderSession(); renderComposition(); renderMetrics(); renderTable(); $("graphs").replaceChildren(...graphDefinitions.map(drawGraph));}
    if (!$("logs").hidden && Date.now() - filesAt > 5000) files();
    if (!failures) return;
    message("error", null); failures = 0;
  }
  async function readStatus() {applyStatus(await request("status"));}
  async function poll() {
    try {await pollOnce();}
    catch (error) {failures++; connection(false); message("error", `${loginRequired ? "Sign-in required." : "Service unavailable."} ${publication ? "Showing the last received values; capture status is unknown. " : ""}${error.name === "AbortError" ? "The request timed out." : error.message} Retrying automatically.`);}
    setTimeout(poll, Math.min(10000, 1000 * Math.max(1, failures)));
  }
  if (initialStatus) applyStatus(initialStatus);
  poll();
})();
