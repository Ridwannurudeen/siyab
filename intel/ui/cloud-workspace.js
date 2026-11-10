(() => {
  "use strict";
  const prefix = "/workspace-api";
  const root = document.getElementById("cloud-workspace");
  const shell = document.getElementById("cloud-shell");
  const pending = new Set();
  const privateDialogs = new Set();
  const state = {session: null, teams: [], team: null, panel: "cases", epoch: 0, offset: 0, after: 0, before: null};
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const date = value => Number.isFinite(value) && value >= 0 && value <= 8640000000000000 ? new Date(value).toLocaleString() : "Time unavailable";
  const writable = () => state.team && state.team.role !== "viewer";
  const localUrl = value => {
    if (typeof value !== "string" || value.length > 2048 || !value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u001f]/.test(value)) return null;
    try {
      const url = new URL(value, location.origin);
      return url.origin === location.origin && (/^\/$|^\/(?:index|workspace|investigate|feed|verify|issuers|top-receivers)\.html$|^\/(?:accounts|entities|flows|clusters|incidents|records)\/[A-Za-z0-9._-]+\.html$/).test(url.pathname) ? url.pathname + url.search + url.hash : null;
    } catch (error) { return null; }
  };
  const caseValue = item => item && typeof item.title === "string" && item.title.trim() && item.title.length <= 120
    && typeof item.notes === "string" && item.notes.length <= 4000 && localUrl(item.url);
  const accountValue = value => typeof value === "string" && /^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$/.test(value) && !value.endsWith(".testnet");
  function importValue(value) {
    if (!value || value.version !== 1 || !Array.isArray(value.cases) || value.cases.length > 500 || !value.cases.every(caseValue)
        || !Array.isArray(value.watchlist) || value.watchlist.length > 100 || !value.watchlist.every(accountValue)) throw new Error("Choose a valid Siyab workspace JSON backup.");
    return {cases: value.cases.map(item => ({title: item.title.trim(), notes: item.notes, url: localUrl(item.url)})), watchlist: [...new Set(value.watchlist)]};
  }
  function chunks(value) {
    const result = [];
    let batch = {cases: [], watchlist: []};
    for (const [field, rows] of [["cases", value.cases], ["watchlist", value.watchlist]]) for (const item of rows) {
      const candidate = {...batch, [field]: [...batch[field], item]};
      if (candidate.cases.length > 50 || new TextEncoder().encode(JSON.stringify(candidate)).length > 24576) {
        if (!batch.cases.length && !batch.watchlist.length) throw new Error("An imported investigation is too large.");
        result.push(batch); batch = {cases: [], watchlist: []};
      }
      batch[field].push(item);
    }
    if (batch.cases.length || batch.watchlist.length) result.push(batch);
    return result;
  }
  class StaleRequest extends Error {}
  async function request(path, values, epoch = state.epoch) {
    if (epoch !== state.epoch) throw new StaleRequest();
    const controller = new AbortController(); pending.add(controller);
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const headers = {Accept: "application/json"};
      if (values !== undefined) {
        headers["Content-Type"] = "application/json";
        if (state.session?.csrf_token) headers["X-CSRF-Token"] = state.session.csrf_token;
      }
      const response = await fetch(prefix + path, {method: values === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
        headers, body: values === undefined ? undefined : JSON.stringify(values), signal: controller.signal});
      if (!(response.headers.get("content-type") || "").includes("application/json")) throw new Error("Shared workspace is unavailable on this copy. Retry on siyab.xyz.");
      const value = await response.json();
      if (controller.signal.aborted || epoch !== state.epoch) throw new StaleRequest();
      if (!response.ok) {
        const error = new Error(typeof value?.message === "string" ? value.message : "The shared workspace request failed. Try again.");
        error.status = response.status; error.code = value?.error;
        throw error;
      }
      if (!value || typeof value !== "object") throw new Error("The shared workspace response could not be read.");
      return value;
    } catch (error) {
      if (epoch !== state.epoch) throw new StaleRequest();
      if (controller.signal.aborted) throw new Error(values === undefined ? "The shared workspace read timed out. Retry." : "The response timed out. Refresh before retrying; the change may have been saved.");
      throw error;
    } finally { clearTimeout(timeout); pending.delete(controller); }
  }
  function invalidate() {
    state.epoch++;
    for (const controller of pending) controller.abort();
    pending.clear();
  }
  function scrubDialogs(predicate = () => true) {
    for (const record of [...privateDialogs]) if (predicate(record)) {
      for (const field of record.dialog.querySelectorAll("input,textarea,pre")) { field.value = ""; field.textContent = ""; }
      record.dialog.replaceChildren(); record.dialog.close(); record.dialog.remove(); privateDialogs.delete(record);
    }
  }
  function status(text, error = false) {
    const target = document.getElementById("cloud-status") || document.getElementById("workspace-feedback");
    if (target) { target.textContent = text; target.classList.toggle("cloud-error", error); }
  }
  function report(error) {
    if (error instanceof StaleRequest) return;
    if (error.status === 401) {
      scrubDialogs();
      invalidate(); state.session = null; state.teams = []; state.team = null; render(); updateDestination(); addGlobalWatches();
    }
    status(error.message, true);
  }
  const button = (text, action, parent, disabled = false) => {
    const control = node("button", text); control.type = "button"; control.disabled = disabled;
    control.addEventListener("click", () => action(control));
    if (parent) parent.append(control);
    return control;
  };
  function field(parent, label, name, options = {}) {
    const wrapper = node("label", label), input = node(options.textarea ? "textarea" : "input");
    input.name = name;
    if (!options.textarea) input.type = options.type || "text";
    for (const [key, value] of Object.entries(options)) if (key !== "textarea") input[key] = value;
    wrapper.append(input); parent.append(wrapper); return input;
  }
  function form(parent, onSubmit, className = "cloud-form") {
    const element = node("form", undefined, className);
    element.addEventListener("submit", async event => {
      event.preventDefault();
      if (element.getAttribute("aria-busy") === "true") return;
      const values = new FormData(element), controls = [...element.querySelectorAll("button,input,textarea,select")];
      const disabled = controls.map(control => control.disabled);
      element.setAttribute("aria-busy", "true");
      controls.forEach(control => control.disabled = true);
      try { await onSubmit(values, element); }
      catch (error) { report(error); }
      finally { element.setAttribute("aria-busy", "false"); controls.forEach((control, index) => { if (control.isConnected) control.disabled = disabled[index]; }); }
    });
    parent.append(element); return element;
  }
  function submit(parent, text) { const control = node("button", text, "primary"); control.type = "submit"; parent.append(control); return control; }
  async function action(control, task) {
    control.disabled = true;
    try { await task(); } catch (error) { report(error); }
    finally { if (control.isConnected) control.disabled = false; }
  }
  function download(name, value) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: "application/json"}));
    const link = node("a"); link.href = url; link.download = name; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function recovery(code, username, userId) {
    if (typeof code !== "string") return;
    const dialog = node("dialog", undefined, "case-dialog cloud-recovery"); dialog.setAttribute("aria-label", "Save your recovery code");
    const record = {dialog, kind: "recovery", userId}; privateDialogs.add(record);
    dialog.append(node("h2", "Keep your recovery code"), node("p", "This code is shown once. It can replace a forgotten password and signs out other sessions. Keep it somewhere private."), node("pre", code));
    const actions = node("div", undefined, "workspace-actions");
    button("Download recovery code", () => download("siyab-recovery.json", {username, recovery_code: code}), actions);
    button("I have kept it", () => dialog.close(), actions); dialog.append(actions); document.body.append(dialog);
    dialog.addEventListener("close", () => { dialog.replaceChildren(); dialog.remove(); privateDialogs.delete(record); }); dialog.showModal();
  }
  async function loadSession(open = false) {
    const selectedTeamId = state.team?.id;
    invalidate();
    scrubDialogs(record => record.kind === "editor");
    state.session = null; state.teams = []; state.team = null; updateDestination(); addGlobalWatches();
    if (shell) shell.replaceChildren(node("p", "Reading your shared workspace session…", "source-line"));
    let data;
    try {
      data = await request("/session");
      if (typeof data.authenticated !== "boolean" || !Array.isArray(data.teams)
          || data.teams.some(team => typeof team.id !== "string" || typeof team.name !== "string" || !["owner", "editor", "viewer"].includes(team.role))
          || data.authenticated && (typeof data.user?.username !== "string" || typeof data.user.id !== "string" || typeof data.csrf_token !== "string")) throw new Error("The workspace session could not be read.");
    }
    catch (error) {
      if (shell && !(error instanceof StaleRequest)) {
        shell.replaceChildren(node("p", error.message, "cloud-error"));
        button("Retry shared workspace", () => loadSession().catch(report), shell);
      }
      throw error;
    }
    state.session = data.authenticated ? data : null;
    scrubDialogs(record => record.userId !== state.session?.user.id);
    state.teams = data.teams;
    state.team = state.teams.find(team => team.id === selectedTeamId) || state.teams[0] || null;
    render(); updateDestination(); addGlobalWatches();
    if (root && (open || data.authenticated)) root.open = true;
  }
  function render() {
    if (!shell) return;
    shell.replaceChildren();
    const feedback = node("p", undefined, "cloud-status"); feedback.id = "cloud-status"; feedback.setAttribute("role", "status"); feedback.setAttribute("aria-live", "polite"); shell.append(feedback);
    if (!state.session) { renderAuth(); return; }
    const head = node("div", undefined, "cloud-identity");
    head.append(node("p", "Signed in as " + state.session.user.username));
    button("Sign out", control => action(control, async () => {
      await request("/auth/logout", {}); scrubDialogs(); invalidate(); state.session = null; state.teams = []; state.team = null;
      render(); updateDestination(); addGlobalWatches(); status("Signed out. Browser-only investigations remain on this device.");
    }), head);
    shell.append(head);
    const selector = node("label", "Team workspace"), select = node("select"); select.id = "cloud-team-select";
    for (const team of state.teams) { const option = node("option", team.name + " · " + team.role); option.value = team.id; select.append(option); }
    select.value = state.team?.id || "";
    select.addEventListener("change", () => { scrubDialogs(record => record.kind === "editor"); invalidate(); state.team = state.teams.find(team => team.id === select.value) || null; state.offset = 0; state.after = 0; state.before = null; render(); addGlobalWatches(); });
    selector.append(select); shell.append(selector);
    const nav = node("div", undefined, "cloud-tabs"); nav.setAttribute("role", "group"); nav.setAttribute("aria-label", "Shared workspace sections");
    for (const [name, title] of [["cases", "Investigations"], ["watches", "Watchlist"], ["inbox", "Alerts"], ["members", "Team"], ["audit", "Audit history"], ["account", "Account"]]) {
      const control = button(title, () => { invalidate(); state.panel = name; state.offset = 0; state.after = 0; state.before = null; renderPanel(); }, nav);
      control.dataset.cloudPanel = name; control.setAttribute("aria-pressed", String(state.panel === name));
    }
    shell.append(nav); const content = node("div", undefined, "cloud-content"); content.id = "cloud-content"; shell.append(content); renderPanel();
  }
  function renderAuth() {
    shell.append(node("p", "Sign in to keep investigations across devices and collaborate with a team. Browser-only work below stays on this device until you choose to import it.", "cloud-intro"));
    const tabs = node("div", undefined, "cloud-tabs");
    const body = node("div", undefined, "cloud-auth"); shell.append(tabs, body);
    const show = mode => {
      body.replaceChildren(); tabs.querySelectorAll("button").forEach(control => control.setAttribute("aria-pressed", String(control.dataset.authMode === mode)));
      const auth = form(body, async (values, element) => {
        const data = mode === "recover" ? {username: values.get("username"), recovery_code: values.get("recovery_code"), new_password: values.get("password")}
          : {username: values.get("username"), password: values.get("password")};
        const result = await request("/auth/" + mode, data); element.reset();
        recovery(result.recovery_code, result.user.username, result.user.id); await loadSession(true);
      });
      field(auth, "Username", "username", {required: true, minLength: 3, maxLength: 40, pattern: "[a-z0-9][a-z0-9._-]{1,38}[a-z0-9]", autocomplete: "username"});
      if (mode === "recover") field(auth, "Recovery code", "recovery_code", {required: true, autocomplete: "off", maxLength: 128});
      field(auth, mode === "recover" ? "New password" : "Password", "password", {type: "password", required: true, minLength: 12, maxLength: 128, autocomplete: mode === "login" ? "current-password" : "new-password"});
      submit(auth, mode === "register" ? "Create account" : mode === "recover" ? "Recover account" : "Sign in");
      if (mode !== "login") body.append(node("p", mode === "register" ? "Use 3 to 40 lowercase characters for your username and at least 12 characters for your password. Keep the recovery code shown after registration." : "Recovery requires your saved one-time code. Email password resets are not configured.", "source-line"));
    };
    for (const [mode, title] of [["login", "Sign in"], ["register", "Create account"], ["recover", "Recover account"]]) {
      const control = button(title, () => show(mode), tabs); control.dataset.authMode = mode;
    }
    show("login");
  }
  async function renderPanel() {
    const content = document.getElementById("cloud-content"); if (!content) return;
    invalidate();
    const epoch = state.epoch;
    shell.querySelectorAll("[data-cloud-panel]").forEach(control => control.setAttribute("aria-pressed", String(control.dataset.cloudPanel === state.panel)));
    content.replaceChildren(node("p", "Reading shared workspace…", "source-line")); content.setAttribute("aria-busy", "true");
    try {
      if (state.panel === "account") { content.replaceChildren(); renderAccount(content); return; }
      if (!state.team) { content.replaceChildren(node("p", "Create a team workspace to begin.")); renderTeamForms(content); return; }
      const base = "/teams/" + encodeURIComponent(state.team.id);
      if (state.panel === "cases") {
        const data = await request(base + "/cases?offset=" + state.offset + "&limit=50");
        if (!Array.isArray(data.cases) || !Number.isInteger(data.total)) throw new Error("Investigations could not be read.");
        content.replaceChildren(); renderCases(content, data, base);
      } else if (state.panel === "watches") {
        const [data, monitoring] = await Promise.all([request(base + "/watches"), request("/monitor")]);
        if (!Array.isArray(data.watches)) throw new Error("The watchlist could not be read.");
        content.replaceChildren(); renderWatches(content, data.watches, monitoring, base);
      } else if (state.panel === "members") {
        const data = await request(base + "/members"); if (!Array.isArray(data.members)) throw new Error("Team members could not be read.");
        content.replaceChildren(); renderMembers(content, data.members, base);
      } else {
        const cursor = state.panel === "inbox" && state.before !== null ? "before=" + state.before : "after=" + state.after;
        const data = await request(base + "/" + state.panel + "?" + cursor + "&limit=50");
        if (!Array.isArray(data.items)) throw new Error("Workspace history could not be read.");
        content.replaceChildren(); renderHistory(content, data, base);
      }
    } catch (error) {
      if (epoch !== state.epoch || error instanceof StaleRequest) return;
      content.replaceChildren(node("p", error.message, "cloud-error")); button("Retry", () => renderPanel(), content); report(error);
    } finally { if (epoch === state.epoch) content.setAttribute("aria-busy", "false"); }
  }
  function renderCases(parent, data, base) {
    const head = node("div", undefined, "cloud-section-head"); head.append(node("h3", "Shared investigations"), node("span", data.total + " saved", "source-line"));
    button("New investigation", () => editCase(null, base), head, !writable()); parent.append(head);
    if (!writable()) parent.append(node("p", "Viewer access. You can read and export investigations.", "source-line"));
    for (const item of data.cases) {
      if (!caseValue(item)) throw new Error("An investigation contains an unsupported page address.");
      const row = node("article", undefined, "cloud-case"); const title = node("h4"), link = node("a", item.title); link.href = localUrl(item.url); title.append(link);
      row.append(title, node("p", item.url, "saved-path")); if (item.notes) row.append(node("p", item.notes, "saved-notes"));
      row.append(node("p", "Updated " + date(item.updated_ms) + " · Version " + item.version, "source-line"));
      const actions = node("div", undefined, "workspace-actions");
      button("Edit notes", () => editCase(item, base), actions, !writable());
      button("Delete", control => action(control, async () => {
        if (!confirm('Delete "' + item.title + '" from this team workspace?')) return;
        await request(base + "/cases/" + item.id + "/delete", {version: item.version}); await renderPanel(); status("Shared investigation deleted.");
      }), actions, !writable()); row.append(actions); parent.append(row);
    }
    if (!data.cases.length) parent.append(node("p", "No shared investigations yet. Create one here or choose a team when saving an account or graph.", "cloud-empty"));
    const pages = node("div", undefined, "workspace-actions");
    button("Previous", () => { state.offset = Math.max(0, state.offset - 50); renderPanel(); }, pages, state.offset === 0);
    pages.append(node("span", data.total ? `${state.offset + 1} to ${state.offset + data.cases.length} of ${data.total}` : "0 investigations", "source-line"));
    button("Next", () => { state.offset = data.next_offset; renderPanel(); }, pages, data.next_offset === null); parent.append(pages);
    renderPortability(parent, base);
  }
  function editCase(item, base) {
    const epoch = state.epoch;
    const dialog = node("dialog", undefined, "case-dialog"); dialog.setAttribute("aria-label", item ? "Edit shared investigation" : "New shared investigation");
    const record = {dialog, kind: "editor", userId: state.session?.user.id}; privateDialogs.add(record);
    dialog.append(node("h2", item ? "Edit shared investigation" : "New shared investigation"));
    let version = item?.version;
    const edit = form(dialog, async values => {
      if (epoch !== state.epoch || !dialog.isConnected) throw new StaleRequest();
      const data = {title: values.get("title"), notes: values.get("notes"), url: values.get("url")};
      if (!caseValue(data)) throw new Error("Enter a title and a local Siyab investigation address.");
      try {
        await request(base + "/cases" + (item ? "/" + item.id : ""), item ? {...data, version} : data, epoch);
        dialog.close(); await renderPanel(); status("Shared investigation saved.");
      } catch (error) {
        feedback.textContent = error.message;
        if (error.code === "version_conflict") reload.hidden = false;
        throw error;
      }
    });
    const title = field(edit, "Title", "title", {required: true, maxLength: 120, value: item?.title || ""});
    const url = field(edit, "Investigation address", "url", {required: true, maxLength: 2048, value: item?.url || "", placeholder: "/investigate.html?account=account.near"});
    const notes = field(edit, "Notes", "notes", {textarea: true, maxLength: 4000, rows: 6, value: item?.notes || ""});
    const feedback = node("p", undefined, "cloud-error"); feedback.setAttribute("role", "alert"); edit.append(feedback);
    const actions = node("div", undefined, "workspace-actions"); submit(actions, "Save investigation");
    const reload = button("Reload latest version", control => action(control, async () => {
      if (!confirm("Replace this draft with the latest saved version? Download your draft first if you want to keep both.")) return;
      const cases = await allCases(base, epoch); const current = cases.find(row => row.id === item.id);
      if (!current) throw new Error("This investigation was deleted. Download your draft, then save it as a new investigation.");
      title.value = current.title; url.value = current.url; notes.value = current.notes; version = current.version;
      feedback.textContent = "Latest version loaded. Review it before saving."; reload.hidden = true;
    }), actions); reload.hidden = true;
    button("Download draft", () => download("siyab-investigation-draft.json", {version: 1, cases: [{title: title.value, notes: notes.value, url: url.value}], watchlist: []}), actions);
    button("Cancel", () => dialog.close(), actions); edit.append(actions); document.body.append(dialog);
    const opener = document.activeElement;
    dialog.addEventListener("cancel", event => { if (edit.getAttribute("aria-busy") === "true") event.preventDefault(); });
    dialog.addEventListener("close", () => { privateDialogs.delete(record); dialog.remove(); if (opener?.isConnected) opener.focus(); else document.getElementById("cloud-team-select")?.focus(); });
    dialog.showModal(); title.focus();
  }
  async function allCases(base, epoch = state.epoch) {
    const rows = []; let offset = 0;
    while (offset !== null) {
      const data = await request(base + "/cases?offset=" + offset + "&limit=50", undefined, epoch);
      if (!Array.isArray(data.cases) || rows.length + data.cases.length > 500 || data.next_offset !== null && (!Number.isInteger(data.next_offset) || data.next_offset <= offset)) throw new Error("The investigation export is incomplete. Retry.");
      rows.push(...data.cases); offset = data.next_offset;
    }
    return rows;
  }
  function renderPortability(parent, base) {
    const details = node("details", undefined, "cloud-portability"); details.append(node("summary", "Import and export"));
    details.append(node("p", "Import creates investigations and watches after you review the counts. Existing notes are never replaced. Imported records receive a new server creation time.", "source-line"));
    const actions = node("div", undefined, "workspace-actions");
    button("Export shared workspace JSON", control => action(control, async () => {
      const epoch = state.epoch;
      const [cases, watches] = await Promise.all([allCases(base, epoch), request(base + "/watches", undefined, epoch)]);
      download("siyab-shared-workspace.json", {version: 1, cases: cases.map(row => ({id: "case-" + row.id, title: row.title, notes: row.notes, url: row.url, created_at: new Date(row.created_ms).toISOString()})), watchlist: watches.watches.map(row => row.account), alerts: false, notifications: []}); status("Shared investigations and watchlist exported.");
    }), actions);
    button("Import browser workspace", control => action(control, async () => {
      let stored; try { stored = localStorage.getItem("siyab-workspace-v1"); } catch (error) { throw new Error("Browser storage is unavailable. Import a saved JSON backup instead."); }
      if (!stored) throw new Error("No browser workspace is saved on this device.");
      await importWorkspace(importValue(JSON.parse(stored)), base);
    }), actions, !writable());
    const label = node("label", "Import JSON backup"), input = node("input"); input.type = "file"; input.accept = "application/json,.json"; input.disabled = !writable();
    input.addEventListener("change", async () => {
      const file = input.files[0]; if (!file) return;
      const epoch = state.epoch;
      let timer;
      try {
        if (file.size > 8388608) throw new Error("Choose a shared workspace JSON backup smaller than 8 MiB.");
        const text = await Promise.race([file.text(), new Promise((resolve, reject) => { timer = setTimeout(() => reject(new Error("File read timed out. Choose the file again.")), 12000); })]);
        if (epoch !== state.epoch) throw new StaleRequest();
        await importWorkspace(importValue(JSON.parse(text)), base);
      } catch (error) { report(error); } finally { clearTimeout(timer); input.value = ""; }
    }); label.append(input); actions.append(label); details.append(actions); parent.append(details);
  }
  async function importWorkspace(value, base) {
    const epoch = state.epoch;
    if (!writable()) throw new Error("Editor or owner access is required to import.");
    if (!confirm(`Import ${value.cases.length} investigations and ${value.watchlist.length} watched accounts into ${state.team.name}? Existing notes and this device's browser data will be kept.`)) return;
    const batches = chunks(value), totals = {created_cases: 0, added_watches: 0, skipped_cases: 0};
    let completed = 0;
    try {
      for (const batch of batches) {
        const result = await request(base + "/import", batch, epoch);
        for (const field of Object.keys(totals)) {
          if (!Number.isInteger(result[field]) || result[field] < 0) throw new Error("Import counts could not be read. Refresh before retrying.");
          totals[field] += result[field];
        }
        completed++; status(`Imported batch ${completed} of ${batches.length}.`);
      }
      await renderPanel(); status(`${totals.created_cases} investigations and ${totals.added_watches} watches imported; ${totals.skipped_cases} duplicate investigations skipped.`);
    } catch (error) {
      if (!(error instanceof StaleRequest)) error.message += ` ${completed} of ${batches.length} batches completed. Refresh and retry the same backup; exact duplicates are skipped.`;
      throw error;
    }
  }
  function renderWatches(parent, rows, monitoring, base) {
    parent.append(node("h3", "Team watchlist"));
    if (writable()) {
      const add = form(parent, async values => { await request(base + "/watches", {account: values.get("account").trim()}); await renderPanel(); status("Account added to the team watchlist."); });
      field(add, "NEAR mainnet account", "account", {required: true, maxLength: 64, placeholder: "account.near", autocomplete: "off"}); submit(add, "Watch account");
    }
    const list = node("ul", undefined, "cloud-watchlist");
    for (const row of rows) {
      const item = node("li"); const link = node("a", row.account); link.href = "/investigate.html?account=" + encodeURIComponent(row.account); item.append(link);
      button("Remove", control => action(control, async () => { await request(base + "/watches/remove", {account: row.account}); await renderPanel(); status("Team watch removed."); }), item, !writable()); list.append(item);
    }
    if (!rows.length) list.append(node("li", "No watched accounts in this team.")); parent.append(list);
    const health = node("div", undefined, "cloud-monitor"); health.append(node("h4", "Retained-feed monitoring"));
    const running = monitoring.running === true;
    health.append(node("p", monitoring.configured === false || !monitoring.initialized && monitoring.configured !== true ? "Retained-feed monitoring is not configured on this copy." : running ? "Retained-feed monitoring is running. Matching feed alerts are saved while your browser is closed." : "Retained-feed monitoring is not currently reporting a running worker. Previously saved alerts remain available."));
    if (monitoring.heartbeat_ms) health.append(node("p", "Last worker update: " + date(monitoring.heartbeat_ms), "source-line"));
    if (Number.isInteger(monitoring.indexed) && Number.isInteger(monitoring.pending)) health.append(node("p", `${monitoring.indexed} evidence files indexed · ${monitoring.pending} pending`, "source-line"));
    if (Number.isInteger(monitoring.retry_pending) && monitoring.retry_pending > 0) health.append(node("p", `${monitoring.retry_pending} evidence files are waiting for a retry. Coverage is incomplete.`, "source-line"));
    if (monitoring.last_error) health.append(node("p", "Worker status: " + monitoring.last_error, "cloud-error"));
    if (typeof monitoring.coverage === "string") health.append(node("p", monitoring.coverage, "source-line"));
    else health.append(node("p", "Coverage follows the indexed source windows. An empty inbox does not establish absence of activity or risk.", "source-line"));
    if (monitoring.external_delivery_configured !== true) health.append(node("p", "Alerts are retained in this team's inbox. External notification delivery is not configured.", "source-line"));
    health.append(node("h4", "Ordinary-transfer polling"));
    const polling = monitoring.account_polling;
    if (!polling || typeof polling !== "object") health.append(node("p", "Ordinary-transfer polling status is unavailable.", "source-line"));
    else {
      health.append(node("p", polling.running === true ? "Ordinary-transfer polling is running. Observed transfers for watched accounts are retained in the team inbox." : "Ordinary-transfer polling is not currently reporting a running worker."));
      if (polling.heartbeat_ms) health.append(node("p", "Last polling worker update: " + date(polling.heartbeat_ms), "source-line"));
      if (polling.last_success_ms) health.append(node("p", "Last successful account poll: " + date(polling.last_success_ms), "source-line"));
      if (Number.isInteger(polling.monitored_accounts) && Number.isInteger(polling.initialized_accounts)) health.append(node("p", `${polling.initialized_accounts} of ${polling.monitored_accounts} watched accounts initialized`, "source-line"));
      for (const [key, label] of [["coverage_gap_accounts", "accounts with recorded coverage gaps"], ["retry_pending", "accounts waiting for retry"], ["backfill_pending", "account history backfills pending"], ["pending_events", "observed events awaiting inbox delivery"]]) {
        if (Number.isInteger(polling[key])) health.append(node("p", polling[key] + " " + label, "source-line"));
      }
      if (polling.oldest_due_ms) health.append(node("p", "Oldest queued account poll: " + date(polling.oldest_due_ms), "source-line"));
      if (polling.last_error) health.append(node("p", "Polling worker status: " + polling.last_error, "cloud-error"));
      if (typeof polling.coverage === "string") health.append(node("p", polling.coverage, "source-line"));
    }
    parent.append(health);
  }
  function renderMembers(parent, members, base) {
    parent.append(node("h3", "Team members"));
    const list = node("ul", undefined, "cloud-members");
    for (const member of members) {
      const row = node("li"); row.append(node("strong", member.username));
      if (state.team.role === "owner") {
        const label = node("label", "Role for " + member.username), select = node("select");
        for (const role of ["owner", "editor", "viewer"]) { const option = node("option", role); option.value = role; select.append(option); }
        select.value = member.role; label.append(select); row.append(label);
        button("Save role", control => action(control, async () => { await request(base + "/members/" + member.id + "/role", {role: select.value}); await loadSession(); status("Team role updated."); }), row);
        button("Remove", control => action(control, async () => {
          if (!confirm("Remove " + member.username + " from this team?")) return;
          await request(base + "/members/" + member.id + "/remove", {}); await loadSession(); status("Team member removed.");
        }), row);
      } else row.append(node("span", member.role, "source-line"));
      list.append(row);
    }
    parent.append(list);
    if (state.team.role === "owner") {
      const invite = form(parent, async values => {
        const result = await request(base + "/invites", {username: values.get("username"), role: values.get("role")});
        code.replaceChildren(node("p", `Invitation for ${result.username}, ${result.role}. Expires ${date(result.expires_at)}. Share this code privately; only that signed-in username can use it.`), node("pre", result.token));
      });
      field(invite, "Invite username", "username", {required: true, minLength: 3, maxLength: 40, autocomplete: "off"});
      const role = node("label", "Access"), select = node("select"); select.name = "role";
      for (const value of ["viewer", "editor"]) { const option = node("option", value); option.value = value; select.append(option); }
      role.append(select); invite.append(role); submit(invite, "Create invitation code");
      const code = node("div", undefined, "cloud-invitation"); code.setAttribute("role", "status"); parent.append(code);
    }
    renderTeamForms(parent);
  }
  function renderTeamForms(parent) {
    const details = node("details", undefined, "cloud-portability"); details.append(node("summary", state.team ? "Create or join another team" : "Create or join a team"));
    const create = form(details, async values => { const team = await request("/teams", {name: values.get("name")}); state.team = team; await loadSession(); status("Team workspace created."); });
    field(create, "Team name", "name", {required: true, maxLength: 120}); submit(create, "Create team");
    const join = form(details, async values => { const team = await request("/teams/join", {invite_token: values.get("invite_token").trim()}); state.team = team; await loadSession(); status("Joined team workspace."); });
    field(join, "Invitation code", "invite_token", {required: true, maxLength: 128, autocomplete: "off"}); submit(join, "Join team"); parent.append(details);
  }
  function renderHistory(parent, data, base) {
    const inbox = state.panel === "inbox"; parent.append(node("h3", inbox ? "Team alerts" : "Audit history"));
    const list = node("ol", undefined, "cloud-history");
    for (const item of data.items) {
      const row = node("li");
      if (inbox) {
        row.append(node("strong", item.event?.signal || "Observed event"), node("p", item.event?.detail || "Open the evidence to inspect this event."));
        const link = node("a", item.account || "Watched account"); link.href = "/investigate.html?account=" + encodeURIComponent(item.account); row.append(link);
        row.append(node("p", (item.candidate ? "Candidate signal; not a confirmed risk verdict. " : "Observed signal. ") + "Indexed " + date(item.ingested_ms), "source-line"));
        if (/^[a-f0-9]{64}$/.test(item.event_id)) { const evidence = node("a", "Open evidence"); evidence.href = item.event?.signal === "account_transfer_observed" ? "/live/account-events/" + item.event_id + ".json" : "/evidence/" + item.event_id; row.append(evidence); }
        button(item.acknowledged_ms ? "Acknowledged" : "Acknowledge", control => action(control, async () => { await request(base + "/inbox/" + item.id + "/ack", {}); await renderPanel(); }), row, !writable() || Boolean(item.acknowledged_ms));
      } else row.append(node("strong", item.action), node("p", item.actor + " · " + date(item.created_ms), "source-line"), node("p", "Resource " + item.resource_id, "saved-path"));
      list.append(row);
    }
    if (!data.items.length) parent.append(node("p", inbox ? "No alerts in this response. This does not establish that a watched account had no activity." : "No audit entries in this response.", "cloud-empty"));
    parent.append(list); const actions = node("div", undefined, "workspace-actions");
    if (inbox) button("Previous alerts", () => { state.before = data.next_before; state.after = 0; renderPanel(); }, actions, data.has_older !== true);
    button(inbox ? "Check newer alerts" : "Next audit entries", () => { state.after = data.next_after; state.before = null; renderPanel(); }, actions, !data.items.length);
    button(inbox ? "Latest alerts" : "First audit entries", () => { state.after = 0; state.before = null; renderPanel(); }, actions); parent.append(actions);
    if (!inbox) parent.append(node("p", "Application audit entries record team changes. They are not tamper-proof against server administrators.", "source-line"));
  }
  function renderAccount(parent) {
    parent.append(node("h3", "Account security"));
    const change = form(parent, async (values, element) => {
      const result = await request("/auth/password", {current_password: values.get("current_password"), new_password: values.get("new_password")});
      element.reset(); recovery(result.recovery_code, result.user.username, result.user.id); await loadSession(); status("Password changed. Other sessions and the previous recovery code were revoked.");
    });
    field(change, "Current password", "current_password", {type: "password", required: true, autocomplete: "current-password", maxLength: 128});
    field(change, "New password", "new_password", {type: "password", required: true, minLength: 12, maxLength: 128, autocomplete: "new-password"});
    submit(change, "Change password");
    parent.append(node("p", "A new recovery code is shown after a password change. Keep it privately; email recovery is not configured.", "source-line"));
  }
  let destination = null, savingShared = false, saveRevision = 0;
  function updateDestination() {
    const saveForm = document.getElementById("case-form"); if (!saveForm) return;
    if (!destination) {
      const label = node("label", "Save to"); destination = node("select"); destination.id = "case-destination"; label.append(destination); saveForm.prepend(label);
      saveForm.addEventListener("submit", async event => {
        if (destination.value === "browser") return;
        event.preventDefault(); event.stopImmediatePropagation();
        if (savingShared) return;
        const feedback = document.getElementById("case-error");
        const data = {title: document.getElementById("case-title").value.trim(), notes: document.getElementById("case-notes").value, url: document.getElementById("case-source").textContent};
        const team = state.teams.find(row => row.id === destination.value);
        if (!state.session || !team || team.role === "viewer" || !caseValue(data)) { feedback.textContent = "Sign in with editing access and choose a valid Siyab investigation."; return; }
        const controls = [...saveForm.querySelectorAll("button,input,textarea,select")], disabled = controls.map(control => control.disabled);
        const revision = saveRevision; savingShared = true; controls.forEach(control => control.disabled = true);
        try { await request("/teams/" + team.id + "/cases", data); if (revision === saveRevision) document.getElementById("save-investigation").close(); status("Investigation saved to " + team.name + "."); if (shell && state.panel === "cases" && state.team?.id === team.id) await renderPanel(); }
        catch (error) { if (!(error instanceof StaleRequest)) feedback.textContent = error.message; }
        finally { savingShared = false; controls.forEach((control, index) => control.disabled = disabled[index]); }
      }, true);
      document.getElementById("save-investigation").addEventListener("cancel", event => { if (savingShared) event.preventDefault(); });
      document.getElementById("save-investigation").addEventListener("close", () => { destination.value = "browser"; saveRevision++; });
    }
    destination.replaceChildren(); const local = node("option", "This browser"); local.value = "browser"; destination.append(local);
    for (const team of state.teams.filter(team => team.role !== "viewer")) { const option = node("option", team.name + " (shared)"); option.value = team.id; destination.append(option); }
    destination.value = "browser";
  }
  function addGlobalWatches() {
    document.querySelectorAll("[data-cloud-watch]").forEach(control => control.remove());
    if (!state.session || !writable()) return;
    for (const local of document.querySelectorAll("[data-watch-account]")) {
      if (!accountValue(local.dataset.watchAccount)) continue;
      const control = button("Watch in " + state.team.name, button => action(button, async () => { await request("/teams/" + state.team.id + "/watches", {account: local.dataset.watchAccount}); status("Account added to the team watchlist."); }));
      control.dataset.cloudWatch = ""; local.after(control);
    }
  }
  window.addEventListener("pagehide", () => { scrubDialogs(); invalidate(); });
  window.addEventListener("pageshow", event => { if (event.persisted) loadSession().catch(report); });
  loadSession().catch(report);
})();
