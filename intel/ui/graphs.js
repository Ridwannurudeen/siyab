(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const mounts = new WeakSet();
  const loading = new WeakSet();
  const views = new WeakMap();
  const payloads = new WeakMap();
  const disposers = new WeakMap();
  const controllers = new WeakMap();
  const recordIndexes = new WeakMap();
  const rangeSetters = new WeakMap();
  const timelineMounts = new WeakSet();
  let sequence = 0;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function svgElement(tag, attributes) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) {
      node.setAttribute(name, String(value));
    }
    return node;
  }

  function shortLabel(value) {
    return value.length > 24 ? value.slice(0, 11) + "…" + value.slice(-9) : value;
  }

  function usd(value) {
    const text = window.SiyabNumbers.decimal(value, 2);
    if (/^-?[\d,]+$/.test(text)) return text + ".00";
    return /\.\d$/.test(text) ? text + "0" : text;
  }

  function decimalSum(values) {
    const parts = values.map(value => {
      const match = /^([+-]?)(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(String(value));
      if (!match) throw new Error("A recorded amount could not be read.");
      const exponent = Number(match[4] || 0);
      if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 1000) throw new Error("A recorded amount is outside the supported range.");
      return {value: BigInt((match[1] === "-" ? "-" : "") + match[2] + (match[3] || "")),
        scale: (match[3] || "").length - exponent};
    });
    const scale = Math.max(0, ...parts.map(part => part.scale));
    const total = parts.reduce((sum, part) => sum + part.value * 10n ** BigInt(scale - part.scale), 0n);
    const digits = (total < 0n ? -total : total).toString().padStart(scale + 1, "0");
    const amount = scale ? digits.slice(0, -scale) + "." + digits.slice(-scale) : digits;
    return (total < 0n ? "-" : "") + amount.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  }

  function edgeRecords(payload, edge) {
    if (!recordIndexes.has(payload)) recordIndexes.set(payload, new Map(payload.records.map(record => [record.id, record])));
    const index = recordIndexes.get(payload);
    return edge.record_ids.map(id => index.get(id));
  }

  function exactUtc(timestamp) {
    if (timestamp === null || timestamp === undefined || !/^\d{1,22}$/.test(String(timestamp))) return "Time unavailable";
    const stamp = BigInt(timestamp);
    const milliseconds = Number(stamp / 1000000n);
    if (!Number.isSafeInteger(milliseconds) || milliseconds > 8640000000000000) return "Time unavailable";
    return new Date(milliseconds).toISOString().slice(0, 19).replace("T", " ") + "." +
      (stamp % 1000000000n).toString().padStart(9, "0") + " UTC";
  }

  function valueSummary(records) {
    const valued = records.filter(record => record.usd_amount !== null && record.usd_amount !== undefined);
    const stamps = records.map(record => record.block_timestamp).filter(stamp => stamp !== null && stamp !== undefined);
    stamps.sort((a, b) => BigInt(a) < BigInt(b) ? -1 : BigInt(a) > BigInt(b) ? 1 : 0);
    return {usd: valued.length ? decimalSum(valued.map(record => record.usd_amount)) : null,
      unknownUsd: records.length - valued.length,
      base: records.length ? decimalSum(records.map(record => record.amount)) : null,
      first: stamps[0] || null, last: stamps[stamps.length - 1] || null};
  }

  function filteredEdges(payload, asset, provenance, from = "", to = "") {
    const edges = payload.edges.filter(edge => (!asset || edge.asset_id === asset) && (!provenance || edge.provenance === provenance));
    if (!from && !to || payload.kind !== "flow") return edges;
    return edges.map(edge => ({...edge, record_ids: edgeRecords(payload, edge).filter(record =>
      record.block_timestamp !== null && record.block_timestamp !== undefined &&
      (!from || BigInt(record.block_timestamp) >= BigInt(from)) &&
      (!to || BigInt(record.block_timestamp) < BigInt(to))).map(record => record.id)}))
      .filter(edge => edge.record_ids.length);
  }

  function displayedAmount(records, asset) {
    if (!records.length) return "Unknown";
    const amount = decimalSum(records.map(record => record.amount));
    const decimals = records.map(record => record.amount_decimals ?? (asset === "native:near" ? 24 : null));
    if (decimals.every(value => Number.isInteger(value) && value >= 0 && value <= 255 && value === decimals[0])) {
      const display = window.SiyabNumbers.baseUnits(amount, decimals[0]);
      if (display !== "Unavailable") return display;
    }
    return amount + " base units";
  }

  function edgeLabel(payload, edge) {
    let detail = "";
    if (payload.kind === "flow") {
      const summary = valueSummary(edgeRecords(payload, edge));
      detail = " · " + displayedAmount(edgeRecords(payload, edge), edge.asset_id) + " " + edge.asset_id +
        " · " + (summary.usd === null ? "USD unknown" : usd(summary.usd) + " known USD") +
        (summary.unknownUsd ? " · " + summary.unknownUsd + (summary.unknownUsd === 1 ? " transfer has unknown USD" : " transfers have unknown USD") : "") +
        " · " + exactUtc(summary.first) + " to " + exactUtc(summary.last);
    }
    return edge.from + " to " + edge.to + " · " +
      (payload.kind === "flow" ? edge.asset_id : edge.rule) + " · " +
      (edge.provenance === "inferred" ? "Attribution hypothesis" : payload.source.recorded === false ? "Live indexed reads" : "Recorded chain reads") + " · " +
      edge.record_ids.length + (payload.source.recorded === false ? " observed " : " recorded ") + (payload.kind === "flow" ? (edge.record_ids.length === 1 ? "transfer" : "transfers") : (edge.record_ids.length === 1 ? "relationship" : "relationships")) + detail;
  }

  function edgePath(edge, from, to, siblings, compact) {
    if (compact && edge.from !== edge.to) {
      const rail = 28 + (siblings.indexOf(edge.id) + 1) / (siblings.length + 1) * 56;
      return "M " + (from.x - 110) + " " + from.y + " C " + rail + " " + from.y + " " +
        rail + " " + to.y + " " + (to.x - 115) + " " + to.y;
    }
    const offset = (siblings.indexOf(edge.id) - (siblings.length - 1) / 2) * 14;
    const dx = to.x - from.x; const dy = to.y - from.y;
    const length = Math.hypot(dx, dy) || 1;
    const boundary = Math.min(105 / Math.abs(dx || 0.001), 24 / Math.abs(dy || 0.001));
    const startX = from.x + dx * boundary + dx / length * 3;
    const startY = from.y + dy * boundary + dy / length * 3;
    const endX = to.x - dx * boundary - dx / length * 8;
    const endY = to.y - dy * boundary - dy / length * 8;
    const direction = edge.from < edge.to ? 1 : -1;
    return edge.from === edge.to ?
      "M " + from.x + " " + (from.y - 24) + " C " + (from.x + 100) + " " + (from.y - 100) +
        " " + (from.x + 100) + " " + (from.y + 100) + " " + from.x + " " + (from.y + 24) :
      "M " + startX + " " + startY + " Q " + ((from.x + to.x) / 2 - dy * direction / length * offset) + " " +
        ((from.y + to.y) / 2 + dx * direction / length * offset) + " " + endX + " " + endY;
  }

  function neighborhood(payload, edges, focus, limit, required) {
    const neighbors = new Map(payload.nodes.map((node) => [node.id, new Map()]));
    for (const edge of edges) {
      const weight = edge.record_ids.length;
      for (const [from, to] of [[edge.from, edge.to], [edge.to, edge.from]]) {
        neighbors.get(from).set(to, (neighbors.get(from).get(to) || 0) + weight);
      }
    }
    const visible = new Set([focus, ...required]);
    const queue = [...visible];
    if (payload.kind === "cluster") {
      const eligible = new Set(edges.flatMap((edge) => [edge.from, edge.to]));
      queue.push(...payload.nodes.filter((node) => edges.length === payload.edges.length || eligible.has(node.id))
        .map((node) => node.id));
    }
    for (let index = 0; index < queue.length && visible.size < limit; index += 1) {
      visible.add(queue[index]);
      const next = [...neighbors.get(queue[index]).entries()].sort(
        (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
      );
      for (const [id] of next) {
        if (visible.size >= limit) break;
        if (!visible.has(id)) {
          visible.add(id);
          queue.push(id);
        }
      }
    }
    return [...visible];
  }

  function initialFocus(payload, edges) {
    if (payload.kind === "flow") return payload.root;
    const weights = new Map(payload.nodes.map((node) => [node.id, 0]));
    for (const edge of edges) {
      weights.set(edge.from, weights.get(edge.from) + edge.record_ids.length);
      weights.set(edge.to, weights.get(edge.to) + edge.record_ids.length);
    }
    return payload.nodes.reduce((best, node) => weights.get(node.id) > weights.get(best) ? node.id : best, payload.root);
  }

  function recordedPath(payload, edges, target) {
    const previous = new Map([[payload.root, null]]);
    const queue = [payload.root];
    for (let index = 0; index < queue.length; index += 1) {
      const current = queue[index];
      if (current === target) break;
      for (const edge of edges) {
        const next = edge.from === current ? edge.to :
          payload.kind === "cluster" && edge.to === current ? edge.from : null;
        if (next !== null && !previous.has(next)) {
          previous.set(next, { node: current, edge: edge.id });
          queue.push(next);
        }
      }
    }
    if (!previous.has(target)) return null;
    const nodes = [target];
    const pathEdges = [];
    let current = target;
    while (previous.get(current)) {
      const step = previous.get(current);
      nodes.unshift(step.node);
      pathEdges.unshift(step.edge);
      current = step.node;
    }
    return { nodes, edges: pathEdges };
  }

  function layout(payload, ids, edges, focus, compact) {
    if (compact) {
      return { positions: new Map(ids.map((id, index) => [id, { x: 210, y: 48 + index * 80 }])),
        width: 360, height: Math.max(336, ids.length * 80 + 16), compact: true };
    }
    if (payload.kind === "cluster") {
      const width = 680 + Math.max(0, ids.length - 8) * 80;
      const height = 480 + Math.max(0, ids.length - 8) * 60;
      const positions = new Map();
      ids.forEach((id, index) => {
        const angle = (index / ids.length) * Math.PI * 2 - Math.PI / 2;
        positions.set(id, { x: width / 2 + Math.cos(angle) * (width / 2 - 110),
          y: height / 2 + Math.sin(angle) * (height / 2 - 60) });
      });
      return { positions, width, height };
    }
    const levels = new Map([[focus, 0]]);
    const queue = [focus];
    for (let index = 0; index < queue.length; index += 1) {
      for (const edge of edges) {
        const next = edge.from === queue[index] ? edge.to :
          edge.to === queue[index] ? edge.from : null;
        if (next !== null && ids.includes(next) && !levels.has(next)) {
          levels.set(next, levels.get(queue[index]) + 1);
          queue.push(next);
        }
      }
    }
    const groups = new Map();
    for (const id of ids) {
      const node = payload.nodes.find((candidate) => candidate.id === id);
      const level = payload.kind === "flow" && focus === payload.root ?
        (node.depth === null ? (levels.get(id) || 0) : node.depth) :
        (levels.has(id) ? levels.get(id) : 3);
      if (!groups.has(level)) groups.set(level, []);
      groups.get(level).push(id);
    }
    const columns = [...groups.keys()].sort((a, b) => a - b);
    const height = Math.max(480, ...[...groups.values()].map((group) => group.length * 64 + 72));
    const width = Math.max(820, columns.length * 250 + 80);
    const positions = new Map();
    columns.forEach((level, column) => {
      groups.get(level).forEach((id, row, group) => {
        positions.set(id, {
          x: columns.length === 1 ? width / 2 : 130 + column * ((width - 260) / (columns.length - 1)),
          y: 48 + (row + 0.5) * ((height - 96) / group.length),
        });
      });
    });
    return { positions, width, height };
  }

  function fittedView(geometry, bounds) {
    const scale = bounds.width && bounds.height ? Math.min(bounds.width / geometry.width, bounds.height / geometry.height) : 1;
    if (scale >= 0.6) return { x: 0, y: 0, width: geometry.width, height: geometry.height };
    const width = bounds.width / 0.6, height = bounds.height / 0.6;
    return { x: (geometry.width - width) / 2, y: 0, width, height };
  }

  function readableView(geometry, focus, bounds) {
    const fittedScale = Math.min(bounds.width / geometry.width, bounds.height / geometry.height);
    const scale = Math.max(0.8, Math.min(1, fittedScale));
    const width = bounds.width / scale;
    const height = bounds.height / scale;
    const center = fittedScale < 0.8 ? geometry.positions.get(focus) :
      { x: geometry.width / 2, y: geometry.height / 2 };
    if (geometry.compact) return { x: (geometry.width - width) / 2,
      y: Math.max(0, center.y - height / 2), width, height };
    return { x: center.x - width / 2, y: center.y - height / 2, width, height };
  }

  function recordingCoverage(payload, saved = false) {
    if (payload.source.recorded === false) return (saved ? "Saved indexed observation: depth " : "Live indexed observation: depth ") + payload.bounds.reached_depth +
      " of " + payload.bounds.requested_depth + " requested; bounded neighborhood, not complete account history.";
    if (payload.kind === "flow") {
      const reason = payload.coverage.completeness === "complete" ? "" :
        " at " + payload.coverage.completeness.reason;
      return "Recording: depth " + payload.bounds.reached_depth + " of " + payload.bounds.requested_depth +
        " requested; " + (payload.coverage.truncated ? "truncated" + reason : "acquisition complete") + ".";
    }
    return "Recording: " + payload.bounds.seeds.length + (payload.bounds.seeds.length === 1 ? " seed. " : " seeds. ") + payload.coverage.standard;
  }

  function graphAddress(source) {
    const url = new URL(source, window.location.href);
    if (url.origin !== window.location.origin || url.hash || url.username || url.password) throw new Error("The graph address is invalid.");
    if (/^\/graph-data\/[a-f0-9]{64}\.json$/.test(url.pathname) && !url.search) return {url, live: false};
    if (/^\/live\/account-data\/[a-f0-9]{64}\.json$/.test(url.pathname) && !url.search) return {url, live: true, snapshot: true};
    const match = /^\/live\/accounts\/([^/]+)\/graph$/.exec(url.pathname);
    const account = match && match[1];
    const keys = [...url.searchParams.keys()];
    if (!account || account.length < 2 || account.length > 64 || !/^[a-z0-9]+(?:[._-][a-z0-9]+)*$/.test(account) ||
      account === "testnet" || account.endsWith(".testnet") || keys.some(key => key !== "depth") || keys.length > 1 ||
      keys.length && !["1", "2"].includes(url.searchParams.get("depth"))) throw new Error("The graph address is invalid.");
    return {url, live: true};
  }

  function validPayload(payload) {
    if (!payload || !["flow", "cluster"].includes(payload.kind) ||
      !Array.isArray(payload.nodes) || !Array.isArray(payload.edges) ||
      !Array.isArray(payload.records) || !payload.source ||
      !payload.nodes.every((node) => node && typeof node.id === "string" && typeof node.label === "string") ||
      !payload.records.every((record) => record && typeof record.id === "string") ||
      !payload.edges.every((edge) => edge && typeof edge.id === "string")) return false;
    const nodes = new Set(payload.nodes.map((node) => node.id));
    const records = new Set(payload.records.map((record) => record.id));
    return nodes.has(payload.root) && nodes.size === payload.nodes.length &&
      records.size === payload.records.length &&
      new Set(payload.edges.map((edge) => edge.id)).size === payload.edges.length &&
      payload.total_nodes === payload.nodes.length && payload.total_records === payload.records.length &&
      payload.edges.every((edge) => nodes.has(edge.from) && nodes.has(edge.to) &&
        Array.isArray(edge.record_ids) && edge.record_ids.length > 0 &&
        edge.record_ids.every((id) => records.has(id)));
  }

  function restoredView(payload, compact) {
    const params = new URL(window.location.href).searchParams;
    if (params.has("g_root") && params.get("g_root") !== payload.root) return {};
    const result = {};
    if (payload.edges.some(edge => edge.asset_id === params.get("g_asset"))) result.asset = params.get("g_asset");
    if (payload.edges.some(edge => edge.provenance === params.get("g_evidence"))) result.provenance = params.get("g_evidence");
    for (const key of ["from", "to"]) {
      const value = params.get("g_" + key);
      if (payload.kind === "flow" && /^\d{1,22}$/.test(value || "") && BigInt(value) <= 253402300799999999999n) result[key] = value;
    }
    if (result.from && result.to && BigInt(result.from) >= BigInt(result.to)) { delete result.from; delete result.to; }
    const node = params.get("g_node"), edge = params.get("g_edge"), focus = params.get("g_focus");
    if (payload.nodes.some(candidate => candidate.id === focus)) result.focus = focus;
    if (payload.nodes.some(candidate => candidate.id === node)) result.selected = {kind: "node", id: node};
    else if (payload.edges.some(candidate => candidate.id === edge)) result.selected = {kind: "edge", id: edge};
    const limit = params.get("g_limit");
    if (/^\d{1,2}$/.test(limit || "") && Number(limit) >= (compact ? 4 : 8) && Number(limit) <= 72) result.limit = Number(limit);
    if (["list", "graph"].includes(params.get("g_view"))) result.mode = params.get("g_view");
    return result;
  }

  function arrangeControls(compact, controls) {
    const {container, toolbar, assetLabel, evidenceLabel, modeControls, expandButton, zoomIn, zoomOut,
      fitControl, viewControls, panControls, shareControl, save, exportView, filters, actions, actionPanel,
      timeControls, legend} = controls;
    const active = document.activeElement;
    const retainFocus = active && container.contains(active);
    if (compact) {
      filters.append(assetLabel, evidenceLabel, timeControls);
      panControls.prepend(zoomIn, zoomOut, fitControl);
      actionPanel.append(shareControl, save, exportView);
      toolbar.replaceChildren(modeControls, expandButton, filters, viewControls, actions);
      if (retainFocus) {
        if (filters.contains(active)) filters.open = true;
        if (viewControls.contains(active)) viewControls.open = true;
        if (actions.contains(active)) actions.open = true;
      }
    } else {
      toolbar.replaceChildren(assetLabel, evidenceLabel, modeControls, expandButton, zoomIn, zoomOut,
        fitControl, viewControls, shareControl, save, exportView);
      container.insertBefore(timeControls, legend);
    }
    if (retainFocus) active.focus({preventScroll: true});
  }

  function mount(container, loadedPayload) {
    if (mounts.has(container)) return container;
    const script = container.querySelector('script[type="application/json"]') ||
      (container.nextElementSibling && container.nextElementSibling.matches('script[type="application/json"]') ?
        container.nextElementSibling : null);
    let payload;
    try {
      payload = loadedPayload || JSON.parse(script ? script.textContent : "null");
    } catch (error) {
      container.append(element("p", "graph-empty", "The recorded graph data could not be read. Open a recorded investigation from the page links."));
      return null;
    }
    if (!validPayload(payload)) {
      container.append(element("p", "graph-empty", "The recorded graph data is incomplete. Open a recorded investigation from the page links."));
      return null;
    }
    container.replaceChildren();
    container.setAttribute("aria-busy", "false");
    const live = payload.source.recorded === false;
    const saved = /^\/live\/account-data\/[a-f0-9]{64}\.json$/.test(container.getAttribute("data-graph-src") || "");
    const prefix = "siyab-graph-" + (++sequence);
    const nodeById = new Map(payload.nodes.map((node) => [node.id, node]));
    const recordById = new Map(payload.records.map((record) => [record.id, record]));
    const facts = new Map((payload.facts || []).map((fact) => [fact.id, fact]));
    const compactQuery = window.matchMedia("(max-width: 760px)");
    const firstFocus = initialFocus(payload, payload.edges);
    const state = {
      asset: "", provenance: "", focus: firstFocus, selected: { kind: "node", id: firstFocus },
      limit: compactQuery.matches ? 4 : 8, compact: compactQuery.matches,
      search: "", accountPage: 0, recordPage: 0, edgePage: 0,
      path: null, view: null, visible: [], edges: [], matches: [], from: "", to: "",
      mode: compactQuery.matches ? "list" : "graph", ...restoredView(payload, compactQuery.matches),
    };
    const selectedEdge = state.selected.kind === "edge" && filteredEdges(payload, state.asset, state.provenance, state.from, state.to)
      .find(edge => edge.id === state.selected.id);
    if (state.selected.kind === "edge" && !selectedEdge) state.selected = {kind: "node", id: state.focus};
    container.dataset.graphView = state.mode;
    container.classList.add("siyab-graph");
    const toolbar = element("div", "graph-toolbar");
    toolbar.setAttribute("aria-label", (live ? "Observed graph controls" : "Recorded graph controls"));

    function button(parent, label, handler, className) {
      const control = element("button", className || "graph-button", label);
      control.type = "button";
      control.addEventListener("click", handler);
      parent.append(control);
      return control;
    }

    function select(label, values, handler) {
      const wrapper = element("label", "graph-filter", label);
      const control = element("select");
      for (const [value, text] of values) {
        const option = element("option", "", text);
        option.value = value;
        control.append(option);
      }
      control.addEventListener("change", () => handler(control.value));
      wrapper.append(control);
      toolbar.append(wrapper);
      return control;
    }

    const assetControl = select("Asset", [["", (live ? "All observed assets" : "All recorded assets")],
      ...[...new Set(payload.edges.map((edge) => edge.asset_id).filter(Boolean))].sort().map((asset) => [asset, asset])],
    (value) => { state.asset = value; changeFilter(); });
    if (payload.kind === "cluster") assetControl.parentElement.hidden = true;
    const provenanceControl = select("Evidence", [["", "All evidence"],
      ...[...new Set(payload.edges.map((edge) => edge.provenance))].sort().map((value) =>
        [value, value === "inferred" ? "Attribution hypothesis" : (live ? "Live indexed reads" : "Recorded chain reads")])],
    (value) => { state.provenance = value; changeFilter(); });
    assetControl.value = state.asset; provenanceControl.value = state.provenance;
    const rangeControls = element("form", "graph-range");
    rangeControls.setAttribute("aria-label", (live ? "Observed transfer time range" : "Recorded transfer time range"));
    const dateInputs = [];
    for (const [key, text] of [["from", "From UTC"], ["to", "Until UTC (exclusive)"]]) {
      const label = element("label", "", text), input = element("input");
      input.type = "datetime-local"; input.step = "1";
      input.value = state[key] ? new Date(Number(BigInt(state[key]) / 1000000n)).toISOString().slice(0, 19) : "";
      label.append(input); rangeControls.append(label); dateInputs.push(input);
    }
    const rangeMessage = element("p", "graph-range-message"); rangeMessage.setAttribute("role", "status");
    if (state.from || state.to) rangeMessage.textContent = "Exact restored interval: " +
      (state.from ? exactUtc(state.from) : "unbounded start") + " until " + (state.to ? exactUtc(state.to) : "unbounded end") + ", end excluded.";
    const applyRange = button(rangeControls, "Apply time range", () => {
      const values = dateInputs.map(input => input.value ? Date.parse(input.value + "Z") : null);
      if (values.some(value => value !== null && !Number.isFinite(value)) || values[0] !== null && values[1] !== null && values[0] >= values[1]) {
        rangeMessage.textContent = "Choose an end after the start. Times are UTC; the end is excluded."; return;
      }
      state.from = values[0] === null ? "" : (BigInt(values[0]) * 1000000n).toString();
      state.to = values[1] === null ? "" : (BigInt(values[1]) * 1000000n).toString();
      changeFilter(); rangeMessage.textContent = live ? "Graph time range applied within this bounded observation." : "Graph time range applied. Full dataset charts below keep their recorded scope.";
    });
    rangeControls.addEventListener("submit", event => { event.preventDefault(); applyRange.click(); });
    rangeControls.append(rangeMessage);
    rangeControls.hidden = payload.kind !== "flow";
    const timeControls = element("details", "graph-time-controls");
    timeControls.hidden = payload.kind !== "flow";
    timeControls.append(element("summary", "", "Time range"), rangeControls);
    const modeControls = element("div", "graph-mode-controls");
    modeControls.setAttribute("role", "group"); modeControls.setAttribute("aria-label", "Investigation view");
    const modeButtons = [];
    for (const [mode, text] of [["list", "List"], ["graph", "Graph"]]) {
      const control = button(modeControls, text, () => {
        state.mode = mode; container.dataset.graphView = mode; list.open = mode === "list";
        modeButtons.forEach(([value, button]) => button.setAttribute("aria-pressed", String(value === mode)));
        if (mode === "graph") frameSelected();
      });
      control.setAttribute("aria-pressed", String(state.mode === mode)); modeButtons.push([mode, control]);
    }
    toolbar.append(modeControls);
    const expandButton = button(toolbar, "Expand neighborhood", () => {
      state.limit = Math.min(payload.kind === "cluster" ? payload.total_nodes : 72, state.limit + 12); renderGraph(true);
    });
    const zoomIn = button(toolbar, "Zoom in", () => zoom(0.8));
    const zoomOut = button(toolbar, "Zoom out", () => zoom(1.25));
    const fitControl = button(toolbar, "Fit", () => {
      if (payload.kind === "cluster") { state.limit = payload.total_nodes; renderGraph(false); }
      fit();
    });
    const viewControls = element("details", "graph-view-controls");
    viewControls.append(element("summary", "", "View controls"));
    const panControls = element("div", "graph-pan-controls");
    button(panControls, "Pan left", () => pan(-1, 0));
    button(panControls, "Pan right", () => pan(1, 0));
    button(panControls, "Pan up", () => pan(0, -1));
    button(panControls, "Pan down", () => pan(0, 1));
    button(panControls, "Reset", () => {
      state.asset = ""; state.provenance = ""; state.focus = firstFocus;
      state.limit = state.compact ? 4 : 8; state.path = null; state.search = ""; state.accountPage = 0;
      assetControl.value = ""; provenanceControl.value = ""; search.value = "";
      state.selected = { kind: "node", id: firstFocus }; state.recordPage = 0;
      state.from = ""; state.to = ""; dateInputs.forEach(input => { input.value = ""; }); rangeMessage.textContent = "";
      pathStatus.textContent = "";
      renderGraph(true); renderInspector(); renderAccounts();
    });
    viewControls.append(panControls); toolbar.append(viewControls);
    const status = element("p", "graph-status");
    status.setAttribute("role", "status");
    const workspace = element("div", "graph-workspace");
    const stage = element("div", "graph-stage");
    const svg = svgElement("svg", { class: "graph-svg", role: "group", "aria-label":
      payload.kind === "flow" ? (live ? "Observed transfer graph. Select accounts with the arrow keys, or use the account list." : "Recorded transfer graph. Select accounts with the arrow keys, or use the account list.") :
        "Recorded relationship graph. Dashed lines are attribution hypotheses. Use the account list for full names." });
    const defs = svgElement("defs", {});
    const marker = svgElement("marker", { id: prefix + "-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse", markerUnits: "userSpaceOnUse" });
    marker.append(svgElement("path", { d: "M 0 1 L 9 5 L 0 9 z", class: "graph-arrow" }));
    defs.append(marker);
    const drawing = svgElement("g", {});
    svg.append(defs, drawing);
    const empty = element("p", "graph-empty", (live ? "No observed edges match these filters. Change a filter or reset the graph." : "No recorded edges match these filters. Change a filter or reset the graph."));
    empty.hidden = true;
    const pathStatus = element("p", "graph-path-status");
    pathStatus.setAttribute("role", "status");
    stage.append(svg, empty, pathStatus);
    const inspector = element("aside", "graph-inspector");
    inspector.setAttribute("aria-label", "Selected graph evidence");
    workspace.append(stage, inspector);

    const list = element("details", "graph-list");
    list.open = state.mode === "list";
    list.append(element("summary", "", "Accounts and relationships"));
    const searchLabel = element("label", "graph-search", (live ? "Find an observed account" : "Find an account in this recording"));
    const search = element("input");
    search.type = "search"; search.autocomplete = "off";
    search.addEventListener("input", () => {
      state.search = search.value; state.accountPage = 0; renderAccounts();
    });
    searchLabel.append(search);
    const accountList = element("div", "graph-account-list");
    const edgeList = element("div", "graph-relationship-list");
    list.append(searchLabel, accountList, edgeList);
    const acquisition = element("p", "graph-source graph-coverage", recordingCoverage(payload, saved));
    const source = element("p", "graph-source graph-provenance", (saved ? "Saved indexed observation · " : live ? "Live indexed mainnet observation · " : "Recorded mainnet data · ") + payload.source.provider +
      " · Read " + payload.source.fetched_at + ". ");
    if (payload.source.evidence_url && /^\/(?:flows|clusters)\/[a-zA-Z0-9_.%-]+\.html$/.test(payload.source.evidence_url)) {
      const evidenceLink = element("a", "", "Open recorded evidence and limits");
      evidenceLink.href = payload.source.evidence_url;
      source.append(evidenceLink);
    }
    const legend = element("div", "graph-legend");
    legend.setAttribute("aria-label", "Graph legend");
    legend.append(element("span", "graph-legend-line", (live ? "Observed direction" : "Recorded direction")),
      element("span", "graph-legend-hypothesis", "Dashed: attribution hypothesis"),
      element("span", "graph-legend-selected", "Teal: selected account or path"));
    if (payload.kind === "flow") legend.append(element("span", "", "Width: known provider USD, logarithmic; thin lines can have unknown value. Gross value is not loss."));
    const tooltip = element("div", "graph-tooltip"); tooltip.setAttribute("role", "tooltip"); tooltip.id = prefix + "-tooltip"; tooltip.hidden = true;
    stage.append(tooltip);
    const overview = element("details", "graph-overview-panel");
    overview.append(element("summary", "", "Neighborhood overview"));
    const overviewSvg = svgElement("svg", {class: "graph-overview", role: "img", "aria-label": "Displayed neighborhood and current viewport"});
    const overviewDrawing = svgElement("g", {});
    const overviewViewport = svgElement("rect", {class: "graph-overview-viewport"});
    overviewSvg.append(overviewDrawing, overviewViewport);
    overview.append(overviewSvg, element("p", "", (live ? "Fit shows this bounded neighborhood; Expand reveals already loaded accounts. It does not request further history." : "Overview of the displayed neighborhood. Fit shows this entire neighborhood; Expand adds recorded accounts.")));
    button(overview, "Show entire neighborhood", fit);
    const shareResult = element("div", "graph-share-result"); shareResult.setAttribute("aria-live", "polite");
    const shareControl = button(toolbar, "Share view", async () => {
      const url = new URL(window.location.href);
      [...url.searchParams.keys()].filter(key => key.startsWith("g_")).forEach(key => url.searchParams.delete(key));
      const fields = {root: payload.root, asset: state.asset, evidence: state.provenance, from: state.from, to: state.to,
        focus: state.focus, limit: String(state.limit), view: state.mode,
        [state.selected.kind]: state.selected.id};
      for (const [key, value] of Object.entries(fields)) if (value) url.searchParams.set("g_" + key, value);
      const label = element("label", "", "View permalink"), input = element("input");
      input.value = url.href; input.readOnly = true; label.append(input);
      const message = element("p", "", (live ? "Copy this link to request this account view again. Live observations can change." : "Copy this link to reopen the recorded view."));
      shareResult.replaceChildren(message, label);
      try {
        if (!navigator.clipboard || !navigator.clipboard.writeText) return;
        await navigator.clipboard.writeText(url.href); message.textContent = "View link copied.";
      } catch (error) { message.textContent = "Clipboard is unavailable. Select and copy the view permalink."; }
    });
    const save = element("button", "graph-button", "Save investigation"); save.type = "button"; save.setAttribute("data-save-investigation", "");
    const exportView = element("button", "graph-button", "Export view"); exportView.type = "button"; exportView.setAttribute("data-export-graph", "");
    toolbar.append(save, exportView);
    const filters = element("details", "graph-compact-filters"); filters.append(element("summary", "", "Filters"));
    const actions = element("details", "graph-compact-actions"); actions.append(element("summary", "", "Actions"));
    const actionPanel = element("div", "graph-actions-panel"); actions.append(actionPanel);
    container.append(toolbar, status, acquisition, timeControls, legend, workspace, list, overview, shareResult, source);
    const controls = {container, toolbar, assetLabel: assetControl.parentElement, evidenceLabel: provenanceControl.parentElement,
      modeControls, expandButton, zoomIn, zoomOut, fitControl, viewControls, panControls, shareControl, save, exportView,
      filters, actions, actionPanel, timeControls, legend};
    arrangeControls(state.compact, controls);

    function changeFilter() {
      state.path = null; state.recordPage = 0; state.edgePage = 0; state.accountPage = 0;
      pathStatus.textContent = "";
      if (state.selected.kind === "edge" && !filteredEdges(payload, state.asset, state.provenance, state.from, state.to)
        .some((edge) => edge.id === state.selected.id)) state.selected = { kind: "node", id: state.focus };
      const matches = filteredEdges(payload, state.asset, state.provenance, state.from, state.to);
      if (payload.kind === "cluster" && matches.length &&
        !matches.some((edge) => edge.from === state.focus || edge.to === state.focus)) {
        state.focus = initialFocus(payload, matches);
        state.selected = { kind: "node", id: state.focus };
      }
      renderGraph(true); renderInspector(); renderAccounts();
    }

    function choose(kind, id) {
      state.selected = { kind, id }; state.recordPage = 0; state.path = null;
      pathStatus.textContent = "";
      renderGraph(false); renderInspector(); renderAccounts();
      if (kind === "node") revealNode(id);
      inspector.querySelector("h3").focus({ preventScroll: state.mode === "graph" });
    }

    function applyView() {
      svg.setAttribute("viewBox", [state.view.x, state.view.y, state.view.width, state.view.height].join(" "));
      overviewViewport.setAttribute("x", String(state.view.x)); overviewViewport.setAttribute("y", String(state.view.y));
      overviewViewport.setAttribute("width", String(state.view.width)); overviewViewport.setAttribute("height", String(state.view.height));
    }

    function fit() {
      if (!state.geometry) return;
      state.view = fittedView(state.geometry, svg.getBoundingClientRect());
      applyView();
    }

    function frameSelected() {
      const bounds = svg.getBoundingClientRect();
      if (!bounds.width || !bounds.height) { fit(); return; }
      const focus = state.selected.kind === "node" && state.geometry.positions.has(state.selected.id) ?
        state.selected.id : state.focus;
      state.view = readableView(state.geometry, focus, bounds);
      applyView();
    }

    function zoom(factor) {
      const nextWidth = state.view.width * factor;
      if (nextWidth < 240 || nextWidth > state.geometry.width * 4) return;
      state.view.x += (state.view.width - nextWidth) / 2;
      const nextHeight = state.view.height * factor;
      state.view.y += (state.view.height - nextHeight) / 2;
      state.view.width = nextWidth; state.view.height = nextHeight;
      applyView();
    }

    function pan(x, y) {
      state.view.x += x * state.view.width * 0.25;
      state.view.y += y * state.view.height * 0.25;
      applyView();
    }

    function revealNode(id) {
      const position = state.geometry.positions.get(id);
      if (!position) return;
      if (position.x - 110 < state.view.x || position.x + 110 > state.view.x + state.view.width ||
        position.y - 28 < state.view.y || position.y + 28 > state.view.y + state.view.height) {
        state.view.x = position.x - state.view.width / 2;
        state.view.y = position.y - state.view.height / 2;
        applyView();
      }
    }

    function renderGraph(refit) {
      state.matches = filteredEdges(payload, state.asset, state.provenance, state.from, state.to);
      const selected = state.selected.kind === "node" ? [state.selected.id] :
        state.matches.filter(edge => edge.id === state.selected.id).flatMap(edge => [edge.from, edge.to]);
      state.visible = neighborhood(payload, state.matches, state.focus, state.limit,
        [...new Set([...(state.path ? state.path.nodes : []), ...selected])]);
      const ids = new Set(state.visible);
      state.edges = state.matches.filter((edge) => ids.has(edge.from) && ids.has(edge.to));
      const visibleRecords = new Set(state.edges.flatMap((edge) => edge.record_ids));
      const matchingRecords = new Set(state.matches.flatMap((edge) => edge.record_ids));
      status.textContent = "Display: " + state.visible.length + " of " + payload.total_nodes + (payload.total_nodes === 1 ? " account" : " accounts") + " shown · " +
        visibleRecords.size + " of " + payload.total_records + (live ? " observed " : " recorded ") +
        (payload.kind === "flow" ? (payload.total_records === 1 ? "transfer" : "transfers") : (payload.total_records === 1 ? "relationship" : "relationships")) + " shown · " + state.edges.length + (state.edges.length === 1 ? " edge" : " edges") +
        (state.asset || state.provenance || state.from || state.to ? " · " + matchingRecords.size + (matchingRecords.size === 1 ? " record matches filters" : " records match filters") : "");
      expandButton.hidden = false;
      expandButton.disabled = state.limit >= 72 || state.visible.length < state.limit || state.visible.length === payload.total_nodes;
      if (state.limit >= 72) expandButton.textContent = "72-account view limit";
      else expandButton.textContent = "Expand neighborhood";
      state.geometry = layout(payload, state.visible, state.edges, state.focus, state.compact);
      drawing.replaceChildren();
      tooltip.hidden = true;
      overviewDrawing.replaceChildren();
      overviewSvg.setAttribute("viewBox", "0 0 " + state.geometry.width + " " + state.geometry.height);
      empty.hidden = state.edges.length > 0;
      const pairs = new Map();
      const values = new Map(payload.kind === "flow" ? state.edges.map(edge => [edge.id, valueSummary(edgeRecords(payload, edge))]) : []);
      const maximumUsd = Math.max(0, ...[...values.values()].map(value => Number(value.usd || 0)));
      for (const edge of state.edges) {
        const key = [edge.from, edge.to].sort().join("\n");
        if (!pairs.has(key)) pairs.set(key, []);
        pairs.get(key).push(edge.id);
      }
      for (const edge of state.edges) {
        const from = state.geometry.positions.get(edge.from);
        const to = state.geometry.positions.get(edge.to);
        const siblings = pairs.get([edge.from, edge.to].sort().join("\n"));
        const d = edgePath(edge, from, to, siblings, state.compact);
        const selected = state.selected.kind === "edge" && state.selected.id === edge.id;
        const path = state.path && state.path.edges.includes(edge.id);
        const group = svgElement("g", { "data-edge-id": edge.id, role: "group", "aria-label": edgeLabel(payload, edge) });
        const line = svgElement("path", { d, class: "graph-edge" +
          (edge.provenance === "inferred" ? " is-inferred" : "") + (selected ? " is-selected" : "") +
          (path ? " is-path" : ""), "marker-end": "url(#" + prefix + "-arrow)" });
        const value = values.get(edge.id);
        if (value && value.usd !== null && maximumUsd > 0) {
          line.style.strokeWidth = String(1.4 + Math.log1p(Math.max(0, Number(value.usd))) / Math.log1p(maximumUsd) * 4.6);
        }
        const hit = svgElement("path", { d, class: "graph-edge-hit", "aria-hidden": "true" });
        hit.addEventListener("click", () => choose("edge", edge.id));
        const title = svgElement("title", {});
        title.textContent = edgeLabel(payload, edge);
        hit.addEventListener("pointerenter", () => { tooltip.textContent = title.textContent; tooltip.hidden = false; });
        hit.addEventListener("pointerleave", () => { tooltip.hidden = true; });
        if (value && (selected || path)) {
          const amount = svgElement("text", {x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 - 8, class: "graph-edge-amount", "text-anchor": "middle"});
          amount.textContent = value.usd === null ? "USD unknown" : usd(value.usd) + " known USD";
          group.append(amount);
        }
        overviewDrawing.append(svgElement("path", {d, class: "graph-overview-edge" + (edge.provenance === "inferred" ? " is-inferred" : "")}));
        group.append(title, line, hit); drawing.append(group);
      }
      state.visible.forEach((id, index) => {
        const node = nodeById.get(id); const position = state.geometry.positions.get(id);
        overviewDrawing.append(svgElement("circle", {cx: position.x, cy: position.y, r: 10, class: "graph-overview-node" + (id === payload.root ? " is-root" : "")}));
        const selected = state.selected.kind === "node" && state.selected.id === id;
        const group = svgElement("g", { transform: "translate(" + position.x + " " + position.y + ")",
          class: "graph-node" + (id === payload.root ? " is-root" : "") + (selected ? " is-selected" : "") +
            (id === state.focus ? " is-focused" : ""),
          role: "button", tabindex: selected ||
            (!(state.selected.kind === "node" && ids.has(state.selected.id)) && index === 0) ? "0" : "-1",
          "aria-label": node.label + (node.depth !== null ? ", depth " + node.depth : "") + (live ? ", inspect observed evidence" : ", inspect recorded evidence"),
          "aria-pressed": selected ? "true" : "false", "data-node-id": id });
        const title = svgElement("title", {}); title.textContent = node.label;
        const body = svgElement("rect", { x: -105, y: -24, width: 210, height: 48, rx: 5, class: "graph-node-body" });
        const label = svgElement("text", { x: 0, y: -2, "text-anchor": "middle", class: "graph-node-label" });
        label.textContent = shortLabel(node.label);
        const detail = svgElement("text", { x: 0, y: 15, "text-anchor": "middle", class: "graph-node-detail" });
        detail.textContent = id === payload.root ? (live ? "Observation root" : "Recording root") : payload.kind === "flow" ?
          (live ? "Observed depth " : "Recorded depth ") + node.depth : (live ? "Observed account" : "Recorded account");
        group.append(title, body, label, detail);
        group.addEventListener("click", () => choose("node", id));
        group.addEventListener("focus", () => revealNode(id));
        group.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault(); choose("node", id);
            drawing.querySelector('[data-node-id="' + CSS.escape(id) + '"]').focus();
          } else if (["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"].includes(event.key)) {
            event.preventDefault();
            const step = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
            const next = event.key === "Home" ? 0 : event.key === "End" ? state.visible.length - 1 :
              (index + step + state.visible.length) % state.visible.length;
            const groups = drawing.querySelectorAll("[data-node-id]");
            groups.forEach((item) => item.setAttribute("tabindex", "-1"));
            groups[next].setAttribute("tabindex", "0"); groups[next].focus();
          }
        });
        drawing.append(group);
      });
      if (refit || !state.view) frameSelected();
      renderRelationships();
    }

    function metadata(parent, entries) {
      const dl = element("dl", "graph-meta");
      for (const [name, value, role] of entries) {
        dl.append(element("dt", "", name), element("dd", role || (typeof value === "number" ? "graph-number" : ""), value === null || value === undefined ? "Unknown" :
          typeof value === "object" ? JSON.stringify(value) : value));
      }
      parent.append(dl);
    }

    function pagination(parent, page, total, size, handler) {
      if (total <= size) return;
      const nav = element("div", "graph-pagination");
      function changePage(event, direction, nextPage) {
        const restore = document.activeElement === event.currentTarget;
        handler(nextPage);
        if (restore) {
          const replacement = parent.querySelector('[data-page-direction="' + direction + '"]:not(:disabled)') ||
            parent.querySelector('.graph-pagination button:not(:disabled)') || parent.querySelector("button, summary");
          if (replacement) replacement.focus({ preventScroll: true });
        }
      }
      const previous = button(nav, "Previous", (event) => changePage(event, "previous", page - 1));
      previous.disabled = page === 0; previous.setAttribute("data-page-direction", "previous");
      nav.append(element("span", "", (page * size + 1) + "-" + Math.min((page + 1) * size, total) + " of " + total));
      const next = button(nav, "Next", (event) => changePage(event, "next", page + 1));
      next.disabled = (page + 1) * size >= total; next.setAttribute("data-page-direction", "next");
      parent.append(nav);
    }

    function renderRecords(records) {
      inspector.append(element("h4", "", records.length + (live ? " observed " : " recorded ") + (payload.kind === "flow" ? (records.length === 1 ? "transfer" : "transfers") : (records.length === 1 ? "relationship" : "relationships"))));
      const rows = element("div", "graph-record-list");
      const pageRecords = records.slice(state.recordPage * 10, (state.recordPage + 1) * 10);
      for (const record of pageRecords) {
        const details = element("details", "graph-record");
        const heading = payload.kind === "flow" ?
          displayedAmount([record], record.asset_id) + " · " + record.asset_id :
          record.rule + " · " + (record.provenance === "inferred" ? "Hypothesis" : "Chain read");
        details.append(element("summary", "", heading));
        if (payload.kind === "flow") {
          metadata(details, [["From", record.from, "graph-identifier"], ["To", record.to, "graph-identifier"], ["Asset", record.asset_id, "graph-identifier"],
            ["Provider float as recorded", record.human_amount, "graph-identifier"], ["Amount (base units)", record.amount, "graph-identifier"],
            ["USD at provider", record.usd_amount === null ? "Unknown" : usd(record.usd_amount), "graph-identifier"],
            ["Block", record.block], ["Time (UTC)", record.timestamp_ms === null ? null : new Date(record.timestamp_ms).toISOString()],
            ["Timestamp (ns)", record.block_timestamp, "graph-identifier"], ["Transaction", record.transaction_hash, "graph-identifier"],
            ["Receipt", record.receipt_id, "graph-identifier"], ["Method", record.method_name || "None"], ["Source", payload.source.provider]]);
        } else {
          metadata(details, [["From", record.from, "graph-identifier"], ["To", record.to, "graph-identifier"], ["Rule", record.rule],
            ["Provenance", record.provenance === "inferred" ? "Attribution hypothesis" : "Recorded chain read"],
            ["Confidence", record.confidence], ["Source", record.source]]);
          for (const factId of record.inputs) {
            const fact = facts.get(factId);
            if (fact) metadata(details, [["Input fact", fact.id, "graph-identifier"], ["Subject", fact.subject, "graph-identifier"], ["Kind", fact.kind],
              ["Value", fact.value], ["Fact provenance", fact.provenance], ["Fact confidence", fact.confidence], ["Fact source", fact.source]]);
          }
        }
        metadata(details, [["Record", record.id, "graph-identifier"]]);
        rows.append(details);
      }
      if (!records.length) rows.append(element("p", "graph-empty", (live ? "No observed evidence matches these filters for this account." : "No recorded evidence matches these filters for this account.")));
      inspector.append(rows);
      pagination(inspector, state.recordPage, records.length, 10, (page) => {
        state.recordPage = page; renderInspector();
      });
    }

    function renderInspector() {
      inspector.replaceChildren();
      let edges;
      if (state.selected.kind === "node") {
        const node = nodeById.get(state.selected.id);
        inspector.append(element("h3", "graph-inspector-title graph-identifier", node.label));
        const actions = element("div", "graph-inspector-actions");
        button(actions, "Focus neighborhood", () => {
          state.focus = node.id; state.limit = state.compact ? 4 : 8; state.path = null; pathStatus.textContent = "";
          renderGraph(true); renderAccounts();
        });
        button(actions, payload.kind === "flow" ? (live ? "Show observed path" : "Show recorded path") : "Show relationship path", () => {
          state.path = recordedPath(payload, state.matches, node.id);
          pathStatus.textContent = state.path ? state.path.edges.length + (live ? " observed " : " recorded ") +
            (payload.kind === "flow" ? "transfer" : "relationship") + (state.path.edges.length === 1 ? " edge connects the " : " edges connect the ") + (live ? "observation root to this account. " : "recording root to this account. ") +
            (payload.kind === "flow" ? "This is an account path, not proof of the same funds or chronological causality." :
              "A relationship path can include attribution hypotheses; it does not establish shared ownership.") :
            "No " + (payload.kind === "flow" ? "directed transfer" : "relationship") + (live ? " path from the observation root matches these filters." : " path from the recording root matches these filters.");
          renderGraph(true);
        });
        if (node.url && (/^(?:\.\.\/|\/)accounts\/[a-zA-Z0-9_.%-]+\.html$/.test(node.url) || live && node.url === "/investigate.html?account=" + encodeURIComponent(node.id))) {
          const link = element("a", "graph-account-link", "Open account"); link.href = node.url; actions.append(link);
        }
        inspector.append(actions);
        edges = state.matches.filter((edge) => edge.from === node.id || edge.to === node.id);
        metadata(inspector, [["Account", node.id, "graph-identifier"], ...(payload.kind === "flow" ? [[(live ? "Observed depth" : "Recorded depth"), node.depth]] :
          [[(live ? "Observation root" : "Recording root"), payload.root, "graph-identifier"]]), ["Matching edges", edges.length],
          ["Scope", (live ? "Matching observed edges in this bounded response, including outside the visible neighborhood" : "All matching recorded edges for this account, including outside the visible neighborhood")]]);
      } else {
        const edge = state.matches.find((candidate) => candidate.id === state.selected.id);
        inspector.append(element("h3", "graph-inspector-title", payload.kind === "flow" ? (live ? "Observed transfers" : "Recorded transfers") : "Recorded relationship"));
        const fromButton = button(inspector, edge.from, () => choose("node", edge.from), "graph-account-button");
        fromButton.setAttribute("aria-label", "Inspect sender " + edge.from);
        const toButton = button(inspector, edge.to, () => choose("node", edge.to), "graph-account-button");
        toButton.setAttribute("aria-label", "Inspect receiver " + edge.to);
        metadata(inspector, [["Provenance", edge.provenance === "inferred" ? "Attribution hypothesis" : (live ? "Live indexed reads" : "Recorded chain reads")],
          ["Confidence", edge.confidence], ...(edge.asset_id ? [["Asset", edge.asset_id, "graph-identifier"]] : [])]);
        edges = [edge];
      }
      const recordIds = new Set(edges.flatMap((edge) => edge.record_ids));
      if (payload.kind === "flow") {
        const relevant = [...recordIds].map(id => recordById.get(id));
        const value = valueSummary(relevant);
        metadata(inspector, [["Known gross USD", value.usd === null ? "Unknown" : usd(value.usd), "graph-number"], ["Transfers without USD", value.unknownUsd],
          [(live ? "First observed transfer (UTC)" : "First recorded transfer (UTC)"), exactUtc(value.first)], [(live ? "Last observed transfer (UTC)" : "Last recorded transfer (UTC)"), exactUtc(value.last)]]);
        if (state.selected.kind === "edge") metadata(inspector, [["Asset", edges[0].asset_id],
          ["Amount", displayedAmount(relevant, edges[0].asset_id), "graph-number"], ["Total base units", value.base, "graph-number"]]);
        else {
          const counterparties = element("div", "graph-counterparties");
          counterparties.append(element("h4", "", live ? "Counterparties in the filtered observation" : "Counterparties in the filtered recording"));
          const ranked = edges.map(edge => ({edge, value: valueSummary(edgeRecords(payload, edge))}))
            .sort((a, b) => Number(b.value.usd || -1) - Number(a.value.usd || -1));
          for (const {edge, value} of ranked.slice(0, 5)) button(counterparties,
            (edge.from === state.selected.id ? "To " + edge.to : "From " + edge.from) + " · " + edge.asset_id + " · " +
            (value.usd === null ? "USD unknown" : usd(value.usd) + " known USD") + (value.unknownUsd ? " (some USD unavailable)" : ""),
            () => choose("edge", edge.id), "graph-counterparty");
          inspector.append(counterparties);
        }
      }
      renderRecords([...recordIds].map((id) => recordById.get(id)));
      inspector.querySelector("h3").setAttribute("tabindex", "-1");
    }

    function renderAccounts() {
      const matchingIds = new Set(state.matches.flatMap((edge) => [edge.from, edge.to]));
      const candidates = payload.nodes.filter((node) => (!state.asset && !state.provenance && !state.from && !state.to || matchingIds.has(node.id)) &&
        node.id.toLowerCase().includes(state.search.toLowerCase()));
      accountList.replaceChildren(element("p", "", candidates.length + (live ? " observed account" : " recorded account") + (candidates.length === 1 ? "" : "s")));
      const size = state.compact ? 4 : 12;
      state.accountPage = Math.min(state.accountPage, Math.max(0, Math.ceil(candidates.length / size) - 1));
      for (const node of candidates.slice(state.accountPage * size, (state.accountPage + 1) * size)) {
        const control = button(accountList, node.label, () => choose("node", node.id), "graph-account-button");
        control.setAttribute("aria-pressed", state.selected.kind === "node" && state.selected.id === node.id ? "true" : "false");
      }
      if (!candidates.length) accountList.append(element("p", "graph-empty", "No account matches this search and these filters."));
      pagination(accountList, state.accountPage, candidates.length, size, (page) => {
        state.accountPage = page; renderAccounts();
      });
    }

    function renderRelationships() {
      edgeList.replaceChildren(element("h4", "", "Relationships in this view"));
      if (!state.edges.length) edgeList.append(element("p", "graph-empty", (live ? "No observed relationships in this view." : "No recorded relationships in this view.")));
      const size = state.compact ? 4 : 12;
      state.edgePage = Math.min(state.edgePage, Math.max(0, Math.ceil(state.edges.length / size) - 1));
      for (const edge of state.edges.slice(state.edgePage * size, (state.edgePage + 1) * size)) {
        const control = button(edgeList, edgeLabel(payload, edge),
        () => choose("edge", edge.id), "graph-relationship-button");
        control.setAttribute("aria-pressed", state.selected.kind === "edge" && state.selected.id === edge.id ? "true" : "false");
      }
      pagination(edgeList, state.edgePage, state.edges.length, size, (page) => {
        state.edgePage = page; renderRelationships();
      });
    }

    svg.addEventListener("wheel", event => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      zoom(event.deltaY < 0 ? 0.8 : 1.25);
    }, {passive: false});
    let drag = null;
    svg.addEventListener("pointerdown", (event) => {
      if (event.pointerType !== "mouse" || event.button !== 0 || event.target !== svg) return;
      drag = { x: event.clientX, y: event.clientY, view: { ...state.view } };
      svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const bounds = svg.getBoundingClientRect();
      const scale = Math.max(state.view.width / bounds.width, state.view.height / bounds.height);
      state.view.x = drag.view.x - (event.clientX - drag.x) * scale;
      state.view.y = drag.view.y - (event.clientY - drag.y) * scale;
      applyView();
    });
    svg.addEventListener("pointerup", () => { drag = null; });
    svg.addEventListener("pointercancel", () => { drag = null; });
    const visibility = () => { if (document.hidden) drag = null; };
    document.addEventListener("visibilitychange", visibility);
    const resize = () => {
      if (state.compact !== compactQuery.matches) {
        const initial = state.limit === (state.compact ? 4 : 8);
        state.compact = compactQuery.matches;
        if (initial) state.limit = state.compact ? 4 : 8;
        arrangeControls(state.compact, controls);
        renderGraph(true); renderAccounts();
      } else frameSelected();
    };
    window.addEventListener("resize", resize);
    disposers.set(container, () => { document.removeEventListener("visibilitychange", visibility); window.removeEventListener("resize", resize); });
    renderGraph(true); renderInspector(); renderAccounts();
    views.set(container, () => ({asset: state.asset, provenance: state.provenance, from: state.from, to: state.to,
      selected: {...state.selected}, focus: state.focus, limit: state.limit, mode: state.mode, root: payload.root, kind: payload.kind,
      recordIds: [...new Set(state.matches.flatMap(edge => edge.record_ids))]}));
    rangeSetters.set(container, (from, to) => {
      state.from = from; state.to = to;
      timeControls.open = true;
      if (state.compact) filters.open = true;
      dateInputs.forEach((input, index) => { input.value = new Date(Number(BigInt(index ? to : from) / 1000000n)).toISOString().slice(0, 19); });
      rangeMessage.textContent = "Exact graph interval: " + exactUtc(from) + " until " + exactUtc(to) + ", end excluded.";
      changeFilter();
    });
    mounts.add(container);
    payloads.set(container, payload);
    return container;
  }

  async function load(container, restoreFocus = false) {
    if (mounts.has(container) || loading.has(container)) return container;
    const source = container.getAttribute("data-graph-src");
    if (!source) return mount(container);
    loading.add(container);
    container.setAttribute("aria-busy", "true");
    const liveSource = source.startsWith("/live/");
    const panel = element("div", "graph-loading");
    panel.setAttribute("role", "status");
    const heading = element("h2", "", "Recorded investigation");
    heading.setAttribute("tabindex", "-1");
    panel.append(heading, element("p", "", "Loading recorded investigation…"));
    container.replaceChildren(panel);
    if (restoreFocus) heading.focus({ preventScroll: true });
    let restoreAfterMount = false;
    const restoreTransfers = !restoreFocus && window.location.hash === "#transfers";
    let readerMoved = false;
    const readerMovement = () => { readerMoved = true; };
    const movementEvents = ["wheel", "touchmove", "keydown", "pointerdown"];
    if (restoreTransfers) movementEvents.forEach(name => window.addEventListener(name, readerMovement, {passive: true}));
    const controller = new AbortController();
    controllers.set(container, controller);
    let timeout;
    try {
      if (window.location.protocol === "file:") throw new Error("Serve this site over HTTP to load the recorded graph.");
      const {url, live, snapshot} = graphAddress(source);
      if (live) { heading.textContent = snapshot ? "Saved indexed observation" : "Live indexed neighborhood"; panel.children[1].textContent = snapshot ? "Loading saved bounded observation…" : "Reading bounded mainnet transfers…"; }
      timeout = setTimeout(() => controller.abort(), live ? 25000 : 15000);
      const response = await fetch(url.href, { signal: controller.signal, credentials: "same-origin" });
      if (!response.ok) throw new Error("The recorded dataset could not be loaded (HTTP " + response.status + ").");
      if (!(response.headers.get("content-type") || "").toLowerCase().includes("application/json")) {
        throw new Error("The recorded dataset response was not JSON.");
      }
      const payload = await response.json();
      if (!validPayload(payload)) throw new Error("The recorded dataset is incomplete.");
      const expectedAccount = snapshot ? container.getAttribute("data-graph-account") : url.pathname.split("/")[3];
      if (live && (payload.source.recorded !== false || payload.root !== expectedAccount)) throw new Error("The indexed neighborhood does not match the requested account.");
      if (controllers.get(container) !== controller) return container;
      restoreAfterMount = restoreFocus && document.activeElement === heading;
      mount(container, payload);
      if (restoreTransfers && !readerMoved && window.location.hash === "#transfers") {
        const transfers = document.getElementById("transfers");
        if (transfers) transfers.scrollIntoView();
      }
      if (restoreAfterMount) container.querySelector('.graph-mode-controls button[aria-pressed="true"]').focus({ preventScroll: true });
    } catch (error) {
      if (controllers.get(container) !== controller) return container;
      const focusRetry = restoreAfterMount || restoreFocus && document.activeElement === heading;
      container.replaceChildren(panel);
      panel.replaceChildren(element("h2", "", liveSource ? "Live neighborhood unavailable" : "Recorded investigation unavailable"),
        element("p", "", controller.signal.aborted ? "The dataset took too long to load. Try again." : error.message));
      const retry = element("button", "", "Retry loading graph");
      retry.type = "button";
      retry.addEventListener("click", () => load(container, true));
      panel.append(retry, element("p", "", liveSource ? "Account tables remain separate reads. An unavailable neighborhood does not establish absence of activity." : "Recorded evidence remains available on this page or through the recorded investigation links."));
      container.setAttribute("aria-busy", "false");
      if (focusRetry) retry.focus({ preventScroll: true });
    } finally {
      clearTimeout(timeout);
      if (restoreTransfers) movementEvents.forEach(name => window.removeEventListener(name, readerMovement));
      if (controllers.get(container) === controller) { controllers.delete(container); loading.delete(container); }
    }
    return container;
  }

  window.SiyabGraph = { mount: load, getView: container => views.has(container) ? views.get(container)() : null,
    getPayload: container => payloads.has(container) ? JSON.parse(JSON.stringify(payloads.get(container))) : null,
    destroy: container => {
      const controller = controllers.get(container); controllers.delete(container); if (controller) controller.abort();
      if (disposers.has(container)) disposers.get(container)();
      disposers.delete(container); payloads.delete(container); views.delete(container); rangeSetters.delete(container);
      recordIndexes.delete(container); mounts.delete(container); loading.delete(container);
    } };
  function mountTimelines() {
    document.querySelectorAll("[data-recorded-timeline]").forEach(chart => {
      const root = chart.getAttribute("data-recorded-timeline");
      if (!root || timelineMounts.has(chart)) return;
      const bars = [...chart.querySelectorAll("[data-bin-index]")];
      if (!bars.length) return;
      const from = chart.querySelector("[data-chart-from]"), through = chart.querySelector("[data-chart-through]");
      const readout = chart.querySelector(".chart-readout"), selection = chart.querySelector(".chart-selection");
      const crosshair = chart.querySelector(".chart-crosshair"), inspect = chart.querySelector("[data-chart-inspect]");
      const selectRange = () => {
        const first = Number(from.value), last = Number(through.value);
        inspect.disabled = last < first;
        bars.forEach((bar, index) => bar.classList.toggle("is-outside-range", index < first || index > last));
        if (last < first) { selection.textContent = "Choose an ending interval after the starting interval."; return; }
        const count = bars.slice(first, last + 1).reduce((total, bar) => total + Number(bar.getAttribute("data-chart-count")), 0);
        selection.textContent = count + (count === 1 ? " root outbound transfer selected. " : " root outbound transfers selected. ") + exactUtc(bars[first].getAttribute("data-chart-start")) +
          " until " + exactUtc(bars[last].getAttribute("data-chart-end")) + ", end excluded.";
      };
      bars.forEach((bar, index) => {
        const reveal = () => {
          readout.textContent = bar.getAttribute("aria-label");
          crosshair.setAttribute("x1", String(index * 40 + 20)); crosshair.setAttribute("x2", String(index * 40 + 20)); crosshair.removeAttribute("hidden");
        };
        const choose = () => { from.value = String(index); through.value = String(index); selectRange(); reveal(); };
        bar.addEventListener("pointerenter", reveal); bar.addEventListener("focus", reveal); bar.addEventListener("click", choose);
        bar.addEventListener("keydown", event => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
          else if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
            event.preventDefault(); const next = event.key === "Home" ? 0 : event.key === "End" ? bars.length - 1 :
              Math.max(0, Math.min(bars.length - 1, index + (event.key === "ArrowLeft" ? -1 : 1)));
            bars[next].focus();
          }
        });
      });
      from.addEventListener("change", selectRange); through.addEventListener("change", selectRange);
      inspect.addEventListener("click", () => {
        const graph = [...document.querySelectorAll("[data-siyab-graph]")].find(container => {
          const view = window.SiyabGraph.getView(container); return view && view.kind === "flow" && view.root === root;
        });
        if (!graph || !rangeSetters.has(graph)) { selection.textContent = "The recorded graph must finish loading before this interval can be inspected."; return; }
        rangeSetters.get(graph)(bars[Number(from.value)].getAttribute("data-chart-start"), bars[Number(through.value)].getAttribute("data-chart-end"));
        selection.textContent += " Applied to the recorded graph. Dataset charts retain their full recording scope.";
      });
      chart.querySelector(".chart-range-controls").hidden = false;
      selectRange(); timelineMounts.add(chart);
    });
  }
  function mountAll() {
    document.querySelectorAll("[data-siyab-graph]").forEach(container => load(container));
    mountTimelines();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mountAll, { once: true });
  else mountAll();
})();
