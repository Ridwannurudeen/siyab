(function () {
  "use strict";
  const node = (tag, text, className) => {
    const value = document.createElement(tag);
    if (text !== undefined) value.textContent = String(text);
    if (className) value.className = className;
    return value;
  };
  function pages(container, current, count, size, change) {
    const total = Math.max(1, Math.ceil(count / size));
    container.hidden = false;
    container.replaceChildren();
    for (const [label, direction] of [["Previous", -1], ["Next", 1]]) {
      const control = node("button", label);
      control.type = "button";
      control.disabled = current + direction < 0 || current + direction >= total;
      control.addEventListener("click", async () => {
        const restoreFocus = document.activeElement === control;
        await change(current + direction);
        if (!restoreFocus || (document.activeElement !== control && document.activeElement !== document.body)) return;
        const preferred = container.children[direction < 0 ? 0 : 2];
        const alternate = container.children[direction < 0 ? 2 : 0];
        if (preferred && !preferred.disabled) preferred.focus();
        else if (alternate && !alternate.disabled) alternate.focus();
      });
      container.append(control);
      if (direction < 0) container.append(node("span", `Page ${current + 1} of ${total}`));
    }
  }
  function tableRow(values, headings) {
    const row = node("tr"); row.setAttribute("role", "row");
    values.forEach((value, index) => {
      const cell = node("td"); cell.setAttribute("role", "cell");
      const label = node("span", headings[index], "cell-label"); label.setAttribute("aria-hidden", "true");
      cell.append(label, typeof value === "string" ? document.createTextNode(value) : value); row.append(cell);
    });
    return row;
  }
  for (const section of document.querySelectorAll("[data-profile-table]")) {
    const input = section.querySelector("[data-profile-search]");
    const pagination = section.querySelector("[data-profile-table-pages]");
    const status = section.querySelector("[data-profile-table-status]");
    section.querySelector(".profile-table-tools").hidden = false;
    let page = 0;
    function render() {
      const all = Array.from(section.querySelectorAll("tbody tr"));
      const matching = all.filter(row => row.textContent.toLowerCase().includes(input.value.trim().toLowerCase()));
      page = Math.min(page, Math.max(0, Math.ceil(matching.length / 20) - 1));
      const visible = new Set(matching.slice(page * 20, (page + 1) * 20));
      all.forEach(row => { row.hidden = !visible.has(row); });
      status.textContent = matching.length ? `${page * 20 + 1} to ${Math.min((page + 1) * 20, matching.length)} of ${matching.length} matching records.` : "No matching recorded rows. Clear the search to return to the collection.";
      pages(pagination, page, matching.length, 20, next => { page = next; render(); });
    }
    input.addEventListener("input", () => { page = 0; render(); });
    section.addEventListener("profile:refresh", () => { page = 0; render(); });
    render();
  }

  for (const section of document.querySelectorAll("[data-profile-live]")) {
    const output = section.querySelector("[data-profile-live-result]");
    const account = section.dataset.profileLive;
    let generation = 0;
    let controller;
    async function read(kind) {
      const request = ++generation;
      if (controller) controller.abort();
      const currentController = new AbortController(); controller = currentController;
      const timeout = setTimeout(() => currentController.abort(), 15000);
      output.replaceChildren(node("p", kind === "payment" ? "Reading a 1 NEAR payment check from mainnet." : "Reading current mainnet access keys."));
      output.setAttribute("aria-busy", "true");
      try {
        const path = kind === "payment" ? `/live/verdict?${new URLSearchParams({receiver: account, amount: "1000000000000000000000000", token: "near"})}` : `/live/scan/${encodeURIComponent(account)}`;
        const response = await fetch(path, {headers: {Accept: "application/json"}, signal: currentController.signal});
        if (!response.ok || !(response.headers.get("content-type") || "").includes("application/json")) throw new Error("Mainnet checks are unavailable on this copy. Try again or open Payment and key checks.");
        const data = await response.json();
        if (request !== generation) return;
        if (kind === "payment") {
          if (!data || !["ALLOW", "ASK", "DENY"].includes(data.outcome) || data.verdict !== null || data.signing !== "never"
              || !Array.isArray(data.checks) || !data.checks.length || !data.checks.every(check => check && typeof check.name === "string"
                && ["pass", "signal", "unknown"].includes(check.status) && typeof check.detail === "string")
              || !Array.isArray(data.reasons) || !data.reasons.every(reason => typeof reason === "string")
              || typeof data.evidence_hash !== "string" || !/^[a-f0-9]{64}$/.test(data.evidence_hash)
              || !data.freshness || typeof data.freshness !== "object"
              || (data.outcome === "ALLOW" && (data.action !== "sign" || !data.checks.every(check => check.status === "pass")
                || !Number.isInteger(data.freshness.block_height) || data.freshness.block_height < 1
                || !Number.isFinite(data.freshness.timestamp_ms) || data.freshness.timestamp_ms < 0))) {
            throw new Error("The mainnet response was incomplete. No favorable payment result is established.");
          }
          output.replaceChildren(node("strong", data.outcome, `profile-outcome ${data.outcome.toLowerCase()}`),
            node("p", `1 NEAR to ${account}. Read only; signing never.`));
          const list = node("ul", undefined, "profile-checks");
          data.checks.forEach(check => { const item = node("li"); item.append(node("strong", `${check.name}: ${check.status}`), node("p", check.detail)); list.append(item); });
          const detail = node("details", undefined, "profile-source-detail"); detail.append(node("summary", "Evidence hash and freshness"), node("pre", JSON.stringify({evidence_hash: data.evidence_hash, freshness: data.freshness}, null, 2)));
          output.append(list, detail);
        } else {
          const fields = ["full_access_keys", "empty_method_names", "dead_keys", "unlimited_allowance"];
          if (!data || !Number.isInteger(data.access_keys) || data.access_keys < 0
              || !fields.every(field => Array.isArray(data[field]) && data[field].length <= data.access_keys && data[field].every(key => typeof key === "string"))
              || !Array.isArray(data.findings) || !data.findings.every(line => typeof line === "string")
              || !Number.isInteger(data.read_at_block) || data.read_at_block < 1) throw new Error("Access keys could not be read. Missing key data is unknown, not a clean result.");
          output.replaceChildren(node("strong", `${data.access_keys} access keys read`), node("p", `Mainnet block ${data.read_at_block}. This is not a payment verdict.`));
          const list = node("ul");
          for (const [field, label] of [["full_access_keys", "Full access"], ["empty_method_names", "Empty method list"], ["dead_keys", "Zero allowance"], ["unlimited_allowance", "Unlimited allowance"]]) list.append(node("li", `${label}: ${data[field].length}`));
          output.append(list);
          const detail = node("details", undefined, "profile-source-detail"); detail.append(node("summary", "Read findings and public keys"), node("pre", JSON.stringify(data, null, 2))); output.append(detail);
        }
        output.append(node("p", `Response read at ${new Date().toISOString()}`, "profile-observed"));
      } catch (error) {
        if (request === generation) output.replaceChildren(node("p", currentController.signal.aborted ? "The mainnet read timed out. Try the check again." : error.message, "empty-state"));
      } finally {
        clearTimeout(timeout);
        if (request === generation) output.setAttribute("aria-busy", "false");
      }
    }
    section.querySelector("[data-profile-payment]").addEventListener("click", () => read("payment"));
    section.querySelector("[data-profile-keys]").addEventListener("click", () => read("keys"));
    window.addEventListener("pagehide", () => { generation++; if (controller) controller.abort(); });
  }

  for (const section of document.querySelectorAll("[data-profile-activity]")) {
    const config = JSON.parse(section.querySelector("[data-profile-sources]").textContent);
    const search = section.querySelector("[data-activity-search]");
    const direction = section.querySelector("[data-activity-direction]");
    const status = section.querySelector("[data-activity-status]");
    const navigation = section.querySelector("[data-activity-pages]");
    const retry = section.querySelector("[data-activity-retry]");
    const table = section.querySelector("#profile-transfers");
    const headings = Array.from(table.querySelectorAll("th"), heading => heading.textContent);
    const counterparties = section.querySelector('[data-profile-table="profile-counterparties"]');
    const accountPaths = new Map();
    let records = null, pending = null, page = 0, revision = 0, counterpartyQuery = null;
    section.querySelector(".profile-table-tools").hidden = false;
    const accountLink = account => {
      const quote = node("q", account, "data");
      if (!accountPaths.has(account)) return quote;
      const link = node("a"); link.href = accountPaths.get(account); link.append(quote); return link;
    };
    function render() {
      const query = search.value.trim().toLowerCase();
      const matches = records.filter(row => (!query || `${row.from} ${row.to} ${row.asset_id}`.toLowerCase().includes(query))
        && (!direction.value || (direction.value === "outbound" ? row.from === config.subject : row.to === config.subject)));
      page = Math.max(0, Math.min(page, Math.ceil(matches.length / 20) - 1));
      const body = table.querySelector("tbody"); body.replaceChildren();
      for (const row of matches.slice(page * 20, (page + 1) * 20)) {
        const outbound = row.from === config.subject;
        const decimals = row.asset_id === "native:near" ? 24 : row.amount_decimals;
        const amount = node("span", Number.isInteger(decimals) ? window.SiyabNumbers.baseUnits(row.amount, decimals) : `${row.amount} base units`, "profile-value");
        amount.append(node("q", row.asset_id, "data profile-asset"));
        const evidence = node("div");
        const link = node("a", "Recorded evidence"); link.href = `/flows/${row.recording}.html`;
        const detail = node("details", undefined, "profile-source-detail");
        detail.append(node("summary", "Transfer details"), node("pre", JSON.stringify({block: row.block, transaction: row.transaction_hash, receipt: row.receipt_id, base_units: row.amount, recorded: row.fetched_at}, null, 2)));
        evidence.append(link, detail);
        const milliseconds = Number(BigInt(row.block_timestamp) / 1000000n);
        const stamp = new Date(milliseconds).toISOString().replace("T", " ").replace(".000Z", " UTC");
        const tr = tableRow([stamp, row.from === row.to ? "Self" : outbound ? "Outbound" : "Inbound", accountLink(outbound ? row.to : row.from), amount,
          row.usd_amount === null ? "Unavailable" : window.SiyabNumbers.decimal(row.usd_amount, 2), evidence], headings);
        tr.dataset.profileTransferRow = ""; body.append(tr);
      }
      status.textContent = `Showing ${matches.length ? page * 20 + 1 : 0} to ${Math.min((page + 1) * 20, matches.length)} of ${matches.length} matching recorded rows. Newest first.`;
      retry.hidden = true;
      pages(navigation, page, matches.length, 20, next => refresh(next));
      const scope = `${query}\u0000${direction.value}`;
      if (scope !== counterpartyQuery) {
        const counts = new Map();
        matches.forEach(row => { const account = row.from === config.subject ? row.to : row.from; counts.set(account, (counts.get(account) || 0) + 1); });
        const counterBody = counterparties.querySelector("tbody"); counterBody.replaceChildren();
        [...counts].sort((a, b) => b[1] - a[1]).forEach(([account, count]) => counterBody.append(tableRow([accountLink(account), String(count)], ["Counterparty", "Recorded rows"])));
        counterparties.dispatchEvent(new Event("profile:refresh"));
        section.querySelector("[data-counterparties-scope]").textContent = `${counts.size} counterparties across ${matches.length} rows matching the activity filters. Counts measure recorded rows, not value or loss.`;
        counterpartyQuery = scope;
      }
    }
    async function load() {
      if (records) return;
      if (!pending) pending = (async () => {
        const combined = [];
        for (const source of config.sources) {
          if (!/^\/graph-data\/[a-f0-9]{64}\.json$/.test(source.url) || !/^[A-Za-z0-9._-]+$/.test(source.name)) throw new Error("Recorded source address is unreadable.");
          const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 15000);
          try {
            const response = await fetch(source.url, {signal: controller.signal, headers: {Accept: "application/json"}});
            if (!response.ok) throw new Error("The recorded transfer source is unavailable.");
            const payload = await response.json();
            if (!payload || payload.kind !== "flow" || !Array.isArray(payload.records) || !payload.source || typeof payload.source.fetched_at !== "string"
                || !payload.records.every(row => row && ["from", "to", "asset_id", "amount", "transaction_hash", "receipt_id", "block_timestamp"].every(field => typeof row[field] === "string")
                  && /^\d{1,22}$/.test(row.block_timestamp) && BigInt(row.block_timestamp) <= 8640000000000000000000n && Number.isInteger(row.block) && row.block >= 0
                  && (row.human_amount === null || typeof row.human_amount === "string") && (row.usd_amount === null || typeof row.usd_amount === "string"))) throw new Error("The recorded transfer source is incomplete.");
            for (const item of payload.nodes || []) if (item && typeof item.id === "string" && typeof item.url === "string" && /^\/accounts\/[A-Za-z0-9._-]+\.html$/.test(item.url)) accountPaths.set(item.id, item.url);
            payload.records.filter(row => row.from === config.subject || row.to === config.subject).forEach(row => combined.push({...row, recording: source.name, fetched_at: payload.source.fetched_at}));
          } finally { clearTimeout(timeout); }
        }
        records = combined.sort((a, b) => BigInt(a.block_timestamp) < BigInt(b.block_timestamp) ? 1 : BigInt(a.block_timestamp) > BigInt(b.block_timestamp) ? -1 : 0);
      })().finally(() => { pending = null; });
      await pending;
    }
    async function refresh(next = 0) {
      const current = ++revision;
      const recovering = document.activeElement === retry;
      status.textContent = "Loading recorded transfer rows. The current view is retained.";
      if (recovering) { status.tabIndex = -1; status.focus(); }
      retry.hidden = true;
      section.setAttribute("aria-busy", "true");
      try {
        await load();
        if (current === revision) { page = next; render(); if (recovering && document.activeElement === status) search.focus(); }
      } catch (error) {
        if (current === revision) {
          status.textContent = `${error.message} Retry or open the complete recorded trace. The displayed rows remain recorded evidence.`;
          retry.hidden = false;
          if (recovering && document.activeElement === status) retry.focus();
        }
      } finally { if (current === revision) section.setAttribute("aria-busy", "false"); }
    }
    search.addEventListener("input", () => refresh());
    direction.addEventListener("change", () => refresh());
    retry.addEventListener("click", () => refresh());
    counterparties.querySelector("[data-profile-search]").addEventListener("input", () => { if (!records) refresh(); });
    pages(navigation, 0, config.total_rows, 20, next => refresh(next));
  }
})();
