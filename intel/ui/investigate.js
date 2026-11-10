(() => {
  "use strict";
  const accountId = value => typeof value === "string" && value.length >= 2 && value.length <= 64
    && /^[a-z0-9]+(?:[._-][a-z0-9]+)*$/.test(value) && value !== "testnet" && !value.endsWith(".testnet");
  const baseUnits = (value, decimals) => {
    if (typeof value !== "string" || !/^\d{1,39}$/.test(value)) return "Unavailable";
    if (!Number.isInteger(decimals) || decimals < 0 || decimals > 255) return value + " base units";
    const digits = value.replace(/^0+(?=\d)/, "").padStart(decimals + 1, "0");
    const whole = decimals ? digits.slice(0, -decimals) : digits;
    const fraction = decimals ? digits.slice(-decimals).replace(/0+$/, "") : "";
    return whole + (fraction ? "." + fraction : "");
  };
  const exactTime = value => {
    if (typeof value !== "string" || !/^\d{1,20}$/.test(value)) return "Not recorded";
    const stamp = BigInt(value), milliseconds = Number(stamp / 1000000n);
    if (!Number.isSafeInteger(milliseconds) || milliseconds > 8640000000000000) return "Not recorded";
    return new Date(milliseconds).toISOString().slice(0, 19).replace("T", " ") + "." + (stamp % 1000000000n).toString().padStart(9, "0") + " UTC";
  };
  const direction = (row, account) => row.from === account ? "Outgoing" : row.to === account ? "Incoming" : "Not established";
  const validPage = (data, account) => Boolean(data && data.account_id === account && data.network === "mainnet"
    && Array.isArray(data.items) && data.items.length <= 100 && data.pagination && typeof data.pagination === "object"
    && data.source && typeof data.source === "object" && data.coverage && typeof data.coverage === "object");
  const root = document.getElementById("account-investigation");
  if (!root) return;
  const form = document.getElementById("investigate-form"), input = document.getElementById("investigate-account");
  const status = document.getElementById("investigate-status"), title = document.getElementById("investigate-title");
  const watch = document.getElementById("investigate-watch"), save = document.getElementById("investigate-save");
  const graphHost = document.getElementById("investigate-graph"), graphButton = document.getElementById("investigate-load-graph");
  const graphDepth = document.getElementById("investigate-depth");
  const identityButton = document.getElementById("investigate-read-identity"), historyButton = document.getElementById("investigate-history-load");
  const historyKind = document.getElementById("investigate-history-kind"), backfillButton = document.getElementById("investigate-backfill");
  const backfillPages = document.getElementById("investigate-backfill-pages"), backfillStatus = document.getElementById("investigate-backfill-status");
  const backfills = new Map();
  const requests = new Map(), positions = new Map();
  let account = "", generation = 0, graph = null;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  };
  const button = (label, callback) => {
    const item = node("button", label); item.type = "button"; item.addEventListener("click", callback); return item;
  };
  const quoted = text => node("q", text === null || text === undefined ? "Not recorded" : text, "data mono");
  const details = (label, data) => {
    const item = node("details", undefined, "investigate-source-detail");
    item.append(node("summary", label), node("pre", JSON.stringify(data, null, 2))); return item;
  };
  function accountLink(value) {
    if (!accountId(value)) return node("span", "Not recorded");
    const link = node("a", value, "mono"); link.href = "/investigate.html?account=" + encodeURIComponent(value); return link;
  }
  function transactionLink(value) {
    if (typeof value !== "string" || !/^[1-9A-HJ-NP-Za-km-z]{20,100}$/.test(value)) return node("span", value || "Not recorded", "mono");
    const link = node("a", value, "mono"); link.href = "https://nearblocks.io/txns/" + encodeURIComponent(value); return link;
  }
  function table(id, headings, rows) {
    const item = node("table", undefined, "investigate-table"); item.id = id; item.setAttribute("role", "table");
    const head = node("thead"), header = node("tr"); header.setAttribute("role", "row");
    for (const text of headings) { const th = node("th", text); th.scope = "col"; th.setAttribute("role", "columnheader"); header.append(th); }
    head.append(header); const body = node("tbody"); body.setAttribute("role", "rowgroup");
    for (const values of rows) {
      const tr = node("tr"); tr.setAttribute("role", "row");
      values.forEach((value, index) => {
        const td = node("td"); td.setAttribute("data-label", headings[index]); td.setAttribute("role", "cell");
        td.append(value instanceof window.Node ? value : node("span", value === null || value === undefined ? "Unavailable" : value)); tr.append(td);
      }); body.append(tr);
    }
    item.append(head, body); return item;
  }
  function sourceInfo(parent, data) {
    const source = data.source;
    parent.append(node("p", (source.provider || "Provider not recorded") + " · Read " + (data.observed_at || "time unavailable"), "investigate-source"));
    if (typeof data.coverage.scope === "string") parent.append(node("p", data.coverage.scope, "investigate-scope"));
    parent.append(details("Source and coverage", {source, coverage: data.coverage}));
  }
  function pageControls(parent, kind, next, index, archived = false) {
    const nav = node("div", undefined, "investigate-pagination");
    const previous = button("Previous page", () => archived ? loadArchive(kind, positions.get("history-" + kind).history[index - 1], index - 1) : load(kind, positions.get(kind).history[index - 1], index - 1)); previous.disabled = index === 0;
    const following = button("Next page", () => archived ? loadArchive(kind, next, index + 1) : load(kind, next, index + 1)); following.disabled = next === null || next === undefined;
    nav.append(previous, node("span", "Page " + (index + 1)), following); parent.append(nav);
  }
  function renderProfile(parent, data) {
    if (!data || data.account_id !== account || data.network !== "mainnet" || typeof data.exists !== "boolean"
        || !Array.isArray(data.labels) || !data.source || !data.coverage) throw new Error("The account response could not be read.");
    if (!data.exists) {
      parent.append(node("p", "The finalized RPC reports that this account does not currently exist. Indexed history can still contain earlier activity.", "investigate-empty"));
    } else {
      const native = data.native;
      if (!native || typeof native.block_height !== "string" || !/^\d+$/.test(native.amount_yocto || "") || !/^\d+$/.test(native.locked_yocto || "")) throw new Error("The account balance response was incomplete.");
      const facts = node("dl", undefined, "investigate-native");
      for (const [label, value, unit, exact] of [["Account balance", window.SiyabNumbers.baseUnits(native.amount_yocto, 24), "NEAR", baseUnits(native.amount_yocto, 24)], ["Locked balance", window.SiyabNumbers.baseUnits(native.locked_yocto, 24), "NEAR", baseUnits(native.locked_yocto, 24)], ["Storage used", native.storage_usage, "bytes"], ["Finalized block", native.block_height, ""]]) {
        const item = node("div"); item.append(node("dt", label)); const dd = node("dd", value, "mono");
        if (exact !== undefined) dd.title = "Exact: " + exact + " " + unit;
        if (unit) dd.append(node("span", unit, "investigate-unit")); item.append(dd); facts.append(item);
      }
      parent.append(facts);
      parent.append(details("Exact finalized state", native));
    }
    if (data.labels.length) {
      const labels = node("ul", undefined, "investigate-labels");
      for (const assertion of data.labels) {
        if (!assertion || typeof assertion.label !== "string" || typeof assertion.provenance !== "string") throw new Error("The source-label response was incomplete.");
        const item = node("li"); item.append(quoted(assertion.label), node("span", assertion.provenance === "inferred" ? "Attribution hypothesis" : assertion.provenance.replaceAll("_", " "), "investigate-source"), details("Label source", assertion)); labels.append(item);
      }
      parent.append(node("h3", "Sourced labels"), labels);
    } else parent.append(node("p", "No current NEAR label is recorded in Siyab's committed store. An unlabelled account is not a favorable payment result.", "investigate-scope"));
    sourceInfo(parent, data);
  }
  function renderPage(parent, kind, data, position, index, archived = false) {
    if (archived ? !(data && data.account_id === account && data.network === "mainnet" && data.kind === kind && Array.isArray(data.items) && data.items.length <= 100 && data.pagination && Array.isArray(data.observations) && data.coverage && data.coverage.history_complete === false) : !validPage(data, account)) throw new Error("The indexed account page could not be read.");
    let rows, headings;
    if (kind === "holdings") {
      headings = ["Token contract", "Indexed balance", "Token metadata", "Indexed update block"];
      rows = data.items.map(item => {
        if (!item || !accountId(item.contract_id)) throw new Error("The token page contains an invalid contract.");
        const amount = node("div"); amount.append(node("span", baseUnits(item.balance, item.metadata?.decimals), "mono"));
        if (item.metadata && typeof item.metadata.symbol === "string") amount.append(quoted(item.metadata.symbol));
        const metadata = node("div"); metadata.append(quoted(item.metadata?.name || "Metadata unavailable"), details("Metadata read", {metadata: item.metadata, source: item.metadata_source, balance_status: item.balance_status}));
        return [accountLink(item.contract_id), amount, metadata, item.last_update_block_height];
      });
    } else if (kind === "transfers") {
      headings = ["Time (UTC)", "Direction", "Counterparty", "Amount and asset", "Transaction and evidence"];
      rows = data.items.map(item => {
        if (!item || !/^\d{1,39}$/.test(item.amount || "") || typeof item.asset_id !== "string") throw new Error("The transfer page contains an unreadable amount.");
        const amount = node("div"); amount.append(node("span", item.human_amount === null || item.human_amount === undefined ? item.amount + " base units" : item.human_amount, "mono"), quoted(item.asset_id));
        amount.append(node("small", item.usd_amount === null || item.usd_amount === undefined ? "USD unavailable" : item.usd_amount + " USD (provider value)"));
        const evidence = node("div"); evidence.append(transactionLink(item.transaction_hash), details("Transfer source", item));
        return [node("span", exactTime(item.block_timestamp), "mono"), direction(item, account), accountLink(item.from === account ? item.to : item.from), amount, evidence];
      });
    } else {
      headings = ["Time (UTC)", "Transaction", "Participation", "Execution", "Block and source"];
      rows = data.items.map(item => {
        if (!item || typeof item.transaction_hash !== "string") throw new Error("The transaction page contains an unreadable record.");
        const roles = [["is_any_signer", "Signer"], ["is_receiver", "Receiver"], ["is_predecessor", "Predecessor"], ["is_event_log", "Event log"], ["is_action_arg", "Action argument"]].filter(([field]) => item[field] === true).map(([, label]) => label);
        const provenance = node("div"); provenance.append(node("span", item.tx_block_height, "mono"), details("Transaction participation", item));
        return [node("span", exactTime(item.tx_block_timestamp), "mono"), transactionLink(item.transaction_hash), roles.join(", ") || "Indexed participation", item.is_success === true ? "Succeeded" : item.is_success === false ? "Failed" : "Not established", provenance];
      });
    }
    if (rows.length) {
      const id = "investigate-" + (archived ? "history-" : "") + kind + "-table";
      const exportButton = node("button", "Export shown rows CSV"); exportButton.type = "button"; exportButton.setAttribute("data-export-table", id);
      parent.append(exportButton, table(id, headings, rows));
    } else parent.append(node("p", archived ? "No retained rows were returned. Earlier or unindexed activity may still exist." : "No rows were returned in this provider page. This does not establish absence of earlier or unindexed activity.", "investigate-empty"));
    const state = positions.get((archived ? "history-" : "") + kind);
    const next = archived ? data.pagination.next_before : kind === "holdings" ? data.pagination.next_offset : data.pagination.next_cursor;
    if (next !== null && next !== undefined && (typeof next !== (archived || kind === "holdings" ? "number" : "string") || next === position || archived && (!Number.isSafeInteger(next) || next <= 0))) throw new Error("The provider pagination could not advance.");
    state.history[index] = position; state.history.length = index + 1;
    pageControls(parent, kind, next, index, archived);
    if (archived) {
      parent.append(node("p", data.coverage.stored_rows + " retained rows for this account and record type. This is not complete chain history.", "investigate-scope"));
      const dates = data.observations.map(item => item.observed_at).filter(value => typeof value === "string").sort();
      parent.append(node("p", data.observations.length + " original provider observations" + (dates.length ? "; read " + dates[0] + (dates.at(-1) === dates[0] ? "" : " through " + dates.at(-1)) : "; read dates unavailable") + ".", "investigate-source"));
      parent.append(details("Original observations and archive coverage", {observations: data.observations, coverage: data.coverage}));
    } else sourceInfo(parent, data);
  }
  function renderIdentity(parent, data) {
    if (!data || data.account_id !== account || data.network !== "mainnet" || !data.contract || !data.validator || !data.coverage || !Array.isArray(data.sources) || !Array.isArray(data.registry_entities) || !Array.isArray(data.assertions)) throw new Error("The identity response could not be read.");
    const contract = data.contract, validator = data.validator;
    const facts = node("dl", undefined, "investigate-identity-facts");
    for (const [label, value] of [["Contract", contract.status === "deployed" ? "Deployed code observed" : contract.status === "none" ? "No deployed code at this block" : "Unknown"], ["Finalized block", contract.block_height], ["Code hash", contract.code_hash], ["Validator", validator.status === "current" ? "In the returned current validator set" : validator.status === "not_current" ? "Not in the returned current validator set" : "Unknown"], ["Validator stake (NEAR)", baseUnits(validator.stake_yocto, 24)], ["Epoch start block", validator.epoch_start_height], ["Epoch", validator.epoch_height]]) {
      const row = node("div"); row.append(node("dt", label), node("dd", value === null || value === undefined ? "Unknown" : value, "mono")); facts.append(row);
    }
    parent.append(facts, details("Contract and validator read sources", {contract: contract.source, validator: validator.source}));
    parent.append(node("h3", "Contract-reported token metadata"));
    if (data.token_metadata) {
      const metadata = data.token_metadata;
      parent.append(table("investigate-token-identity", ["Name", "Symbol", "Decimals", "Standard"], [[quoted(metadata.name), quoted(metadata.symbol), metadata.decimals, quoted(metadata.spec)]]), details("Token metadata source", metadata.source));
    } else parent.append(node("p", "Token metadata was not established by this read. A failed or unsupported metadata method does not establish that the account is not a token.", "investigate-empty"));
    parent.append(node("h3", "Contract-reported source metadata"));
    if (data.contract_source) {
      const metadata = data.contract_source;
      parent.append(table("investigate-contract-identity", ["Version", "Reported HTTPS link", "Reported standards"], [[quoted(metadata.version), quoted(metadata.link), quoted(metadata.standards === null ? "Not reported" : JSON.stringify(metadata.standards))]]), details("Source metadata read", metadata.source));
    } else parent.append(node("p", "Source metadata was not established. Reproducible builds and ownership are not verified.", "investigate-empty"));
    parent.append(node("h3", "Recorded registry memberships"));
    const registry = node("ul", undefined, "investigate-labels");
    for (const entity of data.registry_entities) {
      const item = node("li"); item.append(quoted(entity.name), node("span", "Observed " + entity.observed_at, "investigate-source"), details("Membership evidence", entity)); registry.append(item);
    }
    parent.append(data.registry_entities.length ? registry : node("p", "No matching membership is retained in the committed registry. This does not establish absence from other registries.", "investigate-empty"));
    if (data.assertions.length) parent.append(details("Recorded assertions with original dates and sources", data.assertions));
    parent.append(node("p", "Identity read " + data.observed_at + ". Contract-reported metadata and recorded memberships do not establish verified brand identity, current ownership or a payment verdict.", "investigate-scope"), details("Identity sources and coverage", {sources: data.sources, coverage: data.coverage}));
  }
  async function readExtra(key, path, section, content, renderResult) {
    if (!accountId(account)) return;
    const old = requests.get(key); if (old) old.abort();
    const controller = new AbortController(), version = generation;
    requests.set(key, controller); section.setAttribute("aria-busy", "true");
    if (key === "backfill") backfillButton.disabled = true;
    const focused = content.contains(document.activeElement);
    content.replaceChildren(node("p", "Reading " + (key === "identity" ? "identity evidence" : key === "archive" ? "retained observations" : "bounded provider pages") + "…", "investigate-empty"));
    const timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch("/live/accounts/" + encodeURIComponent(account) + "/" + path, {signal: controller.signal, credentials: "same-origin", headers: {Accept: "application/json"}});
      if (!(response.headers.get("content-type") || "").toLowerCase().includes("application/json")) throw new Error("The server did not return account-data JSON.");
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.message === "string" ? data.message : "The account read is unavailable (HTTP " + response.status + ").");
      if (version !== generation || requests.get(key) !== controller) return;
      const rendered = node("div"); renderResult(rendered, data); content.replaceChildren(...rendered.childNodes);
      if (focused) section.querySelector("h2").focus({preventScroll: true});
    } catch (error) {
      if (version !== generation || requests.get(key) !== controller) return;
      content.replaceChildren(node("p", controller.signal.aborted ? key === "backfill" ? "The bounded read timed out. Partial provider observations may already be retained; retry resumes the saved backfill position." : "The bounded read timed out. Try again." : error.message, "investigate-empty"));
      let retryPath = path;
      if (key === "backfill") {
        const parameters = new URLSearchParams(path.split("?")[1]); parameters.delete("cursor"); retryPath = "backfill?" + parameters;
        backfills.delete(historyKind.value); backfillButton.textContent = "Resume backfill";
      }
      const retry = button("Retry " + key, () => readExtra(key, retryPath, section, content, renderResult)); content.append(retry);
      if (focused) retry.focus({preventScroll: true});
    } finally {
      clearTimeout(timer);
      if (requests.get(key) === controller) {
        requests.delete(key); section.setAttribute("aria-busy", key !== "identity" && (requests.has("archive") || requests.has("backfill")) ? "true" : "false");
        if (key === "backfill") backfillButton.disabled = backfills.get(historyKind.value)?.exhausted === true;
      }
    }
  }
  function loadArchive(kind, before = 0, index = 0) {
    const section = document.getElementById("investigate-history");
    if (!positions.has("history-" + kind)) positions.set("history-" + kind, {history: []});
    return readExtra("archive", "history?kind=" + kind + "&before=" + before + "&limit=25", section, section.querySelector("[data-live-content]"), (parent, data) => renderPage(parent, kind, data, before, index, true));
  }
  async function load(kind, position = kind === "holdings" ? 0 : null, index = 0) {
    const section = document.getElementById("investigate-" + kind), content = section.querySelector("[data-live-content]");
    const previous = requests.get(kind); if (previous) previous.abort();
    const controller = new AbortController(); requests.set(kind, controller);
    const version = generation, subject = account, focused = section.contains(document.activeElement);
    const timer = setTimeout(() => controller.abort(), 25000);
    section.setAttribute("aria-busy", "true"); content.replaceChildren(node("p", "Reading " + (kind === "profile" ? "finalized account state" : "indexed " + kind) + "…", "investigate-empty"));
    try {
      const parameters = new URLSearchParams();
      if (kind === "holdings") { parameters.set("offset", String(position)); parameters.set("limit", "10"); }
      else if (kind !== "profile") { parameters.set("limit", "25"); if (position !== null) parameters.set("cursor", position); }
      const path = "/live/accounts/" + encodeURIComponent(subject) + "/" + kind + (parameters.size ? "?" + parameters : "");
      const response = await fetch(path, {signal: controller.signal, credentials: "same-origin", headers: {Accept: "application/json"}});
      if (!(response.headers.get("content-type") || "").includes("application/json")) throw new Error("The server did not return an account-data response.");
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.message === "string" ? data.message : "The account read is unavailable (HTTP " + response.status + ").");
      if (version !== generation || requests.get(kind) !== controller) return;
      const rendered = node("div");
      if (kind === "profile") renderProfile(rendered, data); else renderPage(rendered, kind, data, position, index);
      content.replaceChildren(...rendered.childNodes);
      if (focused) section.querySelector("h2").focus({preventScroll: true});
    } catch (error) {
      if (version !== generation || requests.get(kind) !== controller) return;
      content.replaceChildren(node("p", controller.signal.aborted ? "The account read timed out. Try again." : error.message, "investigate-empty"));
      const retry = button("Retry " + kind, () => load(kind, position, index)); content.append(retry);
      if (focused) retry.focus({preventScroll: true});
    } finally {
      clearTimeout(timer);
      if (requests.get(kind) === controller) { requests.delete(kind); section.setAttribute("aria-busy", "false"); }
    }
  }
  function stop() {
    generation++;
    for (const controller of requests.values()) controller.abort(); requests.clear();
    if (graph && window.SiyabGraph?.destroy) window.SiyabGraph.destroy(graph);
    graph = null;
  }
  function start(subject, updateHistory = true) {
    if (!accountId(subject)) { status.textContent = "Enter a valid NEAR mainnet account. Testnet accounts are outside this investigation view."; input.focus(); return; }
    stop(); account = subject; input.value = subject; title.textContent = subject;
    watch.dataset.watchAccount = subject; watch.disabled = false; save.disabled = false; graphButton.disabled = false;
    document.getElementById("investigate-refresh").disabled = false;
    identityButton.disabled = false; historyButton.disabled = false; backfillButton.disabled = false;
    backfills.clear(); backfillButton.textContent = "Backfill older pages"; backfillStatus.replaceChildren();
    for (const kind of ["identity", "history"]) {
      const section = document.getElementById("investigate-" + kind); section.setAttribute("aria-busy", "false");
      section.querySelector("[data-live-content]").replaceChildren(node("p", kind === "identity" ? "Read contract, token and validator observations on demand. Metadata does not establish ownership." : "Load retained rows or request a bounded backfill. Retained history can have gaps.", "investigate-empty"));
    }
    positions.delete("history-transfers"); positions.delete("history-transactions");
    document.dispatchEvent(new CustomEvent("siyab:account-selected", {detail: {account: subject}}));
    status.textContent = "Mainnet reads are separate provider observations. No payment is signed or sent.";
    const url = new URL(window.location.href);
    if (updateHistory) { for (const key of [...url.searchParams.keys()]) if (key.startsWith("g_") || key === "observation") url.searchParams.delete(key); url.searchParams.set("account", subject); url.searchParams.set("depth", graphDepth.value); window.history.pushState({}, "", url); }
    else graphDepth.value = ["1", "2"].includes(url.searchParams.get("depth")) ? url.searchParams.get("depth") : "1";
    graphHost.replaceChildren(node("p", "Load a bounded neighborhood from the latest indexed transfers. It is not complete account history.", "investigate-empty"));
    for (const kind of ["profile", "holdings", "transfers", "transactions"]) { positions.set(kind, {history: []}); load(kind); }
    const observation = url.searchParams.get("observation");
    if (!updateHistory && observation && /^[a-f0-9]{64}$/.test(observation)) loadGraph("/live/account-data/" + observation + ".json");
  }
  form.addEventListener("submit", event => { event.preventDefault(); start(input.value.trim()); });
  document.getElementById("investigate-refresh").addEventListener("click", () => { if (account) start(account, false); });
  identityButton.addEventListener("click", () => {
    const section = document.getElementById("investigate-identity");
    return readExtra("identity", "identity", section, section.querySelector("[data-live-content]"), renderIdentity);
  });
  historyButton.addEventListener("click", () => loadArchive(historyKind.value));
  historyKind.addEventListener("change", () => {
    for (const key of ["archive", "backfill"]) { const controller = requests.get(key); requests.delete(key); if (controller) controller.abort(); }
    const section = document.getElementById("investigate-history"); section.setAttribute("aria-busy", "false");
    section.querySelector("[data-live-content]").replaceChildren(node("p", "Load retained " + historyKind.value + " rows.", "investigate-empty"));
    backfillStatus.replaceChildren(); const state = backfills.get(historyKind.value);
    backfillButton.disabled = !account || state?.exhausted === true;
    backfillButton.textContent = state?.exhausted ? "Provider pages exhausted" : state?.cursor ? "Continue backfill" : "Backfill older pages";
  });
  backfillButton.addEventListener("click", () => {
    const kind = historyKind.value, maximum = backfillPages.value === "2" ? "2" : "1";
    const parameters = new URLSearchParams({kind, max_pages: maximum});
    const state = backfills.get(kind); if (state?.cursor) parameters.set("cursor", state.cursor);
    const section = document.getElementById("investigate-history");
    return readExtra("backfill", "backfill?" + parameters, section, backfillStatus, (parent, data) => {
      if (!data || !Number.isInteger(data.pages) || data.pages < 0 || data.pages > Number(maximum) || !Number.isInteger(data.inserted) || data.inserted < 0 || !(data.next_cursor === null || typeof data.next_cursor === "string" && data.next_cursor.length > 0) || !data.coverage || typeof data.coverage.provider_pages_exhausted !== "boolean" || data.coverage.history_complete !== false) throw new Error("The backfill response could not be read.");
      backfills.set(kind, {cursor: data.next_cursor, exhausted: data.coverage.provider_pages_exhausted});
      backfillButton.disabled = data.coverage.provider_pages_exhausted;
      backfillButton.textContent = data.coverage.provider_pages_exhausted ? "Provider pages exhausted" : "Continue backfill";
      parent.append(node("p", "Read " + data.pages + " provider pages; retained " + data.inserted + " new rows. " + (data.coverage.provider_pages_exhausted ? "The provider returned no further page. " : "More provider pages remain. ") + "History may still contain gaps.", "investigate-scope"), details("Backfill coverage and resume position", data));
      loadArchive(kind);
    });
  });
  async function loadGraph(source) {
    if (!accountId(account) || !window.SiyabGraph) return;
    const version = generation;
    if (graph && window.SiyabGraph.destroy) window.SiyabGraph.destroy(graph);
    graph = node("section"); graph.setAttribute("data-siyab-graph", "");
    graph.setAttribute("data-graph-account", account);
    graph.setAttribute("data-graph-src", source);
    graphHost.replaceChildren(graph); graphButton.disabled = true;
    try { await window.SiyabGraph.mount(graph); }
    finally { if (version === generation) graphButton.disabled = false; }
  }
  graphButton.addEventListener("click", () => {
    const depth = ["1", "2"].includes(graphDepth.value) ? graphDepth.value : "1";
    const url = new URL(window.location.href); url.searchParams.set("depth", depth); url.searchParams.delete("observation");
    window.history.replaceState({}, "", url);
    return loadGraph("/live/accounts/" + encodeURIComponent(account) + "/graph?depth=" + depth);
  });
  window.addEventListener("pagehide", stop);
  window.addEventListener("pageshow", event => { if (event.persisted && account) start(account, false); });
  window.addEventListener("popstate", () => {
    const subject = new URL(window.location.href).searchParams.get("account");
    if (subject) { start(subject, false); return; }
    stop(); account = ""; input.value = ""; title.textContent = "Investigate account";
    for (const control of [watch, save, graphButton, identityButton, historyButton, backfillButton, document.getElementById("investigate-refresh")]) control.disabled = true;
    backfillStatus.replaceChildren();
    for (const kind of ["profile", "holdings", "transfers", "transactions", "identity", "history"]) {
      const section = document.getElementById("investigate-" + kind); section.setAttribute("aria-busy", "false");
      section.querySelector("[data-live-content]").replaceChildren(node("p", "Select an account to read this data.", "investigate-empty"));
    }
    graphHost.replaceChildren(); status.textContent = "Enter an account to read mainnet data. No payment is signed or sent.";
  });
  const initial = new URL(window.location.href).searchParams.get("account");
  if (initial) start(initial, false);
})();
