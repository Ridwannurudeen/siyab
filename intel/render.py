"""python -m intel render --out <dir>: the static hosted pages, built from the committed JSON files.

No framework or third-party script; shared local content-hashed scripts and one stylesheet, style.css.
The
pages: index.html (the landing page), explore.html (the recorded investigation workspace, a search box whose inline script filters
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
import hashlib
import base64
import importlib.util
import json
import re
import random
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from urllib.parse import urlsplit

from gate import rules
from intel import cluster, feed, graph, issuers, labels, records, trace
from intel.live import LiveAPI

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"
RECORDS_DIR = ROOT / "fixtures" / "records"
REPLAYS_DIR = ROOT / "fixtures" / "replay"
TESTNET_RPC = "https://test.rpc.fastnear.com"
# The record the verify page offers as its example: a latest view of the testnet registry.
VERIFY_RECORD = ROOT / "fixtures" / "registry" / "testnet-latest-kestrelridge-shop.json"
EVIDENCE_DIR = ROOT / "fixtures" / "evidence"
TOP_RECEIVERS_JUDGEMENTS_DIR = ROOT / "fixtures" / "receivers" / "top" / "judgements"
TOP_RECEIVERS_RANKING_PATH = ROOT / "fixtures" / "receivers" / "top.json"
# The token whose issuer actions intel/issuers.py reads from the owner's transactions.
HISTORY_TOKEN = "usdt.tether-token.near"
HEADLINE = ("Siyab checks an account before you pay, with a source on every answer, and a Gate contract "
            "on testnet pays only on a signed ALLOW.")
COUNTS_OF = ("The counts below are of the recorded mainnet receivers the rules judged, in all and by cohort, the label "
             "store's assertions and subjects by provenance class, the incident pages, the blocks and events of the "
             "feed, the exit traces and their hops, and the verdict latency runs for the allowlisted payee and for a "
             "payee off the allowlist.")
ENTITY_SETS = (("treasuries", "Treasuries", ("treasury",)),
               ("catalog", "Catalog tokens", ("catalog_token",)),
               ("infrastructure", "Infrastructure and bridge accounts", ("infrastructure", "bridge")),
               ("organisations", "Organisations from on chain registries", ("organisation",)))
ROBOTS = "<meta name=\"robots\" content=\"noindex\">\n"
STYLE = (ROOT / "intel/ui/site.css").read_text(encoding="utf-8") + "".join(
    "\n" + path.read_text(encoding="utf-8") for path in sorted((ROOT / "intel/ui").glob("*.css"))
    if path.name != "site.css")
SITE_ORIGIN = "https://siyab.xyz"
BRAND_MARK = '<svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><path d="M26 6H12L6 14l19 5-6 7H5" fill="none" stroke="currentColor" stroke-width="5" stroke-linecap="square" stroke-linejoin="bevel"/></svg>'


def _script_asset(source):
    digest = hashlib.sha256(source.encode("utf-8"))
    return f"/assets/{digest.hexdigest()}.js", "sha256-" + base64.b64encode(digest.digest()).decode("ascii")


def _script_tag(source):
    url, integrity = _script_asset(source)
    return f'<script src="{url}" integrity="{integrity}" defer></script>'


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
        quoted = _UNESCAPE.sub(r"\1", match.group(1))
        css = "data data-identifier" if re.fullmatch(r"(?:nep141:)?[a-z0-9]+(?:[._-][a-z0-9]+)*\.(?:near|testnet)", quoted) else "data"
        parts.append(f'<q class="{css}">{esc(quoted)}</q>')
        last = match.end()
    return "".join(parts) + esc(str(text)[last:])


def _web_url(url):
    """True for an http or https URL with a host, the only kind of source URL rendered as a link."""
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def quote_block(quote, sources):
    source = sources.get(quote["source"], {})
    text = re.sub(r"[0-9a-f]{64}", r'<span class="data-id">\g<0></span>', esc(quote["text"]))
    url = source.get("url") if _web_url(source.get("url", "")) else ""
    return (f'<blockquote class="data" cite="{esc(url)}">{text}'
            f'<br><span class="muted">quoted from '
            + (f'<a href="{esc(url)}" rel="noopener">{esc(source.get("publisher", quote["source"]))}</a>' if url
               else esc(source["publisher"]) if source.get("url") and source.get("publisher")
               else f'[{esc(quote["source"])}]') + '</span></blockquote>')


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


def page(title, body, depth=0, rooted=False, *, inert=False, landing=False):
    """A whole HTML page; depth is how many directories below the site root it sits, and a rooted page
    links root absolute because it is served at any path."""
    body = re.sub(r"(<h1(?: [^>]*)?>.*?</h1>)", r'<div class="page-head">\1</div>', body, count=1) if body.startswith("<h1") else body
    up = "/" if rooted else "../" * depth
    current = {"Siyab": "index.html", "Explore": "explore.html", "Investigate account": "investigate.html", "Workspace": "workspace.html", "Feed": "feed.html", "Verify a record": "verify.html",
               "Incident records": "records/index.html", "Issuer actions": "issuers.html",
               **{label: f"entities/{name}.html" for name, label, _ in ENTITY_SETS}}.get(title)
    primary = (("index.html", "Home"), ("explore.html", "Explore"), ("investigate.html", "Account lookup"), ("flows/rhea-collector.html", "Trace"), ("feed.html", "Feed"), ("workspace.html", "Workspace"),
               ("explore.html#tools", "Payment checks"), ("verify.html", "Verify"))
    secondary = (("explore.html#incidents", "Incidents"),
                 *((f"entities/{name}.html", label) for name, label, _ in ENTITY_SETS),
                 ("records/index.html", "Incident records"), ("issuers.html", "Issuers"))
    tools = (("explore.html#tools", "Payment checks"), ("verify.html", "Verify"))

    def items(links):
        return "".join(f'<a href="{up}{href}"' + (' aria-current="page"' if href == current else "")
                       + f'>{esc(label)}</a>' for href, label in links)
    nav = "".join(f'<nav class="app-{name}-navigation" aria-label="Mobile {name}">{items(links)}</nav>'
                  for name, links in (("primary", primary), ("secondary", secondary)))
    header_nav = (f'<nav class="app-header-navigation" aria-label="Primary">{items(link for link in primary if link not in tools)}</nav>'
                  + "".join(f'<details class="app-menu"><summary>{label}</summary>'
                            f'<nav class="app-menu-panel" aria-label="{label}">{items(links)}</nav></details>'
                            for label, links in (("Explore", secondary), ("Tools", tools))))
    landing_nav = ('<nav class="landing-navigation" aria-label="Primary">'
                   + ''.join(f'<a href="{up}{href}">{label}</a>' for href, label in
                             (("explore.html", "Explore"), ("investigate.html", "Account lookup"),
                              ("explore.html#incidents", "Incidents"), ("verify.html", "Verify"))) + '</nav>') if landing else ''
    shell = "" if inert else (ROOT / "intel/ui/shell.js").read_text(encoding="utf-8")
    workspace_script = "" if inert else (ROOT / "intel/ui/workspace.js").read_text(encoding="utf-8")
    cloud_script = "" if inert else (ROOT / "intel/ui/cloud-workspace.js").read_text(encoding="utf-8")
    number_script = "" if inert else (ROOT / "intel/ui/number-format.js").read_text(encoding="utf-8")
    search_label = '<span class="site-search-label">Search accounts / entities</span><span class="site-search-short" aria-hidden="true">Search</span>'
    search = ('<details class="record-theme"><summary>Theme</summary><fieldset><legend>Theme</legend>'
              '<input type="radio" id="record-theme-dark" name="record-theme" value="dark" checked><label for="record-theme-dark">Dark</label>'
              '<input type="radio" id="record-theme-light" name="record-theme" value="light"><label for="record-theme-light">Light</label></fieldset></details>'
              f'<a class="site-search-link" aria-label="Search accounts and entities" href="{up}explore.html#search">{search_label}</a>' if inert else
              f'<button class="site-search-button" type="button" aria-label="Search accounts and entities" data-open-search>{search_label} <kbd class="search-key">Ctrl K</kbd></button>'
              '<button class="theme-toggle" type="button" id="theme-toggle" aria-label="Switch to light theme"><span class="theme-toggle-label">Use light theme</span></button>')
    dialog = "" if inert else (
        '<dialog class="search-dialog" id="global-search" aria-label="Search accounts and entities">'
        '<form class="search-dialog-head" method="dialog"><input id="global-search-input" aria-label="Account or entity" autocomplete="off" placeholder="Search accounts and entities">'
        '<button aria-label="Close search">Close</button></form><div class="search-results" id="global-search-results" aria-live="polite"></div></dialog>'
        '<dialog class="case-dialog" id="save-investigation" aria-labelledby="case-dialog-title"><h2 id="case-dialog-title">Save investigation</h2>'
        '<form id="case-form"><label for="case-title">Title<input id="case-title" maxlength="120" required></label>'
        '<label for="case-notes">Notes<textarea id="case-notes" maxlength="4000" rows="5"></textarea></label>'
        '<p id="case-source" class="source-line"></p><p id="case-error" role="alert"></p><div class="workspace-actions">'
        '<button class="primary" type="submit">Save investigation</button><button id="case-cancel" type="button">Cancel</button></div></form></dialog>')
    content_class = "app-content record-content" if inert else "app-content"
    description = "Investigate NEAR accounts with sourced labels, recorded activity and read-only payment checks. Inspect the evidence and its coverage."
    metadata = (f'<meta name="description" content="{esc(description)}">\n<meta property="og:type" content="website">\n'
                f'<meta property="og:title" content="{esc(title if title == "Siyab" else title + " | Siyab")}">\n'
                f'<meta property="og:description" content="{esc(description)}">\n'
                f'<meta property="og:image" content="{SITE_ORIGIN}/brand/social.png">\n'
                '<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">\n'
                '<meta name="twitter:card" content="summary_large_image">\n'
                f'<meta name="twitter:image" content="{SITE_ORIGIN}/brand/social.png">\n'
                '<link rel="icon" type="image/svg+xml" href="/brand/favicon.svg"><link rel="alternate icon" href="/favicon.ico">\n')
    theme_script = "" if inert else "<script>try{var t=localStorage.getItem('siyab-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}</script>\n"
    mobile_landing_nav = landing_nav.replace('aria-label="Primary"', 'aria-label="Mobile primary"', 1)
    frame = (f'<header class="app-topbar"><a class="site-brand" href="{up}index.html">{BRAND_MARK}<span>Siyab</span></a>'
             f'{landing_nav}<div class="header-actions">{search}</div>'
             f'<details class="mobile-navigation" id="mobile-navigation"><summary>Menu</summary>'
             f'<div class="mobile-navigation-panel">{mobile_landing_nav}</div></details></header>' if landing else
             f'<header class="app-topbar app-header"><a class="site-brand" href="{up}index.html">{BRAND_MARK}<span>Siyab</span></a>'
             f'{header_nav}<div class="header-actions">{search}</div>'
             f'<details class="mobile-navigation" id="mobile-navigation"><summary>Menu</summary><div class="mobile-navigation-panel">{nav}</div></details>'
             '</header>')
    return (f"<!doctype html>\n<html lang=\"en\">\n<head>\n{theme_script}<meta charset=\"utf-8\">\n"
            '<meta name="color-scheme" content="dark light">\n'
            '<meta name="theme-color" content="#0A0D16">\n'
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n{ROBOTS}"
            f"<title>{esc(title)}</title>\n{metadata}<link rel=\"stylesheet\" href=\"/style.css\">\n"
            + (_script_tag(number_script) if number_script else "") + '</head>\n'
            + ('<body class="landing-page">\n' if landing else '<body>\n')
            + '<a class="skip-link" href="#main-content">Skip to content</a>'
            + f'{frame}{dialog}\n<main class="{content_class}" id="main-content">\n{body}\n</main>\n'
            '<footer class="app-footer"><p>Data: NearBlocks. Blocks: mainnet.neardata.xyz. Transfers: FastNEAR.</p></footer>\n'
            + ('' if inert else '<p id="workspace-feedback" class="workspace-feedback" role="status" aria-live="polite"></p>')
            + f'{"<script>" + shell + "</script>" if shell else ""}'
            + f'{_script_tag(workspace_script) if workspace_script else ""}'
            + f'{_script_tag(cloud_script) if cloud_script else ""}'
            + (_script_tag((ROOT / "intel/ui/landing.js").read_text(encoding="utf-8")) if landing else '')
            + '</body>\n</html>\n')


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


def _graph_asset(payload, evidence_url=None):
    payload = {**payload, "source": dict(payload["source"]), "nodes": [dict(node) for node in payload["nodes"]]}
    if evidence_url:
        payload["source"]["evidence_url"] = evidence_url
    for node in payload["nodes"]:
        if node["url"]:
            node["url"] = "/" + node["url"].removeprefix("../").lstrip("/")
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    return f'/graph-data/{hashlib.sha256(encoded.encode("utf-8")).hexdigest()}.json', encoded


def _graph_section(payload, evidence_url=None):
    url, _ = _graph_asset(payload, evidence_url)
    script = (ROOT / "intel/ui/graphs.js").read_text(encoding="utf-8")
    evidence = f'<a href="{esc(evidence_url)}">Open recorded evidence</a>' if evidence_url else 'Recorded evidence follows below.'
    return (f'<section class="siyab-graph" data-siyab-graph data-graph-kind="{esc(payload["kind"])}" data-graph-src="{url}" aria-label="Recorded investigation graph">'
            '<div class="graph-loading" role="status"><h2>Recorded investigation</h2><p>Interactive graph with recorded evidence.</p></div>'
            f'<noscript><p>JavaScript is needed for the interactive graph. {evidence}</p></noscript>'
            f'</section>{_script_tag(script)}')


def _display_amount(value, places, *, decimals=0):
    try:
        if len(str(value)) > 160:
            return "Unavailable"
        with localcontext() as context:
            context.prec = 340
            amount = Decimal(str(value)).scaleb(-decimals)
            if not amount.is_finite() or abs(amount.adjusted()) > 255:
                return "Unavailable"
            quantum = Decimal(1).scaleb(-places)
            rounded = amount.quantize(quantum, rounding=ROUND_HALF_UP)
            if amount != 0 and rounded == 0:
                return (">-" if amount < 0 else "<") + format(quantum, "f")
            result = format(rounded, ",f")
            return result.rstrip("0").rstrip(".") if "." in result else result
    except (InvalidOperation, ValueError):
        return "Unavailable"


_YOCTO_PROSE = re.compile(r"(?<![0-9.,])\b([0-9]{1,39}) yoctoNEAR\b")
_MS_PROSE = re.compile(r"(?<![0-9.,])\b([0-9]{1,15}) ms\b")


def _display_duration(ms):
    """A readable duration for display prose; the exact milliseconds stay in the event record."""
    if ms < 1000:
        return f"{ms} ms"
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} min" + (f" {rest} s" if rest else "")
    hours, rest = divmod(seconds, 3600)
    return f"{hours} h" + (f" {rest // 60} min" if rest // 60 else "")


def _display_detail(text):
    """Event description prose with NEAR and duration units matching the Amount column; never the stored record."""
    def near(match):
        amount = _display_amount(match.group(1), 4, decimals=24)
        return match.group(0) if amount == "Unavailable" else amount + " NEAR"
    return _MS_PROSE.sub(lambda match: _display_duration(int(match.group(1))), _YOCTO_PROSE.sub(near, text))


def _event_detail(event):
    """The escaped readable description of an event, or unknown when it has none."""
    detail = event.get("detail") if isinstance(event, dict) else None
    return esc(_display_detail(detail)) if isinstance(detail, str) else "unknown"


def _feed_panel(event_rows=(), *, feed_link='<a href="/feed">Open raw feed JSON</a>'):
    rows = []
    for event, event_id in reversed(event_rows):
        observed = event.get("observed") if isinstance(event.get("observed"), dict) else {}
        stamp = observed.get("timestamp_ms")
        try:
            instant = datetime.fromtimestamp(stamp / 1000, UTC) if type(stamp) in (int, float) else None
            when = (f'<time datetime="{instant.isoformat()}">{instant.strftime("%Y-%m-%d")}<br>'
                    f'{instant.strftime("%H:%M:%S")} UTC</time>' if instant else "Not recorded")
        except (OverflowError, OSError, ValueError):
            when = "Not recorded"
        amount = (f'{esc(_display_amount(event["usd_amount"], 2))} USD (provider value)' if event.get("usd_amount") is not None else
                  f'{esc(_display_amount(event["amount_yocto"], 4, decimals=24))} NEAR' if event.get("amount_yocto") is not None else "Not recorded")
        exact_amount = (f'{event["usd_amount"]} USD (provider value)' if event.get("usd_amount") is not None else
                        f'{event["amount_yocto"]} yoctoNEAR' if event.get("amount_yocto") is not None else "Not recorded")
        exact_signal = "".join("unknown" if event.get(key) is None else str(event[key]) for key in ("signal", "detail"))
        exact_signal = re.sub(r"\s+", " ", exact_signal.strip())
        source = event.get("source") if isinstance(event.get("source"), dict) else {}
        provider = source.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            provider = "Provider not recorded"
            if isinstance(source.get("url"), str):
                try:
                    url = urlsplit(source["url"])
                    if url.scheme in ("http", "https") and url.hostname:
                        provider = url.hostname
                except ValueError:
                    provider = "Provider not recorded"
        counterparty = feed.event_counterparty(event)
        evidence_link = (f'<a href="/evidence/{event_id}">Open evidence</a>'
                         if re.fullmatch(r"[0-9a-f]{64}", event_id) else "Unlinked record")
        cells = [when,
                 f'<strong>{_event_field(event, "signal")}</strong><p>{_event_detail(event)}</p>',
                 f'<q class="data">{_event_field(event, "subject")}</q>',
                 f'<q class="data">{esc(counterparty)}</q>' if counterparty else "Not recorded",
                 f'<span class="feed-amount">{amount}</span>'
                 + (f'<small>{_event_field(event, "asset_id")}</small>' if event.get("asset_id") else ""),
                 f'{esc(provider)}<br>{evidence_link}'
                 f'<details><summary>Record details</summary><p>Block {_event_field(event, "observed", "block_height")}</p>'
                 f'<p>Transaction {_event_field(event, "tx_hash")}</p><p>Receipt {_event_field(event, "receipt_id")}</p>'
                 f'<p>Evidence identifier {esc(event_id)}</p><pre>{esc(json.dumps(event, ensure_ascii=False, indent=2))}</pre></details>']
        labels_for_cells = ("Observed (UTC)", "Signal", "Account", "Counterparty", "Amount", "Source and evidence")
        rows.append('<tr>' + ''.join(f'<td data-label="{label}"' + (f' data-export-value="{esc(exact_signal)}"' if label == "Signal" else
                                    f' data-export-value="{esc(exact_amount)}"' if label == "Amount" else "")
                                    + f'>{value}</td>' for label, value in zip(labels_for_cells, cells)) + '</tr>')
    headers = ''.join(f'<th scope="col">{label}</th>' for label in
                      ("Observed (UTC)", "Signal", "Account", "Counterparty", "Amount", "Source and evidence"))
    initial = ('<button type="button" data-export-table="feed-signals">Export shown events CSV</button>'
               f'<table class="feed-table" id="feed-signals"><caption>Recorded signals, newest first</caption>'
               f'<thead><tr>{headers}</tr></thead><tbody>{"".join(rows)}</tbody></table>' if rows else
               '<p class="feed-empty">No events loaded. Read follower health for the observed window; an empty feed does not establish a favorable payment result.</p>')
    return ('<article class="analytics-panel feed-panel"><div class="analytics-panel-head"><h2>Follower health</h2>'
            + feed_link + '</div><div class="workspace-panel-body" id="feed-health" aria-live="polite">'
            '<p>Reading follower state. Live data may be unavailable on a static copy.</p></div>'
            '<div class="feed-actions"><button id="feed-pause" type="button">Pause updates</button> '
            '<button id="feed-apply" type="button" hidden>Show new events</button>'
            '<button id="feed-retry" type="button" hidden>Retry feed</button><span id="feed-update-status" role="status" aria-live="polite">Awaiting current read</span></div>'
            '<div class="feed-filters" id="feed-filters" hidden><label>Signal<select id="feed-signal"><option value="">All signals</option></select></label>'
            '<label>Account or evidence<input id="feed-query" type="search" autocomplete="off" placeholder="Search this response"></label>'
            '<label>Source<select id="feed-source"><option value="">All sources</option></select></label>'
            '<label>Observed since (UTC)<input id="feed-since" type="datetime-local"></label>'
            '<button id="feed-clear" type="button">Clear filters</button></div>'
            f'<div class="feed-results" id="feed-events">{initial}</div></article>')


def _workspace(flows, clusters, store, summary):
    accounts = account_pages(store)
    graph_view = (_graph_section(graph.flow_payload(flows[0][1], accounts), f"/flows/{flows[0][0]}.html") if flows else
                  _graph_section(graph.cluster_payload(clusters[0][1], accounts), f"/clusters/{clusters[0][0]}.html") if clusters else
                  '<p>No recorded investigation is available.</p>')
    graph_link = (f'<a href="flows/{esc(flows[0][0])}.html">Open recorded trace and evidence</a>' if flows else "")
    provenance = store.counts()["provenance"]
    total = sum(provenance.values())
    def bars(items, denominator):
        return ''.join(f'<div class="analytics-row"><span>{esc(label)}</span><div class="analytics-track">'
                       f'<div class="analytics-fill {esc(label.lower())}" style="width:{100 * count / max(1, denominator):.3f}%"></div>'
                       f'</div><strong class="num">{count}</strong></div>' for label, count in items)
    cohort = (bars(((key, summary["verdicts"][key]) for key in ("ALLOW", "DENY", "UNKNOWN")), summary["total"])
              if summary else '<p>Judgements are not yet published.</p>')
    return ('<section class="workspace-overview"><div class="workspace-heading page-head"><div>'
            '<h1 id="page-title">Investigate</h1></div>'
            '<a class="button" href="#tools">Check a payment</a></div>'
            '<form class="workspace-search" id="search" action="/explore.html" method="get">'
            '<input name="q" aria-label="Search recorded accounts and entities" placeholder="Find an account or entity" autocomplete="off">'
            '<button type="submit" data-workspace-search>Investigate</button></form>'
            '<noscript><p>Browse <a href="entities/infrastructure.html">infrastructure accounts</a>, <a href="entities/organisations.html">organisations</a> and the <a href="#explore-title">recorded investigations</a>.</p></noscript>'
            f'<p class="workspace-context">Recorded mainnet investigation. {graph_link}</p>'
            + graph_view + (_flow_analytics(flows[0][1], accounts) if flows else "") + '<div class="analytics-grid workspace-cohorts"><article class="analytics-panel">'
            '<div class="analytics-panel-head"><h2>Recorded receiver cohort</h2><a href="top-receivers.html">Sources and limits</a></div>'
            f'<div class="analytics-chart">{cohort}</div><p class="analytics-note">Recorded receiver judgements, not a rate over all NEAR payees.</p></article>'
            '<article class="analytics-panel"><div class="analytics-panel-head"><h2>Label store provenance</h2>'
            '<a href="#explore-title">Explore sources</a></div>'
            f'<div class="analytics-chart">{bars(sorted(provenance.items()), total)}</div>'
            '<p class="analytics-note">All assertions in the committed label store, including superseded records. Inferred labels remain hypotheses.</p></article></div>'
            + _feed_panel(feed_link='<a href="/feed.html">Open feed</a>') + '</section><script>'
            + (ROOT / "intel/ui/feed.js").read_text(encoding="utf-8") + '</script>')


def explore_page(incidents, flows, clusters, store, top_judgements_dir=TOP_RECEIVERS_JUDGEMENTS_DIR):
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
        f'<p>{esc(description)}</p><span class="mono">{esc(path)}</span></article>'
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
    top_state = "Aggregate counts are published." if top_summary else "Scan in progress, not yet published."
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
        '<label for="tool-amount"><span id="tool-amount-label">Amount in NEAR</span><input id="tool-amount" name="amount" type="text" '
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
        '<article class="explore-card" id="incidents"><h3>Incidents</h3><p>Source pages and account evidence.</p>'
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
        '<li>9 Nov recorded demonstration: three team moderators, with two approvals to accept a report. Recorded threshold run: <span class="mono">fixtures/runs/reports-threshold-demo-20261109.txt</span>.</li>'
        f'<li>Label store size: {label_count["assertions"]:,} assertion{"" if label_count["assertions"] == 1 else "s"} over {label_count["subjects"]:,} subject{"" if label_count["subjects"] == 1 else "s"}.</li>'
        '<li>Live API checks are read only, rate limited and never sign.</li></ul>'
        '<p class="muted">The following quotation describes the recorded 8 Nov demonstration, not current contract totals. The later threshold run is recorded in the 9 Nov demonstration above.</p>'
        '<blockquote class="data">The only report on the testnet Reports contract is the synthetic one filed on 8 Nov by our own accounts against our demo account and overturned by the same owner in that run; a totals read of 1 is not a community signal.</blockquote>'
        '<blockquote class="data">The receiver lines of the counts block are the replay of a recorded mainnet receiver set, not a rate over all NEAR payees.</blockquote>'
        '<p class="muted">An unreadable check returns no ALLOW. Unknown never passes.</p></section>'
        f'<script>{INDEX_SCRIPT}</script>'
    )
    return page("Explore", _workspace(flows, clusters, store, top_summary) + body)


def _landing_graph(document, accounts, excerpt):
    payload = graph.flow_payload(document, accounts)
    depths = {record["id"]: record["depth"] for record in payload["records"]}
    highlighted = {(hop["from"], hop["to"], hop["asset_id"]) for hop in excerpt}
    selected = [edge for edge in payload["edges"] if (edge["from"], edge["to"], edge["asset_id"]) in highlighted]
    node_ids = list(dict.fromkeys([payload["root"], *(edge["to"] for edge in selected)]))
    for node in payload["nodes"]:
        if len(node_ids) >= 41:
            break
        if node["id"] not in node_ids:
            node_ids.append(node["id"])
    edges = selected + [edge for edge in payload["edges"] if edge not in selected
                        and edge["from"] in node_ids and edge["to"] in node_ids][:60 - len(selected)]
    layout = random.Random("siyab-rhea-collector")
    positions = {payload["root"]: (100, 280)}
    for index, node_id in enumerate(node_ids[1:]):
        positions[node_id] = (330 + (index % 7) * 72 + layout.randrange(-18, 19),
                              50 + (index // 7) * 86 + layout.randrange(-18, 19))
    for node_id, position in zip(node_ids[1:3], ((405, 180), (555, 395))):
        positions[node_id] = position
    paths = []
    for edge in edges:
        x1, y1 = positions[edge["from"]]
        x2, y2 = positions[edge["to"]]
        bend = (x1 + x2) / 2
        css = "landing-edge is-highlight" if edge in selected else "landing-edge"
        depth = min(depths[record_id] for record_id in edge["record_ids"])
        paths.append(f'<path class="{css}" data-edge-id="{esc(edge["id"])}" data-depth="{depth}" pathLength="1" '
                     f'd="M{x1} {y1}C{bend:g} {y1} {bend:g} {y2} {x2} {y2}"/>')
    nodes = []
    for node_id, (x, y) in positions.items():
        css = "landing-graph-node is-root" if node_id == payload["root"] else "landing-graph-node"
        nodes.append(f'<circle class="{css}" data-node-id="{esc(node_id)}" cx="{x}" cy="{y}" '
                     f'r="{7 if node_id == payload["root"] else 3}"/>')
    captions = []
    for hop in excerpt:
        x, y = positions[hop["to"]]
        captions.append(f'<text class="landing-graph-caption" x="{x + 12}" y="{y - 12}">{esc(_transfer_amount(hop))}</text>')
    return ('<svg class="landing-graph-svg" viewBox="0 0 850 560" aria-hidden="true" focusable="false" data-landing-sequence="graph">'
            + ''.join(paths + nodes + captions) + '</svg>')


def index_page(incidents, flows, clusters, store, top_judgements_dir=TOP_RECEIVERS_JUDGEMENTS_DIR):
    document = dict(flows)["rhea-collector"]
    accounts = account_pages(store)
    excerpt = [hop for hop in document["hops"] if hop["from"] == document["subject"]][:2]
    status = (f'Recorded mainnet investigation. {len(document["hops"]):,} recorded transfer{"" if len(document["hops"]) == 1 else "s"}. '
              f'Depth reached: {document["depth_reached"]} of {document["depth"]}. '
              + trace.stop_state(document["completeness"]).capitalize() + '.')
    nodes = []
    for subject in [document["subject"], *(hop["to"] for hop in excerpt)]:
        short = subject[:8] + "..." + subject[-8:]
        value = f'<q class="data" title="{esc(subject)}">{esc(short)}</q>'
        if subject in accounts:
            value = f'<a href="/{accounts[subject]}">{value}</a>'
        nodes.append(f'<div class="landing-node">{value}</div>')
    inspector = []
    transfers = []
    for hop in excerpt:
        amount = f'{_transfer_amount(hop)} units of <q class="data">{esc(hop["asset_id"])}</q>.'
        when = trace.utc_minute(hop["block_timestamp"])
        transaction = (f'<a href="https://nearblocks.io/txns/{esc(hop["transaction_hash"])}" rel="noreferrer">'
                       f'{esc(hop["transaction_hash"])}</a>')
        evidence = (f'<dl><div><dt>From</dt><dd class="mono">{esc(hop["from"])}</dd></div>'
                    f'<div><dt>To</dt><dd class="mono">{esc(hop["to"])}</dd></div>'
                    f'<div><dt>Block</dt><dd class="mono">{hop["block"]}</dd></div>'
                    f'<div><dt>Transaction</dt><dd class="mono">{transaction}</dd></div>'
                    f'<div><dt>Receipt</dt><dd class="mono">{esc(hop["receipt_id"])}</dd></div></dl>')
        inspector.append(f'<details><summary>{when}</summary><p>{amount}</p>{evidence}</details>')
        transfers.append(f'<tr><td><time>{when}</time></td><td><p>{amount}</p>'
                         f'<details><summary>Transaction and receipt</summary>{evidence}</details></td></tr>')
    network = _landing_graph(document, accounts, excerpt)
    record = json.loads(VERIFY_RECORD.read_text(encoding="utf-8"))
    _, subject, evidence_hash = verify_example()
    computed_hash = hashlib.sha256((EVIDENCE_DIR / f'{evidence_hash}.json').read_bytes()).hexdigest()
    recorded_at = datetime.fromtimestamp(record["recorded_ms"] / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    comparison = ''.join(f'<tr><th scope="row">{label}</th><td>{esc(value)}</td></tr>' for label, value in
                         (("Subject", subject), ("Registry verdict", record["verdict"]), ("Recorded time", recorded_at),
                          ("Saved file comparison", "Equal" if computed_hash == record["evidence_hash"] else "Different")))
    counts = re.sub(r"\d{4,}", lambda match: f"{int(match.group()):,}",
                    next(line.split(" (")[0] + "." for line in _counts_lines() if line.startswith("Label store:")))
    attack = json.loads((ROOT / "fixtures/rpc/attacks/10-injected.json").read_text(encoding="utf-8"))
    refusal = attack["submissions"][0]["outcome"]["status"]["Failure"]["ActionError"]["kind"]["FunctionCallError"]["ExecutionError"].split(": ", 1)[1]
    exchange = 'Nothing was signed.' if attack["submissions"][0]["sent"]["args"]["verdict"] is None else 'Unavailable'
    capabilities = ''.join(f'<div><h3><a href="{url}">{title}</a></h3><p>{description}</p></div>' for title, url, description in (
        ("Account lookup", "/investigate.html", "Finalized account state, indexed activity and the evidence behind each observation."),
        ("Fund flows", "/flows/rhea-collector.html", "Recorded transfers with transaction hashes and blocks."),
        ("Clusters", "/clusters/rhea.html", "Account links with their source and confidence. Each edge names its heuristic and provenance."),
        ("Feed", "/feed.html", "Follow finalized mainnet blocks, with the source block behind each event."),
        ("Issuers", "/issuers.html", "Issuer status reads and action history."),
        ("Organisations", "/entities/organisations.html", "Accounts from on chain registries, with a source on each assertion.")))
    body = (
        '<div class="landing"><section class="landing-hero" aria-labelledby="landing-title">'
        f'{network}<div class="landing-hero-copy"><div class="page-head"><p class="landing-label">(01)  SIYAB</p>'
        '<h1 id="landing-title"><span>Security intelligence</span> <span>for NEAR, with a source on every row.</span></h1></div>'
        '<p>Sourced labels, incident records and payment checks for people, apps and AI agents on NEAR.</p>'
        '<p>Siyab checks the receiver, token, amount and memo of a payment before it is sent, and shows the evidence behind every answer. For AI agents, a Gate contract on testnet holds the agent\'s funds and pays only on a signed ALLOW bound to that exact transfer.</p>'
        '<div class="landing-actions"><a class="button primary" href="/explore.html">Investigate</a>'
        '<a class="button" href="/verify.html">Verify a record</a></div>'
        f'<p class="landing-status">{esc(status)}</p></div>'
        f'<div class="landing-counter-block"><span class="landing-counter">{len(document["hops"]):,}</span><span>RECORDED TRANSFERS</span>'
        '<button type="button" class="landing-replay" hidden>Replay trace</button></div>'
        '<figure class="landing-graph"><figcaption>Recorded evidence</figcaption><div class="landing-preview">'
        '<div class="landing-identities">' + ''.join(nodes) + '</div><div class="landing-inspector">' + ''.join(inspector) + '</div></div>'
        '<a class="landing-trace" href="/flows/rhea-collector.html">the complete trace</a></figure></section>'
        '<section class="landing-section" aria-labelledby="landing-problem"><p class="landing-label">(02)  THE PROBLEM</p>'
        '<h2 id="landing-problem">What if an invoice is poisoned?</h2><div class="landing-columns landing-problem-layout"><div>'
        '<p>One recorded case:</p>'
        '<p class="landing-statement"><span>A scripted agent follows an instruction planted in a vendor message and asks to pay an account named in a public incident report. The verdict service answers DENY.</span> The agent submits anyway, without a verdict, and the Gate refuses the transfer.</p></div>'
        f'<div class="landing-exchange"><p>{esc(exchange)}</p><p class="mono">{esc(refusal)}</p>'
        '<p class="landing-note">A scripted agent stands in for a prompt injected model. No model runs.</p>'
        '<a href="/incidents/rhea.html">Rhea Lend margin trading route exploit</a></div></div>'
        '<ol class="landing-chips" data-landing-sequence="pipeline">'
        + ''.join('<li><span>' + text + '</span>' + ('<svg class="landing-connector" viewBox="0 0 40 16" aria-hidden="true"><path pathLength="1" d="M0 8H38M32 2L38 8L32 14"/></svg>' if index < 3 else '') + '</li>'
                  for index, text in enumerate(("Changed payment details.", "A lookalike receiver.", "A receiver with an incident label.", "A check that cannot run."))) + '</ol></section>'
        '<section class="landing-section" aria-labelledby="landing-process"><p class="landing-label">(03)  HOW IT WORKS</p>'
        '<div class="landing-columns landing-process-layout"><div><h2 id="landing-process"><span>Check.</span> <span>Enforce.</span> <span>Verify.</span></h2><ol class="landing-steps">'
        '<li><h3>Check</h3><p>Judge the receiver, token, amount and memo before a person, an app or an agent pays. A check that cannot run reads Unknown.</p></li>'
        '<li><h3>Enforce</h3><p>The agent’s function call key can only request a transfer from the Gate. The Gate requires a signed ALLOW and enforces the owner’s payment policy.</p></li>'
        '<li><h3>Verify</h3><p>Compare a registry record with its evidence hash.</p></li></ol></div>'
        '<div><div class="landing-product"><div class="landing-product-bar"><span class="landing-window-dots" aria-hidden="true"><i></i><i></i><i></i></span>'
        f'<span>siyab / verify / {esc(subject)}</span></div><table class="landing-record"><thead><tr><th scope="col">Field</th><th scope="col">Recorded value</th></tr></thead><tbody>{comparison}</tbody></table></div>'
        '<p class="landing-note">Historical example, not a current network response. Hash equality does not verify a signature or establish the truth of the evidence sources.</p>'
        '<a href="/verify.html">Verify a record</a></div></div></section>'
        '<section class="landing-section" aria-labelledby="landing-incident"><p class="landing-label">(04)  RECORDED INCIDENT</p>'
        '<h2 id="landing-incident">Rhea Lend margin trading route exploit.</h2>'
        '<p>Recorded outbound transfers from the collector account named by rekt.news. These rows do not establish loss or causal attribution.</p>'
        '<div class="landing-timeline" data-landing-sequence="timeline"><span class="landing-timeline-rail" aria-hidden="true"></span>'
        '<table class="landing-transfers"><thead><tr><th scope="col">Recorded time</th><th scope="col">Transfer</th></tr></thead>'
        f'<tbody>{"".join(transfers)}</tbody></table></div><p class="landing-note">Recorded transfers. Gross transfer value is not loss.</p>'
        '<div class="landing-actions"><a href="/incidents/rhea.html">Source records and account evidence</a>'
        '<a href="/flows/rhea-collector.html">Open the complete recorded trace</a></div></section>'
        '<section class="landing-section" aria-labelledby="landing-capabilities"><p class="landing-label">(05)  CAPABILITIES</p>'
        f'<h2 id="landing-capabilities">Explore the evidence.</h2><div class="landing-capabilities">{capabilities}</div>'
        f'<p class="landing-count mono">{esc(counts)}</p></section>'
        '<section class="landing-section" aria-labelledby="landing-limits"><p class="landing-label">(06)  LIMITS</p>'
        '<div class="landing-columns"><div><h2 id="landing-limits">What these reads mean.</h2><ul class="landing-limits">'
        '<li>Payment destination intelligence, without transaction or calldata simulation.</li>'
        '<li>The Gate demonstrations run on testnet. There is no mainnet deployment.</li>'
        '<li>Inferred attributions remain hypotheses and never add a subject to the refusal set.</li>'
        '<li>Live API checks are read only and never sign.</li><li>Unknown never passes.</li></ul></div>'
        '<div><a href="/verify.html">the recorded verification example</a></div></div></section>'
        '<section class="landing-section landing-principles" aria-labelledby="landing-principles"><p id="landing-principles" class="landing-label">(07)  PRINCIPLES</p>'
        '<blockquote>Every label has a source. Every verdict has evidence. Unknown never passes.</blockquote></section>'
        '<section class="landing-section" aria-labelledby="landing-start"><p class="landing-label">(08)  START HERE</p>'
        '<h2 id="landing-start">Investigate an account.</h2><p>Enter a NEAR account to read mainnet data. No payment is signed or sent.</p>'
        '<form class="investigate-search" action="/investigate.html" method="get">'
        '<label for="landing-account">NEAR mainnet account<input id="landing-account" name="account" maxlength="64" autocomplete="off" placeholder="account.near" required></label>'
        '<button type="submit" class="primary">Investigate</button></form>'
        '<div class="landing-observations">' + ''.join(f'<div><h3>{title}</h3><p>Shown on the lookup page after you investigate an account.</p></div>' for title in
                                                      ("Finalized account state", "Indexed token holdings", "Transfers", "Transactions")) + '</div>'
        '<nav class="landing-actions" aria-label="Start here"><a href="/investigate.html">Investigate</a>'
        '<a href="/flows/rhea-collector.html">Trace</a><a href="/feed.html">Feed</a><a href="/workspace.html">Workspace</a>'
        '<a href="/verify.html">Verify</a></nav></section></div>'
    )
    return page("Siyab", body, landing=True)


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
        window_text = (f'Window: {window["hours"]} hour{"" if window["hours"] == 1 else "s"}, {esc(window["start_timestamp"])} '
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
    combined_deny = ("<p>The single DENY came from the lookalike and code-without-metadata signals together; it is the verdict an agent's 1 NEAR payment would get, not a finding about the account.</p>"
                     if summary["deny_lookalike_without_metadata"] == summary["verdicts"]["DENY"] == 1 else "")
    body = ("<h1>Top receivers</h1><p>Aggregate results for the committed mainnet receiver ranking.</p>"
            f"<p>{window_text}</p><p>Ranking rule: {esc(rule_text)}</p>"
            "<p>NEP-141 ft_transfer logs with positive amounts and different payer and receiver ids count from any contract whose receipt did not fail. "
            "Logs from uncatalogued contracts count toward distinct payers.</p>"
            f'<p>Judged {summary["total"]}: {summary["verdicts"]["ALLOW"]} ALLOW, '
            f'{summary["verdicts"]["DENY"]} DENY, {summary["verdicts"]["UNKNOWN"]} UNKNOWN.</p>'
            '<div class="receiver-breakdown"><section class="receiver-panel"><h2>UNKNOWN causes</h2><p>Counts overlap.</p><ul>' + causes + "</ul></section>"
            '<section class="receiver-panel"><h2>DENY split</h2>'
            f'<p>New account (account_age signal only): {summary["deny_new_account"]}.</p>'
            f'<p>Lookalike with code without metadata: {summary["deny_lookalike_without_metadata"]}.</p>'
            + combined_deny + other_deny + "<p>Each verdict is what an agent's 1 NEAR payment to that receiver would get, not a finding "
            "that the account is malicious.</p></section></div>"
            "<p>Only aggregate counts are public on this page. Per-account judgement files are not public.</p>")
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
            return page("Feed", "<h1>Feed</h1><p>The feed has not run.</p>" + _feed_panel()
                        + '<script>' + (ROOT / "intel/ui/feed.js").read_text(encoding="utf-8") + '</script>')
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    window = ""
    if state.get("mode") != "follow":
        snapshot_note = ""
        if Path(feed.SNAPSHOT_PATH).is_file():
            snapshot = json.loads(Path(feed.SNAPSHOT_PATH).read_text(encoding="utf-8"))
            snapshot_note = (f" The follower snapshot <code>intel/feed/follow-snapshot.json</code> covers "
                             f"{snapshot['blocks']} block{'' if snapshot['blocks'] == 1 else 's'} from height {snapshot['start_height']} to height "
                             f"{snapshot['last_height']}.")
        window = (f"<p>Recorded window: blocks {state['start_height']} to {state['last_height']}, read on "
                  f"{esc(state['start_time'][:10])} UTC.{snapshot_note}</p>\n")
    stats = "".join(f"<li>{esc(line)}</li>" for line in feed.stats_lines(state, stopped_since))
    if event_rows is None:
        events, unreadable = feed_events(events_dir)
        event_rows = [(document, stem) for _, stem, document in events]
    cards = "".join(
        f'<div class="card"><p><strong>{_event_field(e, "signal")}</strong> at block '
        f'{_event_field(e, "observed", "block_height")}</p><p>{_event_detail(e)}</p><p class="mono">receipt '
        f'{_event_field(e, "receipt_id")}<br>transaction {_event_field(e, "tx_hash")}<br>evidence {esc(stem)}</p></div>'
        for e, stem in event_rows)
    shown = "" if total == len(event_rows) else f"; the {len(event_rows)} most recent by block height are shown"
    count = "" if total is None else f"<p>{total} event{'' if total == 1 else 's'} in total{shown}.</p>\n"
    if unreadable:
        count += f"<p>{unreadable} event file{'' if unreadable == 1 else 's'} could not be read.</p>\n"
    details_title = "Read details" if state.get("mode") == "follow" else "Recorded window details"
    body = (f'<details class="feed-recorded-details"><summary>{details_title}</summary>{window}<ul>{stats}</ul>\n<h2>Events</h2>\n{count}'
            + (cards or ("" if unreadable else "<p>No signal fired over the recorded blocks.</p>"))
            + "\n<p class=\"muted\">The signals: " + ", ".join(feed.SIGNALS) + ".</p></details>")
    return page("Feed", '<div class="page-head feed-heading"><div><h1>Feed over finalized mainnet blocks</h1><p>Inspect observed signals and their evidence. The current read contains at most 50 recent events; filters apply to that response.</p></div></div>'
                + _feed_panel(event_rows) + body + '<script>' + (ROOT / "intel/ui/feed.js").read_text(encoding="utf-8") + '</script>')


def _profile_script():
    return '<script>' + (ROOT / "intel/ui/profiles.js").read_text(encoding="utf-8") + '</script>'


def _profile_table(name, headings, rows, search="Search records"):
    head = ''.join(f'<th scope="col" role="columnheader">{esc(label)}</th>' for label in headings)
    body = ''.join('<tr role="row">' + ''.join(
        f'<td role="cell"><span class="cell-label" aria-hidden="true">{esc(label)}</span>{value}</td>'
        for label, value in zip(headings, row)) + '</tr>' for row in rows)
    return (f'<section class="profile-records" data-profile-table="{esc(name)}">'
            f'<div class="profile-table-tools" hidden><label>{esc(search)}<input type="search" data-profile-search autocomplete="off"></label>'
            f'<button type="button" data-export-table="{esc(name)}">Export shown rows</button></div>'
            f'<div class="table"><table class="profile-data-table" id="{esc(name)}" role="table"><thead role="rowgroup"><tr role="row">{head}</tr></thead>'
            f'<tbody role="rowgroup">{body}</tbody></table></div><p data-profile-table-status role="status"></p>'
            '<nav class="profile-pagination" data-profile-table-pages aria-label="Record pages" hidden></nav></section>')


def _profile_source(assertion):
    source = assertion.source
    if source.get("url"):
        provider = source["url"].split("/")[2]
        summary = f'<a href="{esc(source["url"])}">{esc(provider)}</a>'
    elif source.get("method"):
        summary = f'{esc(source["method"])} at block {source["block"]}'
    else:
        summary = f'Rule <q class="data">{esc(assertion.rule_id)}</q>'
    return (f'{summary}<time class="profile-observed" datetime="{esc(assertion.observed_at)}">{esc(assertion.observed_at)}</time>'
            f'<details class="profile-source-detail"><summary>Evidence</summary><p class="mono">Assertion {esc(assertion.id)}</p>'
            f'<pre><q class="data">{esc(json.dumps(source, ensure_ascii=False, sort_keys=True))}</q></pre></details>')


def _transfer_amount(row):
    decimals = trace.asset_decimals(row["asset_id"])
    return _display_amount(row["amount"], 4, decimals=decimals) if decimals is not None else f'{row["amount"]} base units'


def _profile_transfer_rows(subject, flows):
    rows = []
    for name, document in flows:
        for index, hop in enumerate(document.get("hops", [])):
            if subject in (hop["from"], hop["to"]):
                rows.append(dict(hop, recording=name, fetched_at=document["fetched_at"], row_index=index))
    return sorted(rows, key=lambda row: int(row["block_timestamp"]), reverse=True)


def _profile_transfer_cells(subject, row, accounts):
    direction = "Self" if row["from"] == row["to"] else "Outbound" if row["from"] == subject else "Inbound"
    counterparty = row["to"] if row["from"] == subject else row["from"]
    instant = datetime.fromtimestamp(int(row["block_timestamp"]) // 1000000000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    amount = esc(_transfer_amount(row))
    return [f'<time>{instant}</time>', direction, _subject_cell(counterparty, accounts),
            f'<span class="profile-value">{amount}</span> <q class="data profile-asset">{esc(row["asset_id"])}</q>',
            f'<span class="profile-value">{esc(_display_amount(row["usd_amount"], 2))}</span>',
            f'<a href="/flows/{esc(row["recording"])}.html">Recorded evidence</a>'
            f'<details class="profile-source-detail"><summary>Transfer details</summary><p class="mono">Block {row["block"]}<br>'
            f'Transaction {esc(row["transaction_hash"])}<br>Receipt {esc(row["receipt_id"])}<br>'
            f'Base units {esc(row["amount"])}<br>Recorded {esc(row["fetched_at"])}</p></details>']


def _profile_account_summary(subject, assertions, accounts, flows):
    activity = _profile_transfer_rows(subject, flows)
    observed = sorted({row.observed_at for row in assertions})
    categories = sorted({row.category.replace("_", " ") for row in assertions if row.provenance != "inferred"})
    inferred = [row for row in assertions if row.provenance == "inferred"]
    inferred_tags = ''.join(f'<a href="#attribution-hypothesis">{esc(category)}, inferred</a>'
                            for category in sorted({row.category.replace("_", " ") for row in inferred}))
    assertion_counts = (f'<dd><span class="profile-observed">{len(assertions) - len(inferred)} sourced, {len(inferred)} inferred</span></dd>'
                        if inferred else '')
    labels_rows = [[data_text(row.label), esc(row.chain), esc(row.provenance.replace("_", " ")), _profile_source(row)]
                   for row in assertions if row.provenance != "inferred"]
    source_rows = ''.join('<tr role="row" data-profile-transfer-row>' + ''.join(
        f'<td role="cell"><span class="cell-label" aria-hidden="true">{label}</span>{value}</td>'
        for label, value in zip(("Time UTC", "Direction", "Counterparty", "Amount and asset", "USD value", "Evidence"),
                                _profile_transfer_cells(subject, row, accounts))) + '</tr>' for row in activity[:20])
    counterparties = Counter(row["to"] if row["from"] == subject else row["from"] for row in activity)
    counter_rows = [[_subject_cell(account, accounts), str(count)] for account, count in counterparties.most_common(12)]
    sources = []
    for name, document in flows:
        if any(subject in (hop["from"], hop["to"]) for hop in document.get("hops", [])):
            url, _ = _graph_asset(graph.flow_payload(document, accounts), f"/flows/{name}.html")
            sources.append({"url": url, "name": name})
    payload = json.dumps({"subject": subject, "sources": sources, "total_rows": len(activity)}, ensure_ascii=True).replace("<", "\\u003c")
    summary = ('<section class="profile-intelligence"><div class="profile-account-context"><div>'
               '<h2>Recorded account evidence</h2><p>Recorded labels describe evidence, not a current payment decision.</p>'
               f'<p class="profile-tags">{"".join(f"<span>{esc(category)}</span>" for category in categories)}{inferred_tags}</p>'
               f'<dl class="profile-facts"><div><dt>Label assertions</dt><dd>{len(assertions)}</dd>{assertion_counts}</div>'
               f'<div><dt>Recorded transfer rows</dt><dd>{len(activity)}</dd></div>'
               f'<div><dt>Latest label observation</dt><dd class="stat-timestamp">{esc(observed[-1]) if observed else "Unavailable"}</dd></div></dl>'
               '<p class="profile-scope">Current balance unavailable. Account creation time is not established by these records.</p>'
               f'<div class="profile-actions"><button type="button" data-watch-account="{esc(subject)}">Watch account</button>'
               '<button type="button" data-save-investigation>Save investigation</button></div>'
               '<section class="profile-section" id="account-labels"><h2>Sourced labels</h2>'
               + _profile_table("profile-labels", ("Label", "Chain", "Provenance", "Source and observation"), labels_rows, "Search labels") + '</section></div>'
               f'<section class="profile-live-read" data-profile-live="{esc(subject)}"><h3>Read current mainnet checks</h3>'
               '<p>The payment read checks exactly 1 NEAR. It never signs or sends a payment.</p>'
               '<div class="profile-actions"><button type="button" data-profile-payment>Check 1 NEAR payment</button>'
               '<button type="button" data-profile-keys>Read access keys</button></div>'
               '<div class="profile-live-result" data-profile-live-result role="status" aria-live="polite">Payment result not checked. No live read requested.</div></section></div></section>')
    if not any(row.chain == "near" for row in assertions) or subject.endswith(".testnet"):
        start = summary.index('<section class="profile-live-read"')
        summary = summary[:start] + ('<section class="profile-live-read"><h3>Live check availability</h3>'
                                    '<p>Live checks on this site read NEAR mainnet. This recorded subject is outside that read scope.</p></section></div></section>')
    activity_html = (f'<section class="profile-section" id="account-activity" data-profile-activity><h2>Recorded activity</h2>'
                     '<p>Rows touching this account in the committed transfer recordings. This is not a complete or live account history. USD values come from the recorded provider; unavailable values are not zero.</p>'
                     f'<script type="application/json" data-profile-sources>{payload}</script>'
                     '<div class="profile-table-tools" hidden><label>Find a counterparty or asset<input type="search" data-activity-search></label>'
                     '<label>Direction<select data-activity-direction><option value="">All directions</option><option value="outbound">Outbound</option>'
                     '<option value="inbound">Inbound</option></select></label><button type="button" data-export-table="profile-transfers">Export shown rows</button></div>'
                     '<p data-activity-status role="status">'
                     f'Showing {min(20, len(activity))} of {len(activity)} recorded row{"" if len(activity) == 1 else "s"}, newest first.</p>'
                     '<button type="button" data-activity-retry hidden>Retry recording</button>'
                     '<div class="table"><table class="profile-data-table profile-transfer-table" id="profile-transfers" role="table"><thead role="rowgroup"><tr role="row">'
                     + ''.join(f'<th scope="col" role="columnheader">{label}</th>' for label in ("Time UTC", "Direction", "Counterparty", "Amount and asset", "USD value", "Evidence"))
                     + f'</tr></thead><tbody role="rowgroup">{source_rows}</tbody></table></div>'
                     + ('<p>No committed transfer row touches this account. No activity is inferred from that absence.</p>' if not activity else '')
                     + '<nav class="profile-pagination" data-activity-pages aria-label="Activity pages" hidden></nav>'
                     + '<h2>Recorded counterparties</h2><p>Ranked by row count within the same recordings; counts do not measure value or loss.</p>'
                     + _profile_table("profile-counterparties", ("Counterparty", "Recorded rows"), counter_rows, "Search counterparties")
                     + f'<p data-counterparties-scope>{min(12, len(counterparties))} of {len(counterparties)} recorded {"counterparty" if len(counterparties) == 1 else "counterparties"} initially shown. Search loads the full recording.</p></section>')
    return summary + activity_html


def incident_page(document, accounts):
    sources = {s["id"]: s for s in document["sources"]}
    chains = ", ".join(document["chains"])
    parts = [f"<h1>{esc(document['title'])}</h1>", f"<p>{esc(document['date'])}; chains {esc(chains)}</p>",
             "<h2>Sources</h2>"]
    for source in document["sources"]:
        parts.append(f'<div class="card"><p>[{esc(source["id"])}] {esc(source["publisher"])}: '
                     + (f'<a href="{esc(source["url"])}">{esc(source["url"])}</a>' if _web_url(source["url"]) else esc(source["url"]))
                     + '</p><p class="mono">sha256 '
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
    if not document.get("onchain_state") and not document["onchain_facts"]:
        parts.append("<p>No on chain fact is recorded.</p>")
    at = document.get("rules_today_at_ms")
    parts.append("<h2>Rules as evaluated" + (f" on {datetime.fromtimestamp(at / 1000, UTC):%Y-%m-%d}" if at else "") + "</h2>")
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
    timeline = [(document["date"], "Incident date reported by the sources", "")]
    timeline.extend((source["fetched_at"], "Source snapshot collected",
                     f'<a href="{esc(source["url"])}" rel="noopener">{esc(source["publisher"])}</a>' if _web_url(source["url"])
                     else esc(source["publisher"])) for source in document["sources"])
    if document.get("rules_today_at_ms"):
        when = datetime.fromtimestamp(document["rules_today_at_ms"] / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        timeline.append((when, "Recorded rule evaluation", esc("This is a dated evaluation, not a current verdict.")))
    timeline_html = ''.join(f'<li><time datetime="{esc(when)}">{esc(when)}</time><div><strong>{esc(event)}</strong>'
                            f'<p>{detail}</p></div></li>' for when, event, detail in sorted(timeline))
    lead = next((account["quote"] for account in document["accounts"]), None)
    account_count, chain_count, source_count = len(document["accounts"]), len(document["chains"]), len(document["sources"])
    brief = (f'<section class="profile-case-brief"><h2>Case brief</h2><p>{account_count} named account{"" if account_count == 1 else "s"} across '
             f'{chain_count} chain{"" if chain_count == 1 else "s"}; {source_count} recorded source snapshot{"" if source_count == 1 else "s"}.</p>'
             + (quote_block(lead, sources) if lead else '<p>The source snapshots do not name a public account.</p>')
             + '<button type="button" data-save-investigation>Save investigation</button></section>')
    related = [(name, flow) for name, flow in _flows() if flow["subject"] in {account["subject"] for account in document["accounts"]}]
    routes = []
    for name, flow in related:
        selected = [row for row in flow["hops"] if row["from"] == flow["subject"]][:3]
        for hop in selected:
            routes.append('<li class="profile-route"><div>' + _subject_cell(hop["from"], accounts) + '</div>'
                          '<div class="profile-route-bridge"><svg viewBox="0 0 100 20" aria-hidden="true"><path d="M1 10H95M89 4L95 10L89 16"/></svg>'
                          f'<span>{esc(_transfer_amount(hop))}<br><q class="data">{esc(hop["asset_id"])}</q></span></div>'
                          '<div>' + _subject_cell(hop["to"], accounts) + '</div></li>')
    route = ('<section class="profile-section"><h2>Recorded transfer route</h2><p>First three direct outbound rows in each linked recording, in provider order. These rows do not establish loss or causal attribution.</p>'
             + ('<ol class="profile-routes">' + ''.join(routes) + '</ol>' if routes else '<p>No recorded transfer route is available for this case.</p>')
             + ''.join(f'<a class="button" href="/flows/{esc(name)}.html">Open the complete recorded trace</a>' for name, _ in related) + '</section>')
    replay = ""
    replay_path = REPLAYS_DIR / document["id"] / "run.json"
    if replay_path.is_file():
        run = json.loads(replay_path.read_text(encoding="utf-8"))
        if (not isinstance(run, dict) or run.get("incident_id") != document["id"]
                or run.get("label_store") != {"labels": [], "incidents": []}):
            raise ValueError("incident replay is unreadable")
        if any(type(run.get(field)) is not int or run[field] < 0 for field in ("height", "block_timestamp")):
            replay = '<section class="profile-section"><h2>Replay record invalid</h2></section>'
        else:
            when = datetime.fromisoformat(run["as_of"]).astimezone(UTC)
            date = when.strftime("%d %b %Y").lstrip("0")
            block_time = datetime.fromtimestamp(run["block_timestamp"] // 1_000_000_000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            rows = []
            required = {"receiver_exists", "label_denylist", "memo_injection", *rules.TIER2_CHECKS}
            for entry in run["accounts"]:
                if not isinstance(entry, dict):
                    raise ValueError("incident replay is unreadable")
                checks = entry.get("checks")
                if (entry.get("verdict") not in ("ALLOW", "DENY", "UNKNOWN")
                        or not isinstance(checks, list) or len(checks) != len(required)
                        or any(not isinstance(check, dict) or not isinstance(check.get("id"), str)
                               or check.get("status") not in ("pass", "signal", "unknown")
                               or not isinstance(check.get("detail"), str) for check in checks)
                        or {check["id"] for check in checks} != required):
                    raise ValueError("incident replay is unreadable")
                outcome = LiveAPI._verdict_shape(entry["verdict"], checks, [], None, {}, 0)["outcome"]
                if entry.get("outcome") != outcome:
                    raise ValueError("incident replay outcome does not match its checks")
                subject = entry["account_id"]
                account = _subject_cell(subject, accounts)
                if re.fullmatch(r"[0-9a-f]{64}", subject):
                    short = subject[:12] + "…" + subject[-10:]
                    account = (_linked(subject, f'<q class="data">{esc(short)}</q>', accounts)
                               + f'<details class="analytics-full-account"><summary>Full identifier</summary>{account}</details>')
                checks = '<br>'.join(f'{esc(check["id"])} {esc(check["status"])}: {esc(check["detail"])}'
                                    for check in entry["checks"])
                rows.append((esc(entry["role"]), account, esc(entry["outcome"]), checks))
            replay = (f'<section class="profile-section"><h2>Replayed as of {esc(date)}, with no labels</h2>'
                      f'<p>The guard used only on-chain state at block {esc(run["height"])} ({esc(block_time)}), with no labels. '
                      'This is a replay, not a live prediction. UNKNOWN checks never count as a pass.</p>'
                      + _profile_table("incident-replay", ("Role", "Account", "Outcome", "Checks"), rows) + '</section>')
    body = ('\n'.join(parts[:2]) + '<div class="profile-incident-overview">' + brief + '<section class="profile-section"><h2>Evidence timeline</h2>'
            '<p>Dates track source collection and evaluation. They are not a reconstructed sequence of attack transactions.</p>'
            f'<ol class="profile-timeline">{timeline_html}</ol></section></div>' + replay + route
            + '<details class="profile-evidence"><summary>Source records, account roles and full evidence</summary>'
            + '\n'.join(parts[2:]) + '</details>' + _profile_script())
    return page(document["title"], body, depth=1)


def _entity_parts(text):
    parts, last = [], 0
    for match in _QUOTED.finditer(str(text)):
        parts.append({"text": str(text)[last:match.start()]})
        parts.append({"text": _UNESCAPE.sub(r"\1", match.group(1)), "quoted": True})
        last = match.end()
    parts.append({"text": str(text)[last:]})
    return parts


def _organisation_group_id(assertion):
    payload = {"chain": assertion.chain, "provenance": assertion.provenance, "source": assertion.source,
               "label": assertion.label, "observed_at": assertion.observed_at}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _organisation_groups(store):
    groups = {}
    for assertion in store.assertions:
        if assertion.category != "organisation" or assertion.superseded_by is not None:
            continue
        key = _organisation_group_id(assertion)
        if key not in groups:
            method = assertion.source.get("method")
            title = ("Sputnik DAO factory" if assertion.provenance == "onchain" and method == "get_dao_list" else
                     "Validators in recorded epoch" if assertion.provenance == "onchain" and method == "validators" else
                     "Recorded source assertions")
            groups[key] = {"id": key, "title": title, "assertions": 0, "subjects": set(), "chain": assertion.chain,
                           "provenance": assertion.provenance, "source": assertion.source, "observed_at": assertion.observed_at}
        groups[key]["assertions"] += 1
        groups[key]["subjects"].add(assertion.subject)
    return [{**group, "subjects": len(group["subjects"])} for group in groups.values()]


def _entity_rows(categories, store):
    accounts, by_id = account_pages(store), {a.id: a for a in store.assertions}
    rows = []
    for a in store.assertions:
        if a.category not in categories or a.superseded_by is not None:
            continue
        subject = {"text": a.subject, "quoted": True}
        if a.subject in accounts:
            subject["url"] = "../" + accounts[a.subject]
        cells = [[subject], [{"text": a.chain}], _entity_parts(a.label), [{"text": a.category}]]
        if a.provenance == "inferred":
            inputs = []
            for assertion_id in a.source["inputs"]:
                part = {"text": assertion_id}
                if assertion_id in by_id and by_id[assertion_id].subject in accounts:
                    part["url"] = "../" + accounts[by_id[assertion_id].subject]
                if inputs:
                    inputs.append({"text": "\n"})
                inputs.append(part)
            cells.extend([[{"text": a.rule_id, "quoted": True}], inputs, [{"text": str(a.confidence)}]])
        else:
            cells.append([{"text": a.provenance}])
        cells.append(_entity_parts(a.note))
        rows.append({"id": a.id, "kind": "inferred" if a.provenance == "inferred" else "stated",
                     "subject": a.subject, "chain": a.chain, "label": a.label, "note": a.note,
                     "search": " ".join((a.subject, a.chain, a.label, a.category, a.provenance, a.note)),
                     "cells": cells})
        if a.category == "organisation":
            rows[-1]["source_group"] = _organisation_group_id(a)
    return sorted(rows, key=lambda row: row["kind"] == "inferred")


def _entity_table(rows, kind):
    columns = (["Subject", "Chain", "Label", "Category", "Rule", "Inputs", "Confidence", "Note"]
               if kind == "inferred" else ["Subject", "Chain", "Label", "Category", "Provenance", "Note"])
    body = []
    for row in rows:
        cells = []
        for index, (label, parts) in enumerate(zip(columns, row["cells"])):
            values = []
            for part in parts:
                text = esc(part["text"]).replace("\n", "<br>")
                if part.get("quoted"):
                    text = f'<q class="data">{text}</q>'
                if part.get("url"):
                    text = f'<a href="{esc(part["url"])}">{text}</a>'
                values.append(text)
            css = ' class="mono"' if index == 0 or label in ("Rule", "Inputs") else ""
            exact = (row["subject"] if index == 0 else row["chain"] if index == 1 else row["label"] if index == 2
                     else row["note"] if label == "Note" else ''.join(part["text"] for part in parts))
            cells.append(f'<td role="cell"{css} data-export-value="{esc(exact)}"><span class="cell-label" aria-hidden="true">{label}</span>{"".join(values)}</td>')
        body.append(f'<tr role="row" data-assertion-id="{row["id"]}">{"".join(cells)}</tr>')
    heading = "" if kind == "stated" else (
        '<h2>Attribution hypothesis</h2><p>Each row below is inferred: it names the rule that inferred '
        'it, the assertions the rule read as its inputs and its confidence.</p>')
    headers = "".join(f'<th scope="col" role="columnheader">{label}</th>' for label in columns)
    css = "entity-table entity-table-inferred" if kind == "inferred" else "entity-table"
    return (f'<section data-entity-kind="{kind}">{heading}<p class="entity-table-status" data-table-section-count>{len(rows)} {("hypothesis" if len(rows) == 1 else "hypotheses") if kind == "inferred" else ("source assertion" if len(rows) == 1 else "source assertions")} on this page.</p>'
            f'<button type="button" data-export-table="entity-collection-{kind}">Export shown rows CSV</button>'
            f'<div class="table"><table class="{css}" role="table" id="entity-collection-{kind}">'
            f'<thead role="rowgroup"><tr role="row">{headers}</tr></thead><tbody role="rowgroup">{"".join(body)}</tbody></table></div></section>')


def entity_page(title, categories, store, *, page_number=1, collection_name=None, rows=None):
    rows = _entity_rows(categories, store) if rows is None else rows
    collection_name = collection_name or next(name for name, _, kinds in ENTITY_SETS if set(categories) <= set(kinds))
    count = len(rows)
    pages = max(1, (count + 49) // 50)
    start = (page_number - 1) * 50
    selected = rows[start:start + 50]
    tables = _entity_table([row for row in selected if row["kind"] == "stated"], "stated")
    if any(row["kind"] == "inferred" for row in rows):
        tables += _entity_table([row for row in selected if row["kind"] == "inferred"], "inferred")
    def path(number):
        return f"{collection_name}.html" if number == 1 else f"{collection_name}.page-{number}.html"
    previous = f'<a href="{path(page_number - 1)}" rel="prev">Previous</a>' if page_number > 1 else "<span>Previous</span>"
    following = f'<a href="{path(page_number + 1)}" rel="next">Next</a>' if page_number < pages else "<span>Next</span>"
    chains = "".join(f'<option value="{esc(chain)}">{esc(chain)}</option>' for chain in sorted({row["chain"] for row in rows}))
    script = (ROOT / "intel/ui/tables.js").read_text(encoding="utf-8")
    groups = _organisation_groups(store) if collection_name == "organisations" else []
    group_filter = ('<label>Recorded registry<select data-table-group><option value="">All recorded reads</option>'
                    + ''.join(f'<option value="{group["id"]}">{esc(group["title"])}</option>' for group in groups)
                    + '</select></label>') if groups else ''
    heading = f'<h1>{esc(title)}</h1>'
    intro = f'<p>{count} current assertion{"" if count == 1 else "s"} from the label store.</p>'
    body = (f'<div class="entity-table-panel" data-entity-table data-source="{collection_name}.table.json" data-page="{page_number}">'
            '<div class="entity-table-toolbar" hidden><label>Search this collection<input type="search" data-table-search autocomplete="off" placeholder="Account, label or evidence text"></label>'
            f'<label>Chain<select data-table-chain><option value="">All chains</option>{chains}</select></label>'
            + group_filter +
            '<label>Sort<select data-table-sort><option value="recorded">Recorded order</option><option value="subject">Account A to Z</option>'
            '<option value="subject-desc">Account Z to A</option><option value="chain">Chain A to Z</option><option value="label">Label A to Z</option></select></label></div>'
            f'<p class="entity-table-status" role="status" aria-live="polite">Showing {start + 1 if count else 0} to {min(start + 50, count)} of {count} assertion{"" if count == 1 else "s"}.</p>'
            f'<button type="button" data-table-retry hidden>Retry collection</button>{tables}<p class="empty-state" data-table-empty hidden>No matching recorded assertions. Clear the search or choose another chain.</p>'
            f'<nav class="entity-table-pagination" aria-label="Collection pages">{previous}<span>Page {page_number} of {pages}</span>{following}</nav>'
            f'</div><script>{script}</script>')
    table_panel = body
    body = f'<div class="page-head collection-heading">{heading}{intro}</div>' + table_panel
    if groups:
        overview = []
        for group in groups:
            source = esc(json.dumps(group["source"], ensure_ascii=False, indent=2))
            overview.append(f'<li class="registry-group"><div><h2>{esc(group["title"])}</h2>'
                            f'<p>{group["subjects"]:,} account{"" if group["subjects"] == 1 else "s"} in this recorded read</p>'
                            f'<p class="registry-recorded">{esc(group["provenance"])} on {esc(group["chain"])}. Recorded <time>{esc(group["observed_at"])}</time>.</p>'
                            f'<details><summary>Registry read evidence</summary><blockquote class="data"><pre>{source}</pre></blockquote></details></div>'
                            f'<a class="button" data-registry-group="{group["id"]}" href="?source={group["id"]}#individual-assertions">View matching assertions</a></li>')
        body = (heading + '<section class="registry-overview" aria-label="Recorded registry overview">'
                f'<p class="registry-count">{count:,} <span class="stat-connective">assertion{"" if count == 1 else "s"} from</span> {len(groups)} <span class="stat-connective">recorded registry read{"" if len(groups) == 1 else "s"}.</span></p>'
                '<p class="registry-scope">Factory listings and epoch membership describe recorded associations, not shared ownership or current control.</p>'
                f'<ul class="registry-groups">{"".join(overview)}</ul></section>'
                f'<details class="registry-individuals" id="individual-assertions"{" open" if page_number > 1 else ""}>'
                '<summary>Individual recorded assertions</summary>' + intro + table_panel + '</details>')
    return page(title, body, depth=1)


def account_page(subject, store, accounts, incidents, flows, clusters, events_dir=feed.EVENTS_DIR):
    assertions = [a for a in store.assertions if a.subject == subject and a.superseded_by is None]
    rows = "".join(f'<tr role="row"><td role="cell"><span class="cell-label" aria-hidden="true">Chain</span>{esc(a.chain)}</td><td role="cell"><span class="cell-label" aria-hidden="true">Label</span>{data_text(a.label)}</td><td role="cell"><span class="cell-label" aria-hidden="true">Category</span>{esc(a.category)}</td>'
                   f'<td role="cell"><span class="cell-label" aria-hidden="true">Provenance</span>{esc(a.provenance)}</td><td role="cell"><span class="cell-label" aria-hidden="true">Note</span>{data_text(a.note)}</td><td role="cell" class="mono"><span class="cell-label" aria-hidden="true">Source</span><q class="data">'
                   f'{esc(json.dumps(a.source, ensure_ascii=False, sort_keys=True))}</q></td></tr>'
                   for a in assertions if a.provenance != "inferred")
    provenance = Counter(a.provenance for a in assertions)
    overview = "".join(f'<span>{esc(kind)}<strong>{count}</strong></span>' for kind, count in sorted(provenance.items()))
    body = (f'<div class="profile-heading page-head"><div><h1>Account</h1>'
            f'<p class="mono"><q class="data">{esc(subject)}</q></p></div>'
            '<a class="button" href="/explore.html#tools">Payment and key checks</a></div>'
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
        body += ('<h2 id="attribution-hypothesis">Attribution hypothesis</h2><p>Each row below is inferred: it names the rule that inferred '
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
        f'<li><a href="/flows/{esc(name)}.html">Committed trace root with {len(document.get("hops", []))} edge{"" if len(document.get("hops", [])) == 1 else "s"}</a></li>'
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
        f'{document["observed"]["block_height"]}</p><p>{_event_detail(document)}</p>'
        f'<p class="mono">evidence {esc(stem)}</p></div>' for stem, document in events)
        if events else "<p>No committed feed event names this account.</p>")
    split = body.index('<section class="workspace-panel profile-overview">')
    body = (body[:split] + _profile_account_summary(subject, assertions, accounts, flows)
            + f'<details class="profile-evidence"{" open" if inferred else ""}><summary>Source records and attribution evidence</summary>'
            + body[split:] + '</details>' + _profile_script())
    return page("Account", body, depth=1)


def registry_entity_page(entity, accounts):
    source = entity["source"]
    members = "".join(f'<li>{_subject_cell(member, accounts)}</li>' for member in entity["members"])
    subject = entity["members"][0] if entity["kind"] in ("validator", "dao") else entity["name"]
    body = ('<div class="profile-heading page-head"><div>'
            f'<span class="profile-kind">{esc(entity["kind"].replace("_", " ").title())}</span>'
            f'<h1 class="profile-id">{esc(subject)}</h1></div>'
            f'<p>{len(entity["members"])} recorded member{"" if len(entity["members"]) == 1 else "s"}</p></div>'
            f'<div class="registry-strip"><div class="profile-provenance"><p>Registry read: {esc(source["method"])} at block {source["block"]}, '
            f'arguments {esc(json.dumps(source["args"], sort_keys=True))}</p>'
            f'<p>Observed at {esc(entity["observed_at"])} on {esc(source["rpc"])}</p></div>'
            '<div class="profile-actions"><button type="button" data-save-investigation>Save investigation</button></div></div>'
            '<h2 class="registry-members-title">Members</h2>'
            + _profile_table("registry-members", ("Account", "Membership evidence"),
                             [[_subject_cell(member, accounts), f'Registry read at block {source["block"]}'] for member in entity["members"]], "Search members")
            + f'<details class="profile-evidence"><summary>Original member list and registry source</summary>'
            f'<p><q class="data">{esc(entity["name"])}</q></p><ul>{members}</ul>'
            f'<pre><q class="data">{esc(json.dumps(source, ensure_ascii=False, sort_keys=True))}</q></pre></details>')
    return page("Entity", body + _profile_script(), depth=1)


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
               f'<p>{depositors["count"]} distinct depositor{"" if depositors["count"] == 1 else "s"} of <span class="mono">'
               f'{_linked(depositors["collector"], esc(depositors["collector"]), accounts)}</span> '
               f'since {esc(depositors["since"])}, {depositors["implicit_count"]} of them implicit account{"" if depositors["implicit_count"] == 1 else "s"}; the '
               "cluster file counts them and lists none.</p>")
    payload = graph.cluster_payload(document, accounts)
    context = (f"<h1>Account relationship edges: {esc(name)}</h1><p>{esc(document['standard'])}</p>"
               '<dl class="investigation-summary">'
               f'<div><dt>Recorded accounts</dt><dd>{payload["total_nodes"]}</dd></div>'
               f'<div><dt>Edges from chain reads</dt><dd>{len(chain)}</dd></div>'
               f'<div><dt>Attribution hypotheses</dt><dd>{len(inferred)}</dd></div></dl>'
               '<nav class="investigation-actions" aria-label="Relationship evidence"><a href="#chain-relationships">Chain evidence</a>'
               '<a href="#attribution-relationships">Attribution inputs</a></nav>')
    body = (f"<h2>Seeds</h2><p>{len(document['seeds'])} seed{'' if len(document['seeds']) == 1 else 's'}, each with a current incident label in the label "
            f"store.</p><ul>{seeds}</ul>"
            '<section id="chain-relationships"><h2>Edges from chain reads</h2>' + ("".join(_edge_card(edge, facts, accounts) for edge in chain)
                                                 or "<p>No edge from chain reads.</p>") + '</section>' +
            '<section id="attribution-relationships"><h2>Attribution hypothesis</h2><p>Each edge below is inferred from the facts named as its inputs, at '
            "the confidence shown; it is a hypothesis, not a chain read, and no verdict reads it.</p>"
            + ("".join(_edge_card(edge, facts, accounts) for edge in inferred) or "<p>No inferred edge.</p>") + '</section>' +
            f"<h2>Precision</h2><ul>{precision}</ul><h2>Depositors</h2>{counted}"
            f"<p class=\"muted\">Read {esc(document['fetched_at'])}. docs/CLUSTER.md states the heuristics and their "
            "limits.</p>")
    return page(f"Account relationship edges: {name}", context + _graph_section(payload, f"/clusters/{name}.html") + body, depth=1)


def _hop_card(hop, accounts):
    usd = ', USD unknown' if hop["usd_amount"] is None else f', {_display_amount(hop["usd_amount"], 2)} USD as the Transfers API values it'
    units = " units, from recorded base units" if trace.asset_decimals(hop["asset_id"]) is not None else " (decimals not recorded)"
    human = f'<p>{esc(_transfer_amount(hop))}{units}.</p>'
    return (f'<div class="card flow-transfer"><p class="mono">{_linked(hop["from"], esc(hop["from"]), accounts)} to '
            f'{_linked(hop["to"], esc(hop["to"]), accounts)}</p><p>{esc(hop["amount"])} base units of '
            f'<span class="mono">{esc(hop["asset_id"])}</span>{esc(usd)}, method {esc(hop["method_name"] or "none")}</p>'
            f'{human}<p class="mono muted">block {hop["block"]} at {esc(trace.utc_minute(hop["block_timestamp"]))}<br>transaction '
            f'{esc(hop["transaction_hash"])}<br>receipt {esc(hop["receipt_id"])}</p>'
            f'<p class="flow-transfer-action"><a href="https://nearblocks.io/txns/{esc(hop["transaction_hash"])}" '
            'rel="noreferrer">Open transaction on NearBlocks</a></p></div>')


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
            f'<div class="analytics-row analytics-distribution-row" data-share-bucket="{index}" data-usd="{esc(bucket["usd"])}" data-percent="{esc(bucket["percent"])}">'
            f'<div class="analytics-counterparty">{label}<small>{esc(bucket["kind"])}</small>{full_account}</div>'
            f'<span class="analytics-usd num"><span class="cell-label" aria-hidden="true">USD</span>{Decimal(bucket["usd"]):,.2f} USD</span>'
            f'<span class="analytics-count num"><span class="cell-label" aria-hidden="true">Rows</span>{bucket["count"]} row{"" if bucket["count"] == 1 else "s"}</span>'
            f'<strong class="analytics-share num"><span class="cell-label" aria-hidden="true">Share</span>{"&lt;0.01" if Decimal(bucket["usd"]) > 0 and float(bucket["percent"]) < 0.01 else esc(bucket["percent"])}%</strong>'
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
            description = f'{row["count"]} root outbound transfer{"" if row["count"] == 1 else "s"}, {utc(row["from_ns"], True)} until {utc(row["to_ns"], True)}, end excluded'
            bars.append(f'<g tabindex="0" role="button" aria-label="{esc(description)}" data-bin-index="{index}" '
                        f'data-chart-start="{row["from_ns"]}" data-chart-end="{row["to_ns"]}" data-chart-count="{row["count"]}">'
                        f'<title>{esc(description)}</title><rect class="chart-bin-bar" x="{index * 40 + 3}" y="{100 - height:.3f}" '
                        f'width="34" height="{height:.3f}"/><rect class="chart-bin-hit" x="{index * 40}" y="0" width="40" height="100"/></g>')
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
        options = ''.join(f'<option value="{index}">{utc(row["from_ns"])} ({row["count"]} transfer{"" if row["count"] == 1 else "s"})</option>'
                          for index, row in enumerate(timeline))
        end_options = options.replace(f'value="{len(timeline) - 1}"', f'value="{len(timeline) - 1}" selected')
        chart = (f'<p class="analytics-note">{total} root outbound transfer{"" if total == 1 else "s"} from {utc(timeline[0]["from_ns"])} '
                 f'to {utc(timeline[-1]["to_ns"] - 1)}. Counts include rows with unknown USD; deeper hops are excluded.</p>'
                 '<div class="analytics-timeline-chart">'
                 f'<div class="analytics-count-axis" aria-label="Transfer count scale"><span>{maximum}</span><span>{maximum / 2:g}</span><span>0</span></div>'
                 f'<svg class="analytics-histogram" viewBox="0 0 {len(timeline) * 40} 100" preserveAspectRatio="none" role="img" aria-label="Recorded root outbound transfer counts">'
                 f'<title>{total} recorded transfer{"" if total == 1 else "s"} in {len(timeline)} time bin{"" if len(timeline) == 1 else "s"}</title>{gridlines}{"".join(bars)}'
                 '<line class="chart-crosshair" x1="0" x2="0" y1="0" y2="100" hidden/></svg>'
                 f'<div class="analytics-time-axis">{"".join(ticks)}</div></div>'
                 f'<p class="chart-readout" role="status">Peak interval: {maximum} root outbound transfer{"" if maximum == 1 else "s"}. Select a bar to inspect its exact interval.</p>'
                 '<div class="chart-range-controls" hidden><label>From interval<select data-chart-from>' + options +
                 '</select></label><label>Through interval<select data-chart-through>' + end_options +
                 '</select></label><button type="button" data-chart-inspect>Inspect interval in graph</button></div>'
                 '<p class="chart-selection" role="status"></p>'
                 '<details class="analytics-bin-values"><summary>Exact time bounds and transfer counts</summary>'
                 '<p>Bins include their start and exclude their end; nanosecond bounds come from recorded block timestamps.</p>'
                 '<div class="table"><table><thead><tr><th scope="col">From UTC (inclusive)</th><th scope="col">To UTC (exclusive)</th><th scope="col">Transfers</th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table></div></details>')
    else:
        chart = '<p class="analytics-note">No recorded root outbound transfers.</p>'
    return ('<section class="analytics-grid flow-analytics" id="recorded-dataset" aria-label="Full recorded flow analytics">'
            f'<article class="analytics-panel" data-direct-usd="{esc(shares["direct_outbound_usd"])}"><div class="analytics-panel-head"><h2>Direct outbound distribution</h2></div>'
            f'<p class="analytics-note">Full recorded dataset, not the current graph filter. Denominator: {Decimal(shares["direct_outbound_usd"]):,.2f} USD '
            f'across {shares["rows"]} root outbound row{"" if shares["rows"] == 1 else "s"} valued at or above {esc(shares["min_usd"])} USD by the Transfers API. '
            'Deeper hops are excluded; this is gross transfer value, not loss.</p>'
            '<div class="analytics-chart"><div class="analytics-distribution-head" aria-hidden="true"><span>Counterparty</span><span>USD</span><span>Rows</span><span>Share</span></div>'
            f'{distribution or "<p>No direct outbound rows meet the USD threshold.</p>"}</div>'
            f'<p class="analytics-note">{below["count"]} row{"" if below["count"] == 1 else "s"} below {esc(shares["min_usd"])} USD: {Decimal(below["usd"]):,.2f} USD, counted separately. '
            f'{unknown["count"]} row{"" if unknown["count"] == 1 else "s"} without a USD amount; their value is unknown, not zero. Base units: {esc(units)}.</p></article>'
            f'<article class="analytics-panel" data-recorded-timeline="{esc(document["subject"])}"><div class="analytics-panel-head"><h2>Recorded transfer activity</h2></div>'
            '<p class="analytics-note">Full recorded dataset, not the current graph filter. Transfer counts, not a USD or price history.</p>'
            f'{chart}</article></section>')


def _shares_table(shares, accounts):
    values = [[_linked(bucket["to"], esc(bucket["to"]), accounts), esc(bucket["kind"]),
               esc(trace.usd_text(bucket["usd"])), str(bucket["count"]), str(bucket["percent"])] for bucket in shares["buckets"]]
    below, without = shares["below_min_usd"], shares["without_usd"]
    units = ", ".join(f"{amount} of {asset}" for asset, amount in without["base_units"].items()) or "none"
    values.extend([[f'rows below {esc(shares["min_usd"])} USD, counted apart', '', esc(trace.usd_text(below["usd"])), str(below["count"]), ''],
                   ['rows without a USD amount, counted apart', '', f'{esc(units)} (base units)', str(without['count']), '']])
    columns = ('To', 'Kind', 'USD', 'Rows', 'Percent')
    rows = []
    for values_row in values:
        cells = ''.join(f'<td role="cell" class="{"mono" if index == 0 else "num" if index >= 2 else ""}">'
                        f'<span class="cell-label" aria-hidden="true">{label}</span>{value}</td>'
                        for index, (label, value) in enumerate(zip(columns, values_row)))
        rows.append(f'<tr role="row">{cells}</tr>')
    headers = ''.join(f'<th scope="col" role="columnheader">{label}</th>' for label in columns)
    return (f'<div class="table"><table class="flow-shares-table" role="table"><thead role="rowgroup"><tr role="row">{headers}'
            f'</tr></thead><tbody role="rowgroup">{"".join(rows)}</tbody></table></div>')


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


def flow_page(document, accounts, *, evidence_url=None, page_number=1):
    depths = trace.hop_depths(document)
    ordered = sorted(zip(document["hops"], depths), key=lambda item: item[1])
    pages = max(1, (len(ordered) + 49) // 50)
    if not 1 <= page_number <= pages:
        raise ValueError("flow evidence page is outside the recorded range")
    start = (page_number - 1) * 50
    selected = ordered[start:start + 50]
    groups = []
    for depth in sorted(set(depths)):
        hops = [hop for hop, d in selected if d == depth]
        total = depths.count(depth)
        if hops:
            groups.append(f"<h2>Depth {depth}: {total} hop{'' if total == 1 else 's'}</h2><p>{len(hops)} of these hops appear{'s' if len(hops) == 1 else ''} on this evidence page.</p>" +
                          "".join(_hop_card(hop, accounts) for hop in hops))
    stem = Path(evidence_url).stem if evidence_url else page_names([document["subject"]])[document["subject"]]
    def evidence_path(number):
        return f"{stem}.html#transfers" if number == 1 else f"{stem}.page-{number}.html#transfers"
    previous = f'<a href="{evidence_path(page_number - 1)}" rel="prev">Previous transfers</a>' if page_number > 1 else '<span>Previous transfers</span>'
    following = f'<a href="{evidence_path(page_number + 1)}" rel="next">Next transfers</a>' if page_number < pages else '<span>Next transfers</span>'
    pagination = (f'<nav class="flow-evidence-pagination" aria-label="Recorded transfer evidence pages">{previous}'
                  f'<span>Page {page_number} of {pages}</span>{following}</nav>')
    window = document["window"]
    context = f'<h1>Exit trace</h1><p class="mono">{_linked(document["subject"], esc(document["subject"]), accounts)}</p>'
    context += ('<dl class="investigation-summary">'
                f'<div><dt>Recorded transfers</dt><dd>{len(document["hops"]):,}</dd></div>'
                f'<div><dt>Depth reached</dt><dd>{document["depth_reached"]} <span class="stat-connective">of</span> {document["depth"]}</dd></div>'
                f'<div><dt>Evidence page</dt><dd>{page_number} <span class="stat-connective">of</span> {pages}</dd></div></dl>'
                '<nav class="investigation-actions" aria-label="Recorded trace sections"><a href="#recorded-dataset">Dataset analytics</a>'
                '<a href="#transfers">Transfer evidence</a></nav>')
    body = (f'<div class="card summary"><p>{data_text(trace.summary(document))}</p><p class="muted">Read '
            f'{esc(document["fetched_at"])}.</p></div>'
            f"<p>The window starts at {esc(window['from'])}, {esc(window['rule'])} {esc(window['incident_date'])}; "
            "older outbound rows are outside it.</p>"
            "<h2>Shares of the gross outbound transfer value</h2>" + _shares_table(document["shares"], accounts) +
            "<p>Shares are of the subject's gross outbound transfer value; deeper hops are shown and not re summed.</p>"
            f"<p>{esc(trace.gross_sentence(document))}</p>"
            "<h2>The subject at a block</h2>" + _state_card(document["state_at_block"]) +
            "<p class=\"muted\">Only an account that carries an incident label is traced. Each card is one transfer "
            "read from the FastNEAR Transfers API.</p>"
            f'<section class="flow-evidence" id="transfers" aria-label="Recorded transfer evidence"><h2>Recorded transfers</h2>'
            f'<p>Showing {start + 1 if selected else 0} to {start + len(selected)} of {len(ordered)} transfer{"" if len(ordered) == 1 else "s"}, grouped by recorded depth. '
            'Every transfer remains available across the linked evidence pages; the graph uses the full recorded dataset.</p>'
            + pagination + "".join(groups) + pagination + '</section>')
    return page("Exit trace", context + _graph_section(graph.flow_payload(document, accounts), evidence_url) + _flow_analytics(document, accounts) + body, depth=1)


def _status_answers(reads):
    answers = {}
    for read in reads:
        view = answers.setdefault((read["issuer"], read["method"]), {})
        answer = json.dumps(read["answer"], ensure_ascii=False)
        view[answer] = view.get(answer, 0) + 1
    return answers


def _status_reads(reads):
    """The status reads counted by issuer view and answer, as one sentence."""
    answers = _status_answers(reads)
    views = "; ".join(f"{issuer} {method} answered "
                      + " and ".join(f"{answer} {count} time{'' if count == 1 else 's'}" for answer, count in counts.items())
                      for (issuer, method), counts in answers.items())
    blocks = [read["block"] for read in reads]
    times = [read["observed_at"] for read in reads]
    categories = ", ".join(issuers.STATUS_CATEGORIES[:-1]) + " and " + issuers.STATUS_CATEGORIES[-1]
    return (f"{len(reads)} status read{'' if len(reads) == 1 else 's'} over {len({read['subject'] for read in reads})} account id{'' if len({read['subject'] for read in reads}) == 1 else 's'}: the NEAR "
            f"subjects of the label store in the {categories} categories and the recorded "
            f"receiver set, each read on {len(answers)} issuer view{'' if len(answers) == 1 else 's'} at blocks {min(blocks)} to {max(blocks)}, from "
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
    return (f"{read} function call transaction{'' if coverage['transactions'] == 1 else 's'} the Transactions API lists for {json.dumps(owner)} (the account "
            f"the owner view of {HISTORY_TOKEN} answers) as receiver, transaction blocks {coverage['tx_block_min']} "
            f"to {coverage['tx_block_max']}, {'was' if coverage['transactions'] == 1 else 'were'} read; their receipts carried {sum(coverage['actions'].values())} "
            f"successful issuer action{'' if sum(coverage['actions'].values()) == 1 else 's'} on {HISTORY_TOKEN}: {' and '.join(found)}{absent}. {len(missing)} of the "
            f"{len(named)} account id string{'' if len(named) == 1 else 's'} {'does' if len(missing) == 1 else 'do'} not exist on chain{at}.")


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
            f"<h2>Banned by the issuer</h2><p>{len(status)} account id{'' if len(status) == 1 else 's'} read as banned by their issuer, one row each in "
            f"<code>intel/issuers/status.jsonl</code>; a status read is a view call, so no transaction.</p>{status_cards}"
            f'<h2>Issuer action history</h2><p>{data_text(_history_coverage(history))}</p><p>{len(history)} row{"" if len(history) == 1 else "s"} in '
            f"<code>intel/issuers/history.jsonl</code>, one per action, newest block first.</p>{history_cards}")
    reads = issuers.reads()
    accounts = account_pages(labels.load())
    status_rows = [[_subject_cell(row.subject, accounts).replace('href="../', 'href="/'),
                    f'<q class="data">{esc(row.entity.removesuffix(" issuer action"))}</q>',
                    'Banned at recorded block', str(row.source["block"]),
                    f'<time datetime="{esc(row.observed_at)}">{esc(row.observed_at)}</time>', _profile_source(row)] for row in status]
    history_rows = []
    for row in history:
        action = next((method for method, phrase in issuers.HISTORY_METHODS.items() if row.label.startswith(phrase)), row.source["method"])
        history_rows.append([_subject_cell(row.subject, accounts).replace('href="../', 'href="/'), esc(action), str(row.source["block"]),
                             f'<time datetime="{esc(row.observed_at)}">{esc(row.observed_at)}</time>', _profile_source(row)])
    entries = {entry["token"]: entry for entry in issuers.issuer_entries()}
    other_answers = Counter()
    for (issuer, method), counts in _status_answers(reads).items():
        banned = json.dumps(entries[issuer]["banned"], ensure_ascii=False)
        other_answers.update({answer: count for answer, count in counts.items() if answer != banned})
    other_status = ("Other recorded answers: " + "; ".join(f"{answer}: {count}" for answer, count in other_answers.items()) + "."
                    if other_answers else "No other recorded answers.")
    status_scope = "Banned answers only. " + other_status + " Counts by issuer view are in Coverage below."
    summary = ('<div class="page-head issuers-heading"><h1>Issuer actions</h1><nav class="profile-anchors" aria-label="Issuer sections">'
               '<a href="#issuer-status-section">Recorded issuer status</a><a href="#issuer-ledger">Recorded action ledger</a></nav></div>'
               '<p class="issuers-scope">Recorded token-issuer status reads and successful issuer actions. An issuer status is not a current payment verdict.</p>'
               f'<dl class="profile-facts"><div><dt>Status reads</dt><dd>{len(reads)}</dd></div>'
               f'<div><dt>Account IDs covered</dt><dd>{len({read["subject"] for read in reads})}</dd></div>'
               f'<div><dt>Recorded banned-status rows</dt><dd>{len(status)}</dd></div>'
               f'<div><dt>Recorded issuer actions</dt><dd>{len(history)}</dd></div></dl>'
               '<section class="profile-section" id="issuer-status-section"><h2>Recorded issuer status</h2>'
               + _profile_table("issuer-status", ("Account", "Issuer", "Answer", "Block", "Observed UTC", "Evidence"), status_rows, "Search issuer status")
               + f'<p class="issuers-scope">{data_text(status_scope)}</p>'
               + '</section><section class="profile-section" id="issuer-ledger"><h2>Recorded action ledger</h2>'
               + _profile_table("issuer-history", ("Named account string", "Action", "Block", "Observed UTC", "Evidence"), history_rows, "Search issuer actions")
               + '</section><details class="profile-evidence"><summary>Coverage, original status reads and action evidence</summary>'
               + body.removeprefix('<h1>Issuer actions</h1>') + '</details>')
    return page("Issuer actions", summary + _profile_script())


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
        sections.append(f'<section id="record-{esc(title.lower().replace(" ", "-"))}"><h2>{esc(title)}</h2><dl>{"".join(content)}</dl></section>')
    record_id = esc(record["record_id"])
    anchors = "".join(f'<a href="#record-{esc(title.lower().replace(" ", "-"))}">{esc(title)}</a>' for title, _ in _RECORD_SECTIONS)
    body = ('<div class="page-head record-heading"><h1>Incident record</h1>'
            f'<nav class="profile-anchors" aria-label="Record sections">{anchors}</nav></div>' + "".join(sections)
            + '<p class="muted">Re-check from the repository root with '
            + f'<code><span class="nowrap">python -m intel record-check</span> fixtures/records/{record_id}.json</code>.</p>')
    return page("Incident record", body, depth=1, inert=True)


def record_index_page(documents):
    successors = {record["supersedes"]: record["record_id"] for record in documents if record["supersedes"]}
    rows = []
    for record in documents:
        subjects = '<br>'.join(f'<q class="data">{esc(subject["account"])}</q> ({esc(subject["chain"])})'
                                for subject in record["subjects"]) or "No subject named"
        evidence = '<ul>' + ''.join('<li>' + (esc(source["url"]) if urlsplit(source["url"]).hostname == "github.com"
                                             and urlsplit(source["url"]).path.lower().startswith(("/ridwannurudeen/siyab/", "/ridwannurudeen/shieldbot-near/")) else
                                             f'<a href="{esc(source["url"])}">{esc(source["url"])}</a>') +
                                   f'<br>Fetched {esc(source["fetched_at"])}<br><code>{esc(source["content_sha256"])}</code></li>'
                                   for source in record["evidence"]) + '</ul>'
        successor = successors.get(record["record_id"])
        state = (f'Superseded by <a href="{successor}.html">{successor}</a>' if successor else
                 "No local successor")
        cells = [subjects, f'<time datetime="{esc(record["timeline"][-1]["at"])}">{esc(record["timeline"][-1]["at"])}</time>',
                 f'kind {esc(record["kind"])}<br>outcome {esc(record["outcome"])}',
                 f'{state}<br>{len(record["reports"])} report citation{"" if len(record["reports"]) == 1 else "s"}',
                 f'<details><summary>{len(record["evidence"])} evidence source{"s" if len(record["evidence"]) != 1 else ""}</summary>{evidence}</details>',
                 f'<a href="{esc(record["record_id"])}.html">{esc(record["record_id"])}</a>']
        names = ("Subject", "Recorded (UTC)", "Kind and outcome", "Record status", "Evidence", "Full record")
        rows.append('<tr>' + ''.join(f'<td data-label="{name}">{cell}</td>' for name, cell in zip(names, cells)) + '</tr>')
    headers = ''.join(f'<th scope="col">{name}</th>' for name in
                      ("Subject", "Recorded (UTC)", "Kind and outcome", "Record status", "Evidence", "Full record"))
    table = (f'<table class="record-index-table"><caption>{len(documents)} valid immutable record{"" if len(documents) == 1 else "s"}</caption><thead><tr>{headers}</tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table>' if rows else '<p>No valid records.</p>')
    body = ('<h1>Incident records</h1><p>Read the subjects, cited evidence and recorded decisions behind an incident record.</p>'
            '<p class="record-index-note">Synthetic examples: the two committed examples describe a synthetic report. They are not findings about a real incident.</p>'
            '<p>Record status describes supersession within this index. A report\'s current on-chain state must be read from its contract; it is not established by this saved record. Recorded time is when the publisher wrote the record, not when an incident occurred.</p>'
            + table)
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
(() => {
  "use strict";
  const read = document.getElementById("read"), example = document.getElementById("verify-example"), out = document.getElementById("out");
  const registryInput = document.getElementById("registry"), subjectInput = document.getElementById("subject"), hashInput = document.getElementById("hash");
  let generation = 0, controller = null;
  const add = (parent, tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text); parent.append(node); return node; };
  const account = value => value.length >= 2 && value.length <= 64 && /^(([a-z0-9]+[-_])*[a-z0-9]+\\.)*([a-z0-9]+[-_])*[a-z0-9]+$/.test(value);
  const integer = value => Number.isInteger(value) && Math.abs(value) <= 9007199254740991;
  const message = (title, detail) => { out.replaceChildren(); add(out, "h3", title); add(out, "p", detail); };
  example.addEventListener("click", () => {
    for (const input of [registryInput, subjectInput, hashInput]) input.value = input.getAttribute("placeholder");
    generation++; if (controller) controller.abort(); read.disabled = false; read.textContent = "Read the record"; out.setAttribute("aria-busy", "false");
    message("Recorded example loaded into the form", "No network read has run. Choose Read the record to request the current testnet registry value.");
  });
  const invalidate = () => {
    generation++; if (controller) controller.abort(); read.disabled = false; read.textContent = "Read the record"; out.setAttribute("aria-busy", "false");
    message("Inputs changed", "Choose Read the record to compare these inputs with a new direct testnet RPC read.");
  };
  for (const input of [registryInput, subjectInput, hashInput]) input.addEventListener("input", invalidate);
  read.addEventListener("click", async () => {
    const registry = registryInput.value.trim(), subject = subjectInput.value.trim(), pasted = hashInput.value.trim().toLowerCase();
    if (!account(registry) || !account(subject) || pasted && !/^[0-9a-f]{64}$/.test(pasted)) {
      message("Check the inputs", "Enter valid NEAR account IDs for the registry and subject. An evidence hash, if supplied, must contain exactly 64 hexadecimal characters."); return;
    }
    if (controller) controller.abort(); controller = new AbortController(); const active = controller, request = ++generation;
    const deadline = setTimeout(() => active.abort(), 12000);
    read.disabled = true; read.textContent = "Reading testnet…"; out.setAttribute("aria-busy", "true");
    message("Reading current testnet registry", "The request goes directly from this browser to the public RPC. The recorded example remains separate below.");
    try {
      const args = btoa(unescape(encodeURIComponent(JSON.stringify({subject}))));
      const reply = await fetch("RPC_URL", {method: "POST", headers: {"Content-Type": "application/json", Accept: "application/json"}, signal: active.signal,
        body: JSON.stringify({jsonrpc: "2.0", id: "verify", method: "query", params: {request_type: "call_function", finality: "final", account_id: registry, method_name: "latest", args_base64: args}})});
      if (!reply.ok) throw new Error("The testnet RPC returned HTTP " + reply.status + ".");
      const body = await reply.json();
      if (request !== generation) return;
      if (!body || body.error || !body.result || body.result.error || !Array.isArray(body.result.result) || body.result.result.length > 1048576 || !body.result.result.every(value => Number.isInteger(value) && value >= 0 && value <= 255)) throw new Error("The RPC did not return a readable registry view.");
      const record = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(new Uint8Array(body.result.result)));
      if (record === null) { message("No current record", "No record for this subject (the registry reads it as Unknown). This is not an ALLOW result."); return; }
      if (!record || typeof record !== "object" || !["Allow", "Deny", "Unknown", "ALLOW", "DENY", "UNKNOWN"].includes(record.verdict) || typeof record.evidence_hash !== "string" || !/^[0-9a-f]{64}$/.test(record.evidence_hash) || !integer(record.observed_block) || record.observed_block < 0 || !integer(record.recorded_ms) || record.recorded_ms < 0 || record.recorded_ms > 8640000000000000 || typeof record.verdict_id !== "string") throw new Error("The registry record has an unexpected shape; no comparison is established.");
      const same = pasted && record.evidence_hash === pasted;
      out.replaceChildren();
      add(out, "h3", !pasted ? "Registry read complete; no evidence hash supplied" : same ? "Evidence hashes match" : "Evidence hashes differ");
      add(out, "p", "Read directly from testnet at " + new Date().toISOString() + ". This compares the pasted hash with the returned value; it does not hash an evidence file or verify a signature.");
      const table = add(out, "table"); table.className = "verify-comparison";
      const head = add(add(table, "thead"), "tr"); for (const title of ["Field", "Registry read", "Your comparison"]) add(head, "th", title).setAttribute("scope", "col");
      const rows = [["Subject", subject, "Requested subject"], ["Verdict", record.verdict, "Registry value; no payment is signed"], ["Evidence hash", record.evidence_hash, pasted || "Not supplied"], ["Comparison", !pasted ? "Not compared" : same ? "Equal" : "Different", !pasted ? "Paste a hash to compare" : same ? "Equal to the recorded evidence hash" : "Different from the recorded evidence hash"], ["Observed block", record.observed_block, "Block named by the saved judgement"], ["Recorded time (UTC)", new Date(record.recorded_ms).toISOString(), "When the recorder wrote this value"], ["Verdict ID", record.verdict_id, "Returned by the registry"]];
      const tbody = add(table, "tbody"); for (const values of rows) { const tr = add(tbody, "tr"); for (let index = 0; index < values.length; index++) { const td = add(tr, "td", values[index]); td.setAttribute("data-label", ["Field", "Registry read", "Your comparison"][index]); } }
      const explorer = add(out, "a", "Open registry on the testnet explorer"); explorer.href = "https://testnet.nearblocks.io/address/" + encodeURIComponent(registry);
      const raw = add(out, "details"); add(raw, "summary", "Raw registry response"); add(raw, "pre", JSON.stringify(record, null, 2));
    } catch (error) {
      if (request === generation) message("Testnet read unavailable", active.signal.aborted ? "The read exceeded 12 seconds or was interrupted. Your inputs are retained; choose Read the record to retry." : "The read failed: " + error.message + " Your inputs are retained; choose Read the record to retry.");
    } finally { clearTimeout(deadline); if (request === generation) { read.disabled = false; read.textContent = "Read the record"; out.setAttribute("aria-busy", "false"); } }
  });
  window.addEventListener("pagehide", () => { generation++; if (controller) controller.abort(); });
  window.addEventListener("pageshow", event => { if (event.persisted) { read.disabled = false; read.textContent = "Read the record"; out.setAttribute("aria-busy", "false"); message("Read interrupted", "Choose Read the record for a fresh direct testnet response."); } });
})();
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
    record = json.loads(VERIFY_RECORD.read_text(encoding="utf-8"))
    computed_hash = hashlib.sha256((EVIDENCE_DIR / f'{record["evidence_hash"]}.json').read_bytes()).hexdigest()
    recorded_at = datetime.fromtimestamp(record["recorded_ms"] / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    example_rows = ''.join(f'<tr><th scope="row">{label}</th><td>{esc(value)}</td></tr>' for label, value in
                           (("Subject", subject), ("Registry verdict", record["verdict"]),
                            ("Observed block", record["observed_block"]), ("Recorded time", recorded_at),
                            ("Record evidence hash", record["evidence_hash"]), ("Saved file SHA-256", computed_hash),
                            ("Saved file comparison", "Equal" if computed_hash == record["evidence_hash"] else "Different"),
                            ("Verdict ID", record["verdict_id"])))
    body = ("<h1>Read a registry record</h1><p>The page calls <code>latest</code> on the registry contract over the "
            f"public testnet RPC <code>{TESTNET_RPC}</code> from your browser and shows the record next to the "
            "evidence hash you paste. Compute the hash yourself with <code>sha256sum</code> of the evidence file.</p>"
            "<p>The examples in the empty fields are the vendor's latest record, "
            "<code>fixtures/registry/testnet-latest-kestrelridge-shop.json</code>, and the sha256 of its evidence "
            "document.</p>"
            '<div class="verification-workspace"><section class="verify-live"><h2>Live testnet read</h2>'
            '<p>A current read runs only when you choose Read the record. It does not use the website API or sign a transaction.</p>'
            '<button id="verify-example" type="button">Use recorded example</button>'
            f'<div class="card verify-form"><label>Registry account<input id="registry" autocomplete="off" placeholder="{registry}">'
            f'</label><label>Subject (a NEAR account id)<input id="subject" autocomplete="off" placeholder="{subject}">'
            f'</label><label>Evidence hash (64 hex)<input id="hash" autocomplete="off" placeholder="{evidence_hash}">'
            '</label><button id="read" type="button">Read the record</button></div>'
            '<div id="out" class="verify-result" aria-live="polite" aria-busy="false"><h3>Ready for a direct read</h3>'
            '<p>Enter the registry and subject, and optionally paste an evidence hash. Unavailable reads and absent records do not establish ALLOW.</p></div></section>'
            '<section class="verify-saved"><h2>Recorded example</h2>'
            f'<p>Saved testnet registry view recorded at {recorded_at}. This is a historical example, not a current network response.</p>'
            f'<table class="verify-example-table"><caption>Committed registry view and saved evidence bytes</caption><tbody>{example_rows}</tbody></table>'
            f'<p><a href="https://testnet.nearblocks.io/address/{registry}">Open registry on the testnet explorer</a></p>'
            f'<details><summary>Saved record JSON</summary><pre>{esc(json.dumps(record, indent=2))}</pre></details></section></div>'
            '<section class="verify-limits"><h2>What the comparison establishes</h2>'
            '<p>A matching pasted hash establishes equality with the value returned by the registry. It does not verify a signature, establish the truth of the evidence sources, or prove that the Gate consulted the registry. The live comparison does not compute the hash of an evidence file for you.</p>'
            '<p>The registry and enforcement demonstrations are on testnet. A registry verdict is distinct from a fresh payment check; this browser read sends no payment.</p></section>'
            f'<script>{script}</script>')
    return page("Verify a record", body)


def not_found_page():
    body = ("<h1>Not found</h1><p>No page of this site and no route of the read only API answers this path.</p>"
            '<p><a href="/index.html">Back to the index</a></p>')
    return page("Not found", body, rooted=True)


def investigate_page():
    body = ('<div id="account-investigation"><div class="investigate-heading page-head"><div><h1 id="investigate-title">Investigate account</h1>'
            '<p>Finalized account state, indexed activity and the evidence behind each observation.</p></div>'
            '<div class="workspace-actions"><button id="investigate-refresh" type="button" disabled>Refresh reads</button>'
            '<button id="investigate-watch" type="button" disabled>Watch account</button>'
            '<button id="investigate-save" type="button" data-save-investigation disabled>Save investigation</button></div></div>'
            '<form id="investigate-form" class="investigate-search" action="/investigate.html" method="get">'
            '<label for="investigate-account">NEAR mainnet account<input id="investigate-account" name="account" maxlength="64" autocomplete="off" placeholder="account.near" required></label>'
            '<button type="submit" class="primary">Investigate</button></form>'
            '<p id="investigate-status" class="investigate-status" role="status">Enter an account to read mainnet data. No payment is signed or sent.</p>')
    for kind, title in (("profile", "Finalized account state"), ("holdings", "Indexed token holdings"),
                        ("transfers", "Transfers"), ("transactions", "Transactions")):
        body += (f'<section id="investigate-{kind}" class="investigate-section" data-investigation-panel>'
                 f'<h2 tabindex="-1">{title}</h2><div data-live-content><p class="investigate-empty">Select an account to read this data.</p></div></section>')
    body += ('<section id="investigate-identity" class="investigate-section" data-investigation-panel><h2 tabindex="-1">Account identity</h2>'
             '<button id="investigate-read-identity" type="button" disabled>Read identity evidence</button><div data-live-content>'
             '<p class="investigate-empty">Read contract, token and validator observations on demand. Metadata does not establish ownership.</p></div></section>'
             '<section id="investigate-history" class="investigate-section" data-investigation-panel><h2 tabindex="-1">Retained history</h2>'
             '<div class="investigate-history-actions"><label for="investigate-history-kind">Records<select id="investigate-history-kind"><option value="transfers">Transfers</option><option value="transactions">Transactions</option></select></label>'
             '<button id="investigate-history-load" type="button" disabled>Load retained rows</button>'
             '<label for="investigate-backfill-pages">Provider read bound<select id="investigate-backfill-pages"><option value="1">One page · up to 100 rows</option><option value="2">Two pages · up to 200 rows</option></select></label>'
             '<button id="investigate-backfill" type="button" disabled>Backfill older pages</button></div>'
             '<p class="investigate-scope">Retained provider observations can have gaps. Backfill reads only the chosen bound and saves public chain data. Reaching the provider\'s last page does not establish complete history.</p>'
             '<div id="investigate-backfill-status" role="status"></div><div data-live-content><p class="investigate-empty">Select an account, then load retained rows or request a bounded backfill.</p></div></section>'
             '<section class="investigate-section"><h2>Observed neighborhood</h2><div class="investigate-graph-actions">'
             '<label for="investigate-depth">Expansion depth<select id="investigate-depth"><option value="1">One hop</option><option value="2">Two hops</option></select></label>'
             '<button id="investigate-load-graph" type="button" disabled>Load neighborhood</button></div>'
             '<div id="investigate-graph"><p class="investigate-empty">Load a bounded neighborhood from indexed transfers. It is not complete account history.</p></div></section>'
             '<noscript><p>JavaScript is required for live account reads. Recorded evidence remains available through the navigation.</p></noscript></div>')
    body += (ROOT / "intel/ui/enrichment-panel.html").read_text(encoding="utf-8")
    body += (ROOT / "intel/ui/portfolio-panel.html").read_text(encoding="utf-8")
    body += _script_tag((ROOT / "intel/ui/graphs.js").read_text(encoding="utf-8"))
    body += _script_tag((ROOT / "intel/ui/investigate.js").read_text(encoding="utf-8"))
    body += _script_tag((ROOT / "intel/ui/enrichment.js").read_text(encoding="utf-8"))
    body += _script_tag((ROOT / "intel/ui/portfolio.js").read_text(encoding="utf-8"))
    return page("Investigate account", body)


def _cloud_workspace_panel():
    return ('<details id="cloud-workspace" class="cloud-workspace" open><summary>Shared workspace</summary>'
            '<div id="cloud-shell"><p class="source-line">Reading your shared workspace session…</p></div>'
            '<noscript><p>JavaScript is needed to sign in to a shared workspace. Browser workspace controls remain below.</p></noscript></details>')


def workspace_page():
    body = ('<div class="workspace-page-head page-head"><div><h1>Workspace</h1></div>'
            '<a class="button primary" href="/explore.html">Start investigation</a></div>'
            + _cloud_workspace_panel() +
            (ROOT / "intel/ui/investigation-jobs-panel.html").read_text(encoding="utf-8") +
            (ROOT / "intel/ui/delivery-panel.html").read_text(encoding="utf-8") +
            '<section id="browser-workspace"><h2>In this browser</h2><p>Saved in this browser. Export a backup to move your work between devices.</p>'
            '<p id="workspace-status" role="status" aria-live="polite"></p><div class="workbench-grid workbench-grid--stacked">'
            '<section class="workspace-section"><div class="section-head"><h2>Investigations</h2><span id="case-count" class="source-line">0 saved investigations</span></div>'
            '<label for="case-search">Search saved work<input type="search" id="case-search" placeholder="Title, notes or account"></label>'
            '<div id="saved-cases"></div></section><div class="workspace-side">'
            '<section class="workspace-section"><h2>Watchlist</h2><form id="watch-form" class="watch-form">'
            '<label for="watch-account">Account<input id="watch-account" maxlength="128" autocomplete="off" required placeholder="account.near"></label>'
            '<button type="submit">Watch account</button></form><ul id="watched-accounts" class="watchlist"><li>No watched accounts yet.</li></ul></section>'
            '<section class="workspace-section"><h2>Alerts</h2><label class="monitor-toggle"><input id="alerts-enabled" type="checkbox">Monitor watched accounts</label>'
            '<p>Monitoring runs while a Siyab page stays open. It checks the current feed window; it cannot cover time when the browser is closed.</p>'
            '<p id="alert-state" role="status" aria-live="polite">Browser monitoring is off.</p>'
            '<button id="desktop-alerts" type="button">Allow desktop notifications</button>'
            '<p class="source-line">Browser permission is optional. Matches are also kept below. Pausing the live displays does not stop alert monitoring.</p>'
            '<ul id="watch-notifications" class="watch-notifications"></ul><button id="clear-notifications" type="button">Clear alert history</button></section></div></div>'
            '<section class="workspace-portability"><h2>Your workspace data</h2><p>Investigations, notes, watchlists and alert history stay on this device. Import merges investigations and accounts without replacing existing notes or enabling monitoring.</p>'
            '<div class="workspace-actions"><button id="export-workspace" type="button">Export workspace JSON</button>'
            '<label>Import workspace JSON<input id="import-workspace" type="file" accept="application/json,.json"></label>'
            '<button id="reset-workspace" type="button">Reset local workspace</button></div></section>'
            '<noscript><p>JavaScript is needed to save investigations, export tables and manage browser alerts. Recorded accounts and evidence remain available through the navigation.</p></noscript></section>')
    body += _script_tag((ROOT / "intel/ui/investigation-jobs.js").read_text(encoding="utf-8"))
    body += _script_tag((ROOT / "intel/ui/delivery.js").read_text(encoding="utf-8"))
    return page("Workspace", body)


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
             "explore.html": explore_page(incidents, flows, clusters, store, top_judgements_dir),
             "investigate.html": investigate_page(),
             "workspace.html": workspace_page(),
             "top-receivers.html": top_receivers_page(top_judgements_dir), "feed.html": feed_page(),
             "verify.html": verify_page(), "404.html": not_found_page(), "issuers.html": issuers_page()}
    pages.update(_incident_record_pages())
    for document in incidents:
        pages[f"incidents/{document['id']}.html"] = incident_page(document, accounts)
    entity_data = {}
    for name, title, categories in ENTITY_SETS:
        rows = _entity_rows(categories, store)
        entity_data[f"entities/{name}.table.json"] = {"rows": rows}
        for number in range(1, max(1, (len(rows) + 49) // 50) + 1):
            filename = f"{name}.html" if number == 1 else f"{name}.page-{number}.html"
            pages[f"entities/{filename}"] = entity_page(title, categories, store, page_number=number,
                                                        collection_name=name, rows=rows)
    for entity in registry_entities:
        if entity_paths[entity["id"]] in pages:
            raise ValueError(f"entity page collides with a collection page: {entity['id']}")
        pages[entity_paths[entity["id"]]] = registry_entity_page(entity, accounts)
    pages.update({path: account_page(subject, store, accounts, incidents, flows, clusters)
                  for subject, path in accounts.items()})
    for name, document in flows:
        for number in range(1, max(1, (len(document["hops"]) + 49) // 50) + 1):
            filename = f"{name}.html" if number == 1 else f"{name}.page-{number}.html"
            pages[f"flows/{filename}"] = flow_page(document, accounts, evidence_url=f"/flows/{name}.html", page_number=number)
    for name, document in clusters:
        pages[f"clusters/{name}.html"] = cluster_page(name, document, accounts)
    graph_assets = {}
    for kind, documents, make_payload in (("flows", flows, graph.flow_payload),
                                          ("clusters", clusters, graph.cluster_payload)):
        for name, document in documents:
            url, encoded = _graph_asset(make_payload(document, accounts), f"/{kind}/{name}.html")
            graph_assets[url.lstrip("/")] = encoded
    for relative, encoded in graph_assets.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded, encoding="utf-8", newline="\n")
    script_paths = []
    (out / "assets").mkdir(exist_ok=True)
    for name in ("landing.js", "number-format.js", "graphs.js", "workspace.js", "investigate.js", "cloud-workspace.js", "enrichment.js", "portfolio.js", "investigation-jobs.js", "delivery.js"):
        script = (ROOT / "intel/ui" / name).read_text(encoding="utf-8")
        script_path, _ = _script_asset(script)
        (out / script_path.lstrip("/")).write_text(script, encoding="utf-8", newline="\n")
        script_paths.append(script_path.lstrip("/"))
    for relative, text in pages.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        canonical = SITE_ORIGIN + "/" + ("" if relative == "index.html" else relative)
        if relative != "404.html":
            text = text.replace('</head>', f'<link rel="canonical" href="{esc(canonical)}">\n<meta property="og:url" content="{esc(canonical)}">\n</head>')
        path.write_bytes((text.replace(ROBOTS, "") if indexable else text).encode("utf-8"))
    (out / "style.css").write_text(STYLE, encoding="utf-8", newline="\n")
    (out / "search.json").write_text(json.dumps(search, ensure_ascii=False, separators=(",", ":")) + "\n",
                                     encoding="utf-8", newline="\n")
    for relative, dataset in entity_data.items():
        (out / relative).write_text(json.dumps(dataset, ensure_ascii=False, separators=(",", ":")) + "\n",
                                   encoding="utf-8", newline="\n")
    fonts = []
    for source in sorted((ROOT / "intel/ui/fonts").iterdir()):
        relative = f"fonts/{source.name}"
        (out / "fonts").mkdir(exist_ok=True)
        (out / relative).write_bytes(source.read_bytes())
        fonts.append(relative)
    brand_assets = []
    for source in sorted((ROOT / "intel/ui/brand").glob("*")):
        relative = "brand/" + source.name
        (out / "brand").mkdir(exist_ok=True)
        (out / relative).write_bytes(source.read_bytes())
        brand_assets.append(relative)
    if (out / "brand/favicon.ico").is_file():
        (out / "favicon.ico").write_bytes((out / "brand/favicon.ico").read_bytes())
        brand_assets.append("favicon.ico")
    return sorted([*pages, *graph_assets, *entity_data, *script_paths, "search.json", "style.css", *fonts, *brand_assets])
