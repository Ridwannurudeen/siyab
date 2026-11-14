(() => {
  "use strict";
  const key = "siyab-workspace-v1";
  const empty = () => ({version: 1, cases: [], watchlist: [], alerts: false, notifications: []});
  const localPath = value => {
    if (typeof value !== "string" || value.length > 2048) return null;
    let url;
    try { url = new URL(value, location.origin); }
    catch (error) { return null; }
    if (url.origin !== location.origin || !(/^\/$|^\/(?:index|investigate|workspace|feed|verify|issuers|top-receivers)\.html$|^\/(?:accounts|entities|flows|clusters|incidents|records)\/[A-Za-z0-9._-]+\.html$/).test(url.pathname)) return null;
    const path = url.pathname + url.search + url.hash;
    return path.length <= 2048 ? path : null;
  };
  const utcDate = value => {
    if (typeof value !== "string" || value.length > 32 || !/^(?:[0-9]{4}|[+-][0-9]{6})-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3})?Z$/.test(value)) return false;
    const milliseconds = Date.parse(value);
    if (!Number.isFinite(milliseconds)) return false;
    const normalized = new Date(milliseconds).toISOString();
    return normalized === value || normalized.replace(".000Z", "Z") === value;
  };
  const accountId = value => typeof value === "string" && /^[a-z0-9][a-z0-9._-]{0,126}[a-z0-9]$/.test(value);
  const validCase = item => item && typeof item.id === "string" && /^[a-z0-9-]{1,64}$/.test(item.id)
    && typeof item.title === "string" && item.title.trim().length > 0 && item.title.length <= 120
    && typeof item.notes === "string" && item.notes.length <= 4000 && localPath(item.url)
    && utcDate(item.created_at);
  const valid = value => value && value.version === 1 && Array.isArray(value.cases) && value.cases.length <= 50
    && value.cases.every(validCase) && new Set(value.cases.map(item => item.id)).size === value.cases.length
    && Array.isArray(value.watchlist) && value.watchlist.length <= 100 && value.watchlist.every(accountId)
    && new Set(value.watchlist).size === value.watchlist.length && typeof value.alerts === "boolean"
    && Array.isArray(value.notifications) && value.notifications.length <= 20
    && value.notifications.every(item => item && typeof item.id === "string" && /^[a-f0-9]{64}$/.test(item.id) && accountId(item.account)
      && typeof item.signal === "string" && item.signal.trim().length > 0 && item.signal.length <= 200
      && (utcDate(item.at) || item.at === "Observed time unavailable"))
    && new Set(value.notifications.map(item => item.id)).size === value.notifications.length;
  let state = empty();
  let storageAvailable = true;
  let storageProblem = "";
  let importController = null;
  const graphExports = new Map();
  function load() {
    try {
      const stored = localStorage.getItem(key);
      if (!stored) return empty();
      const value = JSON.parse(stored);
      if (!valid(value)) throw new Error("Saved workspace could not be read. Export a backup before resetting it.");
      return {version: 1, cases: value.cases.map(item => ({id: item.id, title: item.title, notes: item.notes,
        url: localPath(item.url), created_at: item.created_at})), watchlist: [...value.watchlist],
      alerts: value.alerts, notifications: value.notifications.map(item => ({id: item.id, account: item.account, signal: item.signal, at: item.at}))};
    } catch (error) {
      storageAvailable = false;
      storageProblem = "Browser storage is unavailable or unreadable. Changes stay in memory; export to keep them.";
      return empty();
    }
  }
  state = load();
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  const message = text => {
    const region = document.getElementById("workspace-status") || document.getElementById("workspace-feedback");
    if (region) region.textContent = text + (storageProblem ? " " + storageProblem : "");
  };
  function persist(text) {
    if (storageAvailable) {
      try { localStorage.setItem(key, JSON.stringify(state)); }
      catch (error) { storageAvailable = false; storageProblem = "Browser storage could not save this change. Export to keep it."; }
    }
    render();
    message(text);
  }
  function download(name, content, type) {
    const url = URL.createObjectURL(new Blob([content], {type}));
    const link = element("a"); link.href = url; link.download = name;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const csv = rows => rows.map(row => row.map(value => {
    let text = String(value ?? "");
    if (/^[\s]*[=+@-]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  }).join(",")).join("\r\n") + "\r\n";
  function tableExport(id, format = "csv") {
    const table = document.getElementById(id);
    if (!table) { message("The table is not available to export."); return; }
    const rows = [...table.querySelectorAll("tr")].filter(row => !row.hidden).map(row => [...row.querySelectorAll("th,td")].map(cell => {
      const exact = cell.getAttribute("data-export-value");
      if (exact !== null) return exact;
      const copy = cell.cloneNode(true);
      copy.querySelectorAll('[aria-hidden="true"], details.analytics-full-account > summary').forEach(node => node.remove());
      const full = copy.querySelector("details.analytics-full-account");
      if (full) copy.replaceChildren(...full.childNodes);
      copy.querySelectorAll("br").forEach(node => node.replaceWith("; "));
      return copy.textContent.trim().replace(/\s+/g, " ");
    }));
    download("siyab-" + id + "." + format, format === "json" ? JSON.stringify({source: location.href, scope: "Shown table rows", rows}, null, 2) : csv(rows),
      format === "json" ? "application/json" : "text/csv;charset=utf-8");
    message("Shown rows exported.");
  }
  function viewUrl() {
    const url = new URL(location.href);
    const graph = document.querySelector("[data-siyab-graph]");
    const view = graph && window.SiyabGraph && window.SiyabGraph.getView ? window.SiyabGraph.getView(graph) : null;
    if (view) {
      for (const name of [...url.searchParams.keys()]) if (name.startsWith("g_")) url.searchParams.delete(name);
      for (const [field, name] of [["root", "g_root"], ["asset", "g_asset"], ["provenance", "g_evidence"], ["from", "g_from"], ["to", "g_to"],
        ["focus", "g_focus"], ["limit", "g_limit"], ["mode", "g_view"]]) {
        if (view[field] !== "" && view[field] !== null && view[field] !== undefined) url.searchParams.set(name, String(view[field]));
      }
      if (view.selected) url.searchParams.set(view.selected.kind === "edge" ? "g_edge" : "g_node", view.selected.id);
      const payload = window.SiyabGraph.getPayload ? window.SiyabGraph.getPayload(graph) : null;
      const snapshot = payload?.snapshot?.id || graph.getAttribute("data-graph-src")?.match(/^\/live\/account-data\/([a-f0-9]{64})\.json$/)?.[1];
      if (url.pathname === "/investigate.html" && typeof snapshot === "string" && /^[a-f0-9]{64}$/.test(snapshot)) url.searchParams.set("observation", snapshot);
    }
    return localPath(url.href);
  }
  const saveDialog = document.getElementById("save-investigation");
  let opener = null;
  let editing = null;
  function openSave(item = null) {
    if (!saveDialog || typeof saveDialog.showModal !== "function") { message("Open Workspace to manage saved investigations."); return; }
    opener = document.activeElement; editing = item;
    const account = document.querySelector("[data-watch-account]")?.dataset.watchAccount;
    document.getElementById("case-title").value = item ? item.title : (account ? "Account: " + account : document.querySelector("h1")?.textContent || "Investigation").slice(0, 120);
    document.getElementById("case-notes").value = item ? item.notes : "";
    document.getElementById("case-source").textContent = item ? item.url : viewUrl();
    document.getElementById("case-error").textContent = "";
    saveDialog.showModal(); document.getElementById("case-title").focus();
  }
  if (saveDialog) {
    document.getElementById("case-cancel").addEventListener("click", () => saveDialog.close());
    saveDialog.addEventListener("close", () => {
      const target = opener && opener.isConnected ? opener : document.getElementById("case-search") || document.querySelector("[data-save-investigation]");
      if (target) target.focus();
    });
    document.getElementById("case-form").addEventListener("submit", event => {
      event.preventDefault();
      const title = document.getElementById("case-title").value.trim();
      const notes = document.getElementById("case-notes").value;
      const url = editing ? editing.url : viewUrl();
      if (!title || title.length > 120 || notes.length > 4000 || !url) { document.getElementById("case-error").textContent = "Enter a title of up to 120 characters, notes of up to 4000 characters and a valid investigation page."; return; }
      if (!editing && state.cases.length >= 50) { document.getElementById("case-error").textContent = "This browser has 50 investigations. Export or remove one before saving another."; return; }
      const item = {id: editing ? editing.id : "case-" + crypto.randomUUID(), title, notes, url,
        created_at: editing ? editing.created_at : new Date().toISOString()};
      state.cases = editing ? state.cases.map(previous => previous.id === item.id ? item : previous) : [item, ...state.cases];
      persist("Investigation saved in this browser."); saveDialog.close();
    });
    document.getElementById("case-form").addEventListener("invalid", () => {
      document.getElementById("case-error").textContent = "Enter a title before saving the investigation.";
    }, true);
  }
  function watch(account, addOnly = false) {
    if (!accountId(account)) { message("Enter a valid account identifier."); return false; }
    if (addOnly && state.watchlist.includes(account)) { message("This account is already watched in this browser."); return true; }
    if (state.watchlist.includes(account)) state.watchlist = state.watchlist.filter(value => value !== account);
    else {
      if (state.watchlist.length >= 100) { message("This browser has 100 watched accounts. Remove one before adding another."); return false; }
      state.watchlist.push(account);
    }
    persist(state.watchlist.includes(account) ? "Account added to this browser's watchlist." : "Account removed from the watchlist.");
    startMonitor();
    return true;
  }
  document.addEventListener("click", async event => {
    const control = event.target.closest("[data-save-investigation],[data-watch-account],[data-export-table],[data-export-graph]");
    if (!control) return;
    if (control.hasAttribute("data-save-investigation")) { openSave(); return; }
    if (control.hasAttribute("data-watch-account")) { watch(control.dataset.watchAccount); return; }
    if (control.hasAttribute("data-export-table")) { tableExport(control.dataset.exportTable, control.dataset.exportFormat || "csv"); return; }
    const graph = control.closest("[data-siyab-graph]");
    if (!graph || !window.SiyabGraph?.getView) { message("Load a graph before exporting."); return; }
    const view = window.SiyabGraph.getView(graph);
    if (!view) { message("Load a graph before exporting."); return; }
    const sourceUrl = viewUrl();
    if (!sourceUrl) { message("The current investigation address cannot be exported."); return; }
    control.disabled = true;
    const abort = new AbortController(); const deadline = setTimeout(() => abort.abort(), 15000);
    if (graphExports.has(control)) graphExports.get(control).abort();
    graphExports.set(control, abort);
    try {
      let payload = window.SiyabGraph.getPayload ? window.SiyabGraph.getPayload(graph) : null;
      if (!payload) {
        const source = graph.getAttribute("data-graph-src");
        if (!/^\/graph-data\/[a-f0-9]{64}\.json$/.test(source)) throw new Error("Load the graph before exporting its observed records.");
        const response = await fetch(source, {signal: abort.signal, headers: {Accept: "application/json"}});
        if (!response.ok) throw new Error("Recorded graph export is unavailable.");
        payload = await response.json();
      }
      if (graphExports.get(control) !== abort) return;
      if (!Array.isArray(payload.records)) throw new Error("Recorded graph export could not be read.");
      const ids = new Set(view.recordIds);
      const records = payload.records.filter(record => ids.has(record.id));
      download("siyab-investigation.json", JSON.stringify({source: location.origin + sourceUrl, scope: "Records matching graph filters", view, recording: payload.source, records}, null, 2), "application/json");
      message(records.length + (records.length === 1 ? " matching record" : " matching records") + " exported with source and view settings.");
    } catch (error) { if (graphExports.get(control) === abort) message(abort.signal.aborted ? "Export timed out. Try again." : error.message); }
    finally { clearTimeout(deadline); if (graphExports.get(control) === abort) { graphExports.delete(control); control.disabled = false; } }
  });
  function render() {
    document.querySelectorAll("[data-watch-account]").forEach(control => {
      const active = state.watchlist.includes(control.dataset.watchAccount);
      control.setAttribute("aria-pressed", String(active)); control.textContent = active ? "Watching account" : "Watch account";
    });
    const cases = document.getElementById("saved-cases");
    if (!cases) return;
    const query = document.getElementById("case-search").value.trim().toLowerCase();
    const matching = state.cases.filter(item => (item.title + " " + item.notes + " " + item.url).toLowerCase().includes(query));
    cases.replaceChildren();
    for (const item of matching) {
      const row = element("article", undefined, "saved-investigation");
      const heading = element("h3"); const link = element("a", item.title); link.href = item.url; heading.append(link);
      row.append(heading, element("p", item.url, "saved-path"));
      if (item.notes) row.append(element("p", item.notes, "saved-notes"));
      const at = element("time", "Saved " + new Date(item.created_at).toLocaleString()); at.dateTime = item.created_at; row.append(at);
      const actions = element("div", undefined, "workspace-actions");
      for (const [label, handler] of [["Edit notes", () => openSave(item)], ["Export JSON", () => download("siyab-investigation-" + item.id + ".json", JSON.stringify({version: 1, cases: [item], watchlist: [], alerts: false, notifications: []}, null, 2), "application/json")],
        ["Delete", () => { if (confirm('Delete "' + item.title + '" from this browser?')) { state.cases = state.cases.filter(row => row.id !== item.id); persist("Investigation deleted."); } }]]) {
        const button = element("button", label); button.type = "button"; button.addEventListener("click", handler); actions.append(button);
      }
      row.append(actions); cases.append(row);
    }
    if (!matching.length) cases.append(element("p", state.cases.length ? "No saved investigation matches this search." : "Open an account or trace and choose Save investigation. Your notes and view settings stay in this browser.", "empty-state"));
    document.getElementById("case-count").textContent = state.cases.length + (state.cases.length === 1 ? " saved investigation" : " saved investigations");
    const watched = document.getElementById("watched-accounts"); watched.replaceChildren();
    for (const account of state.watchlist) {
      const row = element("li"); row.append(element("span", account, "mono"));
      const remove = element("button", "Remove"); remove.type = "button"; remove.setAttribute("aria-label", "Remove " + account + " from watchlist");
      remove.addEventListener("click", () => watch(account)); row.append(remove); watched.append(row);
    }
    if (!state.watchlist.length) watched.append(element("li", "No watched accounts yet."));
    const toggle = document.getElementById("alerts-enabled"); toggle.checked = state.alerts;
    const alerts = document.getElementById("watch-notifications"); alerts.replaceChildren();
    for (const item of state.notifications) {
      const row = element("li"); const link = element("a", item.signal + ": " + item.account); link.href = "/feed.html";
      row.append(link, element("span", item.at, "source-line")); alerts.append(row);
    }
    if (!state.notifications.length) alerts.append(element("li", "New matching feed events will appear here while monitoring is enabled."));
  }
  const workspace = document.getElementById("saved-cases");
  if (workspace) {
    document.getElementById("case-search").addEventListener("input", render);
    document.getElementById("watch-form").addEventListener("submit", event => {
      event.preventDefault(); const input = document.getElementById("watch-account"); if (watch(input.value.trim(), true)) input.value = "";
    });
    document.getElementById("alerts-enabled").addEventListener("change", event => {
      state.alerts = event.target.checked; persist(state.alerts ? "Browser monitoring enabled." : "Browser monitoring stopped."); startMonitor();
    });
    document.getElementById("desktop-alerts").addEventListener("click", async () => {
      if (!("Notification" in window)) { message("Desktop notifications are unavailable here. Matching events still appear in Workspace."); return; }
      const permission = await Notification.requestPermission();
      message(permission === "granted" ? "Desktop notifications allowed. Enable browser monitoring to receive matching events." : "Desktop notifications were not allowed. Matching events still appear in Workspace.");
    });
    document.getElementById("export-workspace").addEventListener("click", () => download("siyab-workspace.json", JSON.stringify(state, null, 2), "application/json"));
    document.getElementById("import-workspace").addEventListener("change", async event => {
      const file = event.target.files[0];
      if (!file) return;
      if (importController) importController.abort();
      const controller = new AbortController(); importController = controller;
      const deadline = setTimeout(() => controller.abort(), 15000);
      try {
        if (file.size > 524288) throw new Error("Choose a workspace JSON file smaller than 512 KB.");
        const source = await Promise.race([file.text(), new Promise((resolve, reject) => {
          controller.signal.addEventListener("abort", () => reject(new Error("Workspace import was cancelled or timed out. Try the file again.")), {once: true});
        })]);
        if (importController !== controller) return;
        const data = JSON.parse(source);
        if (!valid(data)) throw new Error("This file is not a valid Siyab workspace export.");
        const merged = [...state.cases, ...data.cases.filter(item => !state.cases.some(old => old.id === item.id))];
        const watchlist = [...new Set([...state.watchlist, ...data.watchlist])];
        if (merged.length > 50 || watchlist.length > 100) throw new Error("This import exceeds the 50-investigation or 100-account limit.");
        state.cases = merged.map(item => ({id: item.id, title: item.title, notes: item.notes, url: localPath(item.url), created_at: item.created_at}));
        state.watchlist = watchlist;
        persist("Workspace imported. Existing notes and monitoring preference were preserved."); startMonitor();
      } catch (error) { if (importController === controller) message(error.message); }
      finally { clearTimeout(deadline); if (importController === controller) { importController = null; event.target.value = ""; } }
    });
    document.getElementById("clear-notifications").addEventListener("click", () => { state.notifications = []; persist("Notification history cleared."); });
    document.getElementById("reset-workspace").addEventListener("click", () => {
      if (!confirm("Reset saved investigations, watched accounts and notification history in this browser? Export a backup first.")) return;
      if (importController) importController.abort(); importController = null;
      let removed = false;
      try { localStorage.removeItem(key); storageAvailable = true; storageProblem = ""; removed = true; }
      catch (error) { storageAvailable = false; storageProblem = "Browser storage could not be reset. Original stored data was preserved; changes apply in memory only."; }
      state = empty(); render(); startMonitor();
      message(removed ? "Local workspace reset." : "Workspace cleared in memory. Browser storage was not changed.");
    });
  }
  let monitorTimer = null;
  let monitorController = null;
  let baseline = null;
  const monitorMessage = value => {
    const node = document.getElementById("alert-state"); if (node && node.textContent !== value) node.textContent = value;
  };
  const counterparty = row => {
    if (row.other_account_id) return row.other_account_id;
    if (row.signal !== "full_access_key_then_outflow" || typeof row.subject !== "string" || typeof row.detail !== "string") return null;
    const match = /^full access key \S+ added to ([a-z0-9]+(?:[-_.][a-z0-9]+)*); (transfer of [0-9]+ yoctoNEAR|DeleteAccount) to ([a-z0-9]+(?:[-_.][a-z0-9]+)*) within [0-9]+ ms$/.exec(row.detail);
    if (!match || match[0] !== row.detail || match[1] !== row.subject) return null;
    if (row.drain_kind === "transfer" && match[2].startsWith("transfer of ") || row.drain_kind === "delete_account" && match[2] === "DeleteAccount") return match[3];
    return null;
  };
  function consume(data) {
    if (!data || !Array.isArray(data.events) || !Array.isArray(data.event_ids) || data.events.length !== data.event_ids.length
      || !data.event_ids.every(id => id === null || typeof id === "string" && /^[a-f0-9]{64}$/.test(id))) throw new Error("Feed response could not be read.");
    const current = new Set(data.event_ids.filter(Boolean));
    if (baseline === null) { baseline = current; monitorMessage(data.follower === "stopped" ? "Follower stopped. New coverage is unavailable." : "Monitoring new events in the current feed window."); return; }
    let desktopUnavailable = false;
    for (let index = 0; index < data.events.length; index++) {
      const id = data.event_ids[index]; const event = data.events[index];
      if (!id || baseline.has(id) || !event || typeof event.signal !== "string") continue;
      const signal = event.signal.trim().slice(0, 200);
      if (!signal) continue;
      const accounts = [event.subject, event.account_id, counterparty(event), ...(Array.isArray(event.accounts) ? event.accounts : [])];
      const account = state.watchlist.find(value => accounts.includes(value));
      if (!account || state.notifications.some(item => item.id === id)) continue;
      const at = Number.isInteger(event.observed?.timestamp_ms) && event.observed.timestamp_ms >= 0 && event.observed.timestamp_ms <= 8640000000000000
        ? new Date(event.observed.timestamp_ms).toISOString() : "Observed time unavailable";
      state.notifications.unshift({id, account, signal, at}); state.notifications = state.notifications.slice(0, 20);
      if ("Notification" in window && Notification.permission === "granted") {
        try { const notice = new Notification("Siyab watch: " + account, {body: signal, tag: id}); notice.onclick = () => { location.href = "/feed.html"; }; }
        catch (error) { desktopUnavailable = true; }
      }
      persist("New watched-account event: " + account + ". Open Workspace for details.");
    }
    baseline = current;
    monitorMessage(desktopUnavailable ? "Matching events are saved here; desktop delivery is unavailable." : data.follower === "stopped" ? "Follower stopped. New coverage is unavailable." : "Monitoring new events in the current feed window.");
  }
  async function poll() {
    if (!state.alerts || !state.watchlist.length) return;
    const controller = new AbortController(); monitorController = controller;
    const deadline = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch("/feed", {signal: controller.signal, headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error("Feed unavailable");
      const data = await response.json();
      if (monitorController === controller && state.alerts) consume(data);
    } catch (error) { if (monitorController === controller) monitorMessage("Feed unavailable. Monitoring will retry."); }
    finally {
      clearTimeout(deadline);
      if (monitorController === controller) { monitorController = null; monitorTimer = setTimeout(poll, 30000); }
    }
  }
  function startMonitor() {
    clearTimeout(monitorTimer); if (monitorController) monitorController.abort(); monitorController = null; baseline = null;
    if (state.alerts && state.watchlist.length) poll();
    else monitorMessage(state.alerts ? "Add an account to begin monitoring." : "Browser monitoring is off.");
  }
  window.addEventListener("storage", event => { if (event.key === key) { state = load(); render(); startMonitor(); } });
  window.addEventListener("pagehide", () => {
    clearTimeout(monitorTimer); if (monitorController) monitorController.abort(); monitorController = null;
    if (importController) importController.abort(); importController = null;
    for (const [control, controller] of graphExports) { controller.abort(); control.disabled = false; }
    graphExports.clear();
  });
  window.addEventListener("pageshow", event => { if (event.persisted) startMonitor(); });
  render(); if (storageProblem) message(""); startMonitor();
  document.addEventListener("siyab:account-selected", render);
})();
