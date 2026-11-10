(() => {
  "use strict";
  const panel = document.getElementById("feed-health");
  if (!panel) return;
  const events = document.getElementById("feed-events"), pause = document.getElementById("feed-pause"), apply = document.getElementById("feed-apply");
  const retry = document.getElementById("feed-retry"), updateStatus = document.getElementById("feed-update-status");
  const signal = document.getElementById("feed-signal"), query = document.getElementById("feed-query"), source = document.getElementById("feed-source"), since = document.getElementById("feed-since");
  const clear = document.getElementById("feed-clear"), filters = document.getElementById("feed-filters");
  let paused = false, pending = null, previous = null, current = null, timer, generation = 0, controller;
  const add = (parent, tag, value, className) => {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = String(value);
    if (className) node.className = className;
    parent.append(node); return node;
  };
  const text = value => typeof value === "string" || typeof value === "number" ? String(value) : "Not recorded";
  const provider = row => row.source && typeof row.source.provider === "string" ? row.source.provider : "Provider not recorded";
  const utc = row => typeof row.observed.timestamp_ms === "number" && Number.isFinite(row.observed.timestamp_ms) && Math.abs(row.observed.timestamp_ms) <= 8640000000000000 ? new Date(row.observed.timestamp_ms).toISOString() : null;
  const notify = data => {
    if (typeof document.dispatchEvent === "function" && typeof CustomEvent === "function") document.dispatchEvent(new CustomEvent("siyab-feed", {detail: {events: data.events, event_ids: data.event_ids || data.events.map(() => null), stats: data.stats, follower: data.follower}}));
  };
  const renderEvents = () => {
    if (!current) return;
    const needle = query.value.trim().toLowerCase(), lowerBound = since.value ? Date.parse(since.value + "Z") : null;
    const matches = current.events.map((row, index) => ({row, id: current.event_ids ? current.event_ids[index] : null})).filter(({row, id}) =>
      (!signal.value || row.signal === signal.value) && (!source.value || provider(row) === source.value) &&
      (!since.value || Number.isFinite(lowerBound) && typeof row.observed.timestamp_ms === "number" && row.observed.timestamp_ms >= lowerBound) &&
      (!needle || [row.subject, row.other_account_id, row.signal, row.detail, row.tx_hash, row.receipt_id, id].filter(value => typeof value === "string").join(" ").toLowerCase().includes(needle))).slice(-50).reverse();
    events.replaceChildren(); add(events, "h3", "Recent observed signals");
    add(events, "p", `${matches.length} of ${current.events.length} events in this response. Newest observed block first.`, "feed-count");
    if (!matches.length) {
      add(events, "p", current.events.length ? "No events match these filters. Clear filters to inspect the full response." : "No events were returned in this read. Coverage is described by follower health. This is not a clean bill of health for any account.", "feed-empty"); return;
    }
    const exportButton = add(events, "button", "Export shown events CSV");
    exportButton.type = "button"; exportButton.setAttribute("data-export-table", "feed-signals");
    const table = add(events, "table", undefined, "feed-table"); table.id = "feed-signals";
    add(table, "caption", "Observed signals in the current response");
    const heading = add(add(table, "thead"), "tr"), columns = ["Observed (UTC)", "Signal", "Account", "Counterparty", "Amount", "Source and evidence"];
    for (const name of columns) add(heading, "th", name).setAttribute("scope", "col");
    const body = add(table, "tbody");
    for (const {row, id} of matches) {
      const tr = add(body, "tr"), cells = columns.map(name => { const td = add(tr, "td"); td.setAttribute("data-label", name); return td; });
      const observed = utc(row);
      if (observed) add(cells[0], "time", observed.replace("T", " ").replace(/\.\d{3}Z$/, " UTC")).setAttribute("datetime", observed);
      else add(cells[0], "span", "Not recorded");
      add(cells[1], "strong", row.signal.replace(/_/g, " ")); add(cells[1], "p", row.detail);
      add(cells[2], "q", text(row.subject), "data");
      if (typeof row.subject === "string" && /^[a-z0-9._-]{2,64}$/.test(row.subject)) add(cells[2], "a", "Investigate").href = "/index.html?q=" + encodeURIComponent(row.subject);
      add(cells[3], "q", text(row.other_account_id), "data");
      const exactAmount = typeof row.usd_amount === "string" || typeof row.usd_amount === "number" ? `${row.usd_amount} USD (provider value)` : typeof row.amount_yocto === "string" ? `${row.amount_yocto} yoctoNEAR` : "Not recorded";
      const amount = typeof row.usd_amount === "string" || typeof row.usd_amount === "number" ? `${window.SiyabNumbers.decimal(row.usd_amount, 2)} USD (provider value)` : typeof row.amount_yocto === "string" ? `${window.SiyabNumbers.baseUnits(row.amount_yocto, 24)} NEAR` : "Not recorded";
      cells[4].setAttribute("data-export-value", exactAmount);
      add(cells[4], "span", amount, "feed-amount"); if (typeof row.asset_id === "string") add(cells[4], "small", row.asset_id);
      add(cells[5], "span", provider(row));
      if (typeof id === "string" && /^[0-9a-f]{64}$/.test(id)) add(cells[5], "a", "Open evidence").href = "/evidence/" + id;
      else add(cells[5], "span", "Unlinked record");
      const detail = add(cells[5], "details"); add(detail, "summary", "Record details");
      add(detail, "p", `Block ${row.observed.block_height}`); add(detail, "p", "Signal " + row.signal);
      add(detail, "p", "Transaction " + text(row.tx_hash)); add(detail, "p", "Receipt " + text(row.receipt_id));
      add(detail, "p", "Evidence identifier " + text(id)); add(detail, "pre", JSON.stringify(row, null, 2));
    }
  };
  const updateOptions = (control, values, first) => {
    const selected = control.value; control.replaceChildren(); add(control, "option", first).value = "";
    for (const value of [...new Set(values)].sort()) add(control, "option", value).value = value;
    if (selected && !values.includes(selected)) add(control, "option", selected + " (not in this response)").value = selected;
    control.value = selected;
  };
  const showEvents = data => {
    current = data; updateOptions(signal, data.events.map(row => row.signal), "All signals"); updateOptions(source, data.events.map(provider), "All sources");
    filters.hidden = false; renderEvents(); previous = JSON.stringify([data.events, data.event_ids || null]); pending = null; apply.hidden = true;
    updateStatus.textContent = paused ? "Updates paused; displayed response retained" : "Displayed response is current as of the last successful read"; notify(data);
  };
  apply.addEventListener("click", () => { if (pending) showEvents(pending); });
  for (const control of [query, since]) control.addEventListener("input", renderEvents);
  for (const control of [signal, source]) control.addEventListener("change", renderEvents);
  clear.addEventListener("click", () => { for (const control of [query, since, signal, source]) control.value = ""; renderEvents(); });
  const health = data => {
    panel.replaceChildren(); if (!data.stats) { add(panel, "p", "The feed has not run on this server."); return; }
    const stats = data.stats;
    add(panel, "strong", data.follower === "running" ? "Follower running" : data.follower === "stopped" ? "Follower stopped" : "Recorded feed window");
    add(panel, "p", `Blocks ${stats.start_height ?? "unavailable"} to ${stats.last_height ?? "unavailable"}; lag ${stats.lag_blocks ?? "unavailable"} blocks.`);
    add(panel, "p", `Last update: ${stats.updated_at || stats.last_time || "unavailable"}.`);
    add(panel, "p", "Events below are the latest response, not a complete transaction history. Filtered results do not change recording coverage.");
    if (stats.last_error) add(panel, "p", `Last recorded error: ${JSON.stringify(stats.last_error)}`);
    if (stats.disabled_signals && Object.keys(stats.disabled_signals).length) add(panel, "p", `Disabled signals: ${JSON.stringify(stats.disabled_signals)}`);
    if (stats.coverage_reset) {
      const previousHeight = stats.coverage_reset.previous_last_height, startHeight = stats.start_height;
      add(panel, "p", `Coverage reset: ${stats.coverage_reset.reason}; previous last height ${previousHeight}.`);
      if (Number.isInteger(previousHeight) && Number.isInteger(startHeight) && startHeight > previousHeight + 1) add(panel, "p", `Blocks ${previousHeight + 1} to ${startHeight - 1} were not scanned by the follower.`);
      add(panel, "p", "At window start, up to 10 minutes of prior key/outflow activity and 24 hours of fresh-account context may be missing. Transfers windows are separate and may include earlier data.");
    }
    if (stats.mode === "follow" && !stats.updated_at && !stats.last_time) add(panel, "p", "Freshness unavailable: the follower has not recorded a readable update timestamp.");
    for (const [key, label] of [["large_transfer_page_caps", "Transfer page limits"], ["large_transfer_poll_gaps", "Transfer polling gaps"], ["skipped_heights", "Skipped heights"]]) if (stats[key] && stats[key].length) add(panel, "p", `${label}: ${JSON.stringify(stats[key])}`);
  };
  const poll = async () => {
    if (paused || document.hidden) return;
    clearTimeout(timer); if (controller) controller.abort();
    const requestController = new AbortController(); controller = requestController;
    const deadline = setTimeout(() => requestController.abort(), 12000), request = ++generation;
    retry.hidden = true; panel.setAttribute("aria-busy", "true");
    try {
      const response = await fetch("/feed", {headers: {Accept: "application/json"}, signal: requestController.signal});
      if (!response.ok || !(response.headers.get("content-type") || "").includes("application/json")) throw new Error("Feed unavailable");
      const data = await response.json();
      if (!data || !Array.isArray(data.events) || data.events.length > 50 || !data.events.every(row => row && typeof row.signal === "string" && typeof row.detail === "string" && row.observed && Number.isSafeInteger(row.observed.block_height) && row.observed.block_height >= 0) || data.stats !== null && (typeof data.stats !== "object" || Array.isArray(data.stats)) || ![null, "running", "stopped"].includes(data.follower) || data.event_ids !== undefined && (!Array.isArray(data.event_ids) || data.event_ids.length !== data.events.length || !data.event_ids.every(id => id === null || typeof id === "string" && /^[0-9a-f]{64}$/.test(id)))) throw new Error("Feed unreadable");
      if (paused || request !== generation) return;
      health(data);
      if (previous === null) showEvents(data);
      else if (JSON.stringify([data.events, data.event_ids || null]) !== previous) { pending = data; apply.hidden = false; apply.textContent = "Show updated events"; updateStatus.textContent = "New response available; displayed events are unchanged until you apply it"; }
      else { pending = null; apply.hidden = true; current = data; updateStatus.textContent = "Displayed response is current as of the last successful read"; notify(data); }
    } catch (error) {
      if (!paused && request === generation) { panel.replaceChildren(); add(panel, "p", "Live follower health is unavailable. Previously displayed events are retained and may be stale."); updateStatus.textContent = "Current read unavailable; retry or wait for the next scheduled read"; retry.hidden = false; }
    } finally { clearTimeout(deadline); if (request === generation) panel.setAttribute("aria-busy", "false"); }
    if (!paused && request === generation) timer = setTimeout(poll, 30000);
  };
  retry.addEventListener("click", poll);
  pause.addEventListener("click", () => {
    paused = !paused; generation++; pause.textContent = paused ? "Resume updates" : "Pause updates"; pause.setAttribute("aria-pressed", String(paused)); panel.setAttribute("aria-busy", "false");
    updateStatus.textContent = paused ? "Updates paused; displayed response may become stale" : "Reading current response";
    clearTimeout(timer); if (controller) controller.abort(); if (!paused) poll();
  });
  document.addEventListener("visibilitychange", () => {
    generation++; clearTimeout(timer); if (controller) controller.abort(); panel.setAttribute("aria-busy", "false");
    if (document.hidden) updateStatus.textContent = "Updates suspended while this page is hidden; displayed response may become stale";
    if (!document.hidden && !paused) poll();
  });
  window.addEventListener("pagehide", () => { generation++; clearTimeout(timer); if (controller) controller.abort(); });
  window.addEventListener("pageshow", event => { if (event.persisted && !document.hidden && !paused) poll(); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", poll, {once: true});
  else poll();
})();
