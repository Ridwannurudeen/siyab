(function () {
  "use strict";
  for (const panel of document.querySelectorAll("[data-entity-table]")) {
    const toolbar = panel.querySelector(".entity-table-toolbar");
    const search = panel.querySelector("[data-table-search]");
    const chain = panel.querySelector("[data-table-chain]");
    const sort = panel.querySelector("[data-table-sort]");
    const status = panel.querySelector(".entity-table-status");
    const navigation = panel.querySelector(".entity-table-pagination");
    const retry = panel.querySelector("[data-table-retry]");
    let rows = null;
    let loading = null;
    let page = Number(panel.dataset.page);
    let revision = 0;
    toolbar.hidden = false;

    function validRow(row) {
      return row && /^[a-f0-9]{64}$/.test(row.id) && ["stated", "inferred"].includes(row.kind)
        && ["subject", "chain", "label", "search"].every(key => typeof row[key] === "string")
        && Array.isArray(row.cells) && row.cells.length === (row.kind === "inferred" ? 8 : 6)
        && row.cells.every(cell => Array.isArray(cell) && cell.every(part => part && typeof part.text === "string"
          && (part.quoted === undefined || typeof part.quoted === "boolean")
          && (part.url === undefined || /^\.\.\/accounts\/[A-Za-z0-9._-]+\.html$/.test(part.url))));
    }

    async function load() {
      if (rows) return;
      if (!loading) {
        loading = (async () => {
          const controller = new AbortController();
          const timeout = setTimeout(() => controller.abort(), 10000);
          try {
            const response = await fetch(panel.dataset.source, {signal: controller.signal, headers: {Accept: "application/json"}});
            if (!response.ok) throw new Error("Collection unavailable");
            const data = await response.json();
            if (!data || !Array.isArray(data.rows) || !data.rows.every(validRow)) throw new Error("Collection unreadable");
            rows = data.rows;
          } finally {
            clearTimeout(timeout);
            loading = null;
          }
        })();
      }
      await loading;
    }

    function rowElement(row, headers) {
      const tr = document.createElement("tr");
      tr.setAttribute("role", "row");
      tr.dataset.assertionId = row.id;
      row.cells.forEach((parts, index) => {
        const td = document.createElement("td");
        td.setAttribute("role", "cell");
        if (index === 0 || ["Rule", "Inputs"].includes(headers[index])) td.className = "mono";
        const label = document.createElement("span");
        label.className = "cell-label";
        label.setAttribute("aria-hidden", "true");
        label.textContent = headers[index];
        td.append(label);
        for (const part of parts) {
          if (part.text === "\n") { td.append(document.createElement("br")); continue; }
          let value = document.createTextNode(part.text);
          if (part.quoted) {
            const quote = document.createElement("q");
            quote.className = "data";
            quote.append(value);
            value = quote;
          }
          if (part.url) {
            const link = document.createElement("a");
            link.href = part.url;
            link.append(value);
            value = link;
          }
          td.append(value);
        }
        tr.append(td);
      });
      return tr;
    }

    function render() {
      const query = search.value.trim().toLocaleLowerCase();
      const matches = rows.filter(row => (!query || row.search.toLocaleLowerCase().includes(query))
        && (!chain.value || row.chain === chain.value));
      if (sort.value !== "recorded") {
        const field = sort.value === "subject-desc" ? "subject" : sort.value;
        const direction = sort.value === "subject-desc" ? -1 : 1;
        matches.sort((a, b) => direction * a[field].localeCompare(b[field], "en", {numeric: true}));
      }
      const pages = Math.max(1, Math.ceil(matches.length / 50));
      page = Math.max(1, Math.min(page, pages));
      const selected = matches.slice((page - 1) * 50, page * 50);
      for (const section of panel.querySelectorAll("[data-entity-kind]")) {
        const kind = section.dataset.entityKind;
        const headers = Array.from(section.querySelectorAll("th"), header => header.textContent);
        const visible = selected.filter(row => row.kind === kind);
        section.querySelector("tbody").replaceChildren(...visible.map(row => rowElement(row, headers)));
        section.querySelector("[data-table-section-count]").textContent =
          `${visible.length} of ${matches.filter(row => row.kind === kind).length} matching ${kind === "inferred" ? "hypotheses" : "source assertions"} on this page.`;
      }
      panel.querySelector("[data-table-empty]").hidden = matches.length !== 0;
      status.textContent = `Showing ${matches.length ? (page - 1) * 50 + 1 : 0} to ${Math.min(page * 50, matches.length)} of ${matches.length} matching assertions (${rows.length} recorded).`;
      retry.hidden = true;
      navigation.replaceChildren();
      for (const [text, offset] of [["Previous", -1], ["Next", 1]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = text;
        button.disabled = page + offset < 1 || page + offset > pages;
        button.addEventListener("click", () => {
          page += offset;
          render();
          const preferred = navigation.children[offset === -1 ? 0 : 2];
          const alternate = navigation.children[offset === -1 ? 2 : 0];
          (preferred.disabled ? alternate : preferred).focus();
        });
        navigation.append(button);
        if (offset === -1) {
          const position = document.createElement("span");
          position.textContent = `Page ${page} of ${pages}`;
          navigation.append(position);
        }
      }
    }

    async function refresh() {
      const current = ++revision;
      const restoreRetryFocus = document.activeElement === retry;
      page = 1;
      status.textContent = "Loading the recorded collection. Current rows remain available.";
      panel.setAttribute("aria-busy", "true");
      if (restoreRetryFocus) {
        status.setAttribute("tabindex", "-1");
        status.focus();
      }
      retry.hidden = true;
      try {
        await load();
        if (current === revision) {
          render();
          if (restoreRetryFocus && document.activeElement === status) search.focus();
        }
      } catch (error) {
        if (current === revision) {
          status.textContent = "Collection search is unavailable. Retry or use the page links to browse recorded assertions.";
          retry.hidden = false;
          if (restoreRetryFocus && document.activeElement === status) retry.focus();
        }
      } finally {
        if (current === revision) panel.setAttribute("aria-busy", "false");
      }
    }
    search.addEventListener("input", refresh);
    chain.addEventListener("change", refresh);
    sort.addEventListener("change", refresh);
    retry.addEventListener("click", refresh);
  }
})();
