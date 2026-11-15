import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class LiveSiteClientTest(unittest.TestCase):
    def run_script(self, script):
        if not shutil.which("node"):
            self.skipTest("JavaScript runtime is unavailable")
        result = subprocess.run([shutil.which("node"), "-e", script], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_search_match_pluralization(self):
        source = (ROOT / "intel/ui/shell.js").read_text(encoding="utf-8")
        expression = source.split("const message = ", 1)[1].split(";\n", 1)[0]
        self.run_script("const assert = require('node:assert/strict');"
                        "const failed=false,entries=[],query='account';"
                        "for(const count of [1,2]){const matches=Array(count);"
                        "const message=" + expression + ";"
                        "assert.equal(message,count+' recorded '+(count===1?'match':'matches')+'; showing up to 30.');}")

    def test_graph_account_pluralization(self):
        source = (ROOT / "intel/ui/graphs.js").read_text(encoding="utf-8")
        expression = source.split('accountList.replaceChildren(element("p", "", ', 1)[1].split("));", 1)[0]
        self.run_script("const assert = require('node:assert/strict');"
                        "for(const live of [true,false])for(const count of [0,1,2]){const candidates=Array(count);"
                        "assert.equal(" + expression + ",count+(live?' observed account':' recorded account')+(count===1?'':'s'));}")

    def test_valuation_reports_price_balance_and_decimals_separately(self):
        source = (ROOT / "intel/ui/portfolio.js").read_text(encoding="utf-8")
        summary = "function summary(" + source.split("function summary(", 1)[1].split("  function renderValuation", 1)[0]
        self.run_script("""
const assert = require('node:assert/strict');
const node=(tag,text)=>({text:text||'',children:[],style:{},append(...items){this.children.push(...items);},setAttribute(){}});
const usd=value=>node('span',value),utcLabel=()=>'';
const text=item=>item.text+' '+item.children.map(text).join(' ');
""" + summary + """
const asset=(price,status)=>({price:{status:price},status,value_usd:null});
const render=assets=>text(summary({assets,summary:{known_value_usd:null,total_value_usd:null,priced_assets:0,unpriced_assets:assets.length},coverage:{inventory_complete:true}},'current'));
const balance=render([asset('known','balance_unavailable')]);
assert.match(balance,/1 of 1 asset has a price but no readable balance/);
assert.doesNotMatch(balance,/no usable price/);
assert.match(balance,/No asset in this read has an established USD value[.]/);
const missing=render([asset('missing','price_unavailable')]);
assert.match(missing,/1 of 1 asset has no usable price/);
assert.match(missing,/No asset in this read has a usable price/);
const mixed=render([asset('known','balance_unavailable'),asset('missing','balance_unavailable')]);
assert.match(mixed,/1 of 2 assets has no usable price/);
assert.match(mixed,/1 of 2 assets has a price but no readable balance/);
assert.match(mixed,/Assets with unreadable balances[ ]+2/);
assert.doesNotMatch(mixed,/No asset in this read has a usable price/);
const decimals=render([asset('known','decimals_unavailable')]);
assert.match(decimals,/1 of 1 asset has a price but no usable token decimals/);
assert.doesNotMatch(decimals,/no readable balance|no usable price/);
assert.doesNotMatch(decimals,/No asset in this read has a usable price[.]/);
assert.match(decimals,/No asset in this read has an established USD value[.]/);
""")

    def test_pause_stops_feed_reads_and_resume_requests_a_current_response(self):
        source = (ROOT / "intel/ui/feed.js").read_text(encoding="utf-8")
        handler = source.split('  pause.addEventListener("click", ', 1)[1].split('  document.addEventListener("visibilitychange"', 1)[0]
        self.run_script("""
const assert=require('node:assert/strict');
let paused=false,generation=0,timer=17,cleared=[],aborted=0,reads=0;
const pause={setAttribute(name,value){this[name]=value;}},panel={setAttribute(name,value){this[name]=value;}},updateStatus={};
const clearTimeout=value=>cleared.push(value),controller={abort(){aborted++;}},poll=()=>reads++;
let click;
const control={addEventListener(name,fn){click=fn;}};
control.addEventListener('click', """ + handler + """
click();assert.equal(paused,true);assert.equal(reads,0);assert.equal(aborted,1);assert.equal(generation,1);
assert.equal(pause['aria-pressed'],'true');assert.equal(panel['aria-busy'],'false');assert.deepEqual(cleared,[17]);
click();assert.equal(paused,false);assert.equal(reads,1);assert.equal(aborted,2);assert.equal(generation,2);
assert.equal(pause.textContent,'Pause updates');assert.equal(updateStatus.textContent,'Reading current response');
""")
        docs = (ROOT / "docs/WORKSPACES.md").read_text(encoding="utf-8")
        self.assertIn("Pause updates stops feed reads on that page; Resume updates requests a current response.", docs)

    def test_counted_client_copy_uses_singular_nouns(self):
        for filename, markers in (
            ("feed.js", ("${matches.length} of ${current.events.length}", "lag ${stats.lag_blocks")),
            ("profiles.js", ("${page * 20 + 1}", "${data.access_keys}", "Showing ${matches.length", "${counts.size}")),
            ("index.js", ("${entry.provenance} source: rule", "${data.access_keys}")),
            ("cloud-workspace.js", ("Import ${value.cases.length}", "${totals.created_cases}", "${completed} of ${batches.length}", "${monitoring.indexed}", "${monitoring.retry_pending}", "${polling.initialized_accounts}")),
            ("investigation-jobs.js", ("${job.counts.pages}",)),
            ("tables.js", ("${Math.min(page * 50", "${visible.length}")),
        ):
            source = (ROOT / "intel/ui" / filename).read_text(encoding="utf-8")
            for marker in markers:
                with self.subTest(filename=filename, marker=marker):
                    before, after = source.split(marker, 1)
                    expression = "`" + before.rsplit("`", 1)[1] + marker + after.split("`", 1)[0] + "`"
                    self.run_script(r"""
const assert=require('node:assert/strict');
for(const count of [0,1,2])for(const kind of ['stated','inferred']){
const matches=Array.from({length:count},()=>({kind})),matching=matches,visible=matches,rows=matches,matchingCount=count,counts={size:count},page=0,completed=count,batches=Array(count);
const current={events:matches},stats={start_height:1,last_height:1,lag_blocks:count};
const data={access_keys:count,read_at_block:1},entry={provenance:'recorded'},source={rule_id:'rule',inputs:matches};
const value={cases:matches,watchlist:matches},state={team:{name:'Team'}};
const totals={created_cases:count,added_watches:count,skipped_cases:count},monitoring={indexed:count,pending:count,retry_pending:count},polling={initialized_accounts:count,monitored_accounts:count};
const job={counts:{pages:count,matched_rows:count,scanned_rows:count},bounds:{max_pages:2,max_rows:2}};
const rendered=""" + expression + r""";
assert.doesNotMatch(rendered,/\b1 (?:access keys|input assertions|matching records|matching recorded rows|matching assertions|matching source assertions|counterparties|rows|investigations|watched accounts|watches|duplicate investigations|batches|evidence files|events|blocks)\b/);
}
""")

    def test_old_feed_counterparty_uses_only_exact_stored_outflow_details(self):
        source = (ROOT / "intel/ui/feed.js").read_text(encoding="utf-8")
        start = source.find("  const counterparty = ")
        self.assertGreaterEqual(start, 0)
        helper = source[start:].split("  const utc = ", 1)[0]
        self.run_script(r"""
const assert=require('node:assert/strict');
""" + helper + r"""
const event={signal:'full_access_key_then_outflow',subject:'alice.near',drain_kind:'transfer',detail:'full access key recorded-key added to alice.near; transfer of 12 yoctoNEAR to receiver.near within 34 ms'};
const before=JSON.stringify(event);assert.equal(counterparty(event),'receiver.near');assert.equal(JSON.stringify(event),before);
const deleted={...event,drain_kind:'delete_account',detail:'full access key recorded-key added to alice.near; DeleteAccount to receiver.near within 34 ms'};
assert.equal(counterparty(deleted),'receiver.near');
for(const changed of [{subject:'other.near'},{signal:'other'},{drain_kind:'other'},{detail:event.detail+' extra'},{detail:event.detail+'\n'},{detail:'receiver.near'},{detail:null}])assert.equal(counterparty({...event,...changed}),null);
assert.equal(counterparty({...event,other_account_id:'stored.near'}),'stored.near');
""")

    def test_counted_graph_portfolio_and_source_copy_uses_singular_nouns(self):
        cases = (
            ("graphs.js", 'status.textContent = "Display: ', ';\n', '"Display: '),
            ("graphs.js", 'inspector.append(element("h4", "", ', '));', ''),
            ("graphs.js", 'selection.textContent = count + ', ';\n', 'count + '),
            ("portfolio.js", 'node("p", history.stored + ', ', "portfolio-note")', 'history.stored + '),
            ("portfolio.js", 'node("p", items.length + ', ', "portfolio-note")', 'items.length + '),
            ("portfolio.js", 'sections.history.status.textContent = ', ';', ''),
            ("portfolio.js", 'sections.block.status.textContent = contracts.length > MAX_CONTRACTS ? ', ';\n', 'contracts.length > MAX_CONTRACTS ? '),
            ("investigation-jobs.js", 'element("summary", "Collection sources (" + ', '));', '"Collection sources (" + '),
            ("graphs.js", 'return "Recording: " + payload.bounds.seeds.length + ', ';\n', 'payload.bounds.seeds.length + '),
            ("investigate.js", 'parent.append(node("p", "Read " + data.pages + ', ', "investigate-scope")', '"Read " + data.pages + '),
        )
        for filename, prefix, ending, retained in cases:
            with self.subTest(filename=filename, prefix=prefix):
                source = (ROOT / "intel/ui" / filename).read_text(encoding="utf-8")
                expression = retained + source.split(prefix, 1)[1].split(ending, 1)[0]
                self.run_script(r"""
const assert=require('node:assert/strict');
for(const count of [0,1,2])for(const kind of ['flow','cluster']){
const rows=Array(count),records=rows,items=rows,contracts=rows,MAX_CONTRACTS=20,history={stored:count,items:new Set(rows.map((_,i)=>i))};
const data={observations:rows,pages:count,inserted:count,coverage:{provider_pages_exhausted:false}},live=false,payload={total_nodes:count,total_records:count,kind,bounds:{seeds:rows},coverage:{standard:''}},state={visible:rows,edges:rows};
const visibleRecords={size:count},matchingRecords={size:count},bars=[{getAttribute:()=>''}],first=0,last=0,exactUtc=()=>'';
const rendered=""" + expression + r""";
assert.doesNotMatch(rendered,/\b1 (?:(?:recorded |root outbound )?(?:transfers|relationships)|accounts|edges|retained observations|contracts|pages|seeds|provider pages|new rows)\b/);
}
""")

    def test_counted_workspace_investigate_and_portfolio_copy_uses_singular_nouns(self):
        cases = (
            ("workspace.js", 'document.getElementById("case-count").textContent = ', ';\n', ''),
            ("workspace.js", '      message(records.length + ', ');\n', 'records.length + '),
            ("investigate.js", 'parent.append(node("p", data.coverage.stored_rows + ', ', "investigate-scope")', 'data.coverage.stored_rows + '),
            ("investigate.js", 'parent.append(node("p", data.observations.length + ', ', "investigate-source")', 'data.observations.length + '),
            ("portfolio.js", '      stale: ', ',\n', ''),
            ("portfolio.js", 'const wait = ', ';\n', ''),
        )
        for filename, prefix, ending, retained in cases:
            with self.subTest(filename=filename, prefix=prefix):
                source = (ROOT / "intel/ui" / filename).read_text(encoding="utf-8")
                expression = retained + source.split(prefix, 1)[1].split(ending, 1)[0]
                self.run_script(r"""
const assert=require('node:assert/strict');
for(const count of [0,1,2]){
const rows=Array.from({length:count},(_,i)=>({observed_at:'2026-11-1'+i})),records=rows,dates=rows.map(row=>row.observed_at),age=count;
const state={cases:rows},data={coverage:{stored_rows:count},observations:rows},response={headers:{get:()=>String(count)}};
const rendered=""" + expression + r""";
assert.ok(rendered.includes(count+' '),rendered);
assert.doesNotMatch(rendered,/\b1 (?:saved investigations|matching records|retained rows|original provider observations|seconds)\b/);
}
""")

    def test_live_account_amount_and_asset_id_are_separated(self):
        source = (ROOT / "intel/ui/profiles.js").read_text(encoding="utf-8")
        node = source.split("  const node = ", 1)[1].split("  function pages(", 1)[0]
        build = "        const amount = node(" + source.split("        const amount = node(", 1)[1].split("        const evidence = ", 1)[0]
        self.run_script(r"""
const assert=require('node:assert/strict');
const text=item=>typeof item==='string'?item:item.children.map(text).join('');
const document={createElement:tag=>({tag,children:[],set textContent(value){this.children=[String(value)];},append(...items){this.children.push(...items);}})};
const window={SiyabNumbers:{baseUnits:()=>'7,095.0855'}},row={asset_id:'nep141:zec.omft.near',amount:'7095085500'},decimals=6;
const node = """ + node + build + r"""
assert.equal(text(amount),'7,095.0855 nep141:zec.omft.near');
assert.equal(amount.children.at(-1).className,'data profile-asset');
""")

    def test_watchlist_alerts_use_the_feed_stored_counterparty_rule(self):
        feed = (ROOT / "intel/ui/feed.js").read_text(encoding="utf-8")
        workspace = (ROOT / "intel/ui/workspace.js").read_text(encoding="utf-8")
        helper = "  const counterparty = " + feed.split("  const counterparty = ", 1)[1].split("  const utc = ", 1)[0]
        self.assertIn(helper, workspace)
        consume = "  let baseline = null;" + workspace.split("  let baseline = null;", 1)[1].split("  async function poll()", 1)[0]
        self.run_script(r"""
const assert=require('node:assert/strict');
const state={watchlist:['beneficiary.near'],notifications:[]},window={},persisted=[],persist=value=>persisted.push(value),document={getElementById:()=>null};
""" + consume + r"""
const id=n=>String(n).repeat(64);
const old={signal:'full_access_key_then_outflow',subject:'victim.near',drain_kind:'transfer',detail:'full access key ed25519:abc added to victim.near; transfer of 12 yoctoNEAR to beneficiary.near within 34 ms',observed:{timestamp_ms:1}};
const before=JSON.stringify(old);
consume({events:[],event_ids:[]});
const misses=[{...old,detail:old.detail+' extra'},{...old,subject:'other.near'},{...old,drain_kind:'delete_account'},{...old,signal:'other'}];
consume({events:misses,event_ids:misses.map((_,index)=>id(index+1))});
assert.deepEqual(state.notifications,[]);
consume({events:[old],event_ids:[id(9)]});
assert.equal(JSON.stringify(old),before);
assert.deepEqual(state.notifications.map(item=>[item.id,item.account]),[[id(9),'beneficiary.near']]);
""")

    def test_polling_counts_use_singular_nouns(self):
        source = (ROOT / "intel/ui/cloud-workspace.js").read_text(encoding="utf-8")
        loop = 'for (const [key, label] of ' + source.split('for (const [key, label] of ', 1)[1].split('      if (polling.oldest_due_ms)', 1)[0]
        self.run_script(r"""
const assert=require('node:assert/strict');
for(const count of [0,1,2]){
const polling={coverage_gap_accounts:count,retry_pending:count,backfill_pending:count,pending_events:count},health={rows:[],append(value){this.rows.push(value);}},node=(tag,text)=>text;
""" + loop + r"""
assert.equal(health.rows.length,4);
for(const row of health.rows)assert.doesNotMatch(row,/\b1 (?:accounts|account history backfills|observed events)\b/);
}
""")

    def test_association_scope_is_conditional(self):
        panel = (ROOT / "intel/ui/enrichment-panel.html").read_text(encoding="utf-8")
        self.assertIn("Associations appear here only when an official source documents the exact account.", panel)
        self.assertNotIn("An official source documents these exact accounts.", panel)

    def test_delivery_log_has_signed_out_message(self):
        message = "Sign in and select a team to see deliveries."
        panel = (ROOT / "intel/ui/delivery-panel.html").read_text(encoding="utf-8")
        self.assertIn('<div id="delivery-log" aria-live="polite"><p class="delivery-meta">' + message + '</p></div>', panel)

    def test_delivery_log_restores_signed_out_message(self):
        message = "Sign in and select a team to see deliveries."
        source = (ROOT / "intel/ui/delivery.js").read_text(encoding="utf-8")
        clear = "function clear(" + source.split("function clear(", 1)[1].split("  async function request", 1)[0]
        self.run_script("""
const assert=require('node:assert/strict');
const ids={},el=name=>ids[name]||=( {children:[],textContent:'',replaceChildren(...items){this.children=items;},setAttribute(){}} );
const state={epoch:0,logRevision:0,pending:new Set()},clearTimeout=()=>{},message=()=>{},warn=value=>{el('warning').textContent=value;};el('warning').textContent='stale';
const node=(tag,text,className)=>({tag,text,className});
""" + clear + "clear();assert.equal(el('warning').textContent,'');assert.equal(el('log').children[0].text," + json.dumps(message) + ");assert.equal(el('newer').disabled,true);assert.equal(el('older').disabled,true);")

    def test_feed_provider_falls_back_to_recorded_url(self):
        source = (ROOT / "intel/ui/feed.js").read_text(encoding="utf-8")
        provider = source.split("  const provider = ", 1)[1].split("  const utc = ", 1)[0]
        self.run_script("""
const assert=require('node:assert/strict');
const provider=""" + provider + """
assert.equal(provider({source:{url:'https://mainnet.neardata.xyz/v0/block/123'}}),'mainnet.neardata.xyz');
assert.equal(provider({source:{provider:'Recorded source',url:'invalid'}}),'Recorded source');
for(const url of ['invalid','javascript:alert(1)','/relative',''])assert.equal(provider({source:{url}}),'Provider not recorded');
for(const source of [null,{}, {url:null},{provider:''},{provider:12}])assert.equal(provider({source}),'Provider not recorded');
assert.equal(provider({source:{provider:' ',url:'https://mainnet.neardata.xyz:443/v0/block/123'}}),'mainnet.neardata.xyz');
assert.equal(provider({}),'Provider not recorded');
""")

    def test_feed_shows_last_failed_transfer_poll(self):
        source = (ROOT / "intel/ui/feed.js").read_text(encoding="utf-8")
        health = source.split("  const health = ", 1)[1].split("  const poll = ", 1)[0]
        text = source.split("  const text = ", 1)[1].split("  const provider = ", 1)[0]
        self.run_script("""
const assert=require('node:assert/strict');
const text=""" + text + """
const panel={rows:[],replaceChildren(){this.rows=[];}},add=(parent,tag,value)=>{parent.rows.push(value);return {rows:[]};};
const freshness=()=>({stale:false}),health=""" + health + """
for(const status of [429,null]){health({follower:'running',stats:{last_large_transfer_error:{at:'2026-11-14T10:54:27Z',error:'HttpError',status}}});
const expected=status===429?'The transfer read was rate limited (HTTP 429) at 2026-11-14T10:54:27Z. This read adds no observations; previously recorded rows are retained.':'labelled_account_large_transfer last failed poll at 2026-11-14T10:54:27Z: HttpError, no HTTP status.';
assert.ok(panel.rows.includes(expected));}
""")


if __name__ == "__main__":
    unittest.main()
