"""The registry's record_batch arguments, built from verdict files and their evidence documents.

A verdict file is what python -m gate verdict writes: the Verdict object on ALLOW, or
{literal, evidence_hash} for an unsigned DENY or UNKNOWN. The record's subject, kind and block
come from the evidence document the file names, which must hash to its name. The shape is the
registry interface: {"records": [{subject, verdict: Allow | Deny | Unknown, evidence_hash,
observed_block, verdict_id}]}, at most 29 records per call. A signed ALLOW uses its real verdict
id; an unsigned DENY or UNKNOWN uses the first 32 hex of the evidence hash. An ALLOW passed to a
human is not a decision and is refused. An intents verdict judges an EVM recipient and is a
preflight; the registry records NEAR accounts only, so it is refused.
"""

import json
import re
from pathlib import Path

from gate import evidence

CODES = {"ALLOW": "Allow", "DENY": "Deny", "UNKNOWN": "Unknown"}
MAX_BATCH = 29
_HEX64 = re.compile(r"[0-9a-f]{64}")


class RegistryError(ValueError):
    """A verdict file that cannot become a registry record."""


def record_input(verdict_path, evidence_dir, observed_block=None):
    """(the RecordInput for one verdict file, a note for the output or None)."""
    try:
        verdict = json.loads(Path(verdict_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RegistryError(f"{verdict_path}: cannot read a verdict file: {error}") from error
    if not isinstance(verdict, dict) or verdict.get("literal") not in CODES:
        raise RegistryError(f"{verdict_path}: the literal is not one of {sorted(CODES)}")
    evidence_hash = verdict.get("evidence_hash")
    if not isinstance(evidence_hash, str) or not _HEX64.fullmatch(evidence_hash):
        raise RegistryError(f"{verdict_path}: evidence_hash is not 64 lowercase hex")
    path = Path(evidence_dir) / f"{evidence_hash}.json"
    if not path.exists():
        raise RegistryError(f"{verdict_path}: no evidence document {path.as_posix()}")
    if not evidence.check_file(path):
        raise RegistryError(f"{path.as_posix()}: the file does not hash to its name in canonical form")
    document = json.loads(path.read_bytes())
    if document["verdict"] != verdict["literal"]:
        raise RegistryError(f"{verdict_path}: the literal {verdict['literal']} differs from the evidence "
                            f"verdict {document['verdict']}")
    kind = document["subject"].get("kind")
    if kind == "intents":
        raise RegistryError(f"{verdict_path}: an intents verdict is a preflight on an EVM recipient; the registry "
                            "records NEAR accounts only")
    if kind not in ("near", "ft"):
        raise RegistryError(f"{verdict_path}: the evidence subject kind {kind!r} is not near or ft")
    block = document["observed"].get("block_height")
    if observed_block is not None:
        if block is not None and block != observed_block:
            raise RegistryError(f"{verdict_path}: --observed-block {observed_block} differs from the evidence "
                                f"block height {block}")
        block = observed_block
    if block is None:
        raise RegistryError(f"{verdict_path}: the evidence has no observed block height; pass --observed-block")
    if isinstance(block, bool) or not isinstance(block, int) or block < 0:
        raise RegistryError(f"{verdict_path}: the observed block {block!r} is not a u64")
    note = None
    if verdict["literal"] == "ALLOW":
        verdict_id = verdict.get("verdict_id")
        if verdict_id is None:
            raise RegistryError("an ALLOW passed to a human is not recorded; record the human's decision instead")
        if verdict_id != document["verdict_id"]:
            raise RegistryError(f"{verdict_path}: verdict_id differs from the evidence verdict_id")
    else:
        verdict_id = evidence_hash[:32]
        note = (f"{document['subject']['receiver']}: unsigned {verdict['literal']}, so verdict_id is the first 32 "
                f"hex of the evidence hash: {verdict_id}")
    record = {
        "subject": document["subject"]["receiver"],
        "verdict": CODES[verdict["literal"]],
        "evidence_hash": evidence_hash,
        "observed_block": block,
        "verdict_id": verdict_id,
    }
    return record, note


def record_batch(verdict_paths, evidence_dir, observed_block=None):
    """({"records": [...]}, [notes]) for record_batch; refuses more than MAX_BATCH files."""
    if len(verdict_paths) > MAX_BATCH:
        raise RegistryError(f"{len(verdict_paths)} verdict files; record_batch takes at most {MAX_BATCH}")
    records, notes = [], []
    for path in verdict_paths:
        record, note = record_input(path, evidence_dir, observed_block)
        records.append(record)
        if note:
            notes.append(note)
    return {"records": records}, notes
