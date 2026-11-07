"""The OFAC Specially Designated Nationals list, digital currency addresses only.

python -m intel sanctions-fetch downloads SDN_ADVANCED.XML from the OFAC Sanctions List Service and
writes every Digital Currency Address feature of every entry to
fixtures/sanctions/ofac-sdn-addresses-<UTC fetch time>.json: the entry's uid, name and programs,
OFAC's asset code, the address as published and its lookup key, with the file's URL, fetch time and
sha256 and the list's own date of issue. The raw file (about 128 MB) is not kept in the repository;
extract over a copy whose sha256 is the recorded one gives the same fixture.

SDN_ADVANCED.XML, not SDN.CSV: the CSV remarks column stops at 1000 characters, which cuts long
address lists short.

is_sanctioned is the lookup the verdict path will call; it reads the committed fixture and never the
network. A match is a fact with a date (the entry, the list's date and the file's hash), not a score.
"""

import hashlib
import io
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = ROOT / "fixtures" / "sanctions"
FIXTURE_NAME = "ofac-sdn-addresses"
# OFAC's technical notice of 16 May 2024 names this host for its list files and says it refuses a
# request that carries no User-Agent header.
SDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML"
COMMAND = "python -m intel sanctions-fetch"
# The export host took about 20 seconds to answer some requests, and one fetch with the 30 second RPC
# timeout stopped on a read; the socket timeout here is longer.
TIMEOUT_S = 120
FEATURE_PREFIX = "Digital Currency Address - "
# The list has no NEAR code. NEAR is the coin's ticker, the form OFAC gives most codes (ETH, TRX, SOL).
NEAR_ASSET = "NEAR"
KEY_RULE = ("key is the address lower cased when it is 0x and 40 hex characters (an EVM address), and the "
            "address as published otherwise")
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_SPACE = re.compile(r"\s")
_STAMP = "%Y%m%dT%H%M%SZ"
_TIME = "%Y-%m-%dT%H:%M:%SZ"


class SanctionsError(Exception):
    """A list file or a fixture that does not hold what the extract expects."""


def normalise(address):
    """The lookup key of an address, compared the way the intents path compares a recipient
    (gate/intents.py _same, which ignores case): an EVM address, 0x and 40 hex, lower case, since its
    case is only the EIP-55 checksum; any other address exactly as published (base58, which most of
    them use, is case sensitive)."""
    return address.lower() if _EVM_ADDRESS.fullmatch(address) else address


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _children(element, name):
    return [child for child in element if _local(child.tag) == name]


def _one(items, what, uid):
    if len(items) != 1:
        raise SanctionsError(f"entry {uid} has {len(items)} {what}, not one")
    return items[0]


def _name(party, uid, primary_latin):
    """The entry's name as the file writes it: the parts of the Primary Latin name of its primary
    alias, in the file's order, joined by one space, case unchanged. SDN.XML and SDN.CSV render the
    same entry as LAST, First with the last name upper cased; the uid, not the name, identifies it."""
    profile = _one(_children(party, "Profile"), "profiles", uid)
    identity = _one([i for i in _children(profile, "Identity") if i.get("Primary") == "true"], "primary identities",
                    uid)
    alias = _one([a for a in _children(identity, "Alias") if a.get("Primary") == "true"], "primary aliases", uid)
    name = _one([n for n in _children(alias, "DocumentedName") if n.get("DocNameStatusID") == primary_latin],
                "Primary Latin names", uid)
    return " ".join(value.text for value in name.iter() if _local(value.tag) == "NamePartValue")


def parse(data):
    """(the list's date of issue as YYYY-MM-DD, listings) of SDN_ADVANCED.XML bytes: one listing per
    Digital Currency Address feature, in file order, {uid, name, programs, asset, address, key}. The
    uid is the entry's FixedRef, the asset the code after "Digital Currency Address - " and the
    programs the Program measures of the entry's SanctionsEntry on the SDN List (the file also holds
    entries on other OFAC lists for some of the same parties), all as published."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise SanctionsError("the file declares a DOCTYPE or an entity, which SDN_ADVANCED.XML never does")
    assets, references, programs, found, issued, stack = None, {}, {}, [], None, []
    try:
        for event, element in ElementTree.iterparse(io.BytesIO(data), events=("start", "end")):
            if event == "start":
                if not stack and _local(element.tag) != "Sanctions":
                    raise SanctionsError(f"the root element is {_local(element.tag)!r}, not Sanctions")
                stack.append(_local(element.tag))
                continue
            tag = stack.pop()
            if stack == ["Sanctions"] and tag == "DateOfIssue":
                day = {_local(part.tag): int(part.text) for part in element}
                issued = datetime(day["Year"], day["Month"], day["Day"]).strftime("%Y-%m-%d")
            # Only an item of a top level section (a party, a sanctions entry, a reference value set) is
            # read, and then cleared, so the 128 MB file is never held as one tree.
            if len(stack) != 2 or stack[1] == "DateOfIssue":
                continue
            if tag == "FeatureTypeValues":
                assets = {value.get("ID"): value.text[len(FEATURE_PREFIX):] for value in element
                          if (value.text or "").startswith(FEATURE_PREFIX)}
            elif tag in ("ListValues", "SanctionsTypeValues", "DocNameStatusValues"):
                references.update({(tag, value.text): value.get("ID") for value in element})
            elif tag == "DistinctParty":
                uid = int(element.get("FixedRef"))
                if assets is None or ("DocNameStatusValues", "Primary Latin") not in references:
                    raise SanctionsError(f"entry {uid} comes before the feature types and name statuses")
                addresses = [(assets[feature.get("FeatureTypeID")], detail.text) for feature in element.iter()
                             if _local(feature.tag) == "Feature" and feature.get("FeatureTypeID") in assets
                             for detail in feature.iter() if _local(detail.tag) == "VersionDetail"]
                for asset, address in addresses:
                    if not address or _SPACE.search(address):
                        raise SanctionsError(f"entry {uid}: the {asset} address {address!r} is empty or holds a space")
                if addresses:
                    name = _name(element, uid, references[("DocNameStatusValues", "Primary Latin")])
                    found += [(uid, name, asset, address) for asset, address in addresses]
            elif tag == "SanctionsEntry":
                if not {("ListValues", "SDN List"), ("SanctionsTypeValues", "Program")} <= set(references):
                    raise SanctionsError("a sanctions entry comes before the lists and the sanctions types")
                if element.get("ListID") != references[("ListValues", "SDN List")]:
                    element.clear()
                    continue
                program = references[("SanctionsTypeValues", "Program")]
                uid = int(element.get("ProfileID"))
                if uid in programs:
                    raise SanctionsError(f"entry {uid} has more than one SDN List sanctions entry")
                programs[uid] = [measure.findtext("{*}Comment") for measure in _children(element, "SanctionsMeasure")
                                 if measure.get("SanctionsTypeID") == program]
            element.clear()
    except ElementTree.ParseError as error:
        raise SanctionsError(f"the file does not parse: {error}") from error
    if issued is None or assets is None:
        raise SanctionsError("the file has no DateOfIssue or no feature types")
    missing = sorted({uid for uid, _, _, _ in found if uid not in programs})
    if missing:
        raise SanctionsError(f"entries {missing} have addresses but no SDN List sanctions entry")
    return issued, [{"uid": uid, "name": name, "programs": programs[uid], "asset": asset, "address": address,
                     "key": normalise(address)} for uid, name, asset, address in found]


def extract(data, fetched_at):
    """The fixture document for SDN_ADVANCED.XML bytes read at fetched_at."""
    issued, listings = parse(data)
    return {"source": {"url": SDN_URL, "content_sha256": hashlib.sha256(data).hexdigest(), "fetched_at": fetched_at},
            "publish_date": issued, "command": COMMAND, "key_rule": KEY_RULE, "listings": listings}


def fetch(clock=time.time):
    """(the bytes of SDN_ADVANCED.XML, the UTC time they were read) from the Sanctions List Service."""
    request = urllib.request.Request(SDN_URL, headers={"User-Agent": "shieldbot-near"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:
        data = reply.read()
    return data, datetime.fromtimestamp(clock(), UTC).strftime(_TIME)


def write(document):
    """Write the document to FIXTURE_DIR as ofac-sdn-addresses-<fetch time>.json; returns the path."""
    stamp = datetime.strptime(document["source"]["fetched_at"], _TIME).strftime(_STAMP)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{FIXTURE_NAME}-{stamp}.json"
    path.write_bytes((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return path


def load():
    """The newest fixture under FIXTURE_DIR."""
    found = sorted(FIXTURE_DIR.glob(f"{FIXTURE_NAME}-*.json"))
    if not found:
        raise SanctionsError(f"no {FIXTURE_NAME}-*.json under {FIXTURE_DIR}")
    return json.loads(found[-1].read_text(encoding="utf-8"))


def is_sanctioned(chain_or_asset, address, document=None):
    """[{uid, name, programs, asset, address, publish_date, source_sha256}]: one match per listing of
    the address in document (by default the newest committed fixture), empty when the list does not
    name it. The address is compared by its key (normalise).

    chain_or_asset is an OFAC asset code, upper case as OFAC writes it (XBT, ETH, USDT), which matches
    the listings under that code only, or a chain code, lower case as 1Click writes it (eth, base,
    btc, tron, sol, near), so ETH and eth differ. Any chain code but near matches every listing of the
    address whatever code OFAC files it under: the address is what the list names, one key holds the
    same EVM address on every EVM chain, Omni USDT and XBT share Bitcoin addresses, and OFAC files a
    Tron shaped address under XBT. The match carries the code as published. near matches only a
    listing under the code NEAR, which the list does not have, so no NEAR account matches, a 64 hex
    implicit account or an account written like an EVM address included."""
    if document is None:
        document = load()
    if chain_or_asset == "near":
        asset = NEAR_ASSET
    elif chain_or_asset.isupper():
        asset = chain_or_asset
    else:
        asset = None
    key = normalise(address)
    return [{"uid": listing["uid"], "name": listing["name"], "programs": listing["programs"],
             "asset": listing["asset"], "address": listing["address"], "publish_date": document["publish_date"],
             "source_sha256": document["source"]["content_sha256"]}
            for listing in document["listings"] if listing["key"] == key and asset in (None, listing["asset"])]


def counts(document):
    """Listings, distinct (asset, key) pairs, distinct keys, entries, the distinct keys per asset code
    and the distinct keys under the code NEAR."""
    listings = document["listings"]
    per_asset = {}
    for listing in listings:
        per_asset.setdefault(listing["asset"], set()).add(listing["key"])
    return {"listings": len(listings), "pairs": sum(len(keys) for keys in per_asset.values()),
            "addresses": len({listing["key"] for listing in listings}),
            "entries": len({listing["uid"] for listing in listings}),
            "assets": {asset: len(keys) for asset, keys in sorted(per_asset.items())},
            "near": len(per_asset.get(NEAR_ASSET, ()))}


def counts_line(counts):
    assets = ", ".join(f"{asset} {count}" for asset, count in counts["assets"].items())
    return (f"{counts['listings']} listings, {counts['pairs']} distinct (asset, address) pairs, "
            f"{counts['addresses']} distinct addresses, {counts['entries']} entries, {len(counts['assets'])} asset "
            f"codes, NEAR {counts['near']}; distinct addresses per asset: {assets}")


def main():
    """python -m intel sanctions-fetch: fetch, extract, write the fixture and print what it holds."""
    try:
        data, fetched_at = fetch()
        document = extract(data, fetched_at)
    except (OSError, SanctionsError) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1
    path = write(document)
    print(f"wrote {path}")
    print(f"source {SDN_URL}, sha256 {document['source']['content_sha256']}, fetched {fetched_at}, date of issue "
          f"{document['publish_date']}")
    print(counts_line(counts(document)))
    return 0
