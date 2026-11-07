"""python -m intel render --out <dir>: the static hosted pages, built from the committed JSON files.

No framework and no external script; one shared stylesheet, style.css, that every page links. The
pages: index.html (the headline, what the counts are of, a search box whose inline script filters
search.json in the browser, the counts block of scripts/counts.py, the links), search.json (one entry
per subject with a current row and per registry entity of intel/entities.jsonl, each with the path of
its page relative to the site root), feed.html
(the recorded window or, in follow mode, the follower's lines, then the events),
incidents/<id>.html (sources, accounts, on chain facts, rules today, what is not
public), entities/<set>.html (treasuries, catalog, infrastructure, organisations: one page per set,
each subject shown as quoted data, since a third party chose the account id, and every inferred row
under the heading "Attribution hypothesis" with its rule id, inputs and confidence),
entities/<name>.html (one registry entity of intel/entities.jsonl: its kind, name, members and the
registry read that produced it), accounts/<name>.html (one subject with a current row: its rows from
sources and chain reads with chain, provenance and source, its inferred rows under "Attribution
hypothesis" with their rule id, inputs and confidence, the incident pages naming it, the trace and cluster
edges touching it and the feed events on it), clusters/<name>.html (the seeds, one card per edge:
the two chain reads of an onchain edge, the
input facts and confidence of an inferred edge under "Attribution hypothesis", then the precision
lines as the JSON states them and the depositor count), issuers.html (the status reads counted by
issuer and answer, one card per row banned by the issuer, then the history's coverage and one card
per issuer action with its block and transaction), flows/<name>.html (an exit trace: the
summary line, the shares of the subject's gross outbound
transfer value, the subject's balance and issuer status at a block, then one card per hop grouped
by depth), verify.html (a form that reads a registry record from https://test.rpc.fastnear.com in the
browser, never the ShieldBot API, and shows it next to the evidence hash the reader pastes; its inputs
show the vendor's committed record as examples) and 404.html (the page the API serves for a path that
is neither a route nor a file at any depth, so its links are root absolute). Every
page carries a robots noindex meta, which render --indexable leaves out, and the footer "Data: NearBlocks".
A page name is the subject or entity id with every character outside letters, digits, dot, underscore
and hyphen replaced by an underscore; the incident, entity, cluster and flow pages link each subject
that has an account page to it.

Text from a source (a report's sentence, a token symbol, a quoted string in a label row) is shown
as quoted data: a quote object as <blockquote class="data">, a double quoted string inside a label
or note as <q class="data">, and so is a value a label row stores (its source, its rule id), so a reader and
a test can tell it from ShieldBot's own words.
"""

import html
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path

from intel import cluster, feed, issuers, labels, records, trace

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"
RECORDS_DIR = ROOT / "fixtures" / "records"
TESTNET_RPC = "https://test.rpc.fastnear.com"
# The record the verify page offers as its example: a latest view of the testnet registry.
VERIFY_RECORD = ROOT / "fixtures" / "registry" / "testnet-latest-kestrelridge-shop.json"
EVIDENCE_DIR = ROOT / "fixtures" / "evidence"
# The token whose issuer actions intel/issuers.py reads from the owner's transactions.
HISTORY_TOKEN = "usdt.tether-token.near"
HEADLINE = "ShieldBot for NEAR: security intelligence with a source on every row, enforced on chain"
COUNTS_OF = ("The counts below are of the recorded mainnet receivers the rules judged, in all and by cohort, the label "
             "store's assertions and subjects by provenance class, the incident pages, the blocks and events of the "
             "feed, the exit traces and their hops, and the verdict latency runs for the allowlisted payee and for a "
             "payee off the allowlist.")
ENTITY_SETS = (("treasuries", "Treasuries", ("treasury",)),
               ("catalog", "Catalog tokens", ("catalog_token",)),
               ("infrastructure", "Infrastructure and bridge accounts", ("infrastructure", "bridge")),
               ("organisations", "Organisations from on chain registries", ("organisation",)))
ROBOTS = "<meta name=\"robots\" content=\"noindex\">\n"
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
# The characters a page name keeps; page_names replaces every other one with an underscore.
_PAGE_NAME = re.compile(r"[^A-Za-z0-9._-]")


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


def page_names(keys):
    """{key: page name}: the key with every character outside letters, digits, dot, underscore and hyphen
    replaced by an underscore, a name every file system and URL accepts. Two keys whose names are equal
    ignoring case, one file on a case insensitive file system, raise ValueError."""
    names, seen = {}, {}
    for key in keys:
        name = _PAGE_NAME.sub("_", key)
        other = seen.setdefault(name.casefold(), key)
        if other != key:
            raise ValueError(f"{other!r} and {key!r} share the page name {name!r}")
        names[key] = name
    return names


def account_pages(store):
    """{subject: page path relative to the site root} for every subject with a current row."""
    subjects = sorted({a.subject for a in store.assertions if a.superseded_by is None})
    return {subject: f"accounts/{name}.html" for subject, name in page_names(subjects).items()}


def _linked(subject, inner, accounts):
    """inner, linked to the subject's account page when the subject has one, from a page one directory
    below the site root."""
    return f'<a href="../{accounts[subject]}">{inner}</a>' if subject in accounts else inner


def _subject_cell(subject, accounts):
    """The subject as quoted data, linked to its account page when it has one."""
    return _linked(subject, f'<q class="data">{esc(subject)}</q>', accounts)


def _inputs(assertion, by_id, accounts):
    """The input assertion ids of an inferred row, each linked to the account page of its row's subject."""
    return "<br>".join(_linked(by_id[i].subject, esc(i), accounts) if i in by_id else esc(i)
                       for i in assertion.source["inputs"])


def page(title, body, depth=0, rooted=False):
    """A whole HTML page; depth is how many directories below the site root it sits, and a rooted page
    links root absolute because it is served at any path."""
    up = "/" if rooted else "../" * depth
    nav = "".join(f'<a href="{up}{href}">{label}</a>' for href, label in (
        ("index.html", "Home"), ("feed.html", "Feed"), ("verify.html", "Verify")))
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n{ROBOTS}"
            f"<title>{esc(title)}</title>\n<link rel=\"stylesheet\" href=\"/style.css\">\n</head>\n<body>\n"
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


def _clusters(clusters_dir=cluster.CLUSTERS_DIR):
    return [(path.stem, json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(Path(clusters_dir).glob("*.json"))]


def _inferred(edges):
    return [edge for edge in edges if edge["provenance"] == "inferred"]


def index_page(incidents, flows, clusters):
    counts = "".join(f"<p>{esc(line)}</p>" for line in _counts_lines())
    links = ["<li><a href=\"feed.html\">The block feed and its events</a></li>"]
    links += [f'<li><a href="incidents/{esc(d["id"])}.html">{esc(d["title"])} ({esc(d["date"])})</a></li>'
              for d in incidents]
    links += [f'<li><a href="entities/{name}.html">{esc(title)}</a></li>' for name, title, _ in ENTITY_SETS]
    links += [f'<li><a href="flows/{esc(name)}.html">Exit trace from <span class="mono">{esc(f["subject"])}</span>: '
              f'depth {f["depth_reached"]} of {f["depth"]}, {esc(trace.stop_state(f["completeness"]))}, with the shares '
              'of its gross outbound transfer value</a></li>' for name, f in flows]
    links += [f'<li><a href="clusters/{esc(name)}.html">Account relationship edges over the {len(c["seeds"])} seeds '
              f'of {esc(name)}: {len(c["edges"]) - len(_inferred(c["edges"]))} from chain reads, '
              f'{len(_inferred(c["edges"]))} attribution hypotheses</a></li>' for name, c in clusters]
    links.append("<li><a href=\"issuers.html\">Issuer actions: what the issuer contracts answer for the account ids "
                 "read, and the issuer action history with its blocks and transactions</a></li>")
    links.append("<li><a href=\"records/index.html\">Incident records and their report citations</a></li>")
    links.append("<li><a href=\"verify.html\">Read a registry record and compare its evidence hash</a></li>")
    search = ('<form id="search-box"><label for="search-term">Search accounts and entities</label>'
              '<input id="search-term" name="term" autocomplete="off"><button type="submit">Search</button>'
              '<div id="search-results" aria-live="polite"></div></form>'
              '<script>const form=document.getElementById("search-box");const term=document.getElementById("search-term");'
              'const results=document.getElementById("search-results");let entries=[];'
              'fetch("/search.json").then(response=>response.json()).then(data=>{entries=data.accounts.concat(data.entities)});'
              'form.addEventListener("submit",event=>{event.preventDefault();const value=term.value.trim().toLocaleLowerCase();'
              'const matches=entries.filter(entry=>(entry.subject||"").toLocaleLowerCase().includes(value)||'
              '(entry.labels||[]).some(label=>label.toLocaleLowerCase().includes(value))||'
              '(entry.name||"").toLocaleLowerCase().includes(value));results.replaceChildren();'
              'for(const entry of matches.slice(0,50)){const paragraph=document.createElement("p");'
              'const link=document.createElement("a");link.href=entry.url;const quoted=document.createElement("q");'
              'quoted.className="data";quoted.textContent=entry.subject||entry.name;link.append(quoted);'
              'paragraph.append(link);results.append(paragraph)}});</script>')
    body = (f"<h1>{esc(HEADLINE)}</h1>\n<p>{esc(COUNTS_OF)}</p>\n{search}\n<h2>Counts</h2>\n<div class=\"card\">{counts}</div>\n"
            "<p class=\"muted\">Every number above is printed by <code>scripts/counts.py</code> from the committed "
            "files.</p>\n<h2>Pages</h2>\n<ul>" + "".join(links) + "</ul>")
    return page("ShieldBot for NEAR", body)


def search_index(store, pages, entities, entity_paths):
    accounts = {}
    current = [a for a in store.assertions if a.superseded_by is None]
    for assertion in current:
        entry = accounts.setdefault(assertion.subject, {"type": "account", "subject": assertion.subject,
                                                           "labels": [], "url": pages[assertion.subject]})
        if assertion.label not in entry["labels"]:
            entry["labels"].append(assertion.label)
    return {"accounts": [accounts[subject] for subject in sorted(accounts)],
            "entities": [{"type": "entity", "id": entity["id"], "name": entity["name"],
                          "url": entity_paths[entity["id"]]} for entity in entities]}


def feed_events(events_dir):
    """The (block height, hash, document) of every event file under events_dir, ordered by observed block
    height, then hash: the order of the /feed route and of the feed page; and the number of files skipped
    because they do not read as a JSON object with an integer observed block height."""
    events, unreadable = [], 0
    for path in Path(events_dir).glob("*.json"):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable += 1
            continue
        observed = document.get("observed") if isinstance(document, dict) else None
        height = observed.get("block_height") if isinstance(observed, dict) else None
        if not isinstance(height, int):
            unreadable += 1
            continue
        events.append((height, path.stem, document))
    return sorted(events, key=lambda item: (item[0], item[1])), unreadable


def _event_field(document, *keys):
    """The escaped value at keys in an event document, or unknown when the document lacks it."""
    for key in keys:
        document = document.get(key) if isinstance(document, dict) else None
    return "unknown" if document is None else esc(document)


def feed_page(state_path=feed.STATE_PATH, events_dir=feed.EVENTS_DIR, *, state=None, event_rows=None, total=None,
              unreadable=0, stopped_since=None):
    """The feed page; event_rows, (document, hash) in the order to show, total, the number of events they were
    selected from, and unreadable, the number of event files skipped, come from a caller that selected them,
    else every event under events_dir is shown; stopped_since, the time of the last update of a follower
    judged stopped, comes from the caller that judged it."""
    if state is None:
        if not Path(state_path).is_file():
            return page("Feed", "<h1>Feed</h1><p>The feed has not run.</p>")
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    window = ""
    if state.get("mode") != "follow":
        snapshot_note = ""
        if Path(feed.SNAPSHOT_PATH).is_file():
            snapshot = json.loads(Path(feed.SNAPSHOT_PATH).read_text(encoding="utf-8"))
            snapshot_note = (f" The follower snapshot <code>intel/feed/follow-snapshot.json</code> covers "
                             f"{snapshot['blocks']} blocks from height {snapshot['start_height']} to height "
                             f"{snapshot['last_height']}.")
        window = (f"<p>Recorded window: blocks {state['start_height']} to {state['last_height']}, read on "
                  f"{esc(state['start_time'][:10])} UTC.{snapshot_note}</p>\n")
    stats = "".join(f"<li>{esc(line)}</li>" for line in feed.stats_lines(state, stopped_since))
    if event_rows is None:
        events, unreadable = feed_events(events_dir)
        event_rows = [(document, stem) for _, stem, document in events]
    cards = "".join(
        f'<div class="card"><p><strong>{_event_field(e, "signal")}</strong> at block '
        f'{_event_field(e, "observed", "block_height")}</p><p>{_event_field(e, "detail")}</p><p class="mono">receipt '
        f'{_event_field(e, "receipt_id")}<br>transaction {_event_field(e, "tx_hash")}<br>evidence {esc(stem)}</p></div>'
        for e, stem in event_rows)
    shown = "" if total == len(event_rows) else f"; the {len(event_rows)} most recent by block height are shown"
    count = "" if total is None else f"<p>{total} event{'' if total == 1 else 's'} in total{shown}.</p>\n"
    if unreadable:
        count += f"<p>{unreadable} event file{'' if unreadable == 1 else 's'} could not be read.</p>\n"
    body = (f"<h1>Feed over finalized mainnet blocks</h1>\n{window}<ul>{stats}</ul>\n<h2>Events</h2>\n{count}"
            + (cards or ("" if unreadable else "<p>No signal fired over the recorded blocks.</p>"))
            + "\n<p class=\"muted\">The signals: " + ", ".join(feed.SIGNALS) + ".</p>")
    return page("Feed", body)


def incident_page(document, accounts):
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
        parts.append(f'<div class="card"><p class="mono">{_linked(account["subject"], esc(account["subject"]), accounts)} '
                     f'({esc(account["chain"])})</p>'
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
        parts.append(f'<div class="card"><p class="mono">{_linked(fact["subject"], esc(fact["subject"]), accounts)}</p><p>{esc(fact["method"])} at block '
                     f'{fact["block"]}: {data_text(fact["summary"])}</p><p class="mono muted">result sha256 '
                     f'{esc(fact["result_sha256"])}</p></div>')
    parts.append("<h2>Rules today</h2>")
    if not document["rules_today"]:
        parts.append("<p>No account to judge.</p>")
    for entry in document["rules_today"]:
        checks = ", ".join(f"{c['id']} {c['status']}" for c in entry["checks"])
        css = ' class="deny"' if entry["verdict"] == "DENY" else ""
        parts.append(f'<div class="card"><p class="mono">{_linked(entry["subject"], esc(entry["subject"]), accounts)} as '
                     f'{esc(entry["as"])}: '
                     f'<strong{css}>{esc(entry["verdict"])}</strong></p><p class="muted">{esc(checks)}</p></div>')
    parts.append("<h2>Not public</h2><ul>" + "".join(f"<li>{esc(i)}</li>" for i in document["not_public"]) + "</ul>")
    parts.append(f"<h2>Standard</h2><p>{esc(document['standard'])}</p>")
    return page(document["title"], "\n".join(parts), depth=1)


def entity_page(title, categories, store):
    rows = [a for a in store.assertions if a.category in categories and a.superseded_by is None]
    stated = [a for a in rows if a.provenance != "inferred"]
    inferred = [a for a in rows if a.provenance == "inferred"]
    accounts, by_id = account_pages(store), {a.id: a for a in store.assertions}
    body = "".join(f'<tr><td class="mono">{_subject_cell(a.subject, accounts)}</td><td>{esc(a.chain)}</td>'
                   f'<td>{data_text(a.label)}</td><td>{esc(a.category)}</td><td>{esc(a.provenance)}</td>'
                   f'<td>{data_text(a.note)}</td></tr>' for a in stated)
    table = ("<div class=\"table\"><table><thead><tr><th>Subject</th><th>Chain</th><th>Label</th><th>Category</th>"
             f"<th>Provenance</th><th>Note</th></tr></thead><tbody>{body}</tbody></table></div>")
    if inferred:
        body = "".join(f'<tr><td class="mono">{_subject_cell(a.subject, accounts)}</td><td>{esc(a.chain)}</td>'
                       f'<td>{data_text(a.label)}</td><td>{esc(a.category)}</td>'
                       f'<td class="mono"><q class="data">{esc(a.rule_id)}</q></td>'
                       f'<td class="mono">{_inputs(a, by_id, accounts)}</td><td>{a.confidence}</td>'
                       f'<td>{data_text(a.note)}</td></tr>' for a in inferred)
        table += ("<h2>Attribution hypothesis</h2><p>Each row below is inferred: it names the rule that inferred "
                  "it, the assertions the rule read as its inputs and its confidence.</p><div class=\"table\"><table>"
                  "<thead><tr><th>Subject</th><th>Chain</th><th>Label</th><th>Category</th><th>Rule</th>"
                  f"<th>Inputs</th><th>Confidence</th><th>Note</th></tr></thead><tbody>{body}</tbody></table></div>")
    return page(title, f"<h1>{esc(title)}</h1><p>{len(rows)} current assertions from the label store.</p>{table}",
                depth=1)


def account_page(subject, store, accounts, incidents, flows, clusters, events_dir=feed.EVENTS_DIR):
    assertions = [a for a in store.assertions if a.subject == subject and a.superseded_by is None]
    rows = "".join(f'<tr><td>{esc(a.chain)}</td><td>{data_text(a.label)}</td><td>{esc(a.category)}</td>'
                   f'<td>{esc(a.provenance)}</td><td>{data_text(a.note)}</td><td class="mono"><q class="data">'
                   f'{esc(json.dumps(a.source, ensure_ascii=False, sort_keys=True))}</q></td></tr>'
                   for a in assertions if a.provenance != "inferred")
    body = (f'<h1>Account</h1><p class="mono"><q class="data">{esc(subject)}</q></p>'
            f'<h2>Current label rows from sources and chain reads</h2><div class="table"><table><thead><tr>'
            f'<th>Chain</th><th>Label</th><th>Category</th><th>Provenance</th><th>Note</th><th>Source</th></tr>'
            f'</thead><tbody>{rows}</tbody></table></div>')
    inferred = [a for a in assertions if a.provenance == "inferred"]
    if inferred:
        by_id = {a.id: a for a in store.assertions}
        rows = "".join(f'<tr><td>{esc(a.chain)}</td><td>{data_text(a.label)}</td><td>{esc(a.category)}</td>'
                       f'<td class="mono"><q class="data">{esc(a.rule_id)}</q></td>'
                       f'<td class="mono">{_inputs(a, by_id, accounts)}</td><td>{a.confidence}</td>'
                       f'<td>{data_text(a.note)}</td></tr>' for a in inferred)
        body += ('<h2>Attribution hypothesis</h2><p>Each row below is inferred: it names the rule that inferred '
                 'it, the assertions the rule read as its inputs and its confidence.</p><div class="table"><table>'
                 '<thead><tr><th>Chain</th><th>Label</th><th>Category</th><th>Rule</th><th>Inputs</th>'
                 '<th>Confidence</th><th>Note</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table></div>')
    named = [(document["id"], document["title"]) for document in incidents
             if any(account["subject"] == subject for account in document.get("accounts", []))]
    body += "<h2>Incident pages naming this account</h2>" + ("<ul>" + "".join(
        f'<li><a href="/incidents/{esc(incident_id)}.html">{esc(title)}</a></li>' for incident_id, title in named)
        + "</ul>" if named else "<p>No incident page names this account.</p>")
    # One line per flow and ordered pair of accounts, with the number of hops between them when above one.
    flow_edges = Counter((name, hop["from"], hop["to"]) for name, document in flows for hop in document.get("hops", [])
                         if subject in (hop.get("from"), hop.get("to")))
    flow_roots = [(name, document) for name, document in flows if document.get("subject") == subject]
    body += "<h2>Trace edges</h2>" + ("<ul>" + "".join(
        f'<li><a href="/flows/{esc(name)}.html">Committed trace edge{"s" if count > 1 else ""}: '
        f'<q class="data">{esc(source)}</q> to <q class="data">{esc(target)}</q>'
        f'{f", {count} edges" if count > 1 else ""}</a></li>'
        for (name, source, target), count in flow_edges.items()) + "".join(
        f'<li><a href="/flows/{esc(name)}.html">Committed trace root with {len(document.get("hops", []))} edges</a></li>'
        for name, document in flow_roots if not any(edge_name == name for edge_name, _, _ in flow_edges))
        + "</ul>" if flow_edges or flow_roots else "<p>No committed trace edge touches this account.</p>")
    cluster_edges = [(name, edge) for name, document in clusters for edge in document.get("edges", [])
                     if subject in (edge.get("from"), edge.get("to"))]
    body += "<h2>Cluster edges</h2>" + ("<ul>" + "".join(
        f'<li><a href="/clusters/{esc(name)}.html">{esc(edge["rule"])}: '
        f'<q class="data">{esc(edge["from"])}</q> to <q class="data">{esc(edge["to"])}</q></a></li>'
        for name, edge in cluster_edges) + "</ul>" if cluster_edges else "<p>No committed cluster edge touches this account.</p>")
    events = []
    for path in sorted(Path(events_dir).glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if subject in document.get("accounts", []) or subject == document.get("subject"):
            events.append((path.stem, document))
    body += "<h2>Feed events</h2>" + ("".join(
        f'<div class="card"><p><strong>{esc(document["signal"])}</strong> at block '
        f'{document["observed"]["block_height"]}</p><p>{esc(document["detail"])}</p>'
        f'<p class="mono">evidence {esc(stem)}</p></div>' for stem, document in events)
        if events else "<p>No committed feed event names this account.</p>")
    return page("Account", body, depth=1)


def registry_entity_page(entity, accounts):
    source = entity["source"]
    members = "".join(f'<li>{_subject_cell(member, accounts)}</li>' for member in entity["members"])
    body = (f'<h1>{esc(entity["kind"].replace("_", " ").title())}</h1>'
            f'<p><q class="data">{esc(entity["name"])}</q></p>'
            f'<p>Registry read: {esc(source["method"])} at block {source["block"]}, '
            f'arguments {esc(json.dumps(source["args"], sort_keys=True))}</p>'
            f'<p>Observed at {esc(entity["observed_at"])} on {esc(source["rpc"])}</p>'
            f'<h2>Members</h2><ul>{members}</ul>')
    return page("Entity", body, depth=1)


def _edge_card(edge, facts, accounts):
    head = (f'<div class="card edge"><p><strong>{esc(edge["rule"])}</strong>, provenance {esc(edge["provenance"])}'
            + (f', confidence {edge["confidence"]}' if edge["provenance"] == "inferred" else "")
            + f'</p><p class="mono">{_linked(edge["from"], esc(edge["from"]), accounts)} to '
            f'{_linked(edge["to"], esc(edge["to"]), accounts)}</p>')
    if edge["provenance"] != "inferred":
        reads = "<br>".join(f'{esc(read["method"])} {esc(json.dumps(read["args"], ensure_ascii=False))} at block '
                            f'{read["block"]} from {esc(read["rpc"])}'
                            for read in (edge["source"]["from"], edge["source"]["to"]))
        return head + f'<p class="mono muted">{reads}</p></div>'
    shared = ", ".join(f"{esc(key)} {esc(value)}" for key, value in edge["source"].items())
    inputs = "".join(
        f'<li class="mono">fact {esc(fact["id"])}: {_linked(fact["subject"], esc(fact["subject"]), accounts)} '
        f'{esc(fact["kind"])} {esc(fact["value"])}, '
        f'read from {esc(fact["source"]["provider"])} {esc(fact["source"]["method"])} '
        f'{esc(json.dumps(fact["source"]["args"], ensure_ascii=False))} at block {fact["source"]["block"]}</li>'
        for fact in (facts[fact_id] for fact_id in edge["inputs"]))
    return head + f'<p>shared: <span class="mono">{shared}</span></p><p>inputs</p><ul>{inputs}</ul></div>'


def cluster_page(name, document, accounts):
    facts = {fact["id"]: fact for fact in document["facts"]}
    inferred = _inferred(document["edges"])
    chain = [edge for edge in document["edges"] if edge["provenance"] != "inferred"]
    seeds = "".join(f'<li class="mono">{_linked(seed, esc(seed), accounts)}</li>' for seed in document["seeds"])
    precision = "".join(f"<li>{esc(line)}</li>" for line in document["precision"].values())
    depositors = document["depositors"]
    counted = ("<p>No deposit count in this run.</p>" if depositors is None else
               f'<p>{depositors["count"]} distinct depositors of <span class="mono">'
               f'{_linked(depositors["collector"], esc(depositors["collector"]), accounts)}</span> '
               f'since {esc(depositors["since"])}, {depositors["implicit_count"]} of them implicit accounts; the '
               "cluster file counts them and lists none.</p>")
    body = (f"<h1>Account relationship edges: {esc(name)}</h1><p>{esc(document['standard'])}</p>"
            f"<h2>Seeds</h2><p>{len(document['seeds'])} seeds, each with a current incident label in the label "
            f"store.</p><ul>{seeds}</ul>"
            "<h2>Edges from chain reads</h2>" + ("".join(_edge_card(edge, facts, accounts) for edge in chain)
                                                 or "<p>No edge from chain reads.</p>") +
            "<h2>Attribution hypothesis</h2><p>Each edge below is inferred from the facts named as its inputs, at "
            "the confidence shown; it is a hypothesis, not a chain read, and no verdict reads it.</p>"
            + ("".join(_edge_card(edge, facts, accounts) for edge in inferred) or "<p>No inferred edge.</p>") +
            f"<h2>Precision</h2><ul>{precision}</ul><h2>Depositors</h2>{counted}"
            f"<p class=\"muted\">Read {esc(document['fetched_at'])}. docs/CLUSTER.md states the heuristics and their "
            "limits.</p>")
    return page(f"Account relationship edges: {name}", body, depth=1)


def _hop_card(hop, accounts):
    usd = "" if hop["usd_amount"] is None else f', {hop["usd_amount"]} USD as the Transfers API values it'
    return (f'<div class="card"><p class="mono">{_linked(hop["from"], esc(hop["from"]), accounts)} to '
            f'{_linked(hop["to"], esc(hop["to"]), accounts)}</p><p>{esc(hop["amount"])} of '
            f'<span class="mono">{esc(hop["asset_id"])}</span>{esc(usd)}, method {esc(hop["method_name"] or "none")}</p>'
            f'<p class="mono muted">block {hop["block"]} at {esc(trace.utc_minute(hop["block_timestamp"]))}<br>transaction '
            f'{esc(hop["transaction_hash"])}<br>receipt {esc(hop["receipt_id"])}</p></div>')


def _shares_table(shares, accounts):
    rows = "".join(f'<tr><td class="mono">{_linked(b["to"], esc(b["to"]), accounts)}</td><td>{esc(b["kind"])}</td><td>{esc(trace.usd_text(b["usd"]))}</td>'
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


def flow_page(document, accounts):
    depths = trace.hop_depths(document)
    groups = []
    for depth in sorted(set(depths)):
        hops = [hop for hop, d in zip(document["hops"], depths) if d == depth]
        groups.append(f"<h2>Depth {depth}: {len(hops)} hops</h2>" + "".join(_hop_card(hop, accounts) for hop in hops))
    window = document["window"]
    body = (f'<h1>Exit trace</h1><p class="mono">{_linked(document["subject"], esc(document["subject"]), accounts)}</p>'
            f'<div class="card summary"><p>{data_text(trace.summary(document))}</p><p class="muted">Read '
            f'{esc(document["fetched_at"])}.</p></div>'
            f"<p>The window starts at {esc(window['from'])}, {esc(window['rule'])} {esc(window['incident_date'])}; "
            "older outbound rows are outside it.</p>"
            "<h2>Shares of the gross outbound transfer value</h2>" + _shares_table(document["shares"], accounts) +
            "<p>Shares are of the subject's gross outbound transfer value; deeper hops are shown and not re summed.</p>"
            f"<p>{esc(trace.gross_sentence(document))}</p>"
            "<h2>The subject at a block</h2>" + _state_card(document["state_at_block"]) +
            "<p class=\"muted\">Only an account that carries an incident label is traced. Each card is one transfer "
            "read from the FastNEAR Transfers API.</p>" + "".join(groups))
    return page("Exit trace", body, depth=1)


def _status_reads(reads):
    """The status reads counted by issuer view and answer, as one sentence."""
    answers = {}
    for read in reads:
        view = answers.setdefault((read["issuer"], read["method"]), {})
        answer = json.dumps(read["answer"], ensure_ascii=False)
        view[answer] = view.get(answer, 0) + 1
    views = "; ".join(f"{issuer} {method} answered "
                      + " and ".join(f"{answer} {count} times" for answer, count in counts.items())
                      for (issuer, method), counts in answers.items())
    blocks = [read["block"] for read in reads]
    times = [read["observed_at"] for read in reads]
    categories = ", ".join(issuers.STATUS_CATEGORIES[:-1]) + " and " + issuers.STATUS_CATEGORIES[-1]
    return (f"{len(reads)} status reads over {len({read['subject'] for read in reads})} account ids: the NEAR "
            f"subjects of the label store in the {categories} categories and the recorded "
            f"receiver set, each read on {len(answers)} issuer views at blocks {min(blocks)} to {max(blocks)}, from "
            f"{min(times)} to {max(times)}: {views}.")


def _history_coverage(history):
    """The history's coverage in the words its review allows: what the Transactions API lists, the block
    range, the actions found, and how many named account id strings do not exist on chain."""
    coverage = issuers.history_coverage()
    owner = issuers._owner(issuers.FIXTURE_DIR)
    observations = issuers._account_observations(issuers.FIXTURE_DIR)
    named = list(dict.fromkeys(row.subject for row in history))
    missing = [subject for subject in named if not observations[subject]["exists"]]
    blocks = sorted({observations[subject]["block"] for subject in missing})
    read = (f"All {coverage['transactions']}" if coverage["transactions"] == coverage["txs_count"]
            else f"{coverage['transactions']} of the {coverage['txs_count']}")
    found = [f"{count} {method}" for method, count in coverage["actions"].items() if count]
    absent = "".join(f", no {method}" for method, count in coverage["actions"].items() if not count)
    at = f" at block{'s' if len(blocks) > 1 else ''} {', '.join(str(block) for block in blocks)}" if blocks else ""
    return (f"{read} function call transactions the Transactions API lists for {json.dumps(owner)} (the account "
            f"the owner view of {HISTORY_TOKEN} answers) as receiver, transaction blocks {coverage['tx_block_min']} "
            f"to {coverage['tx_block_max']}, were read; their receipts carried {sum(coverage['actions'].values())} "
            f"successful issuer actions on {HISTORY_TOKEN}: {' and '.join(found)}{absent}. {len(missing)} of the "
            f"{len(named)} account id strings do not exist on chain{at}.")


def issuers_page():
    status = labels.load(issuers.STATUS_PATH).assertions
    history = labels.load(issuers.HISTORY_PATH).assertions
    status_cards = "".join(
        f'<div class="card"><p class="mono">{esc(row.subject)}</p><p>{data_text(row.label)}</p><p class="mono muted">'
        f'{esc(row.source["method"])} {esc(json.dumps(row.source["args"], ensure_ascii=False))} at block '
        f'{row.source["block"]} from {esc(row.source["rpc"])}<br>observed {esc(row.observed_at)}<br>row {esc(row.id)}'
        "</p></div>" for row in status)
    history_cards = "".join(
        f'<div class="card"><p class="mono">{esc(row.subject)}</p><p>{data_text(row.label)}</p><p>{data_text(row.note)}'
        f'</p><p class="mono muted">block {row.source["block"]}<br>transaction {esc(row.source["args"]["tx_hashes"][0])}'
        f'<br>observed {esc(row.observed_at)}<br>row {esc(row.id)}</p></div>' for row in history)
    body = ("<h1>Issuer actions</h1><h2>Status reads</h2>"
            f"<p>{data_text(_status_reads(issuers.reads()))}</p>"
            f"<h2>Banned by the issuer</h2><p>{len(status)} account ids read as banned by their issuer, one row each in "
            f"<code>intel/issuers/status.jsonl</code>; a status read is a view call, so no transaction.</p>{status_cards}"
            f"<h2>Issuer action history</h2><p>{data_text(_history_coverage(history))}</p><p>{len(history)} rows in "
            f"<code>intel/issuers/history.jsonl</code>, one per action, newest block first.</p>{history_cards}")
    return page("Issuer actions", body)


_RECORD_SECTIONS = (
    ("Record", ("version", "record_id", "kind", "outcome", "publisher", "supersedes")),
    ("Subjects", ("subjects",)),
    ("Evidence", ("evidence",)),
    ("Report citations", ("reports",)),
    ("Decisions and reasons", ("reasons",)),
    ("Assertion references", ("assertion_ids",)),
    ("Timeline", ("timeline",)),
)


def _record_value(value, path):
    if isinstance(value, dict):
        if not value:
            return f'<span data-record-path="{esc(path)}">{{}}</span>'
        return "<dl>" + "".join(
            f"<dt>{esc(key)}</dt><dd>{_record_value(child, f'{path}.{key}')}</dd>"
            for key, child in value.items()
        ) + "</dl>"
    if isinstance(value, list):
        if not value:
            return f'<span data-record-path="{esc(path)}">[]</span>'
        return "<ol>" + "".join(
            f"<li>{_record_value(child, f'{path}[{index}]')}</li>"
            for index, child in enumerate(value)
        ) + "</ol>"
    if value is None:
        text = "null"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return f'<span data-record-path="{esc(path)}">{esc(text)}</span>'


def record_page(record):
    sections = []
    for title, fields in _RECORD_SECTIONS:
        content = []
        for field in fields:
            value = record[field]
            if field == "reasons" and value:
                rendered = "".join(
                    f'<div class="card"><h3>{"Moderation" if reason["dispute_id"] is None else "Dispute"} '
                    f'decision</h3>{_record_value(reason, f"reasons[{index}]")}</div>'
                    for index, reason in enumerate(value)
                )
            else:
                rendered = _record_value(value, field)
            content.append(f"<dt>{esc(field)}</dt><dd>{rendered}</dd>")
        sections.append(f"<section><h2>{esc(title)}</h2><dl>{''.join(content)}</dl></section>")
    record_id = esc(record["record_id"])
    body = ("<h1>Incident record</h1>" + "".join(sections)
            + '<p class="muted">Re-check from the repository root with '
            + f"<code>python -m intel record-check fixtures/records/{record_id}.json</code>.</p>")
    return page("Incident record", body, depth=1)


def record_index_page(documents):
    entries = "".join(
        f'<li><a href="{esc(record["record_id"])}.html">{esc(record["record_id"])}</a>, '
        f'kind {esc(record["kind"])}, outcome {esc(record["outcome"])}</li>'
        for record in documents
    )
    body = ("<h1>Incident records</h1>" + (f"<ul>{entries}</ul>" if entries else "<p>No valid records.</p>"))
    return page("Incident records", body, depth=1)


def _incident_record_pages(records_dir=None):
    records_dir = RECORDS_DIR if records_dir is None else records_dir
    documents = []
    for path in sorted(Path(records_dir).glob("*.json")):
        if path.name == "body-vector.json":
            continue
        try:
            documents.append(records.read(path))
        except records.RecordError as error:
            print(f"skipped incident record {path}: {error}")
    pages = {"records/index.html": record_index_page(documents)}
    pages.update({f"records/{record['record_id']}.html": record_page(record) for record in documents})
    return pages


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


def verify_example(record_path=VERIFY_RECORD, evidence_dir=EVIDENCE_DIR):
    """(registry account, subject, evidence hash) of a committed registry record: the hash from the record,
    the subject and the Gate from its evidence document, and the registry as the Gate's registry sub account
    that scripts/setup_testnet.sh deploys."""
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    evidence = json.loads((Path(evidence_dir) / f"{record['evidence_hash']}.json").read_text(encoding="utf-8"))
    return f"registry.{evidence['subject']['gate']}", evidence["subject"]["receiver"], record["evidence_hash"]


def verify_page():
    script = VERIFY_SCRIPT.replace("RPC_URL", TESTNET_RPC)
    registry, subject, evidence_hash = (esc(value) for value in verify_example())
    body = ("<h1>Read a registry record</h1><p>The page calls <code>latest</code> on the registry contract over the "
            f"public testnet RPC <code>{TESTNET_RPC}</code> from your browser and shows the record next to the "
            "evidence hash you paste. Compute the hash yourself with <code>sha256sum</code> of the evidence file.</p>"
            "<p>The examples in the empty fields are the vendor's latest record, "
            "<code>fixtures/registry/testnet-latest-kestrelridge-shop.json</code>, and the sha256 of its evidence "
            "document.</p>"
            f'<div class="card"><label>Registry account<input id="registry" autocomplete="off" placeholder="{registry}">'
            f'</label><label>Subject (a NEAR account id)<input id="subject" autocomplete="off" placeholder="{subject}">'
            f'</label><label>Evidence hash (64 hex)<input id="hash" autocomplete="off" placeholder="{evidence_hash}">'
            '</label><button id="read" type="button">Read the record</button></div>'
            f'<pre id="out" class="card mono"></pre><script>{script}</script>')
    return page("Verify a record", body)


def not_found_page():
    body = ("<h1>Not found</h1><p>No page of this site and no route of the read only API answers this path.</p>"
            '<p><a href="/index.html">Back to the index</a></p>')
    return page("Not found", body, rooted=True)


def render(out_dir, store=None, incidents_dir=INCIDENTS_DIR, flows_dir=trace.FLOWS_DIR,
           clusters_dir=cluster.CLUSTERS_DIR, indexable=False):
    """Write every page under out_dir, without the robots noindex meta when indexable; returns the written
    paths relative to it."""
    store = store or labels.load()
    out = Path(out_dir)
    incidents, flows, clusters = _incidents(incidents_dir), _flows(flows_dir), _clusters(clusters_dir)
    registry_entities = [json.loads(line) for line in (ROOT / "intel" / "entities.jsonl").read_text(encoding="utf-8").splitlines()]
    accounts = account_pages(store)
    entity_paths = {entity_id: f"entities/{name}.html"
                    for entity_id, name in page_names(entity["id"] for entity in registry_entities).items()}
    search = search_index(store, accounts, registry_entities, entity_paths)
    pages = {"index.html": index_page(incidents, flows, clusters), "feed.html": feed_page(),
             "verify.html": verify_page(), "404.html": not_found_page(), "issuers.html": issuers_page()}
    pages.update(_incident_record_pages())
    for document in incidents:
        pages[f"incidents/{document['id']}.html"] = incident_page(document, accounts)
    for name, title, categories in ENTITY_SETS:
        pages[f"entities/{name}.html"] = entity_page(title, categories, store)
    for entity in registry_entities:
        pages[entity_paths[entity["id"]]] = registry_entity_page(entity, accounts)
    pages.update({path: account_page(subject, store, accounts, incidents, flows, clusters)
                  for subject, path in accounts.items()})
    for name, document in flows:
        pages[f"flows/{name}.html"] = flow_page(document, accounts)
    for name, document in clusters:
        pages[f"clusters/{name}.html"] = cluster_page(name, document, accounts)
    for relative, text in pages.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((text.replace(ROBOTS, "") if indexable else text).encode("utf-8"))
    (out / "style.css").write_text(STYLE, encoding="utf-8", newline="\n")
    (out / "search.json").write_text(json.dumps(search, ensure_ascii=False, separators=(",", ":")) + "\n",
                                     encoding="utf-8", newline="\n")
    return sorted([*pages, "search.json", "style.css"])
