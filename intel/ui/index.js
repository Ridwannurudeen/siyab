(() => {
const labelForm = document.getElementById("account-form");
const accountInput = document.getElementById("account-input");
const amountInput = document.getElementById("account-amount");
const labelsPanel = document.getElementById("account-labels");
let searchIndex;
const liveUnavailable = "Live checks are not available on this copy";
const requestTokens = new WeakMap();
const beginRequest = panel => {
  const token = (requestTokens.get(panel) || 0) + 1;
  requestTokens.set(panel, token);
  return token;
};
const isCurrentRequest = (panel, token) => requestTokens.get(panel) === token;
const tokenInput = document.getElementById("tool-token");
const updateAmountLabel = () => {
  document.getElementById("tool-amount-label").textContent = tokenInput.value.trim() === "near"
    ? "Amount in NEAR" : "Amount in base units";
};
const paymentAmount = (value, token) => {
  const amount = value.trim();
  let units;
  if (token === "near") {
    const match = /^(0|[1-9][0-9]{0,14})(?:\.([0-9]{1,24}))?$/.exec(amount);
    if (!match) throw new Error("Enter a NEAR amount with up to 24 decimal places, without a sign or exponent.");
    units = BigInt(match[1] + (match[2] || "").padEnd(24, "0"));
  } else {
    if (!/^[1-9][0-9]{0,38}$/.test(amount)) throw new Error("Enter the token amount as a positive integer in base units.");
    units = BigInt(amount);
  }
  if (units < 1n || units > (1n << 128n) - 1n) {
    throw new Error("The amount must be between 1 and 340282366920938463463374607431768211455 base units.");
  }
  return units.toString();
};
const addText = (parent, tag, text, className = "") => {
  const element = document.createElement(tag);
  element.textContent = String(text);
  if (className) element.className = className;
  parent.append(element);
  return element;
};
const showUnavailable = (panel, token) => {
  if (!isCurrentRequest(panel, token)) return;
  panel.replaceChildren();
  addText(panel, "p", liveUnavailable, "empty-state");
};
const readJson = async (path, panel, token, signal) => {
  try {
    const response = await fetch(path, {headers: {Accept: "application/json"}, signal});
    if (!isCurrentRequest(panel, token)) return null;
    const contentType = (response.headers.get("content-type") || "").toLowerCase();
    if (response.status === 404 || !contentType.includes("application/json")) {
      showUnavailable(panel, token);
      return null;
    }
    const data = await response.json();
    if (!isCurrentRequest(panel, token)) return null;
    if (!data || typeof data !== "object") {
      showUnavailable(panel, token);
      return null;
    }
    if (!response.ok) {
      panel.replaceChildren();
      addText(panel, "p", data.message || "The live request did not return a result.", "empty-state");
      return null;
    }
    return data;
  } catch (error) {
    showUnavailable(panel, token);
    return null;
  }
};
const renderSource = (parent, entry) => {
  const line = document.createElement("p");
  line.className = "source-line";
  const source = entry.source;
  if (source.url && source.url.startsWith("https://")) {
    const link = document.createElement("a");
    link.href = source.url;
    link.textContent = source.url;
    line.append(link);
  } else if (source.method && source.rpc) {
    line.textContent = `${entry.provenance} source: ${source.method} at block ${source.block} via ${source.rpc}`;
  } else if (source.rule_id) {
    line.textContent = `${entry.provenance} source: rule ${source.rule_id}, ${source.inputs.length} input ${source.inputs.length === 1 ? "assertion" : "assertions"}`;
  } else {
    line.textContent = `${entry.provenance} source recorded at ${entry.observed_at}`;
  }
  parent.append(line);
};
const renderLabels = () => {
  if (!searchIndex) return;
  const subject = accountInput.value.trim();
  labelsPanel.replaceChildren();
  if (!subject) return;
  const entry = searchIndex.accounts.find(row => row.subject.toLowerCase() === subject.toLowerCase());
  if (!entry) {
    addText(labelsPanel, "p", "Not in the label store.", "empty-state");
    return;
  }
  const link = addText(labelsPanel, "a", `Open account page for ${entry.subject}`);
  link.href = `/${entry.url}`;
  for (const assertion of entry.label_sources) {
    const item = document.createElement("article");
    item.className = "account-label";
    addText(item, "h3", assertion.label);
    addText(item, "p", `${assertion.chain} · ${assertion.provenance}`, "muted");
    renderSource(item, assertion);
    labelsPanel.append(item);
  }
};
const renderVerdict = (panel, data, showOfac = false, token) => {
  const ofac = showOfac && Array.isArray(data.checks)
    ? data.checks.find(item => item && typeof item.name === "string" && item.name.toLowerCase().includes("ofac"))
    : null;
  const valid = ["ALLOW", "ASK", "DENY"].includes(data.outcome)
    && data.verdict === null && typeof data.action === "string" && typeof data.evidence_hash === "string"
    && "freshness" in data && data.signing === "never" && Number.isFinite(data.latency_ms)
    && Array.isArray(data.reasons) && data.reasons.every(reason => typeof reason === "string")
    && Array.isArray(data.checks) && data.checks.length > 0
    && data.checks.every(check => check && typeof check.name === "string"
      && typeof check.status === "string" && typeof check.detail === "string")
    && data.checks.every(check => ["pass", "signal", "unknown"].includes(check.status.trim().toLowerCase()))
    && (data.outcome !== "ALLOW" || data.checks.every(check => check.status.trim().toLowerCase() === "pass"))
    && (!showOfac || ofac);
  if (!valid) {
    showUnavailable(panel, token);
    return false;
  }
  const isGreen = data.outcome === "ALLOW" && data.action === "sign"
    && data.evidence_hash.trim().length > 0 && data.freshness !== null
    && data.checks.every(check => check.status.trim().toLowerCase() === "pass");
  const displayOutcome = data.outcome === "ALLOW" && !isGreen ? "ASK" : data.outcome;
  panel.replaceChildren();
  addText(panel, "p", displayOutcome === "ASK" ? "WARN" : displayOutcome, `verdict ${isGreen ? "allow" : displayOutcome.toLowerCase()}`);
  if (typeof data.action === "string") addText(panel, "p", `Action: ${data.action === "ask" ? "warn" : data.action}`);
  if (data.outcome === "ALLOW" && data.action === "ask") {
    addText(panel, "p", "A person must decide before this payment can proceed.", "panel-status");
  }
  if (showOfac) {
    addText(panel, "p", `OFAC sanctions: ${ofac.status}. ${ofac.detail}`, "panel-status");
  }
  addText(panel, "h4", "Reasons");
  const reasons = document.createElement("ul");
  for (const reason of data.reasons) addText(reasons, "li", reason);
  panel.append(reasons);
  addText(panel, "h4", "Checks");
  const checks = document.createElement("ul");
  checks.className = "check-list";
  for (const check of data.checks) {
    const item = document.createElement("li");
    addText(item, "span", `${check.name}: ${check.status}`, "check-status");
    addText(item, "p", check.detail);
    checks.append(item);
  }
  panel.append(checks);
  if (typeof data.evidence_hash === "string") addText(panel, "p", `Evidence hash: ${data.evidence_hash}`, "mono");
  addText(panel, "p", `Freshness: ${JSON.stringify(data.freshness)}`, "panel-status");
  addText(panel, "p", `Signing: ${data.signing}`, "panel-status");
  addText(panel, "p", `Read time: ${data.latency_ms} ms`, "panel-status");
  if (typeof data.evidence === "string" && /^[a-f0-9]{64}$/.test(data.evidence_hash)) {
    const receipt = JSON.stringify(data);
    const download = addText(panel, "button", "Download evidence");
    download.type = "button";
    download.addEventListener("click", () => {
      const url = URL.createObjectURL(new Blob([receipt], {type: "application/json"}));
      const link = document.createElement("a");
      link.href = url;
      link.download = `siyab-check-${data.evidence_hash.slice(0, 12)}.json`;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }
  return true;
};
window.SiyabChecks = {beginRequest, readJson, renderVerdict};
if (!labelForm) return;
tokenInput.addEventListener("input", updateAmountLabel);
updateAmountLabel();
const askQuestions = [["why_verdict", "Why did the checks reach this outcome?"], ["what_unknown", "What is unknown, and why?"],
  ["what_changes", "What in the evidence would have to change for the outcome to change?"],
  ["explain_labels", "What do the labels in the evidence say about these accounts?"]];
const askReasons = {
  disabled: "Ask Siyab is turned off on this site.",
  misconfigured: "Ask Siyab is not configured correctly on this site.",
  sign_in_required: "Sign in to ask Siyab to explain this outcome.",
  evidence_empty: "This check returned no evidence to explain.",
  evidence_too_large: "The evidence for this check is too large to explain.",
  question_refused: "Siyab does not answer this question.",
  key_unavailable: "The explanation service is not available right now.",
  sdk_unavailable: "The explanation service is not available right now.",
  busy: "Siyab is answering other questions. Try again in a moment.",
  spend_cap: "The daily limit for explanations has been reached.",
  user_quota: "You have reached your daily limit for explanations.",
  team_quota: "This team has reached its daily limit for explanations.",
  timeout: "The explanation took too long and was stopped.",
  rate_limited: "Too many requests were made. Try again later.",
  provider_error: "The explanation service returned an error.",
  connection_error: "The explanation service could not be reached.",
  refusal: "The explanation service declined to answer.",
  max_tokens: "The explanation was too long to complete.",
  invalid_output: "The explanation did not pass validation, so it is withheld.",
  storage_unavailable: "Ask Siyab storage is not available right now.",
  authentication_required: "Your session has ended. Sign in again to ask Siyab.",
  csrf_denied: "Refresh the page and try again.",
  forbidden: "You are not a member of this team."
};
const askReasonText = (reason, retryAfter) => {
  const text = Object.hasOwn(askReasons, reason) ? askReasons[reason] : `Reason code: ${reason}.`;
  return Number.isInteger(retryAfter) && retryAfter > 0
    ? `${text.replace(/ Try again (later|in a moment)\.$/, "")} Try again in ${retryAfter} ${retryAfter === 1 ? "second" : "seconds"}.`
    : text;
};
const askLinkLine = (parent, text) => {
  const line = addText(parent, "p", "", "panel-status");
  const link = addText(line, "a", text);
  link.href = "/workspace.html";
};
const renderAskAnswer = (answer, data) => {
  answer.replaceChildren();
  addText(answer, "p", "The rules decided this outcome. Ask Siyab only explains it.", "panel-status");
  if (data.status === "explained" && Array.isArray(data.statements)) {
    for (const statement of data.statements) {
      if (!statement || typeof statement.text !== "string") continue;
      const item = document.createElement("div");
      item.className = "ask-statement";
      addText(item, "p", statement.text);
      const chip = statement.evidence_chip;
      if (chip && typeof chip.item_id === "string" && chip.fields && typeof chip.fields === "object") {
        const fields = Object.entries(chip.fields)
          .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`);
        addText(item, "p", [chip.item_id, ...fields].join(" · "), "ask-chip");
      }
      answer.append(item);
    }
  } else {
    addText(answer, "p", typeof data.message === "string" ? data.message : "No explanation is available.");
    addText(answer, "p", askReasonText(typeof data.reason === "string" ? data.reason : "unknown", data.retry_after_s), "panel-status");
  }
  if (typeof data.unknown_notice === "string") {
    addText(answer, "p", data.unknown_notice, "panel-status");
    if (Array.isArray(data.unknown_checks) && data.unknown_checks.length) {
      addText(answer, "p", `Unresolved checks: ${data.unknown_checks.map(String).join(", ")}`, "ask-chip");
    }
  }
  if (Array.isArray(data.not_in_evidence)) {
    for (const sentence of data.not_in_evidence) if (typeof sentence === "string") addText(answer, "p", sentence, "panel-status");
  }
};
const askSiyab = async (answer, session, team, transfer, questionId, buttons, clicked, outcome) => {
  const token = beginRequest(answer);
  for (const button of buttons) button.disabled = true;
  answer.replaceChildren();
  addText(answer, "p", "Asking Siyab.", "panel-status");
  let data = null;
  let status = 0;
  let retryAfter = null;
  try {
    const response = await fetch(`/workspace-api/teams/${encodeURIComponent(team)}/ask`, {method: "POST",
      credentials: "same-origin", cache: "no-store",
      headers: {Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token},
      body: JSON.stringify({...transfer, question_id: questionId})});
    status = response.status;
    retryAfter = Number.parseInt(response.headers.get("retry-after") || "", 10);
    if ((response.headers.get("content-type") || "").toLowerCase().includes("application/json")) data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!isCurrentRequest(answer, token)) return;
  for (const button of buttons) button.disabled = false;
  if (!document.activeElement || document.activeElement === document.body) clicked.focus();
  if (status === 200 && data && typeof data === "object" && ["explained", "unavailable"].includes(data.status)) {
    if (typeof data.outcome === "string" && data.outcome !== outcome) {
      answer.replaceChildren();
      addText(answer, "p", "The outcome changed since this check. Run the check again.", "empty-state");
      return;
    }
    renderAskAnswer(answer, data);
    return;
  }
  answer.replaceChildren();
  const code = data && typeof data.error === "string" ? data.error
    : status === 429 ? "rate_limited" : status === 401 ? "authentication_required" : "";
  if (code === "authentication_required") {
    askLinkLine(answer, askReasons[code]);
    return;
  }
  const message = Object.hasOwn(askReasons, code) || !data || typeof data.message !== "string"
    ? (code ? askReasonText(code, retryAfter) : "Ask Siyab did not return an answer.") : data.message;
  addText(answer, "p", message, "empty-state");
};
const renderAsk = async (panel, verdictPath, outcome) => {
  const query = new URLSearchParams(verdictPath.split("?")[1]);
  const transfer = {receiver: query.get("receiver"), amount: query.get("amount"), token: query.get("token")};
  const section = document.createElement("section");
  section.className = "ask-panel";
  addText(section, "h4", "Ask Siyab");
  panel.append(section);
  let session = null;
  try {
    const response = await fetch("/workspace-api/session", {credentials: "same-origin", cache: "no-store",
      headers: {Accept: "application/json"}});
    if (response.ok && (response.headers.get("content-type") || "").toLowerCase().includes("application/json")) {
      session = await response.json();
    }
  } catch (error) {
    session = null;
  }
  if (!session || typeof session !== "object" || typeof session.authenticated !== "boolean" || !Array.isArray(session.teams)) {
    addText(section, "p", "Ask Siyab is not available on this copy.", "panel-status");
    return;
  }
  if (!session.authenticated || typeof session.csrf_token !== "string") {
    askLinkLine(section, "Sign in to ask Siyab to explain this outcome.");
    return;
  }
  const teams = session.teams.filter(team => team && typeof team.id === "string" && typeof team.name === "string");
  if (!teams.length) {
    askLinkLine(section, "Create a team to use Ask Siyab.");
    return;
  }
  let teamId = teams[0].id;
  if (teams.length > 1) {
    const label = addText(section, "label", "Team", "ask-team");
    const select = document.createElement("select");
    for (const team of teams) {
      const option = addText(select, "option", team.name);
      option.value = team.id;
    }
    select.addEventListener("change", () => { teamId = select.value; });
    label.append(select);
  }
  const actions = document.createElement("div");
  actions.className = "ask-questions";
  section.append(actions);
  const answer = document.createElement("div");
  answer.className = "ask-answer";
  answer.setAttribute("aria-live", "polite");
  const buttons = askQuestions.map(([id, question]) => {
    const button = addText(actions, "button", question);
    button.type = "button";
    button.addEventListener("click", () => askSiyab(answer, session, teamId, transfer, id, buttons, button, outcome));
    return button;
  });
  section.append(answer);
};
const renderScan = (panel, data, token) => {
  const listFields = ["full_access_keys", "empty_method_names", "dead_keys", "unlimited_allowance", "findings"];
  if (!Number.isInteger(data.access_keys) || !listFields.every(key => Array.isArray(data[key]))
      || !data.findings.every(item => typeof item === "string")
      || !listFields.slice(0, 4).every(key => data[key].every(item => typeof item === "string"))
      || !(typeof data.read_at_block === "number" || typeof data.read_at_block === "string")) {
    showUnavailable(panel, token);
    return;
  }
  panel.replaceChildren();
  addText(panel, "p", `${data.access_keys} access ${data.access_keys === 1 ? "key" : "keys"} read at block ${data.read_at_block}.`);
  for (const [key, label] of [["full_access_keys", "Full access keys"], ["empty_method_names", "Keys with an empty method list"],
    ["dead_keys", "Keys with a zero allowance"], ["unlimited_allowance", "Keys without an allowance limit"]]) {
    const section = document.createElement("section");
    addText(section, "h4", `${label}: ${data[key].length}`);
    if (data[key].length) {
      const list = document.createElement("ul");
      for (const value of data[key]) addText(list, "li", value, "mono");
      section.append(list);
    }
    panel.append(section);
  }
  addText(panel, "h4", "Read findings");
  if (data.findings.length) {
    const list = document.createElement("ul");
    for (const finding of data.findings) addText(list, "li", finding);
    panel.append(list);
  } else {
    addText(panel, "p", "No key findings were returned in this read. This is not a payment verdict.", "panel-status");
  }
};
const runAccountChecks = async () => {
  const subject = accountInput.value.trim();
  if (!subject) return;
  const verdictPanel = document.getElementById("account-verdict");
  const scanPanel = document.getElementById("account-scan");
  const verdictToken = beginRequest(verdictPanel);
  const scanToken = beginRequest(scanPanel);
  verdictPanel.replaceChildren();
  scanPanel.replaceChildren();
  const amount = amountInput.value.trim();
  let baseUnits;
  try {
    baseUnits = paymentAmount(amount, "near");
  } catch (error) {
    addText(verdictPanel, "p", error.message, "empty-state");
    addText(scanPanel, "p", "Correct the payment amount to run the checks.", "panel-status");
    return;
  }
  addText(verdictPanel, "p", "Reading the live verdict route.", "panel-status");
  addText(scanPanel, "p", "Reading the live scan route.", "panel-status");
  const verdictUrl = `/live/verdict?${new URLSearchParams({receiver: subject, amount: baseUnits, token: "near"})}`;
  const [verdict, scan] = await Promise.all([
    readJson(verdictUrl, verdictPanel, verdictToken),
    readJson(`/live/scan/${encodeURIComponent(subject)}`, scanPanel, scanToken)
  ]);
  if (verdict && isCurrentRequest(verdictPanel, verdictToken)) {
    renderVerdict(verdictPanel, verdict, false, verdictToken);
    addText(verdictPanel, "p", `Amount checked: ${amount} NEAR to ${subject}.`, "panel-status");
  }
  if (scan && isCurrentRequest(scanPanel, scanToken)) renderScan(scanPanel, scan, scanToken);
};
labelForm.addEventListener("submit", event => {
  event.preventDefault();
  renderLabels();
  runAccountChecks();
});
labelForm.addEventListener("invalid", event => {
  if (!event.target.validity.valueMissing) return;
  for (const id of ["account-verdict", "account-scan"]) {
    const panel = document.getElementById(id);
    beginRequest(panel);
    panel.replaceChildren();
    addText(panel, "p", "Complete the required fields to run the checks.", "empty-state");
  }
}, true);
accountInput.addEventListener("input", renderLabels);
fetch("/search.json", {headers: {Accept: "application/json"}}).then(response => {
  if (!response.ok) throw new Error("search index unavailable");
  return response.json();
}).then(data => {
  if (!data || !Array.isArray(data.accounts) || !data.accounts.every(row => row
      && typeof row.url === "string" && /^accounts\/[A-Za-z0-9._-]+\.html$/.test(row.url))) throw new Error("search index unreadable");
  searchIndex = data;
  renderLabels();
}).catch(() => {
  labelsPanel.replaceChildren();
  addText(labelsPanel, "p", "Committed label search is unavailable.", "empty-state");
});

const tabs = Array.from(document.querySelectorAll("[role=tab]"));
for (const tab of tabs) tab.addEventListener("click", () => {
  for (const other of tabs) {
    const active = other === tab;
    other.setAttribute("aria-selected", String(active));
    document.getElementById(other.getAttribute("aria-controls")).hidden = !active;
  }
});
const addVerdictForm = (formId, panelId, route, showOfac = false, ask = false) => {
  document.getElementById(formId).addEventListener("invalid", event => {
    if (!event.target.validity.valueMissing) return;
    const panel = document.getElementById(panelId);
    beginRequest(panel);
    panel.replaceChildren();
    addText(panel, "p", "Complete the required fields to run the check.", "empty-state");
  }, true);
  document.getElementById(formId).addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const panel = document.getElementById(panelId);
    const requestToken = beginRequest(panel);
    panel.replaceChildren();
    let path;
    try {
      path = route(values);
    } catch (error) {
      addText(panel, "p", error.message, "empty-state");
      return;
    }
    addText(panel, "p", "Reading the live route.", "panel-status");
    const data = await readJson(path, panel, requestToken);
    if (data && isCurrentRequest(panel, requestToken) && renderVerdict(panel, data, showOfac, requestToken) && ask) {
      renderAsk(panel, path, data.outcome);
    }
  });
};
addVerdictForm("tool-verdict-form", "tool-verdict-result", values => {
  const token = values.get("token").trim();
  const params = new URLSearchParams({receiver: values.get("receiver"), amount: paymentAmount(values.get("amount"), token),
    token});
  return `/live/verdict?${params}`;
}, false, true);
document.getElementById("tool-scan-form").addEventListener("submit", async event => {
  event.preventDefault();
  const subject = new FormData(event.currentTarget).get("account").trim();
  const panel = document.getElementById("tool-scan-result");
  const requestToken = beginRequest(panel);
  panel.replaceChildren();
  addText(panel, "p", "Reading the live scan route.", "panel-status");
  const data = await readJson(`/live/scan/${encodeURIComponent(subject)}`, panel, requestToken);
  if (data && isCurrentRequest(panel, requestToken)) renderScan(panel, data, requestToken);
});
addVerdictForm("tool-intent-form", "tool-intent-result", values => {
  const params = new URLSearchParams({destination_asset: values.get("destination_asset"),
    recipient: values.get("recipient"), amount: values.get("amount")});
  return `/live/intents?${params}`;
}, true);

const appendReport = (parent, report) => {
  const item = document.createElement("article");
  item.className = "report-card";
  addText(item, "h4", `Report ${report.id}: ${report.subject}`);
  addText(item, "p", `${report.kind} · ${report.outcome} · ${report.status}`);
  addText(item, "p", `Submitter: ${report.submitter}; moderator: ${report.moderator || "pending"}`);
  if (typeof report.evidence_url === "string" && report.evidence_url.startsWith("https://")) {
    const link = addText(item, "a", "Open cited evidence");
    link.href = report.evidence_url;
  }
  addText(item, "p", `Evidence SHA-256: ${report.evidence_sha256}`, "mono");
  parent.append(item);
};
const validReports = reports => Array.isArray(reports) && reports.every(report => report
  && (typeof report.id === "number" || typeof report.id === "string")
  && ["subject", "kind", "outcome", "status", "evidence_url", "evidence_sha256", "submitter"]
    .every(key => typeof report[key] === "string")
  && (typeof report.moderator === "string" || report.moderator === null));
const renderRecentReports = (panel, data, token) => {
  const reports = Array.isArray(data) ? data : data.reports;
  if (!validReports(reports)) {
    showUnavailable(panel, token);
    return;
  }
  panel.replaceChildren();
  if (!reports.length) addText(panel, "p", "No reports were returned by this read.", "panel-status");
  for (const report of reports) appendReport(panel, report);
};
const recentPanel = document.getElementById("recent-reports");
const recentToken = beginRequest(recentPanel);
readJson("/live/reports/recent", recentPanel, recentToken).then(data => {
  if (data && isCurrentRequest(recentPanel, recentToken)) renderRecentReports(recentPanel, data, recentToken);
});
document.getElementById("report-lookup-form").addEventListener("submit", async event => {
  event.preventDefault();
  const subject = new FormData(event.currentTarget).get("subject").trim();
  const panel = document.getElementById("report-lookup-result");
  const requestToken = beginRequest(panel);
  panel.replaceChildren();
  addText(panel, "p", "Reading the live reports route.", "panel-status");
  const data = await readJson(`/live/reports?${new URLSearchParams({subject})}`, panel, requestToken);
  if (!data || !isCurrentRequest(panel, requestToken)) return;
  const summary = data.subject_summary;
  const fields = ["accepted", "contested", "deny", "pending", "latest_accepted_id", "latest_deny_id"];
  const validSummary = summary && typeof summary === "object"
    && Number.isInteger(summary.accepted) && Number.isInteger(summary.contested)
    && typeof summary.deny === "boolean" && Number.isInteger(summary.pending)
    && (summary.latest_accepted_id === null || Number.isInteger(summary.latest_accepted_id))
    && (summary.latest_deny_id === null || Number.isInteger(summary.latest_deny_id));
  if (!validSummary
      || !validReports(data.reports)) {
    showUnavailable(panel, requestToken);
    return;
  }
  panel.replaceChildren();
  addText(panel, "h4", `Report summary for ${subject}`);
  const summaryGrid = document.createElement("div");
  summaryGrid.className = "summary-grid";
  for (const key of fields) {
    const item = document.createElement("div");
    item.className = "summary-item";
    addText(item, "strong", summary[key] === null ? "none" : summary[key]);
    addText(item, "span", key);
    summaryGrid.append(item);
  }
  panel.append(summaryGrid);
  for (const report of data.reports) appendReport(panel, report);
});

})();
