(function () {
  "use strict";
  for (const panel of document.querySelectorAll("[data-entity-table]")) {
    const toolbar = panel.querySelector(".entity-table-toolbar");
    const search = panel.querySelector("[data-table-search]");
    const chain = panel.querySelector("[data-table-chain]");
    const sort = panel.querySelector("[data-table-sort]");
    const group = panel.querySelector("[data-table-group]");
    const groupIds = group ? new Set(Array.from(group.options, option => option.value).filter(Boolean)) : new Set();
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
        && ["subject", "chain", "label", "note", "search"].every(key => typeof row[key] === "string")
        && (!group || typeof row.source_group === "string" && groupIds.has(row.source_group))
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
            const maximum = 4 * 1024 * 1024, length = response.headers.get("Content-Length");
            if (length !== null && (!/^\d+$/.test(length) || Number(length) > maximum)) throw new Error("Collection exceeds the read bound");
            const reader = response.body.getReader(), decoder = new TextDecoder("utf-8", {fatal: true});
            let size = 0, text = "";
            try {
              for (;;) {
                const part = await reader.read();
                if (part.done) break;
                size += part.value.byteLength;
                if (size > maximum) throw new Error("Collection exceeds the read bound");
                text += decoder.decode(part.value, {stream: true});
              }
              text += decoder.decode();
            } finally { reader.releaseLock(); }
            const data = JSON.parse(text);
            if (!data || !Array.isArray(data.rows) || data.rows.length > 10000 || !data.rows.every(validRow)) throw new Error("Collection unreadable");
            rows = data.rows;
          } catch (error) {
            controller.abort(); throw error;
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
        td.setAttribute("data-export-value", index === 0 ? row.subject : index === 1 ? row.chain : index === 2 ? row.label
          : index === headers.length - 1 ? row.note : parts.map(part => part.text).join(""));
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
        && (!chain.value || row.chain === chain.value) && (!group || !group.value || row.source_group === group.value));
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
    if (group) {
      const disclosure = document.getElementById("individual-assertions");
      const selectGroup = value => {
        if (value && !groupIds.has(value)) return;
        group.value = value; disclosure.open = true;
        const url = new URL(window.location.href);
        if (value) url.searchParams.set("source", value); else url.searchParams.delete("source");
        url.hash = "individual-assertions"; window.history.replaceState(null, "", url);
        refresh();
      };
      group.addEventListener("change", () => selectGroup(group.value));
      for (const link of document.querySelectorAll("[data-registry-group]")) link.addEventListener("click", event => { event.preventDefault(); selectGroup(link.dataset.registryGroup); });
      const value = new URL(window.location.href).searchParams.get("source");
      if (value && /^[a-f0-9]{64}$/.test(value) && groupIds.has(value)) selectGroup(value);
      else if (value) {
        group.value = ""; disclosure.open = true;
        const url = new URL(window.location.href); url.searchParams.delete("source"); window.history.replaceState(null, "", url);
        status.textContent = "The source filter is not in this recorded collection. All static rows remain available.";
      }
    }
  }
})();
