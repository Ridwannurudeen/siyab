(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const mounts = new WeakSet();
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

  function filteredEdges(payload, asset, provenance) {
    return payload.edges.filter(
      (edge) =>
        (!asset || edge.asset_id === asset) &&
        (!provenance || edge.provenance === provenance),
    );
  }

  function edgeLabel(payload, edge) {
    return edge.from + " to " + edge.to + " · " +
      (payload.kind === "flow" ? edge.asset_id : edge.rule) + " · " +
      (edge.provenance === "inferred" ? "Attribution hypothesis" : "Recorded chain reads") + " · " +
      edge.record_ids.length + " recorded " + (payload.kind === "flow" ? "transfers" : "relationships");
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

  function recordingCoverage(payload) {
    if (payload.kind === "flow") {
      const reason = payload.coverage.completeness === "complete" ? "" :
        " at " + payload.coverage.completeness.reason;
      return "Recording: depth " + payload.bounds.reached_depth + " of " + payload.bounds.requested_depth +
        " requested; " + (payload.coverage.truncated ? "truncated" + reason : "acquisition complete") + ".";
    }
    return "Recording: " + payload.bounds.seeds.length + " seeds. " + payload.coverage.standard;
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

  function mount(container) {
    if (mounts.has(container)) return container;
    const script = container.querySelector('script[type="application/json"]') ||
      (container.nextElementSibling && container.nextElementSibling.matches('script[type="application/json"]') ?
        container.nextElementSibling : null);
    let payload;
    try {
      payload = JSON.parse(script ? script.textContent : "null");
    } catch (error) {
      container.append(element("p", "graph-empty", "The recorded graph data could not be read. Open a recorded investigation from the page links."));
      return null;
    }
    if (!validPayload(payload)) {
      container.append(element("p", "graph-empty", "The recorded graph data is incomplete. Open a recorded investigation from the page links."));
      return null;
    }
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
      path: null, view: null, visible: [], edges: [], matches: [],
    };
    container.classList.add("siyab-graph");
    const toolbar = element("div", "graph-toolbar");
    toolbar.setAttribute("aria-label", "Recorded graph controls");

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

    const assetControl = select("Asset", [["", "All recorded assets"],
      ...[...new Set(payload.edges.map((edge) => edge.asset_id).filter(Boolean))].sort().map((asset) => [asset, asset])],
    (value) => { state.asset = value; changeFilter(); });
    if (payload.kind === "cluster") assetControl.parentElement.hidden = true;
    const provenanceControl = select("Evidence", [["", "All evidence"],
      ...[...new Set(payload.edges.map((edge) => edge.provenance))].sort().map((value) =>
        [value, value === "inferred" ? "Attribution hypothesis" : "Recorded chain reads"])],
    (value) => { state.provenance = value; changeFilter(); });
    const expandButton = button(toolbar, "Expand neighborhood", () => {
      state.limit = Math.min(payload.kind === "cluster" ? payload.total_nodes : 72, state.limit + 12); renderGraph(true);
    });
    button(toolbar, "Zoom in", () => zoom(0.8));
    button(toolbar, "Zoom out", () => zoom(1.25));
    button(toolbar, "Fit", () => {
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
      pathStatus.textContent = "";
      renderGraph(true); renderInspector(); renderAccounts();
    });
    viewControls.append(panControls); toolbar.append(viewControls);
    const status = element("p", "graph-status");
    status.setAttribute("role", "status");
    const workspace = element("div", "graph-workspace");
    const stage = element("div", "graph-stage");
    const svg = svgElement("svg", { class: "graph-svg", role: "group", "aria-label":
      payload.kind === "flow" ? "Recorded transfer graph. Select accounts with the arrow keys, or use the account list." :
        "Recorded relationship graph. Dashed lines are attribution hypotheses. Use the account list for full names." });
    const defs = svgElement("defs", {});
    const marker = svgElement("marker", { id: prefix + "-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse", markerUnits: "strokeWidth" });
    marker.append(svgElement("path", { d: "M 0 1 L 9 5 L 0 9 z", class: "graph-arrow" }));
    defs.append(marker);
    const drawing = svgElement("g", {});
    svg.append(defs, drawing);
    const empty = element("p", "graph-empty", "No recorded edges match these filters. Change a filter or reset the graph.");
    empty.hidden = true;
    const pathStatus = element("p", "graph-path-status");
    pathStatus.setAttribute("role", "status");
    stage.append(svg, empty, pathStatus);
    const inspector = element("aside", "graph-inspector");
    inspector.setAttribute("aria-label", "Selected graph evidence");
    workspace.append(stage, inspector);

    const list = element("details", "graph-list");
    list.append(element("summary", "", "Accounts and relationships"));
    const searchLabel = element("label", "graph-search", "Find an account in this recording");
    const search = element("input");
    search.type = "search"; search.autocomplete = "off";
    search.addEventListener("input", () => {
      state.search = search.value; state.accountPage = 0; renderAccounts();
    });
    searchLabel.append(search);
    const accountList = element("div", "graph-account-list");
    const edgeList = element("div", "graph-relationship-list");
    list.append(searchLabel, accountList, edgeList);
    const acquisition = element("p", "graph-source", recordingCoverage(payload));
    const source = element("p", "graph-source", "Recorded mainnet data · " + payload.source.provider +
      " · Read " + payload.source.fetched_at + ". ");
    if (payload.source.evidence_url && /^\/(?:flows|clusters)\/[a-zA-Z0-9_.%-]+\.html$/.test(payload.source.evidence_url)) {
      const evidenceLink = element("a", "", "Open recorded evidence and limits");
      evidenceLink.href = payload.source.evidence_url;
      source.append(evidenceLink);
    }
    container.append(toolbar, status, acquisition, workspace, list, source);

    function changeFilter() {
      state.path = null; state.recordPage = 0; state.edgePage = 0; state.accountPage = 0;
      pathStatus.textContent = "";
      if (state.selected.kind === "edge" && !filteredEdges(payload, state.asset, state.provenance)
        .some((edge) => edge.id === state.selected.id)) state.selected = { kind: "node", id: state.focus };
      const matches = filteredEdges(payload, state.asset, state.provenance);
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
      inspector.querySelector("h3").focus({ preventScroll: true });
    }

    function applyView() {
      svg.setAttribute("viewBox", [state.view.x, state.view.y, state.view.width, state.view.height].join(" "));
    }

    function fit() {
      if (!state.geometry) return;
      state.view = { x: 0, y: 0, width: state.geometry.width, height: state.geometry.height };
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
      state.matches = filteredEdges(payload, state.asset, state.provenance);
      state.visible = neighborhood(payload, state.matches, state.focus, state.limit, state.path ? state.path.nodes : []);
      const ids = new Set(state.visible);
      state.edges = state.matches.filter((edge) => ids.has(edge.from) && ids.has(edge.to));
      const visibleRecords = new Set(state.edges.flatMap((edge) => edge.record_ids));
      const matchingRecords = new Set(state.matches.flatMap((edge) => edge.record_ids));
      status.textContent = "Display: " + state.visible.length + " of " + payload.total_nodes + " accounts shown · " +
        visibleRecords.size + " of " + payload.total_records + " recorded " +
        (payload.kind === "flow" ? "transfers" : "relationships") + " shown · " + state.edges.length + " edges" +
        (state.asset || state.provenance ? " · " + matchingRecords.size + " records match filters" : "");
      expandButton.hidden = false;
      expandButton.disabled = state.limit >= 72 || state.visible.length < state.limit || state.visible.length === payload.total_nodes;
      if (state.limit >= 72) expandButton.textContent = "72-account view limit";
      else expandButton.textContent = "Expand neighborhood";
      state.geometry = layout(payload, state.visible, state.edges, state.focus, state.compact);
      drawing.replaceChildren();
      empty.hidden = state.edges.length > 0;
      const pairs = new Map();
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
        const hit = svgElement("path", { d, class: "graph-edge-hit", "aria-hidden": "true" });
        hit.addEventListener("click", () => choose("edge", edge.id));
        const title = svgElement("title", {});
        title.textContent = edgeLabel(payload, edge);
        group.append(title, line, hit); drawing.append(group);
      }
      state.visible.forEach((id, index) => {
        const node = nodeById.get(id); const position = state.geometry.positions.get(id);
        const selected = state.selected.kind === "node" && state.selected.id === id;
        const group = svgElement("g", { transform: "translate(" + position.x + " " + position.y + ")",
          class: "graph-node" + (id === payload.root ? " is-root" : "") + (selected ? " is-selected" : "") +
            (id === state.focus ? " is-focused" : ""),
          role: "button", tabindex: selected ||
            (!(state.selected.kind === "node" && ids.has(state.selected.id)) && index === 0) ? "0" : "-1",
          "aria-label": node.label + (node.depth !== null ? ", depth " + node.depth : "") + ", inspect recorded evidence",
          "aria-pressed": selected ? "true" : "false", "data-node-id": id });
        const title = svgElement("title", {}); title.textContent = node.label;
        const body = svgElement("rect", { x: -105, y: -24, width: 210, height: 48, rx: 5, class: "graph-node-body" });
        const label = svgElement("text", { x: 0, y: -2, "text-anchor": "middle", class: "graph-node-label" });
        label.textContent = shortLabel(node.label);
        const detail = svgElement("text", { x: 0, y: 15, "text-anchor": "middle", class: "graph-node-detail" });
        detail.textContent = id === payload.root ? "Recording root" : payload.kind === "flow" ?
          "Recorded depth " + node.depth : "Recorded account";
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
      inspector.append(element("h4", "", records.length + " recorded " + (payload.kind === "flow" ? "transfers" : "relationships")));
      const rows = element("div", "graph-record-list");
      const pageRecords = records.slice(state.recordPage * 10, (state.recordPage + 1) * 10);
      for (const record of pageRecords) {
        const details = element("details", "graph-record");
        const heading = payload.kind === "flow" ?
          (record.human_amount === null ? record.amount + " base units" :
            new Intl.NumberFormat(undefined, { maximumSignificantDigits: 12 }).format(Number(record.human_amount))) + " · " + record.asset_id :
          record.rule + " · " + (record.provenance === "inferred" ? "Hypothesis" : "Chain read");
        details.append(element("summary", "", heading));
        if (payload.kind === "flow") {
          metadata(details, [["From", record.from, "graph-identifier"], ["To", record.to, "graph-identifier"], ["Asset", record.asset_id, "graph-identifier"],
            ["Human amount as recorded", record.human_amount, "graph-identifier"], ["Amount (base units)", record.amount, "graph-identifier"],
            ["USD at provider", record.usd_amount === null ? "Unknown" : record.usd_amount, "graph-identifier"],
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
      if (!records.length) rows.append(element("p", "graph-empty", "No recorded evidence matches these filters for this account."));
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
        button(actions, payload.kind === "flow" ? "Show recorded path" : "Show relationship path", () => {
          state.path = recordedPath(payload, state.matches, node.id);
          pathStatus.textContent = state.path ? state.path.edges.length + " recorded " +
            (payload.kind === "flow" ? "transfer" : "relationship") + " edges connect the recording root to this account. " +
            (payload.kind === "flow" ? "This is an account path, not proof of the same funds or chronological causality." :
              "A relationship path can include attribution hypotheses; it does not establish shared ownership.") :
            "No " + (payload.kind === "flow" ? "directed transfer" : "relationship") + " path from the recording root matches these filters.";
          renderGraph(true);
        });
        if (node.url && /^(?:\.\.\/|\/)accounts\/[a-zA-Z0-9_.%-]+\.html$/.test(node.url)) {
          const link = element("a", "graph-account-link", "Open account"); link.href = node.url; actions.append(link);
        }
        inspector.append(actions);
        edges = state.matches.filter((edge) => edge.from === node.id || edge.to === node.id);
        metadata(inspector, [["Account", node.id, "graph-identifier"], ...(payload.kind === "flow" ? [["Recorded depth", node.depth]] :
          [["Recording root", payload.root, "graph-identifier"]]), ["Matching edges", edges.length],
          ["Scope", "All matching recorded edges for this account, including outside the visible neighborhood"]]);
      } else {
        const edge = payload.edges.find((candidate) => candidate.id === state.selected.id);
        inspector.append(element("h3", "graph-inspector-title", payload.kind === "flow" ? "Recorded transfers" : "Recorded relationship"));
        const fromButton = button(inspector, edge.from, () => choose("node", edge.from), "graph-account-button");
        fromButton.setAttribute("aria-label", "Inspect sender " + edge.from);
        const toButton = button(inspector, edge.to, () => choose("node", edge.to), "graph-account-button");
        toButton.setAttribute("aria-label", "Inspect receiver " + edge.to);
        metadata(inspector, [["Provenance", edge.provenance === "inferred" ? "Attribution hypothesis" : "Recorded chain reads"],
          ["Confidence", edge.confidence], ...(edge.asset_id ? [["Asset", edge.asset_id, "graph-identifier"]] : [])]);
        edges = [edge];
      }
      const recordIds = new Set(edges.flatMap((edge) => edge.record_ids));
      renderRecords([...recordIds].map((id) => recordById.get(id)));
      inspector.querySelector("h3").setAttribute("tabindex", "-1");
    }

    function renderAccounts() {
      const matchingIds = new Set(state.matches.flatMap((edge) => [edge.from, edge.to]));
      const candidates = payload.nodes.filter((node) => (!state.asset && !state.provenance || matchingIds.has(node.id)) &&
        node.id.toLowerCase().includes(state.search.toLowerCase()));
      accountList.replaceChildren(element("p", "", candidates.length + " recorded accounts"));
      for (const node of candidates.slice(state.accountPage * 12, (state.accountPage + 1) * 12)) {
        const control = button(accountList, node.label, () => choose("node", node.id), "graph-account-button");
        control.setAttribute("aria-pressed", state.selected.kind === "node" && state.selected.id === node.id ? "true" : "false");
      }
      if (!candidates.length) accountList.append(element("p", "graph-empty", "No account matches this search and these filters."));
      pagination(accountList, state.accountPage, candidates.length, 12, (page) => {
        state.accountPage = page; renderAccounts();
      });
    }

    function renderRelationships() {
      edgeList.replaceChildren(element("h4", "", "Relationships in this view"));
      if (!state.edges.length) edgeList.append(element("p", "graph-empty", "No recorded relationships in this view."));
      state.edgePage = Math.min(state.edgePage, Math.max(0, Math.ceil(state.edges.length / 12) - 1));
      for (const edge of state.edges.slice(state.edgePage * 12, (state.edgePage + 1) * 12)) {
        const control = button(edgeList, edgeLabel(payload, edge),
        () => choose("edge", edge.id), "graph-relationship-button");
        control.setAttribute("aria-pressed", state.selected.kind === "edge" && state.selected.id === edge.id ? "true" : "false");
      }
      pagination(edgeList, state.edgePage, state.edges.length, 12, (page) => {
        state.edgePage = page; renderRelationships();
      });
    }

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
    document.addEventListener("visibilitychange", () => { if (document.hidden) drag = null; });
    window.addEventListener("resize", () => {
      if (state.compact !== compactQuery.matches) {
        const initial = state.limit === (state.compact ? 4 : 8);
        state.compact = compactQuery.matches;
        if (initial) state.limit = state.compact ? 4 : 8;
        renderGraph(true);
      } else frameSelected();
    });
    mounts.add(container);
    renderGraph(true); renderInspector(); renderAccounts();
    return container;
  }

  window.SiyabGraph = { mount };
  function mountAll() {
    document.querySelectorAll("[data-siyab-graph]").forEach(mount);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mountAll, { once: true });
  else mountAll();
})();
