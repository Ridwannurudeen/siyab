"""Replay the guard on recorded chain state, with no labels."""

import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from gate import incident, rpc, rules
from gate.asof import AsOf
from intel import labels
from intel.live import LiveAPI

REPLAY_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "replay"
MAX_SEARCH_STEPS = 64
MAX_SKIPPED_HEIGHTS = 64


class ReplayMismatch(Exception):
    """A fixture differs from the recorded run; rules must not absorb this failure."""


class RecoveryRequired(Exception):
    """Publication needs the retained backups to restore a replayable run."""


class ProviderExcluded(ValueError):
    """The run excludes this provider, so its data is unavailable."""


def build_manifest(fixtures_dir, files):
    manifest = {}
    directory = Path(fixtures_dir).resolve()
    for name in sorted(files):
        if not isinstance(name, str) or re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
            raise ReplayMismatch("replay mismatch: invalid fixture name")
        path = directory / name
        if path.resolve().parent != directory:
            raise ReplayMismatch("replay mismatch: fixture outside the fixture directory")
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


class _FixtureClient(rpc.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixture_files = {}
        self.fixture_data = {}
        self.manifest = None

    def call(self, method, params):
        if self.manifest is not None:
            name = f"{rpc.fixture_key(self.url, method, params)}.json"
            if name not in self.manifest:
                raise ReplayMismatch("replay mismatch: unlisted fixture read")
        try:
            return super().call(method, params)
        except (rpc.FixtureMiss, rpc.HttpError, rpc.RateLimited, rpc.TransportError, OSError) as error:
            if self.mode == "record" or self.manifest is not None:
                raise ReplayMismatch(f"replay mismatch: fixture read failed: {error}") from error
            raise

    def _fixture_bytes(self, path, data=None):
        name = path.name
        if path.resolve().parent != self.fixtures_dir.resolve():
            raise ReplayMismatch("replay mismatch: fixture outside the fixture directory")
        if self.manifest is not None and name not in self.manifest:
            raise ReplayMismatch("replay mismatch: unlisted fixture read")
        if data is None:
            data = self.fixture_data[name] if name in self.fixture_data else path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if self.manifest is not None and digest != self.manifest[name]:
            raise ReplayMismatch("replay mismatch: fixture content changed")
        self.fixture_data[name] = data
        self.fixture_files[name] = digest
        return data


class _NoNearBlocks:
    def __init__(self):
        self.last_source = {"provider": "nearblocks", "request_sha256": None, "fetched_at": None}

    def get(self, url):
        raise ProviderExcluded("NearBlocks is excluded from incident replay")


def _time(value):
    if not isinstance(value, str):
        raise ValueError("as-of time must be text")
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None or when.utcoffset().total_seconds() != 0:
        raise ValueError("as-of time must include UTC (Z or +00:00)")
    return when


def _header(client, height):
    header = client.block_at(height)["header"]
    if header["height"] != height:
        raise rpc.MalformedResponse(f"block {height} came back as block {header['height']}")
    return header


def _resolve(client, target_ns):
    head = client.final_block()["header"]
    if head["height"] >= 2**64:
        raise ValueError("final block height exceeds the search bound")
    low, high, found = 0, head["height"], None
    for _ in range(MAX_SEARCH_STEPS):
        if low > high:
            break
        middle = (low + high) // 2
        height, header = middle, None
        for _ in range(MAX_SKIPPED_HEIGHTS):
            try:
                header = _header(client, height)
                break
            except rpc.RpcError as error:
                if error.cause != "UNKNOWN_BLOCK":
                    raise
                height -= 1
                if height < low:
                    break
        if header is None:
            if height >= low:
                raise ValueError("block search exceeded the skipped-height bound")
            low = middle + 1
        elif header["timestamp"] <= target_ns:
            found = header
            low = middle + 1
        else:
            high = height - 1
    else:
        if low <= high:
            raise ValueError("block search exceeded its bound")
    if found is None:
        raise ValueError("no final block at or before the as-of time")
    return found


def run(incident_id, *, at=None, record=False):
    document = json.loads(incident.path_for(incident_id).read_text(encoding="utf-8"))
    when = _time(at if at is not None else document["date"] + "T00:00:00Z")
    as_of = when.isoformat().replace("+00:00", "Z")
    delta = when - datetime(1970, 1, 1, tzinfo=UTC)
    target_ns = (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000
    path = REPLAY_DIR / incident_id / "run.json"
    recorded = None
    if not record:
        recorded = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(recorded, dict) or recorded["incident_id"] != incident_id or recorded["as_of"] != as_of
                or recorded["label_store"] != {"labels": [], "incidents": []}
                or recorded.get("fixture_directory", "rpc") != "rpc"):
            raise ValueError("replay mismatch")
        for field in ("height", "block_timestamp"):
            if type(recorded[field]) is not int or recorded[field] < 0:
                raise ValueError("replay mismatch: height and timestamp must be non negative integers")
    fixture_options = {}
    if record or recorded.get("fixture_directory") == "rpc":
        fixture_options["fixtures_dir"] = path.parent / "rpc"
    client = _FixtureClient(os.environ.get("NEAR_ARCHIVAL_RPC_URL") or rpc.ARCHIVAL_RPC_URL,
                            "record" if record else "replay", **fixture_options)
    client.fixture_files = {}
    client.fixture_data = {}
    client.manifest = None
    if not record:
        manifest = recorded["fixtures"]
        if (not isinstance(manifest, dict) or any(not isinstance(name, str)
                or re.fullmatch(r"[0-9a-f]{64}\.json", name) is None or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None for name, digest in manifest.items())):
            raise ReplayMismatch("replay mismatch: fixture manifest differs")
        client.manifest = manifest
    if not record:
        return _evaluate(incident_id, document, as_of, target_ns, client, recorded)
    fixtures_dir = client.fixtures_dir
    fixtures_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".incident-record-", dir=fixtures_dir.parent))
    try:
        for source in fixtures_dir.glob("*.json"):
            if re.fullmatch(r"[0-9a-f]{64}\.json", source.name):
                shutil.copyfile(source, staging / source.name)
        client.fixtures_dir = staging
        result = _evaluate(incident_id, document, as_of, target_ns, client, None)
        candidate = staging / "run.json"
        candidate_data = (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        candidate.write_bytes(candidate_data)
        client.mode = "replay"
        client.manifest = result["fixtures"]
        client.fixture_files = {}
        client.fixture_data = {}
        _evaluate(incident_id, document, as_of, target_ns, client,
                  json.loads(candidate.read_text(encoding="utf-8")))
        client.fixtures_dir = fixtures_dir
        _publish(staging, fixtures_dir, path, result["fixtures"], candidate_data,
                 lambda: run(incident_id, at=json.loads(path.read_text(encoding="utf-8"))["as_of"]))
        return result
    finally:
        client.fixtures_dir = fixtures_dir
        try:
            shutil.rmtree(staging)
        except OSError as error:
            print(f"incident replay: warning: cleanup left {staging}: {error}", file=sys.stderr)


def _prepare_bytes(path, data):
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _publish(staging, fixtures_dir, path, manifest, candidate_data, verify):
    contents = {fixtures_dir / name: (staging / name).read_bytes() for name in manifest}
    for destination, data in contents.items():
        if hashlib.sha256(data).hexdigest() != manifest[destination.name]:
            raise ReplayMismatch("replay mismatch: fixture content changed")
    data = (staging / "run.json").read_bytes()
    if data != candidate_data:
        raise ReplayMismatch("replay mismatch: candidate run changed")
    contents[path] = data
    lock = fixtures_dir.parent / ".incident-record-publish.lock"
    lock.mkdir()
    replaced = []
    prepared = {}
    previous = {}
    restores = {}
    temporary_files = []
    verified = False
    try:
        fixtures_dir.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        for destination, data in contents.items():
            prepared[destination] = _prepare_bytes(destination, data)
            temporary_files.append(prepared[destination])
            previous[destination] = None
            if destination.exists():
                previous[destination] = _prepare_bytes(destination, destination.read_bytes())
                temporary_files.append(previous[destination])
                restores[destination] = previous[destination].with_name(previous[destination].name + ".restore")
                os.link(previous[destination], restores[destination])
                temporary_files.append(restores[destination])
        try:
            for destination, temporary in prepared.items():
                os.replace(temporary, destination)
                replaced.append(destination)
            verify()
            verified = True
        except BaseException as publication_error:
            try:
                for destination in reversed(replaced):
                    if previous[destination] is None:
                        os.replace(destination, prepared[destination])
                    else:
                        os.replace(restores[destination], destination)
                verify()
                verified = True
            except BaseException as recovery_error:
                commands = []
                for destination, backup in previous.items():
                    if backup is not None:
                        code = f"import os; os.replace({str(backup)!r}, {str(destination)!r})"
                    elif destination in replaced and destination.exists():
                        code = f"import os; os.replace({str(destination)!r}, {str(prepared[destination])!r})"
                    else:
                        continue
                    quoted = "'" + code.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(code)
                    commands.append("Restore: python -c " + quoted)
                raise RecoveryRequired(
                    f"publication failed: {publication_error}; recovery failed: {recovery_error}; "
                    "backups retained. Restore the previous run, then replay before deleting backups:\n"
                    + "\n".join(commands)) from recovery_error
            raise
    finally:
        backups = {backup for backup in previous.values() if backup is not None}
        for temporary in temporary_files:
            if temporary in backups and not verified:
                continue
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                print(f"incident replay: warning: cleanup left {temporary}: {error}", file=sys.stderr)
        try:
            lock.rmdir()
        except OSError as error:
            print(f"incident replay: warning: cleanup left {lock}: {error}", file=sys.stderr)


def _evaluate(incident_id, document, as_of, target_ns, client, recorded):
    header = _resolve(client, target_ns)
    if recorded is not None:
        if header["height"] != recorded["height"] or header["timestamp"] != recorded["block_timestamp"]:
            raise ValueError("replay mismatch")
    pinned = AsOf(client, header["height"])
    store = labels.Store([], hashlib.sha256(b"").hexdigest())
    rows = []
    for account in document["accounts"]:
        role = re.search(r"\b(master|collector|worker)\b", account["role_as_reported"])
        if account["chain"] != "near" or role is None:
            continue
        request = {"kind": "near", "token_id": None, "receiver_id": account["subject"],
                   "amount": "1000000000000000000000000", "memo": None}
        judgement = rules.judge(request, {"allowlist": [], "caps": []}, store, pinned, _NoNearBlocks(),
                                now_ms=header["timestamp"] // 10**6, injection_scanner=None,
                                archival=pinned, issuers=None, reports_account=None)
        outcome = LiveAPI._verdict_shape(judgement.verdict, judgement.checks, judgement.reasons,
                                         None, {}, 0)["outcome"]
        rows.append({"account_id": account["subject"], "role": role.group(), "verdict": judgement.verdict,
                     "outcome": outcome, "checks": [{key: check[key] for key in ("id", "status", "detail")}
                                                    for check in judgement.checks]})
    result = {"incident_id": incident_id, "as_of": as_of, "height": header["height"],
              "block_timestamp": header["timestamp"], "label_store": {"labels": [], "incidents": []},
              "accounts": rows,
              "fixtures": dict(sorted(client.fixture_files.items()))}
    if recorded is None or recorded.get("fixture_directory") == "rpc":
        result["fixture_directory"] = "rpc"
    if recorded is not None and result != recorded:
        raise ValueError("replay mismatch")
    return result


def main(incident_id, *, at=None, record=False):
    try:
        result = run(incident_id, at=at, record=record)
    except RecoveryRequired as error:
        print(f"incident replay: {error}", file=sys.stderr)
        return 2
    except (ReplayMismatch, OSError, ValueError, KeyError, TypeError, RecursionError, *rules.UNAVAILABLE) as error:
        detail = str(error) if record else f"replay mismatch: {error}"
        print(f"incident replay: {detail}", file=sys.stderr)
        return 1
    print("Role       Account                   Outcome  Checks")
    for row in result["accounts"]:
        account = row["account_id"]
        if len(account) > 25:
            account = account[:12] + "\u2026" + account[-10:]
        checks = "; ".join(f"{check['id']} {check['status']}: {check['detail']}" for check in row["checks"])
        print(f"{row['role']:<10} {account:<25} {row['outcome']:<8} {checks}")
    print(f"As of {result['as_of'].removesuffix('Z')} UTC (block {result['height']}), "
          "with no labels: on-chain state only.")
    return 0
