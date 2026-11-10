const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const window={};vm.runInNewContext(fs.readFileSync('intel/ui/number-format.js','utf8'),{window});
const n=window.SiyabNumbers;
assert.equal(n.decimal('1234567.123456789012345',2),'1,234,567.12');
assert.equal(n.decimal('999999999999999999.995',2),'1,000,000,000,000,000,000');
assert.equal(n.decimal('0.000000000000000001',2),'<0.01');
assert.equal(n.decimal('0',2),'0');
assert.equal(n.decimal('-1234.125',2),'-1,234.13');
assert.equal(n.decimal('1.23e4',2),'12,300');
assert.equal(n.baseUnits('12345678901234567890123456789',24),'12,345.6789');
assert.equal(n.baseUnits('1',24),'<0.0001');
assert.equal(n.baseUnits('1',255),'<0.0001');
assert.equal(n.baseUnits('340282366920938463463374607431768211455',24),'340,282,366,920,938.4635');
for(const value of [null,'NaN','Infinity','<script>','1e9999'])assert.equal(n.decimal(value,2),'Unavailable');
const workspace=fs.readFileSync('intel/ui/workspace.js','utf8');
const extracted=workspace.slice(workspace.indexOf('  const csv ='),workspace.indexOf('  function viewUrl()'));
let download;
const cell={getAttribute:name=>name==='data-export-value'?'12345678901234567890123456789 yoctoNEAR':null,
  cloneNode(){throw new Error('Exact export value must take precedence over rounded display');}};
const context={document:{getElementById:()=>({querySelectorAll:()=>[{hidden:false,querySelectorAll:()=>[cell]}]})},
  download:(name,text)=>{download=text;},message(){}};
vm.runInNewContext(extracted+';globalThis.run=tableExport;',context);context.run('feed-signals');
assert.equal(download,'"12345678901234567890123456789 yoctoNEAR"\r\n');
cell.getAttribute=()=> '=1+1';context.run('feed-signals');assert.equal(download,'"\'=1+1"\r\n');
const graph=fs.readFileSync('intel/ui/graphs.js','utf8').replace('  window.SiyabGraph =','  window.numberGraphTest = {edgeLabel};\n  window.SiyabGraph =');
vm.runInNewContext(graph,{window,document:{readyState:'loading',addEventListener(){}}});
const payload={kind:'flow',source:{recorded:true},records:[{id:'r1',usd_amount:'1234567.123456789012345',human_amount:null,amount:'1',block_timestamp:'1'}]};
const label=window.numberGraphTest.edgeLabel(payload,{from:'a.near',to:'b.near',asset_id:'native:near',provenance:'onchain',record_ids:['r1']});
assert.match(label,/1,234,567\.12 known USD/);assert.equal(payload.records[0].usd_amount,'1234567.123456789012345');
const domEvents={},controls=new Map();let reads=0;
const document={readyState:'loading',getElementById(id){if(!controls.has(id))controls.set(id,{value:'',addEventListener(){},setAttribute(){}});return controls.get(id);},addEventListener(name,fn){domEvents[name]=fn;}};
vm.runInNewContext(fs.readFileSync('intel/ui/feed.js','utf8'),{window:{...window,addEventListener(){}},document,AbortController,
  fetch(){reads++;return new Promise(()=>{});},setTimeout(){return 1;},clearTimeout(){}});
assert.equal(reads,0,'Inline feed waits until deferred numeric formatter is ready');
domEvents.DOMContentLoaded();assert.equal(reads,1);
console.log('Exact string rounding, grouping, tiny values and invalid numeric data passed.');
