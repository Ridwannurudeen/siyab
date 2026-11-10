(() => {
  "use strict";
  const root = document.getElementById("entity-enrichment");
  if (!root) return;
  const el = id => document.getElementById("enrichment-" + id);
  const states = {documented: "Source documented", stale: "Source is stale", source_changed: "Source changed; review needed", source_withdrawn: "Account removed from source"};
  const categories = {dex: "Exchange protocols", liquid_staking: "Liquid staking", social: "Social", token: "Tokens", settlement: "Settlement", oracle: "Oracles"};
  const accountId = value => typeof value === "string" && value.length >= 2 && value.length <= 64 && /^[a-z0-9]+(?:[._-][a-z0-9]+)*$/.test(value) && value !== "testnet" && !value.endsWith(".testnet");
  const text = (value, maximum = 2048) => typeof value === "string" && value.length > 0 && value.length <= maximum;
  const stamp = value => typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value) && Number.isFinite(Date.parse(value));
  const sourceUrl = value => {
    if (!text(value) || /[\s\\]/.test(value)) return null;
    try { const url = new URL(value); return url.protocol === "https:" && !url.username && !url.password ? url.href : null; }
    catch { return null; }
  };
  const validEntry = row => Boolean(row && accountId(row.account_id) && row.network === "mainnet" && text(row.entity_id, 64)
    && text(row.name, 128) && text(row.role, 256) && text(row.category, 64) && Object.hasOwn(states, row.status)
    && ["documented", "deprecated"].includes(row.lifecycle) && row.provenance === "official_source_documented"
    && Array.isArray(row.aliases) && row.aliases.length <= 8 && row.aliases.every(value => text(value, 128))
    && row.evidence && sourceUrl(row.evidence.url) && text(row.evidence.publisher, 128) && stamp(row.evidence.retrieved_at)
    && stamp(row.evidence.first_retrieved_at) && /^[a-f0-9]{64}$/.test(row.evidence.content_sha256) && text(row.evidence.excerpt));
  const node = (tag, value, className) => {
    const item = document.createElement(tag);
    if (value !== undefined) item.textContent = String(value);
    if (className) item.className = className;
    return item;
  };
  function evidence(row) {
    const detail = node("details", undefined, "enrichment-evidence");
    detail.append(node("summary", "Source evidence"));
    const link = node("a", row.evidence.publisher); link.href = sourceUrl(row.evidence.url); link.target = "_blank"; link.rel = "noopener noreferrer";
    detail.append(link);
    const list = node("dl");
    const facts = [["Source retrieved", row.evidence.retrieved_at], ["First captured", row.evidence.first_retrieved_at], ["SHA-256", row.evidence.content_sha256]];
    if (stamp(row.last_checked_at)) facts.push(["Last checked", row.last_checked_at]);
    if (row.last_document_sha256 && row.last_document_sha256 !== row.evidence.content_sha256 && /^[a-f0-9]{64}$/.test(row.last_document_sha256)) facts.push(["Changed document SHA-256", row.last_document_sha256]);
    for (const [label, value] of facts) list.append(node("dt", label), node("dd", value));
    detail.append(list, node("pre", row.evidence.excerpt));
    return detail;
  }
  function records(rows) {
    const list = node("ul", undefined, "enrichment-list");
    for (const row of rows) {
      const item = node("li", undefined, "enrichment-record"), identity = node("div"), association = node("div"), source = node("div");
      const link = node("a", row.account_id, "enrichment-account-link"); link.href = "/investigate.html?account=" + encodeURIComponent(row.account_id);
      identity.append(node("h3", row.name), link);
      const aliases = row.aliases.filter(value => value.toLowerCase() !== row.name.toLowerCase());
      if (aliases.length) identity.append(node("p", "Also known as " + aliases.join(", "), "enrichment-source-meta"));
      const badge = node("span", states[row.status], "enrichment-state"); badge.dataset.state = row.status;
      association.append(badge);
      if (row.lifecycle === "deprecated") { const old = node("span", "Deprecated contract", "enrichment-state"); old.dataset.state = "deprecated"; association.append(old); }
      association.append(node("p", row.role, "enrichment-role"));
      if (row.check_status === "unavailable") association.append(node("p", "Latest source check unavailable. Dated evidence retained.", "enrichment-source-meta"));
      source.append(node("p", row.evidence.publisher, "enrichment-source-meta"), node("p", "Retrieved " + row.evidence.retrieved_at.slice(0, 10), "enrichment-source-meta"), evidence(row));
      item.append(identity, association, source); list.append(item);
    }
    return list;
  }
  const requests = new Map();
  let selected = "", accountGeneration = 0, directoryGeneration = 0, pageOffset = 0, nextOffset = null, appliedQuery = "", appliedCategory = "", directoryLoaded = false;
  async function read(kind, path) {
    requests.get(kind)?.abort();
    const controller = new AbortController(); requests.set(kind, controller);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
    try {
      const response = await fetch(path, {signal: controller.signal, credentials: "same-origin", headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error(response.status === 429 ? "Source reads are busy. Retry shortly." : "The entity directory could not be read. Retry this request.");
      if (!(response.headers.get("Content-Type") || "").includes("application/json")) throw new Error("The entity directory returned an unreadable response. Retry this request.");
      return await response.json();
    } catch (error) {
      if (timedOut) throw new Error("The source read timed out. Retry this request.");
      throw error;
    } finally { clearTimeout(timer); if (requests.get(kind) === controller) requests.delete(kind); }
  }
  async function associations(account) {
    const generation = ++accountGeneration;
    requests.get("account")?.abort();
    selected = accountId(account) ? account : "";
    el("retry").hidden = true;
    const host = el("associations");
    if (!selected) { host.replaceChildren(); host.setAttribute("aria-busy", "false"); el("account").textContent = "Select an account to read its documented associations."; return; }
    el("account").textContent = selected;
    host.setAttribute("aria-busy", "true"); host.replaceChildren(node("p", "Reading documented associations…", "enrichment-empty"));
    try {
      const data = await read("account", "/live/accounts/" + encodeURIComponent(selected) + "/enrichment");
      if (generation !== accountGeneration) return;
      if (!data || data.network !== "mainnet" || data.account_id !== selected || !Array.isArray(data.entities) || data.entities.length > 32 || !data.entities.every(row => validEntry(row) && row.account_id === selected)) throw new Error("The association response could not be validated. Retry this request.");
      host.replaceChildren(data.entities.length ? records(data.entities) : node("p", "No official association is documented for this exact account in the directory. Ownership remains unknown.", "enrichment-empty"));
    } catch (error) {
      if (generation !== accountGeneration) return;
      host.replaceChildren(node("p", error.name === "AbortError" ? "The association read was interrupted. Retry this request." : "Associations are unavailable. Retry this request; ownership remains unknown.", "enrichment-error"));
      el("retry").hidden = false;
    } finally { if (generation === accountGeneration) host.setAttribute("aria-busy", "false"); }
  }
  async function directory(offset = 0) {
    const generation = ++directoryGeneration, host = el("results");
    el("previous").disabled = true; el("next").disabled = true; el("page-count").textContent = "";
    host.setAttribute("aria-busy", "true"); host.replaceChildren(); el("directory-status").textContent = "Reading the entity directory…";
    const params = new URLSearchParams({query: appliedQuery, offset: String(offset), limit: "25"});
    if (appliedCategory) params.set("category", appliedCategory);
    try {
      const data = await read("directory", "/live/entity-directory?" + params);
      if (generation !== directoryGeneration) return;
      const page = data?.pagination;
      if (!data || data.network !== "mainnet" || !Array.isArray(data.items) || data.items.length > 25 || !data.items.every(validEntry)
        || !page || page.offset !== offset || page.limit !== 25 || !Number.isInteger(page.total) || page.total < 0 || page.total > 256
        || !(page.next_offset === null || Number.isInteger(page.next_offset) && page.next_offset === offset + 25 && page.next_offset < page.total)
        || !Array.isArray(data.categories) || data.categories.length > 32 || !data.categories.every(value => text(value, 64))) throw new Error("The entity directory response could not be validated. Search again.");
      const category = el("category"), current = appliedCategory;
      category.replaceChildren(node("option", "All categories")); category.children[0].value = "";
      for (const value of data.categories) { const option = node("option", categories[value] || value.replaceAll("_", " ")); option.value = value; category.append(option); }
      category.value = current;
      pageOffset = offset; nextOffset = page.next_offset; directoryLoaded = true;
      host.replaceChildren(data.items.length ? records(data.items) : node("p", "No documented accounts match this search. Try an organisation name, alias or exact account.", "enrichment-empty"));
      el("directory-status").textContent = page.total + " documented account" + (page.total === 1 ? "" : "s") + (appliedQuery ? " matching this search." : " in this view.");
      el("page-count").textContent = data.items.length ? (offset + 1) + " to " + (offset + data.items.length) + " of " + page.total : "0 results";
      el("previous").disabled = offset === 0; el("next").disabled = nextOffset === null;
    } catch (error) {
      if (generation !== directoryGeneration) return;
      el("directory-status").textContent = "The entity directory is unavailable. Search again to retry.";
      host.replaceChildren(node("p", "Source availability does not establish an account's ownership or safety.", "enrichment-error"));
    } finally { if (generation === directoryGeneration) host.setAttribute("aria-busy", "false"); }
  }
  el("retry").addEventListener("click", () => associations(selected));
  el("search").addEventListener("submit", event => { event.preventDefault(); appliedQuery = el("query").value.trim(); appliedCategory = el("category").value; directory(0); });
  el("category").addEventListener("change", () => { appliedCategory = el("category").value; appliedQuery = el("query").value.trim(); directory(0); });
  el("directory").addEventListener("toggle", () => { if (el("directory").open && !directoryLoaded) directory(0); });
  el("previous").addEventListener("click", () => directory(Math.max(0, pageOffset - 25)));
  el("next").addEventListener("click", () => { if (nextOffset !== null) directory(nextOffset); });
  document.addEventListener("siyab:account-selected", event => associations(event.detail?.account));
  window.addEventListener("pagehide", () => { accountGeneration++; directoryGeneration++; for (const controller of requests.values()) controller.abort(); requests.clear(); });
  const input = document.getElementById("investigate-account");
  const initial = input?.value || new URL(window.location.href).searchParams.get("account");
  if (accountId(initial)) associations(initial);
})();
