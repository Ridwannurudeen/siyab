(() => {
  "use strict";
  const panel = document.getElementById("investigation-jobs");
  if (!panel) return;
  const form = document.getElementById("jobs-form"), list = document.getElementById("jobs-list");
  const feedback = document.getElementById("jobs-status"), contextLine = document.getElementById("jobs-context");
  const results = document.getElementById("jobs-results"), resultBody = document.getElementById("jobs-result-body");
  const refresh = document.getElementById("jobs-refresh"), submit = document.getElementById("jobs-submit");
  const fields = Object.fromEntries(["account", "kind", "from", "until", "pages", "rows", "depth", "accounts", "account-pages"].map(name => [name, document.getElementById("jobs-" + name)]));
  const state = {epoch: 0, context: null, pending: new Set(), timer: null, items: [], inView: true, listing: false, mutating: false, uncertain: false, resultRevision: 0};
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  const identifier = value => typeof value === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(value);
  const account = value => typeof value === "string" && /^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$/.test(value) && !/[._-]{2}/.test(value) && value !== "testnet" && !value.endsWith(".testnet");
  const count = value => Number.isSafeInteger(value) && value >= 0;
  const date = value => Number.isFinite(value) && value >= 0 && value <= 8640000000000000 ? new Date(value).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC") : "Time unavailable";
  const active = item => ["queued", "running", "retry_wait"].includes(item.state);
  const visible = () => document.visibilityState !== "hidden" && state.inView;
  const writable = () => state.context && state.context.team.role !== "viewer" && !state.uncertain;
  const base = () => "/teams/" + encodeURIComponent(state.context.team.id) + "/investigation-jobs";
  class StaleRequest extends Error {}
  function message(text, error = false) { feedback.textContent = text; feedback.dataset.error = String(error); }
  function stopPolling() { clearTimeout(state.timer); state.timer = null; }
  function invalidate() {
    state.epoch++; stopPolling();
    for (const controller of state.pending) controller.abort();
    state.pending.clear(); state.listing = false; state.mutating = false;
  }
  function clear(text) {
    invalidate(); state.context = null; state.items = []; state.uncertain = false;
    form.reset(); kindChanged(); form.hidden = true; form.setAttribute("aria-busy", "false"); submit.disabled = true; refresh.disabled = false; results.hidden = true; resultBody.replaceChildren();
    list.replaceChildren(); contextLine.textContent = text; message("");
  }
  function permissionControls() {
    form.hidden = !state.context || state.context.team.role === "viewer";
    submit.disabled = !writable() || state.mutating;
    contextLine.textContent = state.context ? state.context.team.name + (state.context.team.role === "viewer" ? ". Viewer access: read and download retained results." : ". New jobs belong to this team.") : "Sign in to a team workspace to collect a bounded history or expand a transfer graph.";
  }
  async function request(path, values, epoch = state.epoch) {
    if (epoch !== state.epoch) throw new StaleRequest();
    const controller = new AbortController(); state.pending.add(controller);
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const headers = {Accept: "application/json"};
      if (values !== undefined) { headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = state.context?.csrfToken || ""; }
      const response = await fetch("/workspace-api" + path, {method: values === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", headers,
        body: values === undefined ? undefined : JSON.stringify(values), signal: controller.signal});
      if (!(response.headers.get("content-type") || "").includes("application/json")) throw new Error("Background investigations are unavailable on this copy. Open siyab.xyz or retry.");
      const data = await response.json();
      if (epoch !== state.epoch) throw new StaleRequest();
      if (controller.signal.aborted) throw new Error("Request timed out.");
      if (!response.ok) { const error = new Error(typeof data?.message === "string" ? data.message : "The investigation request failed. Refresh and try again."); error.status = response.status; throw error; }
      if (!data || typeof data !== "object") throw new Error("The investigation response could not be read.");
      return data;
    } catch (error) {
      if (epoch !== state.epoch) throw new StaleRequest();
      if (controller.signal.aborted) {
        const failure = new Error(values === undefined ? "The read timed out. Refresh jobs to retry." : "The response timed out. Refresh jobs before retrying; the change may have been saved.");
        failure.uncertain = values !== undefined; throw failure;
      }
      throw error;
    } finally { clearTimeout(timeout); state.pending.delete(controller); }
  }
  function report(error) {
    if (error instanceof StaleRequest) return;
    if (error.status === 401 || error.status === 403) clear(error.status === 401 ? "Your session expired. Sign in again in Team workspace." : "Your team access changed. Refresh jobs to reload your permissions.");
    if (error.uncertain) { state.uncertain = true; permissionControls(); }
    message(error.message || "The request failed. Refresh jobs to retry.", true);
  }
  function validJob(item) {
    return item && identifier(item.id) && account(item.account_id) && ["transfers", "transactions", "graph"].includes(item.kind)
      && ["queued", "running", "retry_wait", "completed", "cancelled", "failed"].includes(item.state)
      && item.counts && ["pages", "scanned_rows", "matched_rows"].every(key => count(item.counts[key]))
      && item.bounds && Number.isInteger(item.bounds.max_pages) && item.bounds.max_pages >= 1 && item.bounds.max_pages <= 100
      && Number.isInteger(item.bounds.max_rows) && item.bounds.max_rows >= 1 && item.bounds.max_rows <= 5000
      && item.coverage?.history_complete === false;
  }
  function button(parent, title, task, disabled = false) {
    const node = element("button", title); node.type = "button"; node.disabled = disabled;
    node.addEventListener("click", task); parent.append(node); return node;
  }
  function schedule() {
    stopPolling();
    if (!state.context || !visible() || !state.items.some(active)) return;
    state.timer = setTimeout(() => { state.timer = null; loadList().catch(report); }, 20000);
  }
  function renderList() {
    const focus = document.activeElement?.id; list.replaceChildren();
    if (!state.items.length) list.append(element("p", "No retained jobs in this team. Start a bounded investigation above or refresh to check for a teammate’s job.", "jobs-source"));
    const labels = {queued: "Queued", running: "Collecting", retry_wait: "Waiting for retry", completed: "Finished within bounds", cancelled: "Cancelled", failed: "Collection stopped after repeated errors"};
    for (const job of state.items) {
      const row = element("article", undefined, "jobs-entry"), main = element("div"), actions = element("div", undefined, "jobs-actions");
      const heading = element("h3"), link = element("a", job.account_id); link.href = "/investigate.html?account=" + encodeURIComponent(job.account_id); heading.append(link);
      main.append(heading, element("p", (job.kind === "graph" ? "Transfer graph" : job.kind === "transactions" ? "Transactions" : "Transfers") + ". " + labels[job.state] + ".", "jobs-state"));
      main.append(element("p", `${job.counts.pages} / ${job.bounds.max_pages} page limit; ${job.counts.matched_rows} / ${job.bounds.max_rows} retained row limit. ${job.counts.scanned_rows} rows scanned.`, "jobs-counts"));
      main.append(element("p", "Window: " + (job.bounds.from_timestamp_ms === null || job.bounds.from_timestamp_ms === undefined ? "No lower date bound" : date(job.bounds.from_timestamp_ms)) + " to before " + date(job.bounds.to_timestamp_ms) + ".", "jobs-source"));
      if (active(job) && Number.isFinite(job.retry_at_ms) && job.retry_at_ms > Date.now()) main.append(element("p", "Next eligible attempt: " + date(job.retry_at_ms) + ".", "jobs-source"));
      if (job.last_error) main.append(element("p", job.last_error === "history_capacity" ? "Waiting for the account/provider budget, archive capacity or due watch polling. Retained results remain available." : "Last attempt could not read its source (" + String(job.last_error) + "). Retained results remain available.", "jobs-source"));
      const open = button(actions, "View results", () => loadResults(job.id).catch(report)); open.id = "jobs-open-" + job.id;
      if (active(job)) { const cancel = button(actions, "Cancel", () => mutate(job.id, "cancel"), !writable() || state.mutating); cancel.id = "jobs-cancel-" + job.id; }
      if (["failed", "cancelled", "retry_wait"].includes(job.state)) { const retry = button(actions, "Retry", () => mutate(job.id, "retry"), !writable() || state.mutating); retry.id = "jobs-retry-" + job.id; }
      row.append(main, actions); list.append(row);
    }
    if (focus) document.getElementById(focus)?.focus();
  }
  async function loadList() {
    if (!state.context || !visible() || state.listing) return;
    const epoch = state.epoch; state.listing = true; refresh.disabled = true;
    try {
      const data = await request(base() + "?limit=25", undefined, epoch);
      if (!Array.isArray(data.items) || data.items.length > 25 || !data.items.every(validJob)) throw new Error("The retained job list could not be read. Refresh to retry.");
      state.items = data.items; state.uncertain = false; permissionControls(); renderList();
      message("");
    } finally { if (epoch === state.epoch) { state.listing = false; refresh.disabled = false; schedule(); } }
  }
  function validGraph(value) { return value && value.kind === "flow" && account(value.root) && value.source?.network === "mainnet" && ["nodes", "edges", "records"].every(key => Array.isArray(value[key]) && value[key].length <= 200) && value.coverage?.history_complete === false; }
  function download(name, data) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
    const link = element("a"); link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function loadResults(id, before = 0) {
    if (!state.context) return;
    const revision = ++state.resultRevision;
    const epoch = state.epoch, data = await request(base() + "/" + id + "/result?before=" + before + "&limit=100", undefined, epoch);
    if (revision !== state.resultRevision) throw new StaleRequest();
    if (!Array.isArray(data.items) || data.items.length > 100 || !data.items.every(item => item && typeof item === "object") || !Array.isArray(data.observations) || data.observations.length > 100 || !data.observations.every(observation => observation && observation.source && typeof observation.source === "object" && typeof observation.page_id === "string" && /^[a-f0-9]{64}$/.test(observation.page_id)) || data.coverage?.history_complete !== false || !data.pagination || data.pagination.next_before !== null && (!count(data.pagination.next_before) || data.pagination.next_before <= before)) throw new Error("Retained results could not be read. Refresh and retry.");
    if (data.graph !== undefined && data.graph !== null && !validGraph(data.graph)) throw new Error("The private graph could not be read. Refresh retained results to retry.");
    results.hidden = false; resultBody.replaceChildren();
    const actions = element("div", undefined, "jobs-result-actions"); button(actions, "Download this result page", () => download("siyab-investigation-" + id + ".json", data));
    if (data.graph) button(actions, "Download private graph JSON", () => download("siyab-private-graph-" + id + ".json", data.graph));
    resultBody.append(actions, element("p", "Retained provider observations only. " + (typeof data.coverage.stop_reason === "string" ? "Collection stop: " + data.coverage.stop_reason.replaceAll("_", " ") + ". " : "") + "Complete chain history is not established.", "jobs-source"));
    if (!data.items.length) resultBody.append(element("p", "No matching rows have been retained for this job yet. Check its collection status above.", "jobs-source"));
    else {
      const table = element("table", undefined, "jobs-table"), caption = element("caption", "Retained result page. Native amounts use NEAR with exact base units available. Token amounts without known decimals stay in base units; missing amounts and counterparties remain unknown.");
      const head = element("thead"), header = element("tr"); for (const title of ["Time", "Activity", "Amount", "Evidence"]) { const cell = element("th", title); cell.scope = "col"; header.append(cell); }
      head.append(header); const body = element("tbody");
      for (const item of data.items) {
        const row = element("tr"), values = [date(item.timestamp_ms), item.receipt_id ? (typeof item.from === "string" ? item.from : "Unknown sender") + " to " + (typeof item.to === "string" ? item.to : "Unknown receiver") : "Transaction " + (item.transaction_hash || "hash unavailable"), typeof item.amount === "string" && /^\d+$/.test(item.amount) ? item.amount + " base units of " + (typeof item.asset_id === "string" ? item.asset_id : "unknown asset") : "Not supplied by transaction index", item.receipt_id || item.transaction_hash || "Source identifier unavailable"];
        values.forEach((text, index) => {
          const cell = element("td", text); cell.dataset.label = ["Time", "Activity", "Amount", "Evidence"][index];
          if (index === 2 && item.asset_id === "native:near" && typeof item.amount === "string" && /^\d{1,39}$/.test(item.amount)) {
            const exact = element("details"), summary = element("summary", window.SiyabNumbers.baseUnits(item.amount, 24, 4) + " NEAR");
            summary.title = item.amount + " yoctoNEAR"; exact.append(summary, element("p", item.amount + " yoctoNEAR (exact)", "jobs-evidence"));
            cell.dataset.exportValue = item.amount; cell.replaceChildren(exact);
          }
          row.append(cell);
        }); body.append(row);
      }
      table.append(caption, head, body); resultBody.append(table);
    }
    const sources = element("details"); sources.append(element("summary", "Collection sources (" + data.observations.length + " pages)"));
    for (const observation of data.observations) {
      const entry = element("div"); entry.append(element("p", (typeof observation.source?.provider === "string" ? observation.source.provider : "Provider unavailable") + ". Observed " + (typeof observation.observed_at === "string" ? observation.observed_at : "time unavailable") + ".", "jobs-source"));
      if (typeof observation.page_id === "string" && /^[a-f0-9]{64}$/.test(observation.page_id)) entry.append(element("p", "Retained page SHA-256: " + observation.page_id, "jobs-evidence")); sources.append(entry);
    }
    resultBody.append(sources); const pages = element("div", undefined, "jobs-result-actions");
    if (before) button(pages, "First retained page", () => loadResults(id).catch(report));
    if (data.pagination.next_before !== null) button(pages, "Next retained page", () => loadResults(id, data.pagination.next_before).catch(report));
    button(pages, "Refresh retained results", () => loadResults(id).catch(report)); resultBody.append(pages); document.getElementById("jobs-results-title").focus();
  }
  async function mutate(id, action) {
    if (!writable() || state.mutating) return;
    const epoch = state.epoch; state.mutating = true; permissionControls(); renderList();
    try { await request(base() + "/" + id + "/" + action, {}, epoch); await loadList(); message(action === "cancel" ? "Job cancelled. Retained observations are kept." : "Job queued to resume within its original bounds."); }
    catch (error) { report(error); } finally { if (epoch === state.epoch) { state.mutating = false; permissionControls(); renderList(); } }
  }
  async function contextChanged(event) {
    const detail = event.detail; clear("Sign in to a team workspace to collect a bounded history or expand a transfer graph.");
    if (!detail?.authenticated || !identifier(detail.userId) || !detail.team || !identifier(detail.team.id)) return;
    const epoch = state.epoch; contextLine.textContent = "Checking access to " + String(detail.team.name) + "…";
    try {
      const session = await request("/session", undefined, epoch);
      if (!session.authenticated || session.user?.id !== detail.userId || !Array.isArray(session.teams)) throw Object.assign(new Error("Your session changed. Sign in again in Team workspace."), {status: 401});
      const team = session.teams.find(team => team.id === detail.team.id);
      if (!team || !["owner", "editor", "viewer"].includes(team.role) || typeof session.csrf_token !== "string") throw Object.assign(new Error("Your team access changed. Refresh Team workspace."), {status: 403});
      state.context = {userId: session.user.id, team, csrfToken: session.csrf_token}; permissionControls(); await loadList();
    } catch (error) { report(error); }
  }
  function numeric(field, maximum) { if (!/^[1-9]\d{0,3}$/.test(field.value) || Number(field.value) > maximum) throw new Error("Enter collection limits within the shown ranges."); return Number(field.value); }
  function kindChanged() { const graph = fields.kind.value === "graph"; document.getElementById("jobs-graph-fields").hidden = !graph; fields.rows.max = graph ? "200" : "5000"; if (graph && Number(fields.rows.value) > 200) fields.rows.value = "200"; }
  fields.kind.addEventListener("change", kindChanged);
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (!writable() || state.mutating) return;
    const epoch = state.epoch;
    try {
      const subject = fields.account.value.trim(), kind = fields.kind.value;
      if (!account(subject) || !["transfers", "transactions", "graph"].includes(kind)) throw new Error("Enter a valid NEAR mainnet account and collection type.");
      const values = {account: subject, kind, max_pages: numeric(fields.pages, 100), max_rows: numeric(fields.rows, kind === "graph" ? 200 : 5000)};
      for (const [field, name] of [[fields.from, "from_timestamp_ms"], [fields.until, "to_timestamp_ms"]]) if (field.value) { const stamp = Date.parse(field.value + "Z"); if (!Number.isSafeInteger(stamp) || stamp < 0 || stamp > Date.now()) throw new Error("Choose valid UTC date bounds up to the current time."); values[name] = stamp; }
      if (values.from_timestamp_ms !== undefined && values.from_timestamp_ms >= (values.to_timestamp_ms ?? Date.now())) throw new Error("The start must be before the end of the UTC date window.");
      if (kind === "graph") Object.assign(values, {depth: numeric(fields.depth, 5), max_accounts: numeric(fields.accounts, 50), pages_per_account: numeric(fields["account-pages"], 5)});
      state.mutating = true; permissionControls(); form.setAttribute("aria-busy", "true"); const job = await request(base(), values, epoch);
      if (!validJob(job)) throw new Error("The job response could not be read. Refresh jobs before starting another investigation.");
      await loadList();
      const current = state.items.find(item => item.id === job.id) || job;
      message(current.state === "completed" ? "Investigation finished within its collection bounds." : "Investigation queued. Progress follows the worker and source budgets shown below.");
    } catch (error) { report(error); } finally { if (epoch === state.epoch) { state.mutating = false; form.setAttribute("aria-busy", "false"); permissionControls(); } }
  });
  refresh.addEventListener("click", () => { if (state.context) loadList().catch(report); else window.dispatchEvent(new CustomEvent("siyab:workspace-refresh-request")); });
  window.addEventListener("siyab:workspace-context", contextChanged);
  window.addEventListener("pagehide", () => clear("Sign in to a team workspace to reload retained jobs."));
  document.addEventListener("visibilitychange", () => { if (visible()) loadList().catch(report); else stopPolling(); });
  if (typeof IntersectionObserver === "function") new IntersectionObserver(entries => { state.inView = entries.some(entry => entry.isIntersecting); if (state.inView) loadList().catch(report); else stopPolling(); }).observe(panel);
  kindChanged(); permissionControls(); window.dispatchEvent(new CustomEvent("siyab:workspace-context-request"));
})();
