const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const window = {location: new URL('https://siyab.xyz/flows/example.html')};
const source = fs.readFileSync('intel/ui/graphs.js', 'utf8').replace(
  '  window.SiyabGraph =',
  '  window.vizTest = {decimalSum, valueSummary, filteredEdges, restoredView, exactUtc, arrangeControls};\n  window.SiyabGraph ='
);
const document = {readyState: 'loading', addEventListener() {}};
vm.runInNewContext(source, {window, URL, document});
const {decimalSum, valueSummary, filteredEdges, restoredView, exactUtc} = window.vizTest;
assert.equal(decimalSum(['9007199254740993.000000000001', '0.000000000009']), '9007199254740993.00000000001');
assert.equal(decimalSum(['1e-8', '0.00000001']), '0.00000002');
assert.equal(decimalSum(['-1.20', '1.2']), '0');
assert.throws(() => decimalSum(['unavailable']), /could not be read/);
assert.throws(() => decimalSum(['1e9999999']), /outside/);

const first = {id: 'record-0', block_timestamp: '1776237164627685607', usd_amount: '1.000000000000000000000001',
  human_amount: '0.000000000000000000000001', amount: '9007199254740993123456789'};
const second = {...first, id: 'record-1', block_timestamp: '1776237164627685608', usd_amount: null, human_amount: null};
const payload = {kind: 'flow', root: 'root.near', nodes: [{id: 'root.near'}, {id: 'a.near'}],
  records: [first, second], edges: [{id: 'edge-0', asset_id: 'native:near', provenance: 'onchain',
    from: 'root.near', to: 'a.near', record_ids: ['record-0', 'record-1']}]};
const before = JSON.stringify(payload);
const summary = valueSummary(payload.records);
assert.equal(summary.usd, '1.000000000000000000000001');
assert.equal(summary.unknownUsd, 1);
assert.equal(summary.human, null, 'A missing human amount must not become zero or a false total');
assert.equal(summary.base, '18014398509481986246913578');
assert.equal(summary.first, first.block_timestamp); assert.equal(summary.last, second.block_timestamp);
assert.equal(valueSummary([second]).usd, null, 'Unknown USD must not become zero');
assert.equal(exactUtc(first.block_timestamp), '2026-04-15 07:12:44.627685607 UTC');
const selected = filteredEdges(payload, '', '', first.block_timestamp, second.block_timestamp);
assert.equal(selected.length, 1); assert.equal(selected[0].record_ids.join(','), 'record-0');
assert.equal(filteredEdges(payload, '', '', second.block_timestamp, '1776237164627685609')[0].record_ids.join(','), 'record-1');
assert.equal(filteredEdges(payload, 'unknown-asset', '').length, 0);
assert.equal(JSON.stringify(payload), before, 'Time filtering must not mutate the full recorded dataset');

window.location = new URL('https://siyab.xyz/?g_asset=native%3Anear&g_node=a.near&g_focus=a.near&g_from=1776237164627685607&g_to=1776237164627685608&g_limit=20&g_view=list');
const view = restoredView(payload, false);
assert.equal(view.asset, 'native:near'); assert.equal(view.selected.id, 'a.near');
assert.equal(view.from, first.block_timestamp); assert.equal(view.to, second.block_timestamp); assert.equal(view.limit, 20);
window.location = new URL('https://siyab.xyz/?g_asset=javascript%3Aalert(1)&g_node=unknown&g_from=9999999999999999999999&g_to=bad&g_limit=999&g_view=bad');
assert.equal(Object.keys(restoredView(payload, true)).length, 0);
window.location = new URL('https://siyab.xyz/?g_root=other.near&g_view=list');
assert.equal(Object.keys(restoredView(payload, false)).length, 0);
window.location = new URL('https://siyab.xyz/?g_from=10&g_to=1');
assert.equal(restoredView(payload, false).from, undefined);

class Tree {
  constructor(name) { this.name = name; this.children = []; this.parent = null; this.focuses = 0; }
  detach() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
  append(...nodes) { for (const node of nodes) { node.detach(); this.children.push(node); node.parent = this; } }
  prepend(...nodes) { for (const node of nodes) node.detach(); this.children.unshift(...nodes); for (const node of nodes) node.parent = this; }
  replaceChildren(...nodes) { for (const child of this.children) child.parent = null; this.children = []; this.append(...nodes); }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  insertBefore(node, sibling) { node.detach(); const index = this.children.indexOf(sibling); assert.ok(index >= 0); this.children.splice(index, 0, node); node.parent = this; }
  focus() { document.activeElement = this; this.focuses++; }
}
const names = ['container', 'toolbar', 'assetLabel', 'evidenceLabel', 'modeControls', 'expandButton', 'zoomIn', 'zoomOut',
  'fitControl', 'viewControls', 'panControls', 'shareControl', 'save', 'exportView', 'filters', 'actions', 'actionPanel', 'timeControls', 'legend'];
const controls = Object.fromEntries(names.map(name => [name, new Tree(name)]));
const originalPan = ['Pan left', 'Pan right', 'Pan up', 'Pan down', 'Reset'].map(name => new Tree(name));
controls.panControls.append(...originalPan); controls.viewControls.append(controls.panControls);
controls.filters.append(new Tree('Filters summary')); controls.actions.append(new Tree('Actions summary'), controls.actionPanel);
controls.toolbar.append(controls.assetLabel, controls.evidenceLabel, controls.modeControls, controls.expandButton,
  controls.zoomIn, controls.zoomOut, controls.fitControl, controls.viewControls, controls.shareControl, controls.save, controls.exportView);
controls.container.append(controls.toolbar, controls.timeControls, controls.legend);
const originalControls = [controls.assetLabel, controls.evidenceLabel, controls.modeControls, controls.expandButton,
  controls.zoomIn, controls.zoomOut, controls.fitControl, controls.shareControl, controls.save, controls.exportView, controls.timeControls, ...originalPan];
controls.assetLabel.focus();
window.vizTest.arrangeControls(true, controls);
assert.equal(controls.toolbar.children.length, 5, 'Phone toolbar must expose view, expansion and three disclosures rather than every secondary action');
assert.deepEqual(controls.toolbar.children.map(node => node.name), ['modeControls', 'expandButton', 'filters', 'viewControls', 'actions']);
assert.equal(controls.filters.open, true, 'A focused filter must remain visible when moved on resize');
assert.equal(document.activeElement, controls.assetLabel); assert.equal(controls.assetLabel.focuses, 2);
for (const control of originalControls) assert.equal(controls.container.contains(control), true, control.name + ' must be retained');
assert.deepEqual(controls.panControls.children.slice(0, 3), [controls.zoomIn, controls.zoomOut, controls.fitControl]);
controls.save.focus(); window.vizTest.arrangeControls(false, controls);
assert.equal(controls.toolbar.children.length, 11, 'Desktop retains directly available controls');
assert.equal(controls.timeControls.parent, controls.container);
assert.deepEqual(controls.panControls.children, originalPan);
assert.equal(document.activeElement, controls.save);
for (const control of originalControls) assert.equal(controls.container.contains(control), true, control.name + ' must survive a roundtrip resize');
