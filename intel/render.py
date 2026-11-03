"""python -m intel render --out <dir>: the static hosted pages, built from the committed JSON files.

No framework and no external script; one stylesheet inline in each page. The pages: index.html
(the headline, the counts block of scripts/counts.py, the links), feed.html (the feed state and
its events), incidents/<id>.html (sources, accounts, on chain facts, rules today, what is not
public), entities/<set>.html (treasuries, catalog, infrastructure: one page per seed set),
flows/<name>.html (an exit trace: the summary line, the shares of the subject's direct outbound
value, the subject's balance and issuer status at a block, then one card per hop grouped by depth)
and verify.html (a form that reads a registry record from https://test.rpc.fastnear.com in the
browser, never the ShieldBot API, and shows it next to the evidence hash the reader pastes). Every
page carries a robots noindex meta and the footer "Data: NearBlocks".

Text from a source (a report's sentence, a token symbol, a quoted string in a label row) is shown
as quoted data: a quote object as <blockquote class="data">, a double quoted string inside a label
or note as <q class="data">, so a reader and a test can tell it from ShieldBot's own words.
"""

import html
import importlib.util
import json
import re
from pathlib import Path

from intel import feed, labels, trace

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"
TESTNET_RPC = "https://test.rpc.fastnear.com"
HEADLINE = "ShieldBot for NEAR: security intelligence for AI agent money on NEAR"
ENTITY_SETS = (("treasuries", "Treasuries", ("treasury",)),
               ("catalog", "Catalog tokens", ("catalog_token",)),
               ("infrastructure", "Infrastructure and bridge accounts", ("infrastructure", "bridge")))
STYLE = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#5d5b55;--line:#dcd8cf;--card:#ffffff;--accent:#2f5d50;--deny:#8a2b1f}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#a8a59c;--line:#3a3934;
--card:#1f1f1c;--accent:#8fc1ae;--deny:#e08a7c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main,header,footer{max-width:960px;margin:0 auto;padding:16px}
header{border-bottom:1px solid var(--line)}header a{margin-right:16px}
a{color:var(--accent)}h1{font-size:1.6rem;line-height:1.25}h2{font-size:1.2rem;margin-top:2rem}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.85rem;
overflow-wrap:anywhere}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:12px 16px;margin:12px 0}
.muted{color:var(--muted)}.deny{color:var(--deny)}
blockquote.data{margin:8px 0;padding:4px 12px;border-left:3px solid var(--line);color:var(--muted)}
q.data{color:var(--muted)}
table{border-collapse:collapse}td,th{border-bottom:1px solid var(--line);padding:6px 8px;
text-align:left;vertical-align:top}.table{overflow-x:auto}
input{display:block;width:60ch;max-width:calc(100vw - 72px);padding:8px;font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--line)}
button{padding:8px 16px;font:inherit;margin-top:8px}footer{border-top:1px solid var(--line);color:var(--muted)}
"""
# A double quoted source string, which may itself hold escaped quotes and backslashes (JSON text).
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_UNESCAPE = re.compile(r'\\(["\\])')


def esc(text):
    return html.escape(str(text), quote=True)


def data_text(text):
    """Our text with each double quoted source string marked as quoted data."""
    parts, last = [], 0
    for match in _QUOTED.finditer(str(text)):
        parts.append(esc(text[last:match.start()]))
        quoted = esc(_UNESCAPE.sub(r"\1", match.group(1)))
        parts.append(f'<q class="data">{quoted}</q>')
        last = match.end()
    return "".join(parts) + esc(str(text)[last:])


def quote_block(quote, sources):
    source = sources.get(quote["source"], {})
    return (f'<blockquote class="data" cite="{esc(source.get("url", ""))}">{esc(quote["text"])}'
            f'<br><span class="muted">quoted from [{esc(quote["source"])}]</span></blockquote>')


def page(title, body, depth=0):
    """A whole HTML page; depth is how many directories below the site root it sits."""
    up = "../" * depth
    nav = "".join(f'<a href="{up}{href}">{label}</a>' for href, label in (
        ("index.html", "Home"), ("feed.html", "Feed"), ("verify.html", "Verify")))
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<meta name=\"robots\" content=\"noindex\">\n"
            f"<title>{esc(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n"
            f"<header><nav>{nav}</nav></header>\n<main>\n{body}\n</main>\n"
            "<footer><p>Data: NearBlocks. Blocks: mainnet.neardata.xyz. Transfers: FastNEAR.</p></footer>\n"
            "</body>\n</html>\n")


def _counts_lines():
    spec = importlib.util.spec_from_file_location("counts", ROOT / "scripts" / "counts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.lines()


def _incidents(incidents_dir=INCIDENTS_DIR):
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(Path(incidents_dir).glob("*.json"))]


def _flows(flows_dir=trace.FLOWS_DIR):
    return [(path.stem, json.loads(path.read_text(encoding="utf-8"))) for path in sorted(Path(flows_dir).glob("*.json"))]


def index_page(incidents, flows):
    counts = "".join(f"<p>{esc(line)}</p>" for line in _counts_lines())
    links = ["<li><a href=\"feed.html\">The block feed and its events</a></li>"]
    links += [f'<li><a href="incidents/{esc(d["id"])}.html">{esc(d["title"])} ({esc(d["date"])})</a></li>'
              for d in incidents]
    links += [f'<li><a href="entities/{name}.html">{esc(title)}</a></li>' for name, title, _ in ENTITY_SETS]
    links += [f'<li><a href="flows/{esc(name)}.html">Exit trace from <span class="mono">{esc(f["subject"])}</span>: '
              f'depth {f["depth_reached"]} of {f["depth"]}, {esc(trace.stop_state(f["completeness"]))}, with the shares '
              'of its direct outbound value</a></li>' for name, f in flows]
    links.append("<li><a href=\"verify.html\">Read a registry record and compare its evidence hash</a></li>")
    body = (f"<h1>{esc(HEADLINE)}</h1>\n<h2>Counts</h2>\n<div class=\"card\">{counts}</div>\n"
            "<p class=\"muted\">Every number above is printed by <code>scripts/counts.py</code> from the committed "
            "files.</p>\n<h2>Pages</h2>\n<ul>" + "".join(links) + "</ul>")
    return page("ShieldBot for NEAR", body)


def feed_page(state_path=feed.STATE_PATH, events_dir=feed.EVENTS_DIR):
    if not Path(state_path).is_file():
        return page("Feed", "<h1>Feed</h1><p>The feed has not run.</p>")
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    stats = "".join(f"<li>{esc(line)}</li>" for line in feed.stats_lines(state))
    events = sorted((json.loads(p.read_bytes()), p.stem) for p in Path(events_dir).glob("*.json"))
    cards = "".join(
        f'<div class="card"><p><strong>{esc(e["signal"])}</strong> at block {e["observed"]["block_height"]}</p>'
        f'<p>{esc(e["detail"])}</p><p class="mono">receipt {esc(e["receipt_id"])}<br>transaction {esc(e["tx_hash"])}'
        f'<br>evidence {esc(stem)}</p></div>'
        for e, stem in sorted(events, key=lambda item: (item[0]["observed"]["block_height"], item[1])))
    body = (f"<h1>Feed over finalized mainnet blocks</h1>\n<ul>{stats}</ul>\n<h2>Events</h2>\n"
            + (cards or "<p>No signal fired over the recorded blocks.</p>")
            + "\n<p class=\"muted\">The signals: " + ", ".join(feed.SIGNALS) + ".</p>")
    return page("Feed", body)


def incident_page(document):
    sources = {s["id"]: s for s in document["sources"]}
    chains = ", ".join(document["chains"])
    parts = [f"<h1>{esc(document['title'])}</h1>", f"<p>{esc(document['date'])}; chains {esc(chains)}</p>",
             "<h2>Sources</h2>"]
    for source in document["sources"]:
        parts.append(f'<div class="card"><p>[{esc(source["id"])}] {esc(source["publisher"])}: '
                     f'<a href="{esc(source["url"])}">{esc(source["url"])}</a></p><p class="mono">sha256 '
                     f'{esc(source["content_sha256"])}<br>fetched {esc(source["fetched_at"])}</p></div>')
    parts.append("<h2>Accounts named in the sources</h2>")
    if not document["accounts"]:
        parts.append("<p>The sources name no account.</p>")
    for account in document["accounts"]:
        ids = "<br>".join(esc(i) for i in account["assertion_ids"]) or "none"
        parts.append(f'<div class="card"><p class="mono">{esc(account["subject"])} ({esc(account["chain"])})</p>'
                     f'<p>role as reported: {esc(account["role_as_reported"])}</p>'
                     f'{quote_block(account["quote"], sources)}<p class="mono muted">assertions<br>{ids}</p></div>')
    for key in [key for key in document if key.endswith("_as_reported")]:
        item = document[key]
        value = item.get("value") or ", ".join(str(n) for n in item.get("ids", []))
        parts.append(f'<h2>{esc(key.replace("_", " ").capitalize())}</h2><div class="card"><p class="mono">'
                     f'{esc(value)}</p>{quote_block(item["quote"], sources)}</div>')
    parts.append("<h2>On chain facts</h2>")
    if document.get("onchain_state"):
        parts.append(f"<p>{esc(document['onchain_state'])}</p>")
    for fact in document["onchain_facts"]:
        parts.append(f'<div class="card"><p class="mono">{esc(fact["subject"])}</p><p>{esc(fact["method"])} at block '
                     f'{fact["block"]}: {data_text(fact["summary"])}</p><p class="mono muted">result sha256 '
                     f'{esc(fact["result_sha256"])}</p></div>')
    parts.append("<h2>Rules today</h2>")
    if not document["rules_today"]:
        parts.append("<p>No account to judge.</p>")
    for entry in document["rules_today"]:
        checks = ", ".join(f"{c['id']} {c['status']}" for c in entry["checks"])
        css = ' class="deny"' if entry["verdict"] == "DENY" else ""
        parts.append(f'<div class="card"><p class="mono">{esc(entry["subject"])} as {esc(entry["as"])}: '
                     f'<strong{css}>{esc(entry["verdict"])}</strong></p><p class="muted">{esc(checks)}</p></div>')
    parts.append("<h2>Not public</h2><ul>" + "".join(f"<li>{esc(i)}</li>" for i in document["not_public"]) + "</ul>")
    parts.append(f"<h2>Standard</h2><p>{esc(document['standard'])}</p>")
    return page(document["title"], "\n".join(parts), depth=1)


def entity_page(title, categories, store):
    rows = [a for a in store.assertions if a.category in categories and a.superseded_by is None]
    body = "".join(f'<tr><td class="mono">{esc(a.subject)}</td><td>{esc(a.chain)}</td><td>{data_text(a.label)}</td>'
                   f'<td>{esc(a.category)}</td><td>{esc(a.provenance)}</td><td>{data_text(a.note)}</td></tr>'
                   for a in rows)
    table = ("<div class=\"table\"><table><thead><tr><th>Subject</th><th>Chain</th><th>Label</th><th>Category</th>"
             f"<th>Provenance</th><th>Note</th></tr></thead><tbody>{body}</tbody></table></div>")
    return page(title, f"<h1>{esc(title)}</h1><p>{len(rows)} current assertions from the label store.</p>{table}",
                depth=1)


def _hop_card(hop):
    usd = "" if hop["usd_amount"] is None else f', {hop["usd_amount"]} USD as the Transfers API values it'
    return (f'<div class="card"><p class="mono">{esc(hop["from"])} to {esc(hop["to"])}</p><p>{esc(hop["amount"])} of '
            f'<span class="mono">{esc(hop["asset_id"])}</span>{esc(usd)}, method {esc(hop["method_name"] or "none")}</p>'
            f'<p class="mono muted">block {hop["block"]} at {esc(trace.utc_minute(hop["block_timestamp"]))}<br>transaction '
            f'{esc(hop["transaction_hash"])}<br>receipt {esc(hop["receipt_id"])}</p></div>')


def _shares_table(shares):
    rows = "".join(f'<tr><td class="mono">{esc(b["to"])}</td><td>{esc(b["kind"])}</td><td>{esc(trace.usd_text(b["usd"]))}</td>'
                   f'<td>{b["count"]}</td><td>{b["percent"]}</td></tr>' for b in shares["buckets"])
    below, without = shares["below_min_usd"], shares["without_usd"]
    units = ", ".join(f"{amount} of {asset}" for asset, amount in without["base_units"].items()) or "none"
    rows += (f'<tr><td>rows below {shares["min_usd"]} USD, counted apart</td><td></td><td>{esc(trace.usd_text(below["usd"]))}</td>'
             f'<td>{below["count"]}</td><td></td></tr><tr><td>rows without a USD amount, counted apart</td><td></td>'
             f'<td>{esc(units)} (base units)</td><td>{without["count"]}</td><td></td></tr>')
    return ("<div class=\"table\"><table><thead><tr><th>To</th><th>Kind</th><th>USD</th><th>Rows</th><th>Percent</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>")


def _state_card(reads):
    lines = []
    for read in reads:
        answer = json.dumps(read["result"], ensure_ascii=False)
        if read["method"] == "ft_balance_of":
            said = f"balance {trace.human(read['result'], read['decimals'])} ({read['decimals']} decimals)"
        elif read["banned"]:
            said = f"banned by the issuer, {read['method']} answers {answer}"
        else:
            said = f"{read['method']} answers {answer}"
        lines.append(f'<p><span class="mono">{esc(read["token"])}</span> {esc(read["method"])} at block '
                     f'{read["block"]}: {data_text(said)}</p><p class="mono muted">result sha256 '
                     f'{esc(read["result_sha256"])}</p>')
    return '<div class="card state">' + ("".join(lines) or "<p>No issuer read.</p>") + "</div>"


def flow_page(document):
    depths = trace.hop_depths(document)
    groups = []
    for depth in sorted(set(depths)):
        hops = [hop for hop, d in zip(document["hops"], depths) if d == depth]
        groups.append(f"<h2>Depth {depth}: {len(hops)} hops</h2>" + "".join(_hop_card(hop) for hop in hops))
    window = document["window"]
    body = (f'<h1>Exit trace</h1><p class="mono">{esc(document["subject"])}</p>'
            f'<div class="card summary"><p>{data_text(trace.summary(document))}</p><p class="muted">Read '
            f'{esc(document["fetched_at"])}.</p></div>'
            f"<p>The window starts at {esc(window['from'])}, {esc(window['rule'])} {esc(window['incident_date'])}; "
            "older outbound rows are outside it.</p>"
            "<h2>Shares of the direct outbound value</h2>" + _shares_table(document["shares"]) +
            "<p>Shares are of the subject's direct outbound value; deeper hops are shown and not re summed.</p>"
            "<h2>The subject at a block</h2>" + _state_card(document["state_at_block"]) +
            "<p class=\"muted\">Only an account that carries an incident label is traced. Each card is one transfer "
            "read from the FastNEAR Transfers API.</p>" + "".join(groups))
    return page("Exit trace", body, depth=1)


VERIFY_SCRIPT = """
document.getElementById("read").addEventListener("click", async function () {
  var out = document.getElementById("out");
  var registry = document.getElementById("registry").value.trim();
  var subject = document.getElementById("subject").value.trim();
  var pasted = document.getElementById("hash").value.trim().toLowerCase();
  out.textContent = "reading";
  try {
    var args = btoa(unescape(encodeURIComponent(JSON.stringify({subject: subject}))));
    var reply = await fetch("RPC_URL", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({jsonrpc: "2.0", id: "verify", method: "query", params: {request_type: "call_function",
        finality: "final", account_id: registry, method_name: "latest", args_base64: args}})});
    var body = await reply.json();
    if (body.error || !body.result || body.result.error) {
      out.textContent = "the read failed: " + JSON.stringify(body.error || (body.result && body.result.error));
      return;
    }
    var record = JSON.parse(new TextDecoder().decode(new Uint8Array(body.result.result)));
    if (record === null) {
      out.textContent = "no record for this subject (the registry reads it as Unknown)";
      return;
    }
    var same = pasted && record.evidence_hash === pasted;
    out.textContent = JSON.stringify(record, null, 2) + "\\n\\nevidence hash you pasted: " + (pasted || "none") +
      "\\n" + (pasted ? (same ? "equal to the recorded evidence hash" : "different from the recorded evidence hash") : "");
  } catch (error) {
    out.textContent = "the read failed: " + error;
  }
});
"""


def verify_page():
    script = VERIFY_SCRIPT.replace("RPC_URL", TESTNET_RPC)
    body = ("<h1>Read a registry record</h1><p>The page calls <code>latest</code> on the registry contract over the "
            f"public testnet RPC <code>{TESTNET_RPC}</code> from your browser and shows the record next to the "
            "evidence hash you paste. Compute the hash yourself with <code>sha256sum</code> of the evidence file.</p>"
            '<div class="card"><label>Registry account<input id="registry" autocomplete="off"></label>'
            '<label>Subject (a NEAR account id)<input id="subject" autocomplete="off"></label>'
            '<label>Evidence hash (64 hex)<input id="hash" autocomplete="off"></label>'
            '<button id="read" type="button">Read the record</button></div>'
            f'<pre id="out" class="card mono"></pre><script>{script}</script>')
    return page("Verify a record", body)


def render(out_dir, store=None, incidents_dir=INCIDENTS_DIR, flows_dir=trace.FLOWS_DIR):
    """Write every page under out_dir; returns the written paths relative to it."""
    store = store or labels.load()
    out = Path(out_dir)
    incidents, flows = _incidents(incidents_dir), _flows(flows_dir)
    pages = {"index.html": index_page(incidents, flows), "feed.html": feed_page(), "verify.html": verify_page()}
    for document in incidents:
        pages[f"incidents/{document['id']}.html"] = incident_page(document)
    for name, title, categories in ENTITY_SETS:
        pages[f"entities/{name}.html"] = entity_page(title, categories, store)
    for name, document in flows:
        pages[f"flows/{name}.html"] = flow_page(document)
    for relative, text in pages.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return sorted(pages)
