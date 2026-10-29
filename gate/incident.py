"""python -m gate incident <id>: an incident file from intel/incidents/ as plain text under 80
columns (title and date, the sources with their sha256 and fetch time, the accounts with the role
as reported and their assertion ids, the on chain facts with block numbers, the rules today and
what is not public); --json prints the file as it is committed."""

import json
import re
import sys
import textwrap
from pathlib import Path

INCIDENTS_DIR = Path(__file__).resolve().parent.parent / "intel" / "incidents"
WIDTH = 79
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")


def path_for(incident_id):
    """The incident file for an id of lowercase letters, digits and hyphens."""
    if not _ID.fullmatch(incident_id):
        raise ValueError(f"not an incident id: {incident_id!r}")
    path = INCIDENTS_DIR / f"{incident_id}.json"
    if not path.is_file():
        raise ValueError(f"no incident {incident_id!r} under {INCIDENTS_DIR}")
    return path


def _wrap(text, indent=0, hang=2):
    return textwrap.wrap(text, WIDTH, initial_indent=" " * indent, subsequent_indent=" " * (indent + hang),
                         break_on_hyphens=False) or [" " * indent]


def render(document):
    """The lines of the text page for an incident document."""
    unlisted = {a["quote"]["source"] for a in document["accounts"]} - {s["id"] for s in document["sources"]}
    if unlisted:
        raise ValueError(f"a quote names sources the incident does not list: {sorted(unlisted)}")
    lines = _wrap(f"{document['title']} ({document['date']})", hang=0)
    lines += _wrap(f"Chains: {', '.join(document['chains'])}", hang=0)
    lines += ["", "Sources"]
    for source in document["sources"]:
        lines += _wrap(f"[{source['id']}] {source['publisher']}, {source['url']}", 2)
        lines += _wrap(f"sha256 {source['content_sha256']}", 4)
        lines += _wrap(f"fetched {source['fetched_at']}", 4)
    route = set(document.get("route_subjects", []))
    route_heading = ("Protocol accounts the report names as the route (infrastructure and bridge labels, which "
                     "the label check does not refuse)")
    groups = (("Accounts named in the report", [a for a in document["accounts"] if a["subject"] not in route]),
              (route_heading, [a for a in document["accounts"] if a["subject"] in route]))
    for heading, accounts in groups:
        if not accounts:
            continue
        lines += [""] + _wrap(heading, hang=2)
        for account in accounts:
            lines += _wrap(f"{account['subject']} ({account['chain']})", 2)
            lines += _wrap(f"role as reported: {account['role_as_reported']}", 4)
            quote = account["quote"]
            lines += _wrap(f"quoted from [{quote['source']}]: \"{quote['text']}\"", 4)
            lines += _wrap("assertions:", 4)
            lines += [f"      {assertion_id}" for assertion_id in account["assertion_ids"]]
    for key, title in (("code_hash_as_reported", "Code hash as reported"), ("ref_pools_as_reported", "Ref pools as reported")):
        if key in document:
            item = document[key]
            value = item.get("value") or ", ".join(str(n) for n in item.get("ids", []))
            lines += ["", title]
            lines += _wrap(value, 2)
            lines += _wrap(f"quoted from [{item['quote']['source']}]: \"{item['quote']['text']}\"", 4)
    lines += ["", "On chain facts"]
    if document.get("onchain_state"):
        lines += _wrap(document["onchain_state"], 2)
    for fact in document["onchain_facts"]:
        lines += _wrap(f"{fact['subject']}", 2)
        lines += _wrap(f"{fact['method']} at block {fact['block']}: {fact['summary']}", 4)
    lines += ["", "Rules today (gate.rules.judge in replay, empty allowlist)"]
    for entry in document["rules_today"]:
        lines += _wrap(f"{entry['subject']} as {entry['as']}: {entry['verdict']}", 2)
        lines += _wrap("; ".join(f"{c['id']} {c['status']}" for c in entry["checks"]), 4)
    lines += ["", "Not public"]
    for item in document["not_public"]:
        lines += _wrap(f"- {item}", 2)
    lines += ["", "Standard"]
    lines += _wrap(document["standard"], 2)
    return lines


def main(incident_id, as_json=False, out=print):
    """The incident command; returns 0, or 2 for an unknown id."""
    try:
        path = path_for(incident_id)
    except ValueError as error:
        print(f"no incident: {error}", file=sys.stderr)
        return 2
    if as_json:
        out(path.read_text(encoding="utf-8").rstrip("\n"))
        return 0
    for line in render(json.loads(path.read_text(encoding="utf-8"))):
        out(line)
    return 0
