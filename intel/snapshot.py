"""A label snapshot: the current rows of the label store that a scope selects, for a consumer that reads them
offline.

docs/spec/snapshot-v1.md is the specification. A scope is a name and a list of selectors. A row is in the
snapshot when it is current (superseded_by null) and at least one selector matches it; a selector matches a
row when, for each key it gives, the row's chain, category or subject is one of the listed values. The
document holds the normalized scope, the sha256 of the store file it was built from, and the rows exactly as
the store holds them, sorted by chain, subject and id. Its content_sha256 is the sha256 hex of the canonical
bytes of the document without that key, and a snapshot file holds exactly the canonical bytes of the whole
document, so the content hash and every row id can be recomputed by the reader.
"""

import dataclasses
import hashlib
import json
import re
from pathlib import Path

from intel import labels

FORMAT = "label-snapshot"
VERSION = "1"
KEYS = ("content_sha256", "format", "rows", "scope", "store_sha256", "version")
SCOPE_KEYS = ("name", "select")
# Each selector key with the row field it matches.
SELECTOR_FIELDS = {"categories": "category", "chains": "chain", "subjects": "subject"}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CHAIN = re.compile(r"[a-z0-9]+")


class SnapshotError(ValueError):
    """A scope or a snapshot that breaks the snapshot rules."""


def canonical_bytes(value):
    """Compact JSON with sorted keys, non ASCII unescaped, UTF-8, no trailing newline; NaN, infinity and text
    that UTF-8 cannot encode are refused."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except ValueError as error:
        raise SnapshotError(f"no canonical form: {error}") from error


def _values(selector, key):
    values = selector[key]
    if not isinstance(values, list) or not values:
        raise SnapshotError(f"select.{key}: a non empty list is required")
    for value in values:
        if not isinstance(value, str) or not value:
            raise SnapshotError(f"select.{key}: every entry is a non empty string")
        if key == "chains" and not _CHAIN.fullmatch(value):
            raise SnapshotError(f"select.chains: {value!r} is not near or a lowercase chain code")
        if key == "categories" and value not in labels.CATEGORIES:
            raise SnapshotError(f"select.categories: {value!r} is not one of {labels.CATEGORIES}")
    if len(set(values)) != len(values):
        raise SnapshotError(f"select.{key}: an entry is listed twice")
    return sorted(values)


def scope(value):
    """The normalized scope: each list sorted and the selectors sorted by their canonical bytes; raise
    SnapshotError when a rule is broken."""
    if not isinstance(value, dict) or sorted(value) != list(SCOPE_KEYS):
        raise SnapshotError(f"a scope has exactly the keys {SCOPE_KEYS}")
    if not isinstance(value["name"], str) or not value["name"]:
        raise SnapshotError("name: a non empty string is required")
    if not isinstance(value["select"], list) or not value["select"]:
        raise SnapshotError("select: a non empty list of selectors is required")
    selectors = []
    for selector in value["select"]:
        if not isinstance(selector, dict) or not selector or not set(selector) <= set(SELECTOR_FIELDS):
            raise SnapshotError(f"select: each selector has one or more of the keys {sorted(SELECTOR_FIELDS)}")
        selectors.append({key: _values(selector, key) for key in sorted(selector)})
    selectors.sort(key=canonical_bytes)
    if any(first == second for first, second in zip(selectors, selectors[1:])):
        raise SnapshotError("select: a selector is listed twice")
    return {"name": value["name"], "select": selectors}


def read_scope(path):
    """The normalized scope of a scope file: one JSON object in UTF-8."""
    return scope(json.loads(Path(path).read_text(encoding="utf-8")))


def in_scope(assertion, normalized):
    """True when the assertion is current and a selector of the normalized scope matches it."""
    return assertion.superseded_by is None and any(
        all(getattr(assertion, SELECTOR_FIELDS[key]) in values for key, values in selector.items())
        for selector in normalized["select"])


def _order(assertion):
    return (assertion.chain, assertion.subject, assertion.id)


def content_sha256(document):
    """The sha256 hex of the canonical bytes of the document without content_sha256."""
    body = {key: value for key, value in document.items() if key != "content_sha256"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def build(store, scope_value):
    """The version 1 snapshot document of the store's current rows the scope selects."""
    normalized = scope(scope_value)
    chosen = sorted((assertion for assertion in store.assertions if in_scope(assertion, normalized)), key=_order)
    document = {"format": FORMAT, "version": VERSION, "scope": normalized, "store_sha256": store.sha256,
                "rows": [dataclasses.asdict(assertion) for assertion in chosen]}
    document["content_sha256"] = content_sha256(document)
    return document


def write(document, path):
    """Write the canonical bytes of the document to path and return them."""
    data = canonical_bytes(document)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def check(data):
    """The snapshot document the bytes hold; raise SnapshotError unless they are the canonical bytes of a
    version 1 snapshot with a normalized scope, whose rows are valid label assertions, current, selected by
    the scope and in order with no id twice, and whose content_sha256 recomputes."""
    try:
        document = json.loads(data.decode("utf-8"))
    except ValueError as error:
        raise SnapshotError(f"not a UTF-8 JSON document: {error}") from error
    if not isinstance(document, dict) or sorted(document) != list(KEYS):
        raise SnapshotError(f"a snapshot has exactly the keys {KEYS}")
    if (document["format"], document["version"]) != (FORMAT, VERSION):
        raise SnapshotError(f"format {FORMAT!r} and version {VERSION!r} are required")
    if scope(document["scope"]) != document["scope"]:
        raise SnapshotError("scope: not in normalized form")
    if not isinstance(document["store_sha256"], str) or not _HEX64.fullmatch(document["store_sha256"]):
        raise SnapshotError("store_sha256: 64 lowercase hex characters are required")
    if not isinstance(document["rows"], list):
        raise SnapshotError("rows: a list is required")
    previous = None
    for number, row in enumerate(document["rows"]):
        try:
            assertion = labels.validate(row)
        except labels.LabelError as error:
            raise SnapshotError(f"rows[{number}]: {error}") from error
        if not in_scope(assertion, document["scope"]):
            raise SnapshotError(f"rows[{number}]: {assertion.id} is superseded or outside the scope")
        if previous is not None and _order(assertion) <= previous:
            raise SnapshotError(f"rows[{number}]: rows are sorted by chain, subject and id, each id once")
        previous = _order(assertion)
    if canonical_bytes(document) != data:
        raise SnapshotError("the bytes are not the canonical form of the document")
    if document["content_sha256"] != content_sha256(document):
        raise SnapshotError("content_sha256: not the sha256 of the canonical bytes without it")
    return document
