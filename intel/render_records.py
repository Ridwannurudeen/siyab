"""python -m intel render-records --out <dir>: the incident desk pages, from the committed records.

The pages are built from fixtures/records/, so the desk can be reviewed before the contract lane
lands and the same renderer reads real records later without a second template. Nothing here invents
a number: every count is computed from the committed files, and the action beside a record comes from
records.verdict_for, the same function the signing paths call.

Two pages: index.html, one row per subject with its state and what a payment may do, and
records/<id>.html, one record with its subjects, state, timeline, evidence, decisions and the record
it supersedes. A page links the policy that governs it, and it never shows a victim: a subject is an
account agents should not pay.
"""

import html
import json
from pathlib import Path

from intel import records, render

ROOT = Path(__file__).resolve().parent.parent
RECORDS_DIR = ROOT / "fixtures" / "records"

ACTION = {"DENY": "blocked", "UNKNOWN": "asks a human", None: "no effect"}


def esc(text):
    return html.escape(str(text), quote=True)


def load(records_dir=RECORDS_DIR):
    """Every committed record, sorted by id, refusing the report body vector by shape."""
    found = []
    for path in sorted(Path(records_dir).glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if "record_id" in document:
            records.validate(document)
            found.append(document)
    return sorted(found, key=lambda document: document["record_id"])


def action_for(record):
    """What a payment may do for this record alone, through the model's one mapping."""
    return ACTION[records.verdict_for(records.summary([record]), record["kind"])]


def _subject_rows(document):
    action = action_for(document)
    return "".join(
        f"<tr><td><code>{esc(subject)}</code></td><td>{esc(document['state'])}</td>"
        f"<td>{esc(action)}</td></tr>" for subject in document["subjects"])


def _timeline_rows(document):
    return "".join(
        f"<tr><td>{esc(entry['event'])}</td><td>{esc(entry['at'])}</td>"
        f"<td>{esc(entry['source']['type'])}: {esc(entry['source']['ref'])}</td></tr>"
        for entry in document["timeline"])


def _evidence_rows(document):
    return "".join(
        f"<tr><td><code>{esc(entry['sha256'])}</code></td>"
        f"<td><a href=\"{esc(entry['url'])}\">{esc(entry['url'])}</a></td>"
        f"<td>{esc(entry['fetched_at'])}</td></tr>" for entry in document["evidence"])


def _moderation_rows(document):
    if not document["moderation"]:
        return "<p class=\"muted\">No decision yet.</p>"
    return "".join(
        f"<tr><td>{esc(entry['decision'])}</td><td>{esc(entry['decided_at'])}</td>"
        f"<td>{esc(entry['reason'])}</td><td><code>{esc(entry['reason_sha256'])}</code></td></tr>"
        for entry in document["moderation"])


def index_page(loaded):
    rows = []
    for document in loaded:
        for subject in document["subjects"]:
            rows.append(
                f"<tr><td><a href=\"records/{esc(document['record_id'])}.html\"><code>{esc(subject)}</code></a></td>"
                f"<td>{esc(document['kind'])}</td><td>{esc(document['outcome'])}</td>"
                f"<td>{esc(document['state'])}</td><td>{esc(action_for(document))}</td></tr>")
    body = (
        "<h1>The incident desk</h1>\n"
        "<p>Every row is one committed record. A subject is an account agents should not pay, never a "
        "victim. A verdict is advisory: it states what the rules found and where they found it.</p>\n"
        f"<p>{len(loaded)} records, {len(rows)} subject rows.</p>\n"
        "<table><thead><tr><th>Subject</th><th>Kind</th><th>Outcome</th><th>State</th>"
        "<th>A payment</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>\n"
        "<p>What this desk commits to is in docs/REPORTING.md, and how a record is written is in "
        "docs/INCIDENTS.md.</p>\n")
    return render.page("The incident desk", body, depth=0)


def record_page(document):
    supersedes = ("<p>Supersedes <code>" + esc(document["supersedes"]) + "</code>.</p>\n"
                  if document["supersedes"] else "")
    body = (
        f"<h1>{esc(document['kind'])}, {esc(document['outcome'])}, {esc(document['state'])}</h1>\n"
        f"<p><code>{esc(document['record_id'])}</code></p>\n{supersedes}"
        f"<p>Publisher <code>{esc(document['publisher'])}</code>. A payment: "
        f"<strong>{esc(action_for(document))}</strong>.</p>\n"
        "<h2>Subjects</h2>\n"
        "<p>An account here is one agents should not pay. It is never a victim.</p>\n"
        "<table><thead><tr><th>Subject</th><th>State</th><th>A payment</th></tr></thead><tbody>"
        + _subject_rows(document) + "</tbody></table>\n"
        "<h2>Timeline</h2>\n"
        "<p>Append only. Every timestamp names its source, and none is inferred.</p>\n"
        "<table><thead><tr><th>Event</th><th>At</th><th>Source</th></tr></thead><tbody>"
        + _timeline_rows(document) + "</tbody></table>\n"
        "<h2>Evidence</h2>\n"
        "<table><thead><tr><th>sha256</th><th>Document</th><th>Fetched</th></tr></thead><tbody>"
        + _evidence_rows(document) + "</tbody></table>\n"
        "<h2>Decisions</h2>\n"
        "<p>The chain keeps the reason hash; the readable reason is published here.</p>\n"
        "<table><thead><tr><th>Decision</th><th>At</th><th>Reason</th><th>Reason sha256</th></tr></thead>"
        "<tbody>" + _moderation_rows(document) + "</tbody></table>\n")
    return render.page(f"{document['kind']} {document['record_id'][:12]}", body, depth=1)


def render_records(out_dir, records_dir=RECORDS_DIR):
    """Write the desk pages under out_dir and return the paths written, relative to it."""
    loaded = load(records_dir)
    out = Path(out_dir)
    pages = {"index.html": index_page(loaded)}
    for document in loaded:
        pages[f"records/{document['record_id']}.html"] = record_page(document)
    for relative, text in pages.items():
        path = out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    (out / "style.css").write_text(render.STYLE, encoding="utf-8", newline="\n")
    return sorted([*pages, "style.css"])
