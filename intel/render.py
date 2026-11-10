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
a test can tell it from Siyab's own words.
"""

import html
import importlib.util
import json
import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from intel import cluster, feed, graph, issuers, labels, records, trace

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"
RECORDS_DIR = ROOT / "fixtures" / "records"
TESTNET_RPC = "https://test.rpc.fastnear.com"
# The record the verify page offers as its example: a latest view of the testnet registry.
VERIFY_RECORD = ROOT / "fixtures" / "registry" / "testnet-latest-kestrelridge-shop.json"
EVIDENCE_DIR = ROOT / "fixtures" / "evidence"
TOP_RECEIVERS_JUDGEMENTS_DIR = ROOT / "fixtures" / "receivers" / "top" / "judgements"
TOP_RECEIVERS_RANKING_PATH = ROOT / "fixtures" / "receivers" / "top.json"
GITHUB_REPO = "https://github.com/Ridwannurudeen/shieldbot-near"
# The token whose issuer actions intel/issuers.py reads from the owner's transactions.
HISTORY_TOKEN = "usdt.tether-token.near"
HEADLINE = ("Siyab checks whether an account is " + "sa" + "fe"
            + " to pay, with a source on every answer, and enforces it on chain.")
COUNTS_OF = ("The counts below are of the recorded mainnet receivers the rules judged, in all and by cohort, the label "
             "store's assertions and subjects by provenance class, the incident pages, the blocks and events of the "
             "feed, the exit traces and their hops, and the verdict latency runs for the allowlisted payee and for a "
             "payee off the allowlist.")
ENTITY_SETS = (("treasuries", "Treasuries", ("treasury",)),
               ("catalog", "Catalog tokens", ("catalog_token",)),
               ("infrastructure", "Infrastructure and bridge accounts", ("infrastructure", "bridge")),
               ("organisations", "Organisations from on chain registries", ("organisation",)))
ROBOTS = "<meta name=\"robots\" content=\"noindex\">\n"
STYLE = (ROOT / "intel/ui/site.css").read_text(encoding="utf-8")


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


def page(title, body, depth=0, rooted=False, *, inert=False):
    """A whole HTML page; depth is how many directories below the site root it sits, and a rooted page
    links root absolute because it is served at any path."""
    up = "/" if rooted else "../" * depth
    current = {"Siyab": "index.html", "Feed": "feed.html", "Verify a record": "verify.html",
               "Incident records": "records/index.html", "Issuer actions": "issuers.html",
               **{label: f"entities/{name}.html" for name, label, _ in ENTITY_SETS}}.get(title)
    primary = (("index.html", "Home"), ("flows/rhea-collector.html", "Trace"), ("feed.html", "Feed"),
               ("index.html#tools", "Payment checks"), ("verify.html", "Verify"))
    secondary = (("index.html#explore-title", "Incidents"),
                 *((f"entities/{name}.html", label) for name, label, _ in ENTITY_SETS),
                 ("records/index.html", "Incident records"), ("issuers.html", "Issuers"))
    navigation = []
    for name, links in (("primary", primary), ("secondary", secondary)):
        items = "".join(f'<a href="{up}{href}"' + (' aria-current="page"' if href == current else "")
                        + f'>{esc(label)}</a>' for href, label in links)
        navigation.append(f'<nav class="app-{name}-navigation" aria-label="{name.title()}">{items}</nav>')
    nav = "".join(navigation)
    shell = "" if inert else (ROOT / "intel/ui/shell.js").read_text(encoding="utf-8")
    search_label = '<span class="site-search-label">Search accounts / entities</span><span class="site-search-short" aria-hidden="true">Search</span>'
    search = (f'<a class="site-search-link" aria-label="Search accounts and entities" href="{up}index.html#search">{search_label}</a>' if inert else
              f'<button class="site-search-button" type="button" aria-label="Search accounts and entities" data-open-search>{search_label} <kbd class="search-key">Ctrl K</kbd></button>'
              '<button class="theme-toggle" type="button" id="theme-toggle">Use light theme</button>')
    dialog = "" if inert else (
        '<dialog class="search-dialog" id="global-search" aria-label="Search accounts and entities">'
        '<form class="search-dialog-head" method="dialog"><input id="global-search-input" aria-label="Account or entity" autocomplete="off" placeholder="Search accounts and entities">'
        '<button aria-label="Close search">Close</button></form><div class="search-results" id="global-search-results" aria-live="polite"></div></dialog>')
    content_class = "app-content record-content" if inert else "app-content"
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n{ROBOTS}"
            f"<title>{esc(title)}</title>\n<link rel=\"stylesheet\" href=\"/style.css\">\n</head>\n<body>\n"
            '<a class="skip-link" href="#main-content">Skip to content</a>'
            f'<aside class="app-sidebar"><a class="site-brand" href="{up}index.html">Siyab</a>{nav}</aside>'
            f'<header class="app-topbar"><a class="app-mobile-brand" href="{up}index.html">Siyab</a>'
            f'<span class="app-context">{esc("Investigate" if title == "Siyab" else title)}</span>'
            f'<div class="header-actions">{search}</div>'
            f'<details class="mobile-navigation" id="mobile-navigation"><summary>Menu</summary><div class="mobile-navigation-panel">{nav}</div></details>'
            f'</header>{dialog}\n<main class="{content_class}" id="main-content">\n{body}\n</main>\n'
            '<footer class="app-footer"><p>Data: NearBlocks. Blocks: mainnet.neardata.xyz. Transfers: FastNEAR.</p></footer>\n'
            f'{"<script>" + shell + "</script>" if shell else ""}</body>\n</html>\n')


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


def _top_receivers_summary(judgements_dir, ranking_path=TOP_RECEIVERS_RANKING_PATH):
    paths = sorted(Path(judgements_dir).glob("*.json"))
    if not paths:
        return None

    documents = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if any(document.get("subject") != path.stem or document.get("verdict") not in ("ALLOW", "DENY", "UNKNOWN")
           or not isinstance(document.get("checks"), list)
           or any(not isinstance(check, dict) or not isinstance(check.get("id"), str)
                  or not isinstance(check.get("status"), str) for check in document["checks"])
           for path, document in zip(paths, documents)):
        raise ValueError("top receiver judgement is unreadable")

    verdicts = Counter(document["verdict"] for document in documents)
    unknown_causes = Counter()
    deny_groups = Counter()
    for document in documents:
        if document["verdict"] == "UNKNOWN":
            unknown_causes.update(check["id"] for check in document["checks"] if check["status"] == "unknown")
        elif document["verdict"] == "DENY":
            signals = tuple(sorted(check["id"] for check in document["checks"] if check["status"] == "signal"))
            deny_groups[signals] += 1

    ranking = {}
    if ranking_path is not None and Path(ranking_path).is_file():
        ranking = json.loads(Path(ranking_path).read_text(encoding="utf-8"))
    return {"total": len(documents), "verdicts": verdicts, "unknown_causes": unknown_causes,
            "deny_new_account": deny_groups[("account_age",)],
            "deny_lookalike_without_metadata": deny_groups[("code_without_metadata", "lookalike")],
            "deny_other": verdicts["DENY"] - deny_groups[("account_age",)]
            - deny_groups[("code_without_metadata", "lookalike")],
            "ranking": ranking}


INDEX_SCRIPT = (ROOT / "intel/ui/index.js").read_text(encoding="utf-8")


def _graph_section(payload, evidence_url=None):
    if evidence_url:
        payload["source"]["evidence_url"] = evidence_url
    for node in payload["nodes"]:
        if node["url"]:
            node["url"] = "/" + node["url"].removeprefix("../")
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    script = (ROOT / "intel/ui/graphs.js").read_text(encoding="utf-8")
    return ('<section data-siyab-graph aria-label="Recorded investigation graph">'
            f'<script type="application/json">{encoded}</script></section><script>{script}</script>')


def _feed_panel():
    return ('<article class="analytics-panel"><div class="analytics-panel-head"><h2>Follower health</h2>'
            '<a href="/feed.html">Open feed</a></div><div class="workspace-panel-body" id="feed-health" aria-live="polite">'
            '<p>Reading follower state. Live data may be unavailable on a static copy.</p></div>'
            '<div class="workspace-panel-body"><button id="feed-pause" type="button">Pause updates</button> '
            '<button id="feed-apply" type="button" hidden>Show new events</button>'
            '<div id="feed-events"></div></div></article>')


def _workspace(flows, clusters, store, summary):
    accounts = account_pages(store)
    graph_view = (_graph_section(graph.flow_payload(flows[0][1], accounts), f"/flows/{flows[0][0]}.html") if flows else
                  _graph_section(graph.cluster_payload(clusters[0][1], accounts), f"/clusters/{clusters[0][0]}.html") if clusters else
                  '<p>No recorded investigation is available.</p>')
    graph_link = (f'<a href="flows/{esc(flows[0][0])}.html">Open recorded trace and evidence</a>' if flows else "")
    provenance = Counter(a.provenance for a in store.assertions if a.superseded_by is None)
    total = sum(provenance.values())
    def bars(items, denominator):
        return ''.join(f'<div class="analytics-row"><span>{esc(label)}</span><div class="analytics-track">'
                       f'<div class="analytics-fill {esc(label.lower())}" style="width:{100 * count / max(1, denominator):.3f}%"></div>'
                       f'</div><strong class="num">{count}</strong></div>' for label, count in items)
    cohort = (bars(((key, summary["verdicts"][key]) for key in ("ALLOW", "DENY", "UNKNOWN")), summary["total"])
              if summary else '<p>Judgements are not yet published.</p>')
    return ('<section class="workspace-overview"><div class="workspace-heading"><div>'
            '<h1 id="page-title">Investigate</h1></div>'
            '<a class="button" href="#tools">Check a payment</a></div>'
            '<form class="workspace-search" id="search" action="/index.html" method="get">'
            '<input name="q" aria-label="Search recorded accounts and entities" placeholder="Find an account or entity" autocomplete="off">'
            '<button type="submit" data-workspace-search>Investigate</button></form>'
            '<noscript><p>Browse <a href="entities/infrastructure.html">infrastructure accounts</a>, <a href="entities/organisations.html">organisations</a> and the <a href="#explore-title">recorded investigations</a>.</p></noscript>'
            f'<p class="workspace-context">Recorded mainnet investigation. {graph_link}</p>'
            + graph_view + (_flow_analytics(flows[0][1], accounts) if flows else "") + '<div class="analytics-grid workspace-cohorts"><article class="analytics-panel">'
            '<div class="analytics-panel-head"><h2>Recorded receiver cohort</h2><a href="top-receivers.html">Sources and limits</a></div>'
            f'<div class="analytics-chart">{cohort}</div><p class="analytics-note">Recorded receiver judgements, not a rate over all NEAR payees.</p></article>'
            '<article class="analytics-panel"><div class="analytics-panel-head"><h2>Current label provenance</h2>'
            '<a href="#explore-title">Explore sources</a></div>'
            f'<div class="analytics-chart">{bars(sorted(provenance.items()), total)}</div>'
            '<p class="analytics-note">Current assertions in the committed label store. Inferred labels remain hypotheses.</p></article></div>'
            + _feed_panel() + '</section><script>'
            + (ROOT / "intel/ui/feed.js").read_text(encoding="utf-8") + '</script>')


def index_page(incidents, flows, clusters, store, top_judgements_dir=TOP_RECEIVERS_JUDGEMENTS_DIR):
    counts = "".join(f"<p>{esc(line)}</p>" for line in _counts_lines())
    label_count = store.counts()
    scan_tool_label = "Is this account " + "sa" + "fe" + " to use?"
    top_summary = _top_receivers_summary(top_judgements_dir)
    proofs = (
        ("Live report beat", "Report 0 was cited by a DENY verdict, disputed, then overturned. No payment was sent.",
         "fixtures/runs/live-beat-20261108T111313Z.txt"),
        ("Kill switch drill", "All four parts completed with matching readbacks. The Gate was paused, drained, restored and unpaused.",
         "fixtures/runs/kill-switch-drill-2026-11-08.txt"),
        ("Recorder rotation", "The registry recorder moved to the owner and back. Each readback matched.",
         "fixtures/runs/recorder-rotation-2026-11-08.txt"),
        ("Registry gates", "The 7 Nov readback shows 7 verdict records over 5 subjects and 43 labels. Invoice 5 has no registry record.",
         "fixtures/runs/registry-readback-20261107.txt"),
    )
    proof_cards = "".join(
        f'<article class="proof-card"><h3>{esc(title)}</h3><p class="muted">Testnet record</p>'
        f'<p>{esc(description)}</p><a href="{GITHUB_REPO}/blob/main/{path}" rel="noopener">'
        f'<span class="mono">{esc(path)}</span></a></article>'
        for title, description, path in proofs)
    incident_links = " ".join(
        f'<a class="button" href="incidents/{esc(document["id"])}.html">{esc(document["title"])}</a>'
        for document in incidents)
    flow_links = " ".join(
        f'<a class="button" href="flows/{esc(name)}.html">{esc(document["subject"])}</a>'
        for name, document in flows)
    cluster_links = " ".join(
        f'<a class="button" href="clusters/{esc(name)}.html">{esc(name)}</a>'
        for name, _ in clusters)
    entity_links = " ".join(
        f'<a class="button" href="entities/{name}.html">{esc(title)}</a>'
        for name, title, _ in ENTITY_SETS)
    top_state = "Judgements are published." if top_summary else "Scan in progress, not yet published."
    top_counts = (f'<p class="muted">{top_summary["total"]} judged: {top_summary["verdicts"]["ALLOW"]} ALLOW, '
                  f'{top_summary["verdicts"]["DENY"]} DENY, {top_summary["verdicts"]["UNKNOWN"]} UNKNOWN.</p>'
                  if top_summary else "")
    body = (
        '<section class="hero-grid" aria-labelledby="payment-introduction"><div class="hero-copy">'
        f'<p id="payment-introduction">{esc(HEADLINE)}</p>'
        '<div class="hero-topline"><p class="muted">NEAR PAYMENT PREFLIGHT</p></div>'
        '<p class="lede">Every answer names its source. ALLOW, ASK and DENY are the outcomes. Only an ALLOW is signed, and the Gate contract enforces it.</p>'
        '<div class="outcomes" aria-label="Verdict outcomes"><span class="outcome allow">ALLOW</span>'
        '<span class="outcome ask">ASK</span><span class="outcome deny">DENY</span></div>'
        '</div><form class="lookup-card" id="account-form"><label for="account-input">Paste any NEAR account'
        '<input id="account-input" name="account" type="text" autocomplete="off" spellcheck="false" '
        'placeholder="account.near" required></label>'
        '<label for="account-amount">Amount in NEAR<input id="account-amount" name="amount" type="text" '
        'inputmode="decimal" value="1" required></label><button class="primary" type="submit">Check account</button>'
        '<div id="account-labels" class="account-results" aria-live="polite"></div></form></section>'
        '<section aria-labelledby="account-results-title"><div class="section-head"><div>'
        '<h2 id="account-results-title">Payment and key checks</h2><p class="muted">Live read</p></div></div><div class="live-grid">'
        '<article class="panel"><h3>Live verdict</h3><p class="muted">A live read runs when you submit an account.</p>'
        '<div id="account-verdict" class="results" aria-live="polite"></div></article>'
        '<article class="panel"><h3>Access key scan</h3><p class="muted">Key findings are a read, not a payment verdict.</p>'
        '<div id="account-scan" class="results" aria-live="polite"></div></article></div></section>'
        '<section id="tools" aria-labelledby="tools-title"><h2 id="tools-title">Run a check</h2><p class="muted">Live tools</p>'
        '<div class="tool-tabs" role="tablist" aria-label="Live check tools">'
        '<button type="button" role="tab" id="tab-verdict" aria-controls="panel-verdict" aria-selected="true">Can I pay this?</button>'
        f'<button type="button" role="tab" id="tab-scan" aria-controls="panel-scan" aria-selected="false">{esc(scan_tool_label)}</button>'
        '<button type="button" role="tab" id="tab-intent" aria-controls="panel-intent" aria-selected="false">Where is this swap really going?</button>'
        '</div><section class="tool-panel panel" id="panel-verdict" role="tabpanel" aria-labelledby="tab-verdict">'
        '<form class="tool-form" id="tool-verdict-form"><label for="tool-receiver">Receiver'
        '<input id="tool-receiver" name="receiver" type="text" required></label>'
        '<label for="tool-amount">Amount in NEAR<input id="tool-amount" name="amount" type="text" '
        'inputmode="decimal" value="1" required></label><label for="tool-token">Token'
        '<input id="tool-token" name="token" type="text" value="near" required></label>'
        '<button class="primary" type="submit">Check payment</button></form><div id="tool-verdict-result" class="results" aria-live="polite"></div></section>'
        '<section class="tool-panel panel" id="panel-scan" role="tabpanel" aria-labelledby="tab-scan" hidden>'
        '<form class="tool-form" id="tool-scan-form"><label for="tool-account">NEAR account'
        '<input id="tool-account" name="account" type="text" required></label><button class="primary" type="submit">Scan keys</button>'
        '</form><div id="tool-scan-result" class="results" aria-live="polite"></div></section>'
        '<section class="tool-panel panel" id="panel-intent" role="tabpanel" aria-labelledby="tab-intent" hidden>'
        '<form class="tool-form" id="tool-intent-form"><label for="destination-asset">Destination asset ID'
        '<input id="destination-asset" name="destination_asset" type="text" required></label>'
        '<label for="intent-recipient">Recipient address<input id="intent-recipient" name="recipient" type="text" required></label>'
        '<label for="intent-amount">Amount in base units<input id="intent-amount" name="amount" type="text" '
        'inputmode="numeric" required></label><button class="primary" type="submit">Check destination</button></form>'
        '<div id="tool-intent-result" class="results" aria-live="polite"></div></section></section>'
        '<section aria-labelledby="proof-title"><h2 id="proof-title">Proven on testnet</h2><p class="muted">Committed run logs</p>'
        f'<div class="proof-grid">{proof_cards}</div></section>'
        '<section aria-labelledby="reports-title"><h2 id="reports-title">Reports with evidence</h2><p class="muted">Community reports</p>'
        '<div class="report-tools"><article class="panel"><h3>Recent reports</h3><div id="recent-reports" class="report-list" '
        'aria-live="polite"></div></article><article class="panel"><h3>Look up a subject</h3>'
        '<form class="report-form" id="report-lookup-form"><label for="report-subject">NEAR account'
        '<input id="report-subject" name="subject" type="text" required></label><button type="submit">Look up reports</button>'
        '</form><div id="report-lookup-result" class="results" aria-live="polite"></div></article></div>'
        '<article class="panel"><h3>How to report</h3><p>Use the documented testnet command with a reports account, subject, kind, outcome, evidence hash and evidence URL.</p>'
        '<code>scripts/report.sh &lt;reports_account&gt; &lt;subject&gt; &lt;kind&gt; &lt;outcome&gt; &lt;evidence_sha256&gt; &lt;evidence_url&gt;</code>'
        '<p class="muted">Browser wallet signing for reports is out of scope.</p></article></section>'
        '<section aria-labelledby="receivers-title"><div class="section-head"><div>'
        f'<h2 id="receivers-title">Top receivers</h2><p class="muted">Mainnet judgement set</p><p class="muted">{esc(top_state)}</p>{top_counts}</div>'
        '<a class="button" href="top-receivers.html">Open aggregate results</a></div></section>'
        '<section aria-labelledby="explore-title"><h2 id="explore-title">Explore</h2><p class="muted">Committed intelligence</p>'
        '<div class="explore-grid">'
        '<article class="explore-card"><h3>Feed</h3><p>Recorded blocks and events.</p><a href="feed.html">Open feed</a></article>'
        '<article class="explore-card"><h3>Verify</h3><p>Compare a registry record with its evidence hash.</p><a href="verify.html">Verify a record</a></article>'
        '<article class="explore-card"><h3>Incidents</h3><p>Source pages and account evidence.</p>'
        f'{incident_links}</article><article class="explore-card"><h3>Incident records</h3>'
        '<p>Published records and report citations.</p><a href="records/index.html">Open records</a></article>'
        f'<article class="explore-card"><h3>Fund flows</h3><p>Recorded exit traces.</p>{flow_links}</article>'
        f'<article class="explore-card"><h3>Clusters</h3><p>Account links with their source and confidence.</p>{cluster_links}</article>'
        f'<article class="explore-card"><h3>Entities</h3><p>Registry entities and label sets.</p>{entity_links}</article>'
        '<article class="explore-card"><h3>Issuers</h3><p>Issuer status reads and action history.</p>'
        '<a href="issuers.html">Open issuer records</a></article></div>'
        '<div class="coverage"><details><summary>Committed data counts</summary><p>' + esc(COUNTS_OF) + '</p>'
        f'<div class="card">{counts}</div><p class="muted">Every count is read from committed files by '
        '<code>scripts/counts.py</code>.</p></details></div></section>'
        '<section class="limits" aria-labelledby="limits-title"><h2 id="limits-title">What these reads mean</h2><p class="muted">Scope and limits</p>'
        '<ul><li>The Gate and Reports contracts shown here run on testnet. Live verdict, scan and swap checks read mainnet.</li>'
        '<li>8 Nov demonstration configuration: One team moderator today.</li>'
        f'<li>Label store size: {label_count["assertions"]} assertions over {label_count["subjects"]} subjects.</li>'
        '<li>Live API checks are read only, rate limited and never sign.</li></ul>'
        f'<p class="muted">The following quotation describes the recorded 8 Nov demonstration, not current contract totals. <a href="{GITHUB_REPO}/blob/main/docs/TESTNET.md">Dated reports updates</a> include the later threshold run.</p>'
        '<blockquote class="data">The only report on the testnet Reports contract is the synthetic one filed on 8 Nov by our own accounts against our demo account and overturned by the same owner in that run; a totals read of 1 is not a community signal.</blockquote>'
        '<blockquote class="data">The receiver lines of the counts block are the replay of a recorded mainnet receiver set, not a rate over all NEAR payees.</blockquote>'
        '<p class="muted">An unreadable check returns no ALLOW. Unknown never passes.</p></section>'
        f'<script>{INDEX_SCRIPT}</script>'
    )
    return page("Siyab", _workspace(flows, clusters, store, top_summary) + body)


def search_index(store, pages, entities, entity_paths):
    accounts = {}
    current = [a for a in store.assertions if a.superseded_by is None]
    for assertion in current:
        entry = accounts.setdefault(assertion.subject, {"type": "account", "subject": assertion.subject,
                                                           "labels": [], "label_sources": [],
                                                           "url": pages[assertion.subject]})
        if assertion.label not in entry["labels"]:
            entry["labels"].append(assertion.label)
        entry["label_sources"].append({"label": assertion.label, "chain": assertion.chain,
                                       "provenance": assertion.provenance, "source": assertion.source,
                                       "observed_at": assertion.observed_at})
    return {"accounts": [accounts[subject] for subject in sorted(accounts)],
            "entities": [{"type": "entity", "id": entity["id"], "name": entity["name"],
                          "url": entity_paths[entity["id"]]} for entity in entities]}


def top_receivers_page(judgements_dir=TOP_RECEIVERS_JUDGEMENTS_DIR, ranking_path=TOP_RECEIVERS_RANKING_PATH):
    summary = _top_receivers_summary(judgements_dir, ranking_path)
    if not summary:
        body = ("<h1>Top receivers</h1><p class=\"empty-state\">Scan in progress, not yet published.</p>"
                "<p>Aggregate results appear after committed judgement files are available.</p>")
        return page("Top receivers", body)

    ranking = summary["ranking"]
    window = ranking.get("window")
    if isinstance(window, dict):
        window_text = (f'Window: {window["hours"]} hours, {esc(window["start_timestamp"])} '
                       f'(height {window["start_height"]}) through {esc(window["end_timestamp"])} '
                       f'(height {window["end_height"]}).')
    else:
        window_text = "Ranking window is not available."
    rule = ranking.get("rule", "")
    order = re.search(r"Receivers are ordered by ([^.]+)\.", rule)
    rule_text = f"Receivers are ordered by {order.group(1)}." if order else "Ranking rule is not available."
    causes = "".join(f"<li>{esc(cause)}: {count}</li>"
                      for cause, count in sorted(summary["unknown_causes"].items())) or "<li>None recorded.</li>"
    other_deny = (f'<p>Other DENY signal groups: {summary["deny_other"]}.</p>' if summary["deny_other"] else "")
    body = ("<h1>Top receivers</h1><p>Aggregate results for the committed mainnet receiver ranking.</p>"
            f"<p>{window_text}</p><p>Ranking rule: {esc(rule_text)}</p>"
            f'<p>Judged {summary["total"]}: {summary["verdicts"]["ALLOW"]} ALLOW, '
            f'{summary["verdicts"]["DENY"]} DENY, {summary["verdicts"]["UNKNOWN"]} UNKNOWN.</p>'
            "<h2>UNKNOWN causes</h2><p>Counts overlap.</p><ul>" + causes + "</ul>"
            "<h2>DENY split</h2>"
            f'<p>New account (account_age signal only): {summary["deny_new_account"]}.</p>'
            f'<p>Lookalike with code without metadata: {summary["deny_lookalike_without_metadata"]}.</p>'
            + other_deny + "<p>Each verdict is what an agent's 1 NEAR payment to that receiver would get, not a finding "
            "that the account is malicious.</p>"
            f'<p>See the <a href="{GITHUB_REPO}/tree/main/fixtures/receivers/top/judgements/">committed judgement files</a> '
            "for per-account results.</p>")
    return page("Top receivers", body)


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
    return page("Feed", _feed_panel() + body + '<script>' + (ROOT / "intel/ui/feed.js").read_text(encoding="utf-8") + '</script>')


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
    rows = "".join(f'<tr role="row"><td role="cell"><span class="cell-label" aria-hidden="true">Chain</span>{esc(a.chain)}</td><td role="cell"><span class="cell-label" aria-hidden="true">Label</span>{data_text(a.label)}</td><td role="cell"><span class="cell-label" aria-hidden="true">Category</span>{esc(a.category)}</td>'
                   f'<td role="cell"><span class="cell-label" aria-hidden="true">Provenance</span>{esc(a.provenance)}</td><td role="cell"><span class="cell-label" aria-hidden="true">Note</span>{data_text(a.note)}</td><td role="cell" class="mono"><span class="cell-label" aria-hidden="true">Source</span><q class="data">'
                   f'{esc(json.dumps(a.source, ensure_ascii=False, sort_keys=True))}</q></td></tr>'
                   for a in assertions if a.provenance != "inferred")
    provenance = Counter(a.provenance for a in assertions)
    overview = "".join(f'<span>{esc(kind)}<strong>{count}</strong></span>' for kind, count in sorted(provenance.items()))
    body = (f'<div class="profile-heading"><div><h1>Account</h1>'
            f'<p class="mono"><q class="data">{esc(subject)}</q></p></div>'
            '<a class="button" href="/index.html#tools">Payment and key checks</a></div>'
            f'<section class="workspace-panel profile-overview"><div class="workspace-panel-head"><h2>Recorded evidence overview</h2></div>'
            f'<div class="workspace-panel-body analytics-inline profile-provenance">{overview}</div>'
            '<p class="analytics-note">Current label assertions by provenance. These records do not establish a live balance or payment result.</p></section>'
            f'<h2>Current label rows from sources and chain reads</h2><div class="table"><table class="account-assertions" role="table"><thead role="rowgroup"><tr role="row">'
            f'<th scope="col" role="columnheader">Chain</th><th scope="col" role="columnheader">Label</th><th scope="col" role="columnheader">Category</th><th scope="col" role="columnheader">Provenance</th><th scope="col" role="columnheader">Note</th><th scope="col" role="columnheader">Source</th></tr>'
            f'</thead><tbody role="rowgroup">{rows}</tbody></table></div>')
    inferred = [a for a in assertions if a.provenance == "inferred"]
    if inferred:
        by_id = {a.id: a for a in store.assertions}
        rows = "".join(f'<tr role="row"><td role="cell"><span class="cell-label" aria-hidden="true">Chain</span>{esc(a.chain)}</td><td role="cell"><span class="cell-label" aria-hidden="true">Label</span>{data_text(a.label)}</td><td role="cell"><span class="cell-label" aria-hidden="true">Category</span>{esc(a.category)}</td>'
                       f'<td role="cell" class="mono"><span class="cell-label" aria-hidden="true">Rule</span><q class="data">{esc(a.rule_id)}</q></td>'
                       f'<td role="cell" class="mono"><span class="cell-label" aria-hidden="true">Inputs</span>{_inputs(a, by_id, accounts)}</td><td role="cell"><span class="cell-label" aria-hidden="true">Confidence</span>{a.confidence}</td>'
                       f'<td role="cell"><span class="cell-label" aria-hidden="true">Note</span>{data_text(a.note)}</td></tr>' for a in inferred)
        body += ('<h2>Attribution hypothesis</h2><p>Each row below is inferred: it names the rule that inferred '
                 'it, the assertions the rule read as its inputs and its confidence.</p><div class="table"><table class="account-inferred" role="table">'
                 '<thead role="rowgroup"><tr role="row"><th scope="col" role="columnheader">Chain</th><th scope="col" role="columnheader">Label</th><th scope="col" role="columnheader">Category</th><th scope="col" role="columnheader">Rule</th><th scope="col" role="columnheader">Inputs</th>'
                 '<th scope="col" role="columnheader">Confidence</th><th scope="col" role="columnheader">Note</th></tr></thead>'
                 f'<tbody role="rowgroup">{rows}</tbody></table></div>')
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
    body = ('<div class="profile-heading"><div>'
            f'<h1>{esc(entity["kind"].replace("_", " ").title())}</h1></div>'
            f'<p>{len(entity["members"])} recorded members</p></div>'
            f'<p><q class="data">{esc(entity["name"])}</q></p>'
            f'<div class="profile-provenance"><p>Registry read: {esc(source["method"])} at block {source["block"]}, '
            f'arguments {esc(json.dumps(source["args"], sort_keys=True))}</p>'
            f'<p>Observed at {esc(entity["observed_at"])} on {esc(source["rpc"])}</p></div>'
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
    context = f"<h1>Account relationship edges: {esc(name)}</h1><p>{esc(document['standard'])}</p>"
    body = (f"<h2>Seeds</h2><p>{len(document['seeds'])} seeds, each with a current incident label in the label "
            f"store.</p><ul>{seeds}</ul>"
            "<h2>Edges from chain reads</h2>" + ("".join(_edge_card(edge, facts, accounts) for edge in chain)
                                                 or "<p>No edge from chain reads.</p>") +
            "<h2>Attribution hypothesis</h2><p>Each edge below is inferred from the facts named as its inputs, at "
            "the confidence shown; it is a hypothesis, not a chain read, and no verdict reads it.</p>"
            + ("".join(_edge_card(edge, facts, accounts) for edge in inferred) or "<p>No inferred edge.</p>") +
            f"<h2>Precision</h2><ul>{precision}</ul><h2>Depositors</h2>{counted}"
            f"<p class=\"muted\">Read {esc(document['fetched_at'])}. docs/CLUSTER.md states the heuristics and their "
            "limits.</p>")
    return page(f"Account relationship edges: {name}", context + _graph_section(graph.cluster_payload(document, accounts), f"/clusters/{name}.html") + body, depth=1)


def _hop_card(hop, accounts):
    usd = "" if hop["usd_amount"] is None else f', {hop["usd_amount"]} USD as the Transfers API values it'
    return (f'<div class="card"><p class="mono">{_linked(hop["from"], esc(hop["from"]), accounts)} to '
            f'{_linked(hop["to"], esc(hop["to"]), accounts)}</p><p>{esc(hop["amount"])} of '
            f'<span class="mono">{esc(hop["asset_id"])}</span>{esc(usd)}, method {esc(hop["method_name"] or "none")}</p>'
            f'<p class="mono muted">block {hop["block"]} at {esc(trace.utc_minute(hop["block_timestamp"]))}<br>transaction '
            f'{esc(hop["transaction_hash"])}<br>receipt {esc(hop["receipt_id"])}</p></div>')


def _flow_timeline(document):
    timestamps = [int(hop["block_timestamp"]) for hop in document["hops"] if hop["from"] == document["subject"]]
    if not timestamps:
        return []
    first, last = min(timestamps), max(timestamps)
    span = last - first + 1
    size = min(12, span)
    counts = Counter(min(size - 1, (stamp - first) * size // span) for stamp in timestamps)
    return [{"from_ns": first + (index * span + size - 1) // size,
             "to_ns": first + ((index + 1) * span + size - 1) // size,
             "count": counts[index]} for index in range(size)]


def _flow_analytics(document, accounts):
    shares = document["shares"]
    distribution_rows = []
    for index, bucket in enumerate(shares["buckets"]):
        label = _subject_cell(bucket["to"], accounts).replace('href="../', 'href="/')
        full_account = ""
        if re.fullmatch(r"[0-9a-f]{64}", bucket["to"]):
            full_account = f'<details class="analytics-full-account"><summary>Full identifier</summary>{label}</details>'
            short = bucket["to"][:12] + "…" + bucket["to"][-10:]
            label = _linked(bucket["to"], f'<q class="data">{esc(short)}</q>', accounts).replace('href="../', 'href="/')
        distribution_rows.append(
            f'<div class="analytics-row analytics-distribution-row" data-share-bucket="{index}" data-usd="{esc(bucket["usd"])}">'
            f'<div class="analytics-counterparty">{label}<small>{esc(bucket["kind"])}</small>{full_account}</div>'
            f'<span class="analytics-usd num"><span class="cell-label" aria-hidden="true">USD</span>{Decimal(bucket["usd"]):,.2f} USD</span>'
            f'<span class="analytics-count num"><span class="cell-label" aria-hidden="true">Rows</span>{bucket["count"]} rows</span>'
            f'<strong class="analytics-share num"><span class="cell-label" aria-hidden="true">Share</span>{esc(bucket["percent"])}%</strong>'
            f'<div class="analytics-track"><div class="analytics-fill" style="width:{max(0, min(100, float(bucket["percent"]))):.2f}%"></div></div>'
            '</div>')
    distribution = "".join(distribution_rows)
    below, unknown = shares["below_min_usd"], shares["without_usd"]
    units = ", ".join(f"{amount} of {asset}" for asset, amount in unknown["base_units"].items()) or "none recorded"
    timeline = _flow_timeline(document)
    if timeline:
        def utc(stamp, exact=False):
            seconds = datetime.fromtimestamp(stamp // 1000000000, UTC).strftime("%Y-%m-%d %H:%M:%S")
            return seconds + (f'.{stamp % 1000000000:09d} UTC' if exact else " UTC")
        maximum = max(row["count"] for row in timeline)
        bars, rows = [], []
        for index, row in enumerate(timeline):
            height = 100 * row["count"] / maximum
            bars.append(f'<rect x="{index * 40 + 3}" y="{100 - height:.3f}" width="34" height="{height:.3f}">'
                        f'<title>{row["count"]} transfers</title></rect>')
            rows.append(f'<tr data-from-ns="{row["from_ns"]}" data-to-ns="{row["to_ns"]}">'
                        f'<td>{utc(row["from_ns"], True)}</td><td>{utc(row["to_ns"], True)}</td><td class="num">{row["count"]}</td></tr>')
        total = sum(row["count"] for row in timeline)
        first, last = timeline[0]["from_ns"], timeline[-1]["to_ns"] - 1
        ticks = []
        for stamp in (first, first + (last - first) // 2, last):
            instant = datetime.fromtimestamp(stamp // 1000000000, UTC)
            ticks.append(f'<time datetime="{instant.strftime("%Y-%m-%dT%H:%M:%SZ")}">{instant.strftime("%Y-%m-%d")}<br>{instant.strftime("%H:%M")} UTC</time>')
        gridlines = ''.join(f'<line class="analytics-gridline" x1="0" x2="{len(timeline) * 40}" y1="{level}" y2="{level}"/>'
                            for level in (0, 50, 100))
        chart = (f'<p class="analytics-note">{total} root outbound transfers from {utc(timeline[0]["from_ns"])} '
                 f'to {utc(timeline[-1]["to_ns"] - 1)}. Counts include rows with unknown USD; deeper hops are excluded.</p>'
                 '<div class="analytics-timeline-chart">'
                 f'<div class="analytics-count-axis" aria-label="Transfer count scale"><span>{maximum}</span><span>{(maximum + 1) // 2}</span><span>0</span></div>'
                 f'<svg class="analytics-histogram" viewBox="0 0 {len(timeline) * 40} 100" preserveAspectRatio="none" role="img" aria-label="Recorded root outbound transfer counts">'
                 f'<title>{total} recorded transfers in {len(timeline)} time bins</title>{gridlines}{"".join(bars)}</svg>'
                 f'<div class="analytics-time-axis">{"".join(ticks)}</div></div>'
                 '<details class="analytics-bin-values"><summary>Exact time bounds and transfer counts</summary>'
                 '<p>Bins include their start and exclude their end; nanosecond bounds come from recorded block timestamps.</p>'
                 '<div class="table"><table><thead><tr><th>From UTC (inclusive)</th><th>To UTC (exclusive)</th><th>Transfers</th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table></div></details>')
    else:
        chart = '<p class="analytics-note">No recorded root outbound transfers.</p>'
    return ('<section class="analytics-grid flow-analytics" aria-label="Full recorded flow analytics">'
            f'<article class="analytics-panel" data-direct-usd="{esc(shares["direct_outbound_usd"])}"><div class="analytics-panel-head"><h2>Direct outbound distribution</h2></div>'
            f'<p class="analytics-note">Full recorded dataset, not the current graph filter. Denominator: {Decimal(shares["direct_outbound_usd"]):,.2f} USD '
            f'across {shares["rows"]} root outbound rows valued at or above {esc(shares["min_usd"])} USD by the Transfers API. '
            'Deeper hops are excluded; this is gross transfer value, not loss.</p>'
            '<div class="analytics-chart"><div class="analytics-distribution-head" aria-hidden="true"><span>Counterparty</span><span>USD</span><span>Rows</span><span>Share</span></div>'
            f'{distribution or "<p>No direct outbound rows meet the USD threshold.</p>"}</div>'
            f'<p class="analytics-note">{below["count"]} rows below {esc(shares["min_usd"])} USD: {Decimal(below["usd"]):,.2f} USD, counted separately. '
            f'{unknown["count"]} rows without a USD amount; their value is unknown, not zero. Base units: {esc(units)}.</p></article>'
            '<article class="analytics-panel"><div class="analytics-panel-head"><h2>Recorded transfer activity</h2></div>'
            '<p class="analytics-note">Full recorded dataset, not the current graph filter. Transfer counts, not a USD or price history.</p>'
            f'{chart}</article></section>')


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


def flow_page(document, accounts, *, evidence_url=None):
    depths = trace.hop_depths(document)
    groups = []
    for depth in sorted(set(depths)):
        hops = [hop for hop, d in zip(document["hops"], depths) if d == depth]
        groups.append(f"<h2>Depth {depth}: {len(hops)} hops</h2>" + "".join(_hop_card(hop, accounts) for hop in hops))
    window = document["window"]
    context = f'<h1>Exit trace</h1><p class="mono">{_linked(document["subject"], esc(document["subject"]), accounts)}</p>'
    body = (f'<div class="card summary"><p>{data_text(trace.summary(document))}</p><p class="muted">Read '
            f'{esc(document["fetched_at"])}.</p></div>'
            f"<p>The window starts at {esc(window['from'])}, {esc(window['rule'])} {esc(window['incident_date'])}; "
            "older outbound rows are outside it.</p>"
            "<h2>Shares of the gross outbound transfer value</h2>" + _shares_table(document["shares"], accounts) +
            "<p>Shares are of the subject's gross outbound transfer value; deeper hops are shown and not re summed.</p>"
            f"<p>{esc(trace.gross_sentence(document))}</p>"
            "<h2>The subject at a block</h2>" + _state_card(document["state_at_block"]) +
            "<p class=\"muted\">Only an account that carries an incident label is traced. Each card is one transfer "
            "read from the FastNEAR Transfers API.</p>" + "".join(groups))
    return page("Exit trace", context + _graph_section(graph.flow_payload(document, accounts), evidence_url) + _flow_analytics(document, accounts) + body, depth=1)


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
    return page("Incident record", body, depth=1, inert=True)


def record_index_page(documents):
    entries = "".join(
        f'<li><a href="{esc(record["record_id"])}.html">{esc(record["record_id"])}</a>, '
        f'kind {esc(record["kind"])}, outcome {esc(record["outcome"])}</li>'
        for record in documents
    )
    body = ("<h1>Incident records</h1>" + (f"<ul>{entries}</ul>" if entries else "<p>No valid records.</p>"))
    return page("Incident records", body, depth=1, inert=True)


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
            f'<div class="card verify-form"><label>Registry account<input id="registry" autocomplete="off" placeholder="{registry}">'
            f'</label><label>Subject (a NEAR account id)<input id="subject" autocomplete="off" placeholder="{subject}">'
            f'</label><label>Evidence hash (64 hex)<input id="hash" autocomplete="off" placeholder="{evidence_hash}">'
            '</label><button id="read" type="button">Read the record</button></div>'
            f'<pre id="out" class="verify-result mono" aria-live="polite"></pre><script>{script}</script>')
    return page("Verify a record", body)


def not_found_page():
    body = ("<h1>Not found</h1><p>No page of this site and no route of the read only API answers this path.</p>"
            '<p><a href="/index.html">Back to the index</a></p>')
    return page("Not found", body, rooted=True)


def render(out_dir, store=None, incidents_dir=INCIDENTS_DIR, flows_dir=trace.FLOWS_DIR,
           clusters_dir=cluster.CLUSTERS_DIR, indexable=False, *, top_judgements_dir=TOP_RECEIVERS_JUDGEMENTS_DIR):
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
    pages = {"index.html": index_page(incidents, flows, clusters, store, top_judgements_dir),
             "top-receivers.html": top_receivers_page(top_judgements_dir), "feed.html": feed_page(),
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
        pages[f"flows/{name}.html"] = flow_page(document, accounts, evidence_url=f"/flows/{name}.html")
    for name, document in clusters:
        pages[f"clusters/{name}.html"] = cluster_page(name, document, accounts)
    for relative, text in pages.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((text.replace(ROBOTS, "") if indexable else text).encode("utf-8"))
    (out / "style.css").write_text(STYLE, encoding="utf-8", newline="\n")
    (out / "search.json").write_text(json.dumps(search, ensure_ascii=False, separators=(",", ":")) + "\n",
                                     encoding="utf-8", newline="\n")
    fonts = []
    for source in sorted((ROOT / "intel/ui/fonts").iterdir()):
        relative = f"fonts/{source.name}"
        (out / "fonts").mkdir(exist_ok=True)
        (out / relative).write_bytes(source.read_bytes())
        fonts.append(relative)
    return sorted([*pages, "search.json", "style.css", *fonts])
