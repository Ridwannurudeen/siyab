(() => {
  "use strict";
  const panel = document.getElementById("feed-health");
  if (!panel) return;
  const events = document.getElementById("feed-events");
  const pause = document.getElementById("feed-pause");
  const apply = document.getElementById("feed-apply");
  let paused = false, pending = null, previous = null, timer, generation = 0, controller;
  const add = (parent, tag, text) => { const node = document.createElement(tag); node.textContent = String(text); parent.append(node); return node; };
  const showEvents = rows => {
    events.replaceChildren();
    add(events, "h3", "Recent observed signals");
    if (!rows.length) add(events, "p", "No events were returned in this read. Coverage is described by follower health.");
    for (const row of rows.slice(-50).reverse()) {
      const article = document.createElement("article"); article.className = "card";
      add(article, "strong", row.signal); add(article, "p", row.detail);
      add(article, "p", `Block ${row.observed.block_height}; transaction ${row.tx_hash || "unavailable"}`);
      events.append(article);
    }
    previous = JSON.stringify(rows); pending = null; apply.hidden = true;
  };
  apply.addEventListener("click", () => { if (pending) showEvents(pending); });
  const poll = async () => {
    if (paused || document.hidden) return;
    const requestController = new AbortController();
    controller = requestController;
    const deadline = setTimeout(() => requestController.abort(), 12000);
    const request = ++generation;
    try {
      const response = await fetch("/feed", {headers: {Accept: "application/json"}, signal: requestController.signal});
      if (!response.ok || !(response.headers.get("content-type") || "").includes("application/json")) throw new Error("Feed unavailable");
      const data = await response.json();
      if (!Array.isArray(data.events) || !data.events.every(row => row && typeof row.signal === "string" && typeof row.detail === "string" && row.observed && Number.isFinite(row.observed.block_height))) throw new Error("Feed unreadable");
      if (paused || request !== generation) return;
      panel.replaceChildren();
      if (!data.stats) { add(panel, "p", "The feed has not run on this server."); }
      else {
        const stats = data.stats;
        add(panel, "strong", data.follower === "running" ? "Follower running" : data.follower === "stopped" ? "Follower stopped" : "Recorded feed window");
        add(panel, "p", `Blocks ${stats.start_height ?? "unavailable"} to ${stats.last_height ?? "unavailable"}; lag ${stats.lag_blocks ?? "unavailable"} blocks.`);
        add(panel, "p", `Last update: ${stats.updated_at || stats.last_time || "unavailable"}.`);
        if (stats.last_error) add(panel, "p", `Last recorded error: ${JSON.stringify(stats.last_error)}`);
        if (stats.disabled_signals && Object.keys(stats.disabled_signals).length) add(panel, "p", `Disabled signals: ${JSON.stringify(stats.disabled_signals)}`);
        if (stats.coverage_reset) {
          const previousHeight = stats.coverage_reset.previous_last_height;
          const startHeight = stats.start_height;
          add(panel, "p", `Coverage reset: ${stats.coverage_reset.reason}; previous last height ${previousHeight}.`);
          if (Number.isInteger(previousHeight) && Number.isInteger(startHeight) && startHeight > previousHeight + 1) {
            add(panel, "p", `Blocks ${previousHeight + 1} to ${startHeight - 1} were not scanned by the follower.`);
          }
          add(panel, "p", "At window start, up to 10 minutes of prior key/outflow activity and 24 hours of fresh-account context may be missing. Transfers windows are separate and may include earlier data.");
        }
      }
      if (previous === null) showEvents(data.events);
      else if (JSON.stringify(data.events) !== previous) { pending = data.events; apply.hidden = false; apply.textContent = "Show updated events"; }
      else { pending = null; apply.hidden = true; }
    } catch (error) {
      if (!paused && request === generation) { panel.replaceChildren(); add(panel, "p", "Live follower health is unavailable. Previously displayed events are retained and may be stale."); }
    } finally { clearTimeout(deadline); }
    if (!paused && request === generation) timer = setTimeout(poll, 30000);
  };
  pause.addEventListener("click", () => {
    paused = !paused; generation++; pause.textContent = paused ? "Resume updates" : "Pause updates";
    clearTimeout(timer); if (controller) controller.abort(); if (!paused) poll();
  });
  document.addEventListener("visibilitychange", () => {
    generation++; clearTimeout(timer); if (controller) controller.abort();
    if (!document.hidden && !paused) poll();
  });
  window.addEventListener("pagehide", () => { generation++; clearTimeout(timer); if (controller) controller.abort(); });
  window.addEventListener("pageshow", event => { if (event.persisted && !document.hidden && !paused) poll(); });
  poll();
})();
