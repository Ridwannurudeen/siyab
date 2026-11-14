(() => {
  "use strict";
  const root = document.getElementById("team-delivery");
  if (!root) return;
  const el = name => document.getElementById("delivery-" + name);
  const state = {epoch: 0, context: null, pending: new Set(), status: null, link: null, busy: false, uncertain: false, timer: null, pages: [null], page: 0, next: null, logRevision: 0};
  const identifier = value => typeof value === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(value);
  const positive = value => Number.isSafeInteger(value) && value > 0;
  const destinationLabel = value => typeof value === "string" && value.trim().length > 0 && value.length <= 200;
  const date = value => Number.isSafeInteger(value) && value >= 0 && value <= 8640000000000000 ? new Date(value).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC") : "Not recorded";
  const node = (tag, value, className) => { const item = document.createElement(tag); if (value !== undefined) item.textContent = String(value); if (className) item.className = className; return item; };
  const owner = () => state.context?.team.role === "owner";
  const base = () => "/teams/" + encodeURIComponent(state.context.team.id) + "/delivery";
  const message = value => { el("status").textContent = value; };
  const warn = value => { if (el("warning").textContent !== value) el("warning").textContent = value; };
  const button = (parent, label, action, disabled = false) => { const item = node("button", label); item.type = "button"; item.disabled = disabled; item.addEventListener("click", action); parent.append(item); return item; };
  class StaleRequest extends Error {}
  function clear(text = "Sign in and select a team to inspect its delivery destination.") {
    state.epoch++; state.logRevision++; clearTimeout(state.timer); state.timer = null;
    for (const controller of state.pending) controller.abort(); state.pending.clear();
    state.context = null; state.status = null; state.link = null; state.busy = false; state.uncertain = false; state.pages = [null]; state.page = 0; state.next = null;
    el("context").textContent = text; message(""); warn(""); el("controls").replaceChildren(); el("controls").setAttribute("aria-busy", "false"); el("log").replaceChildren(node("p", "Sign in and select a team to see deliveries.", "delivery-meta"));
    el("newer").disabled = true; el("older").disabled = true;
  }
  async function request(path, values, epoch = state.epoch) {
    if (epoch !== state.epoch) throw new StaleRequest();
    const controller = new AbortController(); state.pending.add(controller);
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const headers = {Accept: "application/json"};
      if (values !== undefined) { headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = state.context?.csrfToken || ""; }
      const response = await fetch("/workspace-api" + path, {method: values === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", headers, body: values === undefined ? undefined : JSON.stringify(values), signal: controller.signal});
      if (epoch !== state.epoch) throw new StaleRequest();
      if (response.status === 401 || response.status === 403) throw Object.assign(new Error("Your session or team access changed. Refresh Team workspace before continuing."), {status: response.status});
      if (!(response.headers.get("content-type") || "").includes("application/json")) throw new Error("Telegram delivery is unavailable on this copy. Open siyab.xyz or refresh.");
      const result = await response.json();
      if (epoch !== state.epoch) throw new StaleRequest();
      if (controller.signal.aborted) throw new Error("The request timed out.");
      if (values !== undefined && response.status === 503 && result?.error === "delivery_capacity") throw Object.assign(new Error("The retry was refused and nothing changed. This team's undelivered alerts hold its delivery attempt records; retry after they finish."), {unchanged: true});
      if (!response.ok) throw new Error(values === undefined ? "Delivery could not be read. Refresh delivery to retry." : "The change was not confirmed. Refresh delivery before retrying.");
      return result;
    } catch (error) {
      if (epoch !== state.epoch) throw new StaleRequest();
      if (values !== undefined && controller.signal.aborted) { state.uncertain = true; throw new Error("The response timed out. Refresh delivery before retrying; the change may have been saved."); }
      throw error;
    } finally { clearTimeout(timeout); state.pending.delete(controller); }
  }
  function report(error) {
    if (error instanceof StaleRequest) return;
    if (error.status === 401 || error.status === 403) clear("Your team session changed. Refresh Team workspace to reload access.");
    message(error.message || "Delivery is unavailable. Refresh delivery to retry.");
  }
  function validStatus(data) {
    return data && typeof data.configured === "boolean" && ["disconnected", "pending", "observed", "expired", "active"].includes(data.state)
      && (data.kind === undefined || ["private", "group"].includes(data.kind)) && (data.groups_enabled === undefined || typeof data.groups_enabled === "boolean")
      && (!owner() || data.kind !== "group" || typeof data.verified === "boolean")
      && (data.link_conflict === undefined || typeof data.link_conflict === "boolean")
      && (data.link_conflict_chats === undefined || Number.isSafeInteger(data.link_conflict_chats) && data.link_conflict_chats >= 0)
      && typeof data.bot_username === "string" && /^[A-Za-z0-9_]{5,32}$/.test(data.bot_username) && data.delivery_semantics === "at_least_once"
      && (!owner() || !["pending", "observed"].includes(data.state) || positive(data.generation) && positive(data.expires_at))
      && (!owner() || data.state !== "observed" || destinationLabel(data.observed_chat_label))
      && data.capacity && ["paused", "queue_paused", "attempts_paused"].every(key => typeof data.capacity[key] === "boolean")
      && data.capacity.paused === (data.capacity.queue_paused || data.capacity.attempts_paused)
      && positive(data.capacity.queue_rows_limit) && positive(data.capacity.attempt_rows_limit)
      && data.counts && Object.entries(data.counts).every(([key, value]) => ["pending", "sending", "sent", "dead", "cancelled"].includes(key) && Number.isSafeInteger(value) && value >= 0);
  }
  function renderStatus() {
    const host = el("controls"); host.replaceChildren();
    if (!state.context || !state.status) { warn(""); return; }
    const data = state.status, disabled = state.busy || state.uncertain;
    const expired = data.expires_at !== null && data.expires_at !== undefined && data.expires_at <= Date.now();
    const group = data.kind === "group" && (data.state === "active" || ["pending", "observed"].includes(data.state) && !expired);
    const labels = {disconnected: "No Telegram destination connected.", pending: "Waiting for the Telegram account to open the link and press Start.", observed: (data.kind === "group" ? "A Telegram group" : "A Telegram account") + " was observed. Confirm this destination before sending alerts.", expired: "The connection link expired. Create a new link to continue.", active: "Telegram destination confirmed for new eligible alerts."};
    const chats = data.link_conflict_chats > 1 ? data.link_conflict_chats + " other chats" : "another chat";
    warn(owner() && data.kind === "group" && data.state === "observed" && !expired && data.link_conflict === true ? "This link code was also used in " + chats + ". If you did not post it there, someone else has it: cancel this link and create a new one." : "");
    host.append(node("p", !data.configured ? "Telegram delivery is not configured by the operator. Team inbox alerts remain available." : labels[expired && data.state !== "active" ? "expired" : data.state]));
    if (group) host.append(node("p", "Everyone in this group will see this team's alerts."));
    if (data.capacity.paused) {
      const resume = " until an owner disconnects or connects Telegram again, which cancels undelivered alerts.";
      const paused = data.capacity.queue_paused && data.capacity.attempts_paused ? "New alert queueing, send attempts and retries are paused" + resume : data.capacity.queue_paused ? "New alert queueing is paused; retained deliveries can still be attempted, and queueing resumes as they finish." : "Send attempts and retries are paused" + resume;
      host.append(node("p", "Delivery retention limit reached for this team. " + paused + " Team inbox evidence remains available."));
    }
    if (owner() && destinationLabel(data.observed_chat_label)) host.append(node("p", (group ? "Observed Telegram group: " : "Observed Telegram account: ") + data.observed_chat_label));
    if (data.state === "pending" || data.state === "observed") host.append(node("p", "Link expires " + date(data.expires_at), "delivery-meta"));
    if (state.link && !expired && state.link.expires_at > Date.now()) {
      const link = node("a", "Open connection link in Telegram", "delivery-link"); link.href = state.link.link; link.target = "_blank"; link.rel = "noopener noreferrer"; host.append(link);
      host.append(node("p", group ? "A group admin must open the link, select the group and send the start command there. Return here, wait for admin verification, then check and confirm the observed group." : "Press Start in Telegram, return here, then check and confirm the observed account.", "delivery-meta"));
    }
    const actions = node("div", undefined, "delivery-actions");
    if (owner() && data.configured) {
      if (data.state !== "active") button(actions, data.state === "disconnected" ? "Connect Telegram" : "Create a new link", () => mutate("link"), disabled);
      if (data.state !== "active" && data.groups_enabled === true) button(actions, "Connect a Telegram group", () => mutate("link-group"), disabled);
      if (data.state === "observed" && !expired && positive(data.generation) && destinationLabel(data.observed_chat_label)) button(actions, "Confirm observed destination", () => mutate("confirm", {generation: data.generation}), disabled || group && data.verified !== true);
      if (data.state !== "disconnected") button(actions, data.state === "active" ? "Disconnect Telegram" : "Cancel connection", () => mutate("revoke"), disabled);
    } else if (!owner()) host.append(node("p", "Only a team owner can connect, confirm, disconnect or retry deliveries.", "delivery-meta"));
    host.append(actions);
    const counts = node("dl", undefined, "delivery-counts");
    for (const [key, label] of [["pending", "Queued"], ["sending", "Sending"], ["sent", "Accepted by Telegram"], ["dead", "Stopped after retries"], ["cancelled", "Cancelled"]]) {
      const item = node("div"); item.append(node("dt", label), node("dd", data.counts[key] || 0)); counts.append(item);
    }
    host.append(counts, node("p", "Worker last polled: " + date(data.last_poll_ms) + ". Last accepted send: " + date(data.last_send_ms) + ".", "delivery-meta"));
    if (!positive(data.last_poll_ms) || Date.now() - data.last_poll_ms > 120000) host.append(node("p", "Worker heartbeat is missing or stale. Delivery is not established by a connected destination alone.", "delivery-meta"));
    if (data.last_error || data.last_delivery_error) host.append(node("p", "A worker or delivery error was recorded. Inspect the delivery log and refresh before retrying.", "delivery-meta"));
  }
  function schedule() {
    clearTimeout(state.timer); state.timer = null;
    if (!state.context || document.hidden || !state.status?.configured || !["pending", "observed", "active"].includes(state.status.state)) return;
    state.timer = setTimeout(() => refresh(false).catch(report), state.status.state === "active" ? 30000 : 10000);
  }
  async function refresh(reset = true) {
    if (!state.context || state.busy) return;
    const epoch = state.epoch; state.busy = true; renderStatus(); el("controls").setAttribute("aria-busy", "true");
    try {
      const data = await request(base(), undefined, epoch);
      if (!validStatus(data)) {
        if (state.status?.kind === "group") state.uncertain = true;
        throw new Error("Delivery status could not be validated. Refresh delivery to retry.");
      }
      if (state.link && state.link.generation !== data.generation) state.link = null;
      state.status = data; state.uncertain = false;
      if (reset) { state.pages = [null]; state.page = 0; }
      await log(state.pages[state.page], state.page);
    } finally { if (epoch === state.epoch) { state.busy = false; renderStatus(); el("controls").setAttribute("aria-busy", "false"); schedule(); } }
  }
  async function log(before = null, index = 0) {
    if (!state.context) return;
    const epoch = state.epoch, revision = ++state.logRevision;
    el("newer").disabled = true; el("older").disabled = true;
    const data = await request(base() + "/log?limit=25" + (before === null ? "" : "&before=" + before), undefined, epoch);
    if (revision !== state.logRevision) return;
    if (!data || !Array.isArray(data.items) || data.items.length > 25 || !data.items.every(row => positive(row.id) && ["pending", "sending", "sent", "dead", "cancelled"].includes(row.state) && Number.isSafeInteger(row.attempts) && row.attempts >= 0)) throw new Error("The delivery log could not be validated. Refresh delivery.");
    state.page = index; state.pages[index] = before;
    state.next = data.items.length === 25 && positive(data.next_before) && (before === null || data.next_before < before) ? data.next_before : null;
    const host = el("log"); host.replaceChildren();
    if (!data.items.length) host.append(node("p", "No deliveries were retained in this page. Existing inbox alerts are not sent retroactively.", "delivery-meta"));
    const list = node("ul", undefined, "delivery-log-list");
    for (const row of data.items) {
      const item = node("li"), main = node("div");
      const labels = {pending: "Queued", sending: "Sending", sent: "Accepted by Telegram", dead: "Stopped after retries", cancelled: "Cancelled"};
      main.append(node("h3", labels[row.state]), node("p", "Attempts: " + row.attempts + ". Updated " + date(row.updated_ms), "delivery-meta"));
      if (row.uncertain_ack) main.append(node("p", "Telegram acknowledgement is uncertain. This alert may already have been sent; retrying can duplicate it."));
      if (row.state === "pending") main.append(node("p", "Next attempt: " + date(row.next_attempt_ms), "delivery-meta"));
      if (typeof row.event_id === "string" && /^[a-f0-9]{64}$/.test(row.event_id)) { const link = node("a", "Open alert evidence"); link.href = "/evidence/" + row.event_id; main.append(link); }
      item.append(main);
      if (row.state === "dead" && owner() && state.status?.state === "active") button(item, "Retry this delivery", () => mutate(row.id + "/retry"), state.uncertain || state.status.capacity.attempts_paused);
      list.append(item);
    }
    host.append(list); el("newer").disabled = index === 0; el("older").disabled = state.next === null;
  }
  async function mutate(action, values = {}) {
    if (!state.context || !owner() || state.busy || state.uncertain) return;
    const epoch = state.epoch; state.busy = true; renderStatus();
    try {
      const data = await request(base() + "/" + action, values, epoch);
      if (action === "link" || action === "link-group") {
        const expected = new RegExp("^https://t\\.me/" + state.status.bot_username + "\\?" + (action === "link-group" ? "startgroup" : "start") + "=[a-f0-9]{64}$");
        if (!data || !positive(data.generation) || !positive(data.expires_at) || typeof data.link !== "string" || !expected.test(data.link)) throw new Error("The connection link could not be validated. Refresh delivery before continuing.");
        state.link = {link: data.link, generation: data.generation, expires_at: data.expires_at};
      } else if (action === "revoke" || action === "confirm") state.link = null;
      state.busy = false; await refresh(); message(action === "confirm" ? "Destination confirmed for new eligible alerts." : action === "revoke" ? "Telegram destination disconnected. In-flight sends may still complete." : action === "link-group" ? "Ask a group admin to open the link and send the start command in the group, then return to confirm after verification." : action === "link" ? "Open the Telegram link, press Start, then verify the observed destination." : "Delivery queued for another attempt; an uncertain earlier send can cause a duplicate.");
    } catch (error) {
      if (!(error instanceof StaleRequest) && state.context && !error.unchanged && error.status !== 401 && error.status !== 403) {
        state.uncertain = true;
        error = new Error(error.message + " Refresh delivery before retrying; the change may have been saved.");
      }
      report(error);
    }
    finally { if (epoch === state.epoch) { state.busy = false; renderStatus(); } }
  }
  async function contextChanged(event) {
    clear(); const detail = event.detail;
    if (!detail?.authenticated || !identifier(detail.userId) || !detail.team || !identifier(detail.team.id)) return;
    const epoch = state.epoch;
    try {
      const session = await request("/session", undefined, epoch);
      if (!session?.authenticated || session.user?.id !== detail.userId || !Array.isArray(session.teams)) throw Object.assign(new Error("The session changed. Refresh Team workspace."), {status: 401});
      const team = session.teams.find(row => row.id === detail.team.id);
      if (!team || !["owner", "editor", "viewer"].includes(team.role) || typeof session.csrf_token !== "string") throw Object.assign(new Error("Team access changed. Refresh Team workspace."), {status: 403});
      state.context = {userId: session.user.id, team, csrfToken: session.csrf_token}; el("context").textContent = "Delivery for " + String(team.name); await refresh();
    } catch (error) { report(error); }
  }
  el("refresh").addEventListener("click", () => { if (state.context) refresh().catch(report); else window.dispatchEvent(new CustomEvent("siyab:workspace-refresh-request")); });
  el("newer").addEventListener("click", () => log(state.pages[state.page - 1], state.page - 1).catch(report));
  el("older").addEventListener("click", () => { if (state.next !== null) log(state.next, state.page + 1).catch(report); });
  window.addEventListener("siyab:workspace-context", contextChanged);
  window.addEventListener("pagehide", () => clear());
  document.addEventListener("visibilitychange", () => { if (document.hidden) { clearTimeout(state.timer); state.timer = null; } else if (state.context) refresh(false).catch(report); });
  window.dispatchEvent(new CustomEvent("siyab:workspace-context-request"));
})();
