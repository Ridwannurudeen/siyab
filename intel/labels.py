"""The label store: one assertion per line of intel/labels.jsonl, and every assertion has a source.

docs/LABEL-RECORD.md is the specification. An assertion's id is the sha256 hex of its canonical
row without id and superseded_by, so the only field that may change after a row is written is
superseded_by, which names the id of the row that replaces it.
"""

import dataclasses
import hashlib
import json
import re
from pathlib import Path

STORE_PATH = Path(__file__).resolve().parent / "labels.jsonl"
FIELDS = ("id", "entity", "subject", "chain", "label", "category", "provenance", "source",
          "observed_at", "confidence", "rule_id", "superseded_by", "note")
CATEGORIES = ("treasury", "bridge", "catalog_token", "infrastructure", "incident", "denylist", "watch")
PROVENANCES = ("onchain", "official_source", "third_party", "inferred", "unknown")
SOURCE_KEYS = {
    "document": ("content_sha256", "fetched_at", "url"),
    "onchain": ("args", "block", "method", "rpc"),
    "inferred": ("inputs", "rule_id"),
}
_SOURCE_SHAPE = {"official_source": "document", "third_party": "document", "onchain": "onchain",
                 "inferred": "inferred"}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CHAIN = re.compile(r"[a-z0-9]+")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)


class LabelError(ValueError):
    """A row that breaks the label record rules."""


@dataclasses.dataclass(frozen=True)
class Assertion:
    id: str
    entity: str
    subject: str
    chain: str
    label: str
    category: str
    provenance: str
    source: dict
    observed_at: str
    confidence: float
    rule_id: str | None
    superseded_by: str | None
    note: str


def assertion_id(row):
    """sha256 hex of the row without id and superseded_by, as sorted compact UTF-8 JSON."""
    body = {key: value for key, value in row.items() if key not in ("id", "superseded_by")}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(row, key):
    if not isinstance(row.get(key), str) or not row[key]:
        raise LabelError(f"{key}: a non empty string is required")


def _source_shape(source):
    for shape, keys in SOURCE_KEYS.items():
        if isinstance(source, dict) and tuple(sorted(source)) == keys:
            return shape
    raise LabelError(f"source: not one of the source shapes {sorted(SOURCE_KEYS)}: {source!r}")


def _check_source(source):
    shape = _source_shape(source)
    if shape == "document":
        if not source["url"].startswith("https://"):
            raise LabelError("source.url: an https URL is required")
        if not _HEX64.fullmatch(str(source["content_sha256"])):
            raise LabelError("source.content_sha256: 64 lowercase hex characters are required")
        if not _TIME.fullmatch(str(source["fetched_at"])):
            raise LabelError("source.fetched_at: a UTC time YYYY-MM-DDTHH:MM:SSZ is required")
    elif shape == "onchain":
        if not isinstance(source["block"], int) or isinstance(source["block"], bool) or source["block"] < 1:
            raise LabelError("source.block: the block height the read was made at is required")
        if not str(source["rpc"]).startswith("https://") or not source["method"]:
            raise LabelError("source: an https rpc URL and a method are required")
        if not isinstance(source["args"], dict):
            raise LabelError("source.args: an object is required")
    else:
        if not source["rule_id"] or not isinstance(source["inputs"], list) or not source["inputs"]:
            raise LabelError("source: a rule_id and a non empty list of input assertion ids are required")
    return shape


def validate(row):
    """Return the row as an Assertion; raise LabelError when any rule is broken."""
    if not isinstance(row, dict) or tuple(sorted(row)) != tuple(sorted(FIELDS)):
        raise LabelError(f"a row has exactly the fields {FIELDS}")
    for key in ("entity", "subject", "label", "note"):
        _text(row, key)
    if not isinstance(row["chain"], str) or not _CHAIN.fullmatch(row["chain"]):
        raise LabelError(f"chain: near or a lowercase chain code is required, not {row['chain']!r}")
    if row["category"] not in CATEGORIES:
        raise LabelError(f"category: {row['category']!r} is not one of {CATEGORIES}")
    if row["provenance"] not in PROVENANCES:
        raise LabelError(f"provenance: {row['provenance']!r} is not one of {PROVENANCES}")
    if row["source"] is None:
        raise LabelError("source: every row names its source")
    shape = _check_source(row["source"])
    expected = _SOURCE_SHAPE.get(row["provenance"])
    if expected and shape != expected:
        raise LabelError(f"source: provenance {row['provenance']} needs the {expected} source shape")
    if row["provenance"] == "inferred":
        if row["rule_id"] != row["source"]["rule_id"]:
            raise LabelError("rule_id: an inferred row repeats its source rule_id")
    elif row["rule_id"] is not None:
        raise LabelError("rule_id: only an inferred row has a rule_id")
    if not isinstance(row["observed_at"], str) or not _TIME.fullmatch(row["observed_at"]):
        raise LabelError("observed_at: a UTC time YYYY-MM-DDTHH:MM:SSZ is required")
    confidence = row["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise LabelError("confidence: a number from 0 to 1 is required")
    if row["superseded_by"] is not None and not _HEX64.fullmatch(str(row["superseded_by"])):
        raise LabelError("superseded_by: null or an assertion id is required")
    if row["id"] != assertion_id(row):
        raise LabelError(f"id: {row['id']!r} is not the sha256 of the row")
    return Assertion(**row)


def row_line(row):
    """The labels.jsonl line for a row: compact JSON with the fields in FIELDS order."""
    return json.dumps({key: row[key] for key in FIELDS}, separators=(",", ":"), ensure_ascii=False)


class Store:
    """The assertions of one labels.jsonl file, with the file's sha256."""

    def __init__(self, assertions, sha256):
        self.assertions = assertions
        self.sha256 = sha256
        self._by_subject = {}
        for assertion in assertions:
            self._by_subject.setdefault((assertion.subject, assertion.chain), []).append(assertion)

    def by_subject(self, subject, chain):
        """Every assertion on the subject on that chain, superseded ones included."""
        return list(self._by_subject.get((subject, chain), []))

    def counts(self):
        """Assertions, distinct (chain, subject) pairs, and the count per provenance and category."""
        return {
            "assertions": len(self.assertions),
            "subjects": len(self._by_subject),
            "provenance": {p: sum(a.provenance == p for a in self.assertions) for p in PROVENANCES},
            "category": {c: sum(a.category == c for a in self.assertions) for c in CATEGORIES},
        }


def load(path=STORE_PATH):
    """Read and validate a labels.jsonl file; a duplicate id or a dangling superseded_by raises."""
    data = Path(path).read_bytes()
    assertions, seen = [], set()
    for number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            assertion = validate(json.loads(line))
        except (LabelError, ValueError) as error:
            raise LabelError(f"{path}:{number}: {error}") from error
        if assertion.id in seen:
            raise LabelError(f"{path}:{number}: duplicate assertion id {assertion.id}")
        seen.add(assertion.id)
        assertions.append(assertion)
    for assertion in assertions:
        if assertion.superseded_by is not None and assertion.superseded_by not in seen:
            raise LabelError(f"{assertion.id}: superseded_by names no assertion in the store")
    return Store(assertions, hashlib.sha256(data).hexdigest())


def counts_line(counts):
    p = counts["provenance"]
    return (f"{counts['assertions']} assertions over {counts['subjects']} subjects: onchain {p['onchain']}, "
            f"official_source {p['official_source']}, third_party {p['third_party']}, "
            f"inferred {p['inferred']}, unknown {p['unknown']}")
