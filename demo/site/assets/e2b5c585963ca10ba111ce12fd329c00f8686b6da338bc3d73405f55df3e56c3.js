(() => {
  "use strict";
  const U64_MAX = 18446744073709551615n, MAX_CONTRACTS = 20, MAX_POINTS = 500, TIMEOUT_MS = 25000;
  const HASH = /^[a-f0-9]{64}$/;
  const accountId = value => typeof value === "string" && value.length >= 2 && value.length <= 64
    && /^[a-z0-9]+(?:[._-][a-z0-9]+)*$/.test(value) && value !== "testnet" && !value.endsWith(".testnet");
  const decimalText = value => typeof value === "string" && value.length <= 4096 && /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(value);
  const baseUnitText = value => typeof value === "string" && /^[0-9]{1,39}$/.test(value) && BigInt(value) <= 340282366920938463463374607431768211455n;
  const utcText = value => typeof value === "string" && value.length <= 40
    && /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$/.test(value) && Number.isFinite(Date.parse(value));
  const utcLabel = value => utcText(value) ? value.replace("T", " ").replace("Z", " UTC") : "Time not recorded";
  const secondsLabel = value => Number.isSafeInteger(value) && value >= 0 && value <= 8640000000000
    ? new Date(value * 1000).toISOString().replace("T", " ").replace(".000Z", " UTC") : "Time not recorded";
  const assetIdValid = value => value === "native:near" || typeof value === "string" && value.startsWith("nep141:") && accountId(value.slice(7));
  const evidencePath = (url, id) => typeof id === "string" && HASH.test(id) && url === "/live/portfolio-data/" + id + ".json" ? url : null;
  const finite = value => {
    if (!decimalText(value)) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };
  function parseBlock(text) {
    const value = typeof text === "string" ? text.trim() : "";
    if (!value) return {error: "Enter a block height."};
    if (!/^[1-9][0-9]{0,19}$/.test(value) || BigInt(value) > U64_MAX) return {error: "Use a positive whole block height, such as 130000000, with no separators."};
    return {value};
  }
  function parseContracts(text) {
    const parts = (typeof text === "string" ? text : "").split(/[\s,]+/).filter(Boolean);
    const contracts = [];
    for (const part of parts) {
      if (!accountId(part)) return {error: "\"" + part.slice(0, 80) + "\" is not a valid NEAR mainnet contract account."};
      if (contracts.includes(part)) return {error: "List each contract once; " + part + " appears more than once."};
      contracts.push(part);
    }
    if (contracts.length > MAX_CONTRACTS) return {error: "List at most " + MAX_CONTRACTS + " contracts; " + contracts.length + " were entered."};
    return {value: contracts};
  }
  function checkPortfolio(data, account, kind) {
    const fail = () => { throw new Error(kind === "historical" ? "The archived balance response could not be read." : "The valuation response could not be read."); };
    if (!data || typeof data !== "object" || data.account_id !== account || data.network !== "mainnet" || data.kind !== kind
      || !utcText(data.observed_at) || !utcText(data.effective_at) || typeof data.asset_scope_id !== "string" || !HASH.test(data.asset_scope_id)
      || !Array.isArray(data.assets) || data.assets.length < 1 || data.assets.length > 101 || !Array.isArray(data.sources)
      || !data.summary || typeof data.summary !== "object" || !data.coverage || typeof data.coverage !== "object"
      || data.coverage.history_complete !== false || typeof data.coverage.inventory_complete !== "boolean") fail();
    if (kind === "historical" && data.coverage.inventory_complete) fail();
    const summary = data.summary;
    if (!(summary.known_value_usd === null || decimalText(summary.known_value_usd)) || !(summary.total_value_usd === null || decimalText(summary.total_value_usd))
      || !Number.isSafeInteger(summary.priced_assets) || !Number.isSafeInteger(summary.unpriced_assets) || summary.priced_assets < 0 || summary.unpriced_assets < 0
      || summary.priced_assets + summary.unpriced_assets !== data.assets.length) fail();
    if (summary.total_value_usd !== null && (data.coverage.inventory_complete !== true || summary.unpriced_assets !== 0 || summary.known_value_usd !== summary.total_value_usd)) fail();
    if ((summary.known_value_usd === null) !== (summary.priced_assets === 0)) fail();
    const seen = new Set();
    for (const asset of data.assets) {
      if (!asset || typeof asset !== "object" || !assetIdValid(asset.asset_id) || seen.has(asset.asset_id)) fail();
      seen.add(asset.asset_id);
      if (asset.contract_id !== (asset.asset_id === "native:near" ? null : asset.asset_id.slice(7))) fail();
      if (!(asset.balance_base_units === null || baseUnitText(asset.balance_base_units)) || !(asset.balance === null || decimalText(asset.balance))
        || !(asset.value_usd === null || decimalText(asset.value_usd)) || !(asset.decimals === null || Number.isInteger(asset.decimals) && asset.decimals >= 0 && asset.decimals <= 255)
        || !(asset.metadata === null || asset.metadata && typeof asset.metadata === "object") || typeof asset.status !== "string") fail();
      const price = asset.price;
      if (!price || typeof price !== "object" || typeof price.status !== "string" || !(price.usd === null || decimalText(price.usd))
        || !(price.confidence === null || decimalText(price.confidence)) || !(price.timestamp === null || Number.isSafeInteger(price.timestamp) && price.timestamp >= 0)) fail();
      if (asset.value_usd !== null && (price.status !== "known" || asset.status !== "priced")) fail();
    }
    if (data.assets[0].asset_id !== "native:near" || data.assets.filter(asset => asset.value_usd !== null).length !== summary.priced_assets) fail();
    if (data.snapshot !== undefined && !(data.snapshot && evidencePath(data.snapshot.url, data.snapshot.id))) fail();
    return data;
  }
  function checkHistory(data, account, assetId) {
    const fail = () => { throw new Error("The retained observation response could not be read."); };
    if (!data || typeof data !== "object" || data.account_id !== account || data.network !== "mainnet" || !Array.isArray(data.items) || data.items.length > 100
      || !data.pagination || typeof data.pagination !== "object"
      || !(data.pagination.next_before === null || Number.isSafeInteger(data.pagination.next_before) && data.pagination.next_before > 0)
      || !data.coverage || typeof data.coverage !== "object" || data.coverage.history_complete !== false
      || (data.coverage.asset_id ?? null) !== (assetId || null) || !Number.isSafeInteger(data.coverage.stored_observations) || data.coverage.stored_observations < 0) fail();
    for (const item of data.items) {
      if (!item || typeof item !== "object" || !evidencePath(item.url, item.id) || !["current", "historical"].includes(item.kind)
        || !utcText(item.observed_at) || !utcText(item.effective_at) || typeof item.asset_scope_id !== "string" || !HASH.test(item.asset_scope_id)
        || !(item.known_value_usd === null || decimalText(item.known_value_usd)) || !(item.total_value_usd === null || decimalText(item.total_value_usd))
        || ![item.priced_assets, item.unpriced_assets, item.assets_count].every(count => Number.isSafeInteger(count) && count >= 0)
        || item.priced_assets + item.unpriced_assets !== item.assets_count || item.total_value_usd !== null && (item.unpriced_assets !== 0 || item.kind === "historical")) fail();
      if (assetId && (item.asset_id !== assetId || !(item.balance === null || decimalText(item.balance)) || !(item.balance_base_units === null || baseUnitText(item.balance_base_units))
        || !(item.asset_value_usd === null || decimalText(item.asset_value_usd)) || !(item.decimals === null || Number.isInteger(item.decimals) && item.decimals >= 0 && item.decimals <= 255))) fail();
    }
    return data;
  }
  const measureValue = (item, measure) => measure === "total" ? item.total_value_usd : measure === "known" ? item.known_value_usd
    : measure === "balance" ? item.balance ?? null : measure === "value" ? item.asset_value_usd ?? null : null;
  function scopes(items) {
    const groups = new Map();
    for (const item of items) {
      if (!groups.has(item.asset_scope_id)) groups.set(item.asset_scope_id, []);
      groups.get(item.asset_scope_id).push(item);
    }
    for (const group of groups.values()) group.sort((a, b) => Date.parse(a.effective_at) - Date.parse(b.effective_at) || (a.observed_at < b.observed_at ? -1 : 1));
    return new Map([...groups.entries()].sort((a, b) => Date.parse(b[1].at(-1).effective_at) - Date.parse(a[1].at(-1).effective_at)));
  }
  function plot(items, measure) {
    const times = items.map(item => Date.parse(item.effective_at));
    const first = Math.min(...times), last = Math.max(...times);
    const known = [], unknown = [];
    items.forEach((item, index) => {
      const x = last === first ? 50 : (times[index] - first) / (last - first) * 100;
      const text = measureValue(item, measure), number = finite(text);
      if (number === null) unknown.push({x, item}); else known.push({x, number, text, item});
    });
    let low = null, high = null;
    for (const point of known) {
      if (!low || point.number < low.number) low = point;
      if (!high || point.number > high.number) high = point;
    }
    for (const point of known) point.y = !low || high.number === low.number ? 50 : (point.number - low.number) / (high.number - low.number) * 100;
    return {known, unknown, low, high, first: items[times.indexOf(first)], last: items[times.indexOf(last)]};
  }
  const root = document.getElementById("account-portfolio");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const ids = ["investigate-account", "portfolio-read", "portfolio-current", "portfolio-current-status", "portfolio-current-block", "portfolio-current-heading",
    "portfolio-history-form", "portfolio-history-asset", "portfolio-history-measure", "portfolio-history-scope", "portfolio-history-load", "portfolio-history",
    "portfolio-history-status", "portfolio-history-block", "portfolio-history-heading", "portfolio-block-form", "portfolio-block-height", "portfolio-block-height-error",
    "portfolio-block-contracts", "portfolio-block-contracts-error", "portfolio-use-current", "portfolio-block-read", "portfolio-block", "portfolio-block-status",
    "portfolio-block-block", "portfolio-block-heading"];
  if (ids.some(id => !$(id))) return;
  const input = $("investigate-account"), readButton = $("portfolio-read");
  const seriesSelect = $("portfolio-history-asset"), measureSelect = $("portfolio-history-measure"), scopeSelect = $("portfolio-history-scope");
  const historyButton = $("portfolio-history-load"), heightInput = $("portfolio-block-height"), contractsInput = $("portfolio-block-contracts");
  const useCurrent = $("portfolio-use-current"), blockButton = $("portfolio-block-read");
  const sections = {
    current: {block: $("portfolio-current-block"), heading: $("portfolio-current-heading"), content: $("portfolio-current"), status: $("portfolio-current-status"), button: readButton},
    history: {block: $("portfolio-history-block"), heading: $("portfolio-history-heading"), content: $("portfolio-history"), status: $("portfolio-history-status"), button: historyButton},
    block: {block: $("portfolio-block-block"), heading: $("portfolio-block-heading"), content: $("portfolio-block"), status: $("portfolio-block-status"), button: blockButton}
  };
  const requests = new Map();
  let account = "", generation = 0, current = null, archived = null, history = null, userMeasure = "total", measureChosen = false;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  };
  const button = (label, callback) => { const item = node("button", label); item.type = "button"; item.addEventListener("click", callback); return item; };
  const quoted = (text, className = "portfolio-quoted") => node("q", typeof text === "string" && text ? text : "Not reported", className);
  const details = (label, data) => {
    const item = node("details", undefined, "portfolio-detail");
    item.append(node("summary", label), node("pre", JSON.stringify(data, null, 2))); return item;
  };
  const empty = text => node("p", text, "portfolio-empty");
  const evidenceLink = (url, label) => {
    const link = node("a", label, "portfolio-evidence-link"); link.href = url; link.setAttribute("type", "application/json"); return link;
  };
  function contractLinks(contract) {
    const wrap = node("span", undefined, "portfolio-contract");
    const local = node("a", contract, "mono"); local.href = "/investigate.html?account=" + encodeURIComponent(contract);
    const explorer = node("a", "NearBlocks", "portfolio-explorer"); explorer.href = "https://nearblocks.io/address/" + encodeURIComponent(contract);
    explorer.setAttribute("aria-label", "Open " + contract + " on NearBlocks"); explorer.rel = "noopener noreferrer";
    wrap.append(local, explorer); return wrap;
  }
  function table(id, headings, rows, className) {
    const item = node("table", undefined, "portfolio-table" + (className ? " " + className : "")); item.id = id;
    const head = node("thead"), header = node("tr");
    for (const text of headings) { const th = node("th", text); th.scope = "col"; header.append(th); }
    head.append(header);
    const body = node("tbody");
    for (const values of rows) {
      const tr = node("tr");
      values.forEach((value, index) => {
        const td = node("td"); td.setAttribute("data-label", headings[index]);
        if (value instanceof window.Node && value.getAttribute("data-export-value") !== null) td.setAttribute("data-export-value", value.getAttribute("data-export-value"));
        td.append(value instanceof window.Node ? value : node("span", value === null || value === undefined ? "Unavailable" : value)); tr.append(td);
      });
      body.append(tr);
    }
    item.append(head, body); return item;
  }
  const exportButton = id => { const item = node("button", "Export shown rows CSV", "portfolio-export"); item.type = "button"; item.setAttribute("data-export-table", id); return item; };
  function number(value, places, unit, className = "mono", baseUnits = null) {
    const item = node("div", undefined, "portfolio-number");
    const formatted = window.SiyabNumbers?.decimal(value, places);
    item.append(node("span", (formatted && formatted !== "Unavailable" ? formatted : "See exact value") + unit, className));
    const exact = node("details", undefined, "portfolio-exact");
    exact.append(node("summary", "Exact value"), node("span", value + unit, "portfolio-exact-value"));
    if (baseUnits !== null) exact.append(node("span", baseUnits + " base units", "portfolio-exact-value"));
    item.append(exact); return item;
  }
  const usd = (value, className = "mono") => number(value, 2, " USD", className);
  function priceStatus(status, coverage) {
    const age = Number.isSafeInteger(coverage?.prices_max_age_seconds) ? coverage.prices_max_age_seconds : null;
    const confidence = decimalText(coverage?.prices_min_confidence) ? coverage.prices_min_confidence : null;
    return {
      known: "Price used",
      missing: "No price returned for this exact asset",
      stale: "Price older than " + (age === null ? "the age limit" : age + " seconds from the balance time") + "; not used",
      future: "Price timestamp is after the read; not used",
      low_confidence: "Provider confidence below " + (confidence ?? "the minimum") + "; not used",
      invalid: "Provider price entry invalid; not used",
      decimals_mismatch: "Provider decimals differ from contract metadata; not used",
      provider_unavailable: "Price provider unavailable"
    }[status] || "Price status not recognized; not used";
  }
  const assetStatus = status => ({priced: "Valued", price_unavailable: "Not valued: no usable price", decimals_unavailable: "Not valued: token decimals unavailable",
    balance_unavailable: "Not valued: balance unavailable"}[status] || "Not valued");
  const balanceStatus = status => ({finalized: "Finalized RPC state", indexed: "Indexed balance", historical: "Archival balance at the block",
    account_absent: "Account does not exist at this read", unavailable: "Balance not read"}[status] || (typeof status === "string" && status.length <= 64 ? "Provider status: " + status : "Balance status not recorded"));
  const metadataOf = asset => {
    const metadata = asset.metadata;
    return {name: typeof metadata?.name === "string" && metadata.name.length <= 256 ? metadata.name : null,
      symbol: typeof metadata?.symbol === "string" && metadata.symbol.length <= 64 ? metadata.symbol : null,
      provenance: metadata?.provenance === "protocol" ? "Protocol asset" : metadata?.provenance === "contract_reported" ? "Contract-reported metadata" : "Metadata unavailable"};
  };
  function assetRows(data) {
    return data.assets.map(asset => {
      const metadata = metadataOf(asset);
      const name = node("div", undefined, "portfolio-asset");
      name.append(quoted(metadata.name || (asset.contract_id ? "Name not reported" : "NEAR"), "portfolio-asset-name"));
      if (metadata.symbol) name.append(quoted(metadata.symbol, "portfolio-asset-symbol"));
      name.append(asset.contract_id ? contractLinks(asset.contract_id) : node("span", "Native NEAR, account plus locked balance", "portfolio-small"));
      name.append(node("small", metadata.provenance, "portfolio-small"));
      const balance = node("div");
      if (asset.balance !== null) {
        balance.append(number(asset.balance, 4, "", "mono portfolio-amount", asset.balance_base_units));
        if (metadata.symbol) balance.append(node("span", metadata.symbol, "portfolio-unit"));
      } else balance.append(node("span", asset.balance_base_units !== null ? asset.balance_base_units + " base units" : "Balance unavailable", asset.balance_base_units !== null ? "mono portfolio-amount" : "portfolio-unknown"));
      balance.append(node("small", balanceStatus(asset.balance_status), "portfolio-small"));
      const price = node("div");
      if (asset.price.usd !== null && asset.price.status === "known") price.append(usd(asset.price.usd));
      price.append(node("span", priceStatus(asset.price.status, data.coverage), "portfolio-chip " + (asset.price.status === "known" ? "is-known" : "is-unknown")));
      if (asset.price.timestamp !== null) price.append(node("small", "Price time " + secondsLabel(asset.price.timestamp), "portfolio-small"));
      if (asset.price.confidence !== null) price.append(node("small", "Provider confidence " + asset.price.confidence, "portfolio-small"));
      const value = node("div");
      if (asset.value_usd !== null) value.append(usd(asset.value_usd, "mono portfolio-amount"));
      else value.append(node("span", "Not established", "portfolio-unknown"));
      value.append(node("small", assetStatus(asset.status), "portfolio-small"));
      const evidence = details("Balance and price sources", {asset_id: asset.asset_id, balance_status: asset.balance_status, balance_source: asset.balance_source ?? null, details: asset.details ?? {}, price: asset.price});
      return [name, balance, price, value, evidence];
    });
  }
  function summary(data, mode) {
    const result = node("div", undefined, "portfolio-summary"); result.setAttribute("data-changed", "true");
    const totals = data.summary, coverage = data.coverage, count = data.assets.length;
    const reasons = [];
    if (totals.unpriced_assets) reasons.push(totals.unpriced_assets + " of " + count + (count === 1 ? " asset" : " assets") + (totals.unpriced_assets === 1 ? " has" : " have") + " no usable price.");
    if (mode === "current" && !coverage.inventory_complete) reasons.push(Number.isSafeInteger(coverage.tokens_total) && Number.isSafeInteger(coverage.tokens_read)
      ? coverage.tokens_read + " of " + coverage.tokens_total + " indexed tokens were read, so the inventory is incomplete."
      : "The indexed token inventory could not be read in full.");
    if (mode === "block") reasons.push("Only native NEAR and the listed contracts were read at this block; other holdings are not included.");
    const figure = node("div", undefined, "portfolio-figure");
    if (totals.total_value_usd !== null) {
      figure.append(node("p", "Total value", "portfolio-figure-label"), usd(totals.total_value_usd, "portfolio-figure-value mono"),
        node("p", "Every asset in the complete indexed inventory has a usable price.", "portfolio-figure-note"));
    } else if (totals.known_value_usd !== null) {
      figure.append(node("p", "Known priced subtotal, not a complete total", "portfolio-figure-label"), usd(totals.known_value_usd, "portfolio-figure-value mono"),
        node("p", reasons.join(" "), "portfolio-figure-note"));
    } else {
      figure.append(node("p", "USD value", "portfolio-figure-label"), node("p", "Not established", "portfolio-figure-value portfolio-unknown"),
        node("p", (reasons.length ? reasons.join(" ") + " " : "") + "No asset in this read has a usable price.", "portfolio-figure-note"));
    }
    const meter = node("div", undefined, "portfolio-meter"); meter.setAttribute("aria-hidden", "true");
    const fill = node("span", undefined, "portfolio-meter-fill"); fill.style.width = (count ? totals.priced_assets / count * 100 : 0) + "%"; meter.append(fill);
    const facts = node("dl", undefined, "portfolio-facts");
    const rows = [["Priced assets", totals.priced_assets + " of " + count], ["Unpriced assets", String(totals.unpriced_assets)]];
    if (mode === "current") rows.push(["Indexed tokens read", Number.isSafeInteger(coverage.tokens_total) ? coverage.tokens_read + " of " + coverage.tokens_total : "Inventory size unavailable"]);
    else rows.push(["Block height", typeof coverage.block_height === "string" ? coverage.block_height : "Not recorded"], ["Block time", utcLabel(data.effective_at)]);
    rows.push(["Read at", utcLabel(data.observed_at)]);
    for (const [label, value] of rows) { const item = node("div"); item.append(node("dt", label), node("dd", value, "mono")); facts.append(item); }
    figure.append(meter);
    result.append(figure, facts); return result;
  }
  function renderValuation(parent, data, mode) {
    parent.append(summary(data, mode));
    const notes = node("div", undefined, "portfolio-notes");
    notes.append(node("p", "Balances and prices come from separate observations. A USD value is a source-based estimate, not a spendable balance.", "portfolio-note"));
    for (const field of ["balances", "scope", "excluded", mode === "block" ? "excluded_historical_tokens" : null]) {
      if (field && typeof data.coverage[field] === "string" && data.coverage[field].length <= 1000) notes.append(node("p", data.coverage[field], "portfolio-note"));
    }
    if (mode === "block") {
      const selected = Array.isArray(data.coverage.selected_contracts) ? data.coverage.selected_contracts.filter(accountId) : [];
      notes.append(node("p", selected.length ? "Contracts read at this block: " + selected.join(", ") + "." : "No token contracts were listed; only native NEAR was read.", "portfolio-note"));
    }
    parent.append(notes);
    const id = mode === "block" ? "portfolio-block-assets-table" : "portfolio-assets-table";
    parent.append(exportButton(id), table(id, ["Asset and contract", "Exact balance", "Price (USD)", "Value", "Evidence"], assetRows(data)));
    const evidence = node("div", undefined, "portfolio-evidence");
    if (data.snapshot) evidence.append(evidenceLink(evidencePath(data.snapshot.url, data.snapshot.id), "Open the retained observation JSON"), node("small", data.snapshot.id, "mono portfolio-small"));
    else evidence.append(node("p", "This read was not retained; it will not appear in retained observations.", "portfolio-note"));
    evidence.append(details("Sources and coverage", {asset_scope_id: data.asset_scope_id, sources: data.sources, coverage: data.coverage}));
    parent.append(evidence);
  }
  function seriesOptions() {
    const chosen = seriesSelect.value, assets = new Map([["", "Portfolio value"], ["native:near", "NEAR (native)"]]);
    if (chosen && history?.asset === chosen && assetIdValid(chosen)) assets.set(chosen, chosen === "native:near" ? "NEAR (native)" : chosen.slice(7));
    for (const data of [current, archived]) for (const asset of data?.assets || []) {
      if (asset.contract_id && assets.size < 103) { const symbol = metadataOf(asset).symbol; assets.set(asset.asset_id, (symbol ? symbol + " " : "") + "(" + asset.contract_id + ")"); }
    }
    seriesSelect.replaceChildren(...[...assets].map(([value, label]) => { const option = node("option", label); option.value = value; return option; }));
    seriesSelect.value = assets.has(chosen) ? chosen : "";
  }
  function measureOptions() {
    const chosen = measureSelect.value;
    const values = seriesSelect.value ? [["balance", "Balance"], ["value", "Value (USD)"]] : [["total", "Complete total (USD)"], ["known", "Known priced subtotal (USD)"]];
    measureSelect.replaceChildren(...values.map(([value, label]) => { const option = node("option", label); option.value = value; return option; }));
    measureSelect.value = values.some(([value]) => value === chosen) ? chosen : values[0][0];
  }
  const measureLabel = () => ({total: "Complete total (USD)", known: "Known priced subtotal (USD)", balance: "Balance", value: "Value (USD)"}[measureSelect.value]);
  function unknownReason(item, measure) {
    if (measure === "total") return item.known_value_usd !== null ? "Not complete; known subtotal " + item.known_value_usd + " USD" : "No asset was priced";
    if (measure === "known") return "No asset was priced";
    if (measure === "balance") return item.balance_base_units ? item.balance_base_units + " base units; decimals unavailable" : "Asset not read in this observation";
    return item.balance === null || item.balance === undefined ? "Asset not read in this observation" : "Asset not priced in this observation";
  }
  function renderChart(parent, items, measure) {
    const geometry = plot(items, measure), unit = measure === "balance" ? "" : " USD";
    const chart = node("div", undefined, "portfolio-chart"); chart.setAttribute("role", "img");
    const span = geometry.first === geometry.last ? utcLabel(geometry.first.effective_at) : utcLabel(geometry.first.effective_at) + " to " + utcLabel(geometry.last.effective_at);
    chart.setAttribute("aria-label", measureLabel() + ": " + geometry.known.length + " retained observations with a value and " + geometry.unknown.length + " without one, " + span
      + (geometry.high ? ". Highest " + geometry.high.text + unit + ", lowest " + geometry.low.text + unit : "") + ". Exact values are in the table below.");
    const axis = node("div", undefined, "portfolio-y-axis"); axis.setAttribute("aria-hidden", "true");
    for (const [point, missing] of [[geometry.high, "No value"], [geometry.low !== geometry.high ? geometry.low : null, ""]]) {
      let label = point ? window.SiyabNumbers?.decimal(point.text, measure === "balance" ? 4 : 2) || point.text : missing;
      if (point && (label === "Unavailable" || label.length + unit.length > 20)) label = point.number.toExponential(8);
      const tick = node("span", label + (point ? unit : ""), "mono");
      if (point) tick.title = point.text + unit;
      axis.append(tick);
    }
    const area = node("div", undefined, "portfolio-plot"); area.setAttribute("aria-hidden", "true");
    for (const position of [0, 50, 100]) { const line = node("span", undefined, "portfolio-gridline"); line.style.bottom = position + "%"; area.append(line); }
    for (const point of geometry.known) {
      const mark = node("span", undefined, "portfolio-mark " + (point.item.kind === "historical" ? "is-block" : "is-current"));
      mark.style.left = point.x + "%"; mark.style.bottom = point.y + "%"; mark.title = utcLabel(point.item.effective_at) + ": " + point.text + unit; area.append(mark);
    }
    const lane = node("div", undefined, "portfolio-unknown-lane"); lane.setAttribute("aria-hidden", "true");
    lane.append(node("span", "Not established", "portfolio-lane-label"));
    const ticks = node("div", undefined, "portfolio-lane-ticks");
    for (const point of geometry.unknown) {
      const tick = node("span", undefined, "portfolio-tick"); tick.style.left = point.x + "%"; tick.title = utcLabel(point.item.effective_at) + ": " + unknownReason(point.item, measure); ticks.append(tick);
    }
    lane.append(ticks);
    const times = node("div", undefined, "portfolio-x-axis"); times.setAttribute("aria-hidden", "true");
    times.append(node("time", utcLabel(geometry.first.effective_at)), node("time", geometry.first === geometry.last ? "" : utcLabel(geometry.last.effective_at)));
    chart.append(axis, area, lane, times);
    parent.append(chart, node("p", "Each mark is one retained read. Filled circles are current reads; hollow squares are archived block reads. Marks are not joined because values between reads were never observed. Marks in the lower lane have no value for this measure.", "portfolio-legend"));
  }
  function renderHistory() {
    const content = sections.history.content;
    if (!history) return;
    const items = [...history.items.values()], groups = scopes(items);
    let measure = [...measureSelect.children].some(option => option.value === userMeasure) ? userMeasure : measureSelect.firstChild.value;
    measureSelect.value = measure;
    const chosen = scopeSelect.value;
    scopeSelect.replaceChildren(...[...groups].map(([scope, group]) => {
      const option = node("option", "Inventory " + scope.slice(0, 12) + " (" + group.length + (group.length === 1 ? " read, " : " reads, ") + group[0].assets_count + (group[0].assets_count === 1 ? " asset)" : " assets)")); option.value = scope; return option;
    }));
    scopeSelect.disabled = groups.size === 0;
    if (!groups.size) {
      const option = node("option", "No retained observations"); option.value = ""; scopeSelect.append(option);
      content.replaceChildren(empty("No valuation has been retained for this account yet. Read the current valuation or archived balances to retain one."),
        node("p", history.stored + " retained observations for this account. Retained observations are not complete history.", "portfolio-note"));
      return;
    }
    scopeSelect.value = groups.has(chosen) ? chosen : [...groups.keys()][0];
    const group = groups.get(scopeSelect.value), result = node("div");
    result.append(node("p", groups.size > 1 ? groups.size + " different inventory scopes are retained. Each scope covers a different set of assets, so only one is shown at a time." : "One inventory scope is retained.", "portfolio-note"));
    const valued = option => group.filter(item => finite(measureValue(item, option)) !== null).length;
    let switchedFrom = null;
    if (!measureChosen && !valued(measure)) {
      const fallback = [...measureSelect.children].map(option => option.value).find(option => valued(option));
      if (fallback) { switchedFrom = measureLabel(); measureSelect.value = measure = fallback; }
    }
    if (switchedFrom) result.append(node("p", "Showing " + measureLabel() + " because no read in this scope has a " + switchedFrom + "." + (measure === "known" ? " The subtotal covers priced assets only and is not a complete total." : ""), "portfolio-note"));
    const missing = group.length - valued(measure);
    if (missing) result.append(node("p", measureLabel() + " is not established for " + missing + " of " + group.length + (group.length === 1 ? " read" : " reads") + " in this scope"
      + (measure === "total" ? ". The table below gives the reason and known subtotal for each read." : ". The table below gives the reason for each read."), "portfolio-note"));
    renderChart(result, group, measure);
    const unit = measure === "balance" ? "" : " USD";
    const rows = [...group].reverse().map(item => {
      const text = measureValue(item, measure), value = node("div");
      if (text !== null && text !== undefined) value.append(number(text, measure === "balance" ? 4 : 2, unit, "mono portfolio-amount"));
      else {
        value.append(node("span", "Not established", "portfolio-unknown"));
        if (measure === "total" && item.known_value_usd !== null) value.append(node("small", "Not complete; known subtotal", "portfolio-small"), usd(item.known_value_usd));
        else value.append(node("small", unknownReason(item, measure), "portfolio-small"));
      }
      value.setAttribute("data-export-value", text !== null && text !== undefined ? text + unit : unknownReason(item, measure));
      if (measure === "balance" && text !== null && text !== undefined && item.balance_base_units) value.append(node("small", item.balance_base_units + " base units", "mono portfolio-small"));
      const evidence = node("div"); evidence.append(evidenceLink(item.url, "Observation JSON"), node("small", "Read " + utcLabel(item.observed_at), "portfolio-small"), node("small", item.id, "mono portfolio-small"));
      return [node("span", utcLabel(item.effective_at), "mono"), item.kind === "historical" ? "Archived block read" : "Current read", value,
        item.priced_assets + " priced, " + item.unpriced_assets + " unpriced of " + item.assets_count + (item.assets_count === 1 ? " asset" : " assets"), evidence];
    });
    result.append(exportButton("portfolio-history-table"), table("portfolio-history-table", ["Balance time (UTC)", "Read", measureLabel(), "Valuation coverage", "Evidence"], rows));
    result.append(node("p", items.length + " of " + history.stored + " retained observations loaded for this account. Retained observations are not complete history, and no return or profit is calculated.", "portfolio-note"));
    if (history.next !== null) {
      const more = button("Load older observations", () => loadHistory(true));
      more.disabled = items.length >= MAX_POINTS;
      result.append(more);
      if (items.length >= MAX_POINTS) result.append(node("p", "The browser view holds at most " + MAX_POINTS + " observations. Older observations remain retained on the server.", "portfolio-note"));
    }
    content.replaceChildren(...result.childNodes);
  }
  async function read(key, path, render, retry) {
    const section = sections[key];
    if (!accountId(account)) return;
    const old = requests.get(key); if (old) old.controller.abort();
    const controller = new AbortController(), version = generation, request = {controller, timedOut: false};
    requests.set(key, request);
    const focused = section.block.contains(document.activeElement);
    section.content.setAttribute("aria-busy", "true"); section.button.disabled = true;
    section.content.replaceChildren(node("p", "Reading requested observations…", "portfolio-loading"));
    section.status.textContent = key === "current" ? "Reading balances and prices for " + account + "…" : key === "history" ? "Loading retained observations…" : "Reading archived balances…";
    let rejectDeadline;
    const deadline = new Promise((resolve, reject) => { rejectDeadline = reject; });
    controller.signal.addEventListener("abort", () => rejectDeadline(new Error("Portfolio read cancelled.")), {once: true});
    const timer = setTimeout(() => { request.timedOut = true; controller.abort(); rejectDeadline(new Error("Portfolio read deadline exceeded.")); }, TIMEOUT_MS);
    try {
      const response = await Promise.race([fetch(path, {signal: controller.signal, credentials: "same-origin", redirect: "error", cache: "no-store", headers: {Accept: "application/json"}}), deadline]);
      if (!(response.headers.get("content-type") || "").toLowerCase().includes("application/json")) throw new Error("The server did not return portfolio JSON (HTTP " + response.status + ").");
      let data;
      try { data = await Promise.race([response.json(), deadline]); } catch (error) { throw new Error("The server response could not be read as JSON."); }
      if (!response.ok) {
        const message = data && typeof data.message === "string" && data.message.length <= 300 ? data.message : "The portfolio read is unavailable (HTTP " + response.status + ").";
        const wait = /^[0-9]{1,4}$/.test(response.headers.get("retry-after") || "") ? " Retry after " + response.headers.get("retry-after") + " seconds." : "";
        throw new Error(message + wait);
      }
      if (version !== generation || requests.get(key) !== request) return;
      const rendered = node("div"); render(rendered, data);
      section.content.replaceChildren(...rendered.childNodes);
      if (focused) section.heading.focus({preventScroll: true});
    } catch (error) {
      if (version !== generation || requests.get(key) !== request) return;
      const message = request.timedOut ? "The read did not finish within " + TIMEOUT_MS / 1000 + " seconds. Previous values were cleared; try again." : controller.signal.aborted ? "The read was cancelled." : error.message;
      section.status.textContent = message;
      const again = button(retry, () => section.button.click());
      section.content.replaceChildren(node("p", message, "portfolio-error"), again);
      if (focused) again.focus({preventScroll: true});
    } finally {
      clearTimeout(timer);
      if (requests.get(key) === request) {
        requests.delete(key); section.content.setAttribute("aria-busy", "false");
        if (version === generation) section.button.disabled = !accountId(account);
      }
    }
  }
  function readCurrent() {
    current = null; seriesOptions(); useCurrent.disabled = true;
    return read("current", "/live/accounts/" + encodeURIComponent(account) + "/portfolio?max_tokens=40", (parent, data) => {
      current = checkPortfolio(data, account, "current");
      renderValuation(parent, current, "current");
      seriesOptions(); measureOptions(); useCurrent.disabled = !current.assets.some(asset => asset.contract_id);
      sections.current.status.textContent = current.summary.total_value_usd !== null ? "Total value read." : current.summary.known_value_usd !== null ? "Known priced subtotal read; the total is incomplete." : "Balances read; no USD value was established.";
    }, "Retry current valuation");
  }
  function loadHistory(older = false) {
    const asset = seriesSelect.value, before = older && history ? history.next : 0;
    if (!older || !history || history.asset !== asset) history = null;
    const parameters = new URLSearchParams({before: String(before), limit: "100"});
    if (asset) parameters.set("asset_id", asset);
    return read("history", "/live/accounts/" + encodeURIComponent(account) + "/portfolio-history?" + parameters, (parent, data) => {
      checkHistory(data, account, asset);
      if (older && data.pagination.next_before !== null && data.pagination.next_before >= before) throw new Error("The retained observation cursor did not advance.");
      if (!history) history = {asset, items: new Map(), next: null, stored: 0};
      for (const item of data.items) if (history.items.size < MAX_POINTS && !history.items.has(item.id)) history.items.set(item.id, item);
      history.next = data.pagination.next_before; history.stored = data.coverage.stored_observations;
      measureSelect.disabled = false; renderHistory();
      for (const child of [...sections.history.content.childNodes]) parent.append(child);
      sections.history.status.textContent = history.items.size + " retained observations loaded.";
    }, "Retry retained observations");
  }
  function fieldError(field, message) {
    const target = $(field.id + "-error"); target.textContent = message || "";
    if (message) field.setAttribute("aria-invalid", "true"); else field.removeAttribute("aria-invalid");
  }
  function readBlock() {
    const height = parseBlock(heightInput.value), contracts = parseContracts(contractsInput.value);
    fieldError(heightInput, height.error); fieldError(contractsInput, contracts.error);
    if (height.error || contracts.error) {
      sections.block.status.textContent = "Correct the highlighted field to read archived balances.";
      (height.error ? heightInput : contractsInput).focus(); return;
    }
    archived = null; seriesOptions();
    const parameters = new URLSearchParams({block_height: height.value});
    if (contracts.value.length) parameters.set("contracts", contracts.value.join(","));
    return read("block", "/live/accounts/" + encodeURIComponent(account) + "/portfolio-at-block?" + parameters, (parent, data) => {
      checkPortfolio(data, account, "historical");
      const selected = data.coverage.selected_contracts;
      if (data.coverage.block_height !== height.value || !Array.isArray(selected) || selected.length !== contracts.value.length || selected.some((value, index) => value !== contracts.value[index])
        || data.assets.length !== contracts.value.length + 1 || data.assets.slice(1).some((asset, index) => asset.contract_id !== contracts.value[index])) throw new Error("The archived balance response does not match the requested block and contracts.");
      archived = data; renderValuation(parent, data, "block"); seriesOptions(); measureOptions();
      sections.block.status.textContent = "Balances read at block " + height.value + ".";
    }, "Retry archived balances");
  }
  function reset(subject) {
    generation++;
    for (const request of requests.values()) request.controller.abort();
    requests.clear();
    account = accountId(subject) ? subject : ""; current = null; archived = null; history = null;
    const ready = Boolean(account);
    for (const control of [readButton, historyButton, blockButton, seriesSelect, heightInput, contractsInput]) control.disabled = !ready;
    useCurrent.disabled = true; measureSelect.disabled = !ready;
    seriesOptions(); seriesSelect.value = ""; measureOptions(); measureSelect.value = "total"; userMeasure = "total"; measureChosen = false;
    scopeSelect.replaceChildren(node("option", "Load observations to choose a scope")); scopeSelect.firstChild && (scopeSelect.firstChild.value = ""); scopeSelect.disabled = true;
    heightInput.value = ""; contractsInput.value = ""; fieldError(heightInput, ""); fieldError(contractsInput, "");
    for (const section of Object.values(sections)) { section.content.setAttribute("aria-busy", "false"); section.status.textContent = ""; }
    sections.current.content.replaceChildren(empty(ready ? "Read the current valuation of " + account + " when you need it. Up to 40 indexed tokens are read; unpriced assets stay unavailable rather than counting as zero." : "Select an account, then read its current valuation."));
    sections.history.content.replaceChildren(empty(ready ? "Load the valuations retained for " + account + ". Only reads that were actually retained are shown." : "Select an account, then load its retained valuation observations."));
    sections.block.content.replaceChildren(empty(ready ? "Enter a block height to read archived balances for " + account + ". The result covers only native NEAR and the contracts you list." : "Select an account, then enter a block height."));
  }
  readButton.addEventListener("click", readCurrent);
  $("portfolio-history-form").addEventListener("submit", event => { event.preventDefault(); if (account) loadHistory(false); });
  seriesSelect.addEventListener("change", () => {
    measureOptions(); measureChosen = false; const active = requests.get("history"); if (active) { requests.delete("history"); active.controller.abort(); }
    history = null; scopeSelect.replaceChildren(node("option", "Load observations to choose a scope")); scopeSelect.firstChild.value = ""; scopeSelect.disabled = true;
    sections.history.content.setAttribute("aria-busy", "false"); sections.history.status.textContent = ""; historyButton.disabled = !account;
    sections.history.content.replaceChildren(empty("Load retained observations for this series."));
  });
  measureSelect.addEventListener("change", () => { userMeasure = measureSelect.value; measureChosen = true; renderHistory(); });
  scopeSelect.addEventListener("change", renderHistory);
  $("portfolio-block-form").addEventListener("submit", event => { event.preventDefault(); if (account) readBlock(); });
  heightInput.addEventListener("blur", () => { if (heightInput.value.trim()) fieldError(heightInput, parseBlock(heightInput.value).error); });
  contractsInput.addEventListener("blur", () => fieldError(contractsInput, parseContracts(contractsInput.value).error));
  useCurrent.addEventListener("click", () => {
    const contracts = (current?.assets || []).filter(asset => asset.contract_id).map(asset => asset.contract_id);
    contractsInput.value = contracts.slice(0, MAX_CONTRACTS).join("\n"); fieldError(contractsInput, "");
    sections.block.status.textContent = contracts.length > MAX_CONTRACTS ? "Listed the first " + MAX_CONTRACTS + " of " + contracts.length + " contracts from the current valuation." : "Listed " + contracts.length + " contracts from the current valuation.";
  });
  document.addEventListener("siyab:account-selected", event => {
    const subject = event && event.detail && typeof event.detail.account === "string" ? event.detail.account : "";
    reset(subject);
  });
  window.addEventListener("popstate", () => { if (!accountId(input.value.trim())) reset(""); });
  window.addEventListener("pagehide", () => { generation++; for (const request of requests.values()) request.controller.abort(); requests.clear(); });
  reset(accountId(input.value.trim()) ? input.value.trim() : "");
})();
