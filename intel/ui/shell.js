(() => {
  "use strict";
  const toggle = document.getElementById("theme-toggle");
  const themeLabel = document.createElement("span");
  themeLabel.className = "theme-toggle-label";
  toggle.replaceChildren(themeLabel);
  const labelTheme = () => {
    const light = document.documentElement.dataset.theme === "light";
    document.querySelectorAll('meta[name="theme-color"]').forEach(meta => { meta.content = light ? "#F5F7FA" : "#0A0D16"; });
    themeLabel.textContent = light ? "Use dark theme" : "Use light theme";
    toggle.setAttribute("aria-label", light ? "Switch to dark theme" : "Switch to light theme");
  };
  document.documentElement.dataset.theme = "dark";
  try {
    const saved = localStorage.getItem("siyab-theme");
    if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
  } catch (error) { toggle.title = "Theme choice applies to this page; browser storage is unavailable."; }
  labelTheme();
  toggle.addEventListener("click", () => {
    const light = document.documentElement.dataset.theme === "light";
    document.documentElement.dataset.theme = light ? "dark" : "light";
    labelTheme();
    try { localStorage.setItem("siyab-theme", document.documentElement.dataset.theme); }
    catch (error) { toggle.title = "Theme choice applies to this page; browser storage is unavailable."; }
  });
  const dialog = document.getElementById("global-search");
  const menu = document.getElementById("mobile-navigation");
  menu.addEventListener("click", event => {
    if (event.target.closest("a")) menu.open = false;
  });
  const input = document.getElementById("global-search-input");
  const results = document.getElementById("global-search-results");
  let entries = null;
  let failed = false;
  let opener = null;
  const render = () => {
    results.replaceChildren();
    const query = input.value.trim().toLowerCase();
    const matches = entries && query ? entries.filter(row => `${row.label} ${row.detail}`.toLowerCase().includes(query)) : [];
    const message = failed ? "Search is unavailable. Browse the account and entity collections below." : !entries ? "Loading recorded search index." : !query ? "Type an account or entity name." : !matches.length ? "No recorded match. This does not imply a favorable payment result." : `${matches.length} recorded matches; showing up to 30.`;
    const note = document.createElement("p");
    note.className = "search-empty"; note.textContent = message; results.append(note);
    if (query.length >= 2 && query.length <= 64 && /^[a-z0-9]+(?:[._-][a-z0-9]+)*$/.test(query)
        && query !== "testnet" && !query.endsWith(".testnet")) {
      const link = document.createElement("a"); link.className = "search-result"; link.href = "/investigate.html?account=" + encodeURIComponent(query);
      const text = document.createElement("span"); text.textContent = "Investigate " + query;
      const kind = document.createElement("span"); kind.className = "search-result-type"; kind.textContent = "Mainnet account";
      link.append(text, kind); results.append(link);
    }
    for (const row of matches.slice(0, 30)) {
      const link = document.createElement("a"); link.className = "search-result"; link.href = `/${row.url}`;
      const text = document.createElement("span"); text.textContent = row.label;
      const detail = document.createElement("small"); detail.textContent = row.detail; text.append(detail);
      const kind = document.createElement("span"); kind.className = "search-result-type"; kind.textContent = row.type;
      link.append(text, kind); results.append(link);
    }
  };
  const open = (query = "") => {
    if (typeof dialog.showModal !== "function") { location.href = "/index.html#search"; return; }
    if (!dialog.open) opener = document.activeElement;
    input.value = query; if (!dialog.open) dialog.showModal(); input.focus(); render();
  };
  document.querySelectorAll("[data-open-search]").forEach(button => button.addEventListener("click", () => open()));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && menu.open && !dialog.open) {
      event.preventDefault(); menu.open = false; menu.querySelector("summary").focus();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); open(); }
    if (event.key === "ArrowDown" && dialog.open && document.activeElement === input) {
      const first = results.querySelector("a"); if (first) { event.preventDefault(); first.focus(); }
    }
  });
  dialog.addEventListener("close", () => { if (opener) opener.focus(); });
  dialog.addEventListener("keydown", event => {
    if (event.key === "Enter" && document.activeElement === input) {
      const first = results.querySelector("a");
      event.preventDefault(); if (first) first.click(); return;
    }
    if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
    const links = Array.from(results.querySelectorAll("a"));
    const index = links.indexOf(document.activeElement);
    if (index < 0 || !links.length) return;
    event.preventDefault();
    if (event.key === "ArrowUp" && index === 0) input.focus();
    else links[(index + (event.key === "ArrowDown" ? 1 : -1) + links.length) % links.length].focus();
  });
  input.addEventListener("input", render);
  const form = document.getElementById("search");
  if (form) form.addEventListener("submit", event => { event.preventDefault(); open(new FormData(form).get("q") || ""); });
  fetch("/search.json", {headers: {Accept: "application/json"}}).then(response => {
    if (!response.ok) throw new Error("Search unavailable"); return response.json();
  }).then(data => {
    if (!Array.isArray(data.accounts) || !Array.isArray(data.entities)) throw new Error("Search unreadable");
    const localPath = /^(accounts|entities)\/[A-Za-z0-9._-]+\.html$/;
    if (!data.accounts.every(row => row && typeof row.subject === "string" && Array.isArray(row.labels)
        && row.labels.every(label => typeof label === "string") && typeof row.url === "string" && localPath.test(row.url))
        || !data.entities.every(row => row && typeof row.id === "string" && typeof row.name === "string"
        && typeof row.url === "string" && localPath.test(row.url))) throw new Error("Search index paths unreadable");
    entries = [...data.accounts.map(row => ({label: row.subject, detail: row.labels.join(", "), url: row.url, type: "Account"})),
      ...data.entities.map(row => ({label: row.name, detail: row.id, url: row.url, type: "Entity"}))];
    render();
  }).catch(() => { failed = true; render(); });
  const query = new URLSearchParams(location.search).get("q");
  if (query) open(query);
})();
