import contextlib
import errno
import io
import json
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import incident, incident_replay, rpc, rules
from gate.asof import AsOf

START_NS = int(datetime(2026, 4, 1, tzinfo=UTC).timestamp()) * 10**9
HEIGHT = 15 * 86400
YOUNG_HEIGHT = HEIGHT - 86400
BLOCK_HASH = rpc.base58.encode(bytes([1]) * 32)
OTHER_BLOCK_HASH = rpc.base58.encode(bytes([2]) * 32)
PUBLIC_KEY = "ed25519:" + rpc.base58.encode(bytes([3]) * 32)


class Chain(incident_replay._FixtureClient):
    def __init__(self):
        super().__init__("https://archival.example", "replay")
        self.calls = []
        self.missing = set()

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        self.last_source = {"provider": "archival.example", "request_sha256": None, "fetched_at": None}
        height = params.get("block_id", HEIGHT + 100)
        self.last_block_height = height
        if method == "block":
            if height in self.missing:
                raise rpc.RpcError({"name": "HANDLER_ERROR", "code": -32000, "message": "Server error",
                                    "cause": {"name": "UNKNOWN_BLOCK"}})
            return {"header": {"height": height, "timestamp": START_NS + height * 10**9, "hash": BLOCK_HASH}}
        kind = params["request_type"]
        account = params["account_id"]
        if kind == "view_account":
            if account == "missing.near" or account == "young.near" and height < YOUNG_HEIGHT:
                raise rpc.RpcError({"name": "HANDLER_ERROR", "code": -32000, "message": "Server error",
                                    "cause": {"name": "UNKNOWN_ACCOUNT", "info": {
                    "block_height": height, "requested_account_id": account, "block_hash": BLOCK_HASH}}})
            return {"code_hash": rpc.EMPTY_CODE_HASH, "block_height": height, "block_hash": BLOCK_HASH,
                    "amount": "1", "locked": "0", "storage_usage": 1}
        if kind == "view_access_key_list":
            nonce = (YOUNG_HEIGHT - 1) * rules.NONCE_PER_HEIGHT if account == "young.near" else 1
            return {"keys": [{"public_key": PUBLIC_KEY, "access_key": {
                "nonce": nonce, "permission": "FullAccess"}}], "block_height": height, "block_hash": BLOCK_HASH}
        if kind == "call_function":
            metadata = {"version": "1", "link": None, "standards": []}
            return {"result": list(json.dumps(metadata).encode("utf-8")), "block_height": height,
                    "block_hash": BLOCK_HASH, "logs": []}
        raise AssertionError((method, params))


class ReplayCase(unittest.TestCase):
    def setUp(self):
        for target in ("urllib.request.urlopen", "gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            patcher = mock.patch(target, side_effect=AssertionError("network"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.chain = Chain()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.chain.fixtures_dir = self.directory
        self.document = {"id": "example", "date": "2026-04-16", "accounts": [
            {"subject": "young.near", "chain": "near", "role_as_reported": "the collector account"},
            {"subject": "old.near", "chain": "near", "role_as_reported": "the master account"},
            {"subject": "missing.near", "chain": "near", "role_as_reported": "one of the five worker accounts"},
            {"subject": "token.near", "chain": "near", "role_as_reported": "token contract"},
            {"subject": "route.near", "chain": "near", "role_as_reported": "protocol route account"},
            {"subject": "elsewhere", "chain": "eth", "role_as_reported": "the master account"}]}
        self.incident_path = self.directory / "incident.json"
        self.incident_path.write_text(json.dumps(self.document), encoding="utf-8")
        patcher = mock.patch.object(incident, "path_for", return_value=self.incident_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(incident_replay, "REPLAY_DIR", self.directory / "replay")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(incident_replay, "_FixtureClient", return_value=self.chain)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self):
        return incident_replay.run("example", record=True)

    def test_recording_another_incident_keeps_the_first_replayable(self):
        day = [16]
        head_offset = [0]
        client_class = Chain.__bases__[0]
        def client(url, mode, **kwargs):
            kwargs.setdefault("fixtures_dir", self.directory / "shared")
            return client_class(url, mode, clock=lambda: datetime(2026, 4, day[0], tzinfo=UTC).timestamp(),
                                **kwargs)
        def fetch(url, body, headers, errors):
            try:
                result = self.chain.call(body["method"], body["params"])
                if body["method"] == "block" and body["params"].get("finality") == "final":
                    result["header"]["height"] += head_offset[0]
                    result["header"]["timestamp"] += head_offset[0] * 10**9
                return {"jsonrpc": "2.0", "id": "gate", "result": result}
            except rpc.RpcError as error:
                return {"jsonrpc": "2.0", "id": "gate", "error": error.error}
        with mock.patch.object(incident_replay, "_FixtureClient", side_effect=client):
            with mock.patch.object(rpc, "_fetch", side_effect=fetch):
                first = incident_replay.run("example", record=True)
                run_path = self.directory / "replay/example/run.json"
                previous = run_path.read_bytes()
                day[0] = 18
                head_offset[0] = 200
                incident_replay.run("another", record=True)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)
            self.assertEqual(incident_replay.run("example"), first)
        self.assertEqual(run_path.read_bytes(), previous)
        first_dir = run_path.parent / "rpc"
        second_dir = self.directory / "replay/another/rpc"
        self.assertTrue(first_dir.is_dir())
        self.assertTrue(second_dir.is_dir())
        self.assertEqual(incident_replay.build_manifest(first_dir, first["fixtures"]), first["fixtures"])

    def test_legacy_replay_ignores_empty_incident_fixture_directory(self):
        client, recorded = self.fixture_run()
        recorded.pop("fixture_directory", None)
        path = self.directory / "replay/example/run.json"
        path.write_text(json.dumps(recorded), encoding="utf-8")
        (path.parent / "rpc").mkdir()
        client_class = Chain.__bases__[0]
        def legacy_client(url, mode, **kwargs):
            kwargs.setdefault("fixtures_dir", self.directory)
            return client_class(client.url, mode, **kwargs)
        with mock.patch.object(incident_replay, "_FixtureClient", side_effect=legacy_client):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)

    def test_all_current_read_paths_are_pinned(self):
        client = AsOf(self.chain, HEIGHT)
        client.view_account("old.near")
        client.code_hash("old.near")
        client.view_access_key_list("old.near")
        client.call_function("old.near", "contract_source_metadata", {})
        client.final_block()
        for method, params in self.chain.calls:
            self.assertNotIn("finality", params)
            self.assertEqual(params["block_id"], HEIGHT)
        self.assertEqual(client.last_source, self.chain.last_source)
        self.assertEqual(client.last_block_height, HEIGHT)

    def test_earlier_explicit_reads_are_preserved(self):
        client = AsOf(self.chain, HEIGHT)
        client.view_account_at("old.near", 10)
        client.call_function("old.near", "method", {}, block_id=11)
        client.block_at(12)
        self.assertEqual([(method, params["block_id"]) for method, params in self.chain.calls],
                         [("query", 10), ("block", 10), ("query", 11), ("block", 11), ("block", 12)])

    def test_unknown_or_unpinnable_reads_raise_without_forwarding(self):
        client = AsOf(self.chain, HEIGHT)
        for method, params in (("status", {}), ("query", {"request_type": "view_state", "finality": "final"}),
                               ("block", {}), ("block", {"finality": "optimistic"}),
                               ("block", {"block_id": HEIGHT + 1}), ("block", {"block_id": True}),
                               ("query", {"request_type": "view_account", "finality": "final", "block_id": 2})):
            with self.subTest(method=method, params=params), self.assertRaises(ValueError):
                client.call(method, params)
        with self.assertRaises(ValueError):
            client.get("/present")
        self.assertEqual(self.chain.calls, [])

    def test_wrong_response_height_raises(self):
        with mock.patch.object(self.chain, "call", return_value={"code_hash": rpc.EMPTY_CODE_HASH,
                                                                "block_height": HEIGHT + 1}):
            with self.assertRaises(ValueError):
                AsOf(self.chain, HEIGHT).view_account("old.near")

    def test_absence_requires_matching_error_context(self):
        self.chain.view_account("old.near")
        valid = {"block_height": HEIGHT, "requested_account_id": "old.near", "block_hash": BLOCK_HASH}
        for info in ({**valid, "block_height": HEIGHT + 1},
                     {**valid, "requested_account_id": "other.near"},
                     {**valid, "block_hash": OTHER_BLOCK_HASH}, {}, None, []):
            with self.subTest(info=info):
                error = rpc.RpcError({"name": "HANDLER_ERROR", "code": -32000, "message": "Server error",
                                  "cause": {"name": "UNKNOWN_ACCOUNT", "info": info}})
                original = self.chain.call
                def call(method, params):
                    if method == "query":
                        raise error
                    return original(method, params)
                with mock.patch.object(self.chain, "call", side_effect=call):
                    with self.assertRaises(rpc.MalformedResponse):
                        AsOf(self.chain, HEIGHT).view_account("old.near")
                    judgement = rules.judge(
                        {"kind": "near", "token_id": None, "receiver_id": "old.near",
                         "amount": "1", "memo": None}, {"allowlist": [], "caps": []},
                        incident_replay.labels.Store([], ""), AsOf(self.chain, HEIGHT),
                        incident_replay._NoNearBlocks(), now_ms=START_NS // 10**6,
                        archival=AsOf(self.chain, HEIGHT), injection_scanner=None,
                        issuers=None, reports_account=None)
                receiver = next(check for check in judgement.checks if check["id"] == "receiver_exists")
                self.assertEqual(receiver["status"], "unknown")
                self.assertEqual(judgement.verdict, "UNKNOWN")

    def test_absence_context_requires_hash_and_preserves_valid_error(self):
        for include_hash in (False, True):
            info = {"block_height": HEIGHT, "requested_account_id": "old.near"}
            if include_hash:
                info["block_hash"] = BLOCK_HASH
            error = rpc.RpcError({"name": "HANDLER_ERROR", "code": -32000, "message": "Server error",
                                  "cause": {"name": "UNKNOWN_ACCOUNT", "info": info}})
            original = self.chain.call
            def call(method, params):
                if method == "query":
                    raise error
                return original(method, params)
            with self.subTest(include_hash=include_hash), mock.patch.object(self.chain, "call", side_effect=call):
                if include_hash:
                    with self.assertRaises(rpc.RpcError) as raised:
                        AsOf(self.chain, HEIGHT).view_account("old.near")
                    self.assertIs(raised.exception, error)
                else:
                    with self.assertRaises(rpc.MalformedResponse):
                        AsOf(self.chain, HEIGHT).view_account("old.near")

    def test_malformed_block_header_fails_closed(self):
        for header in (None, [], "block", 1):
            with self.subTest(header=header), mock.patch.object(self.chain, "call", return_value={"header": header}):
                with self.assertRaises(rpc.MalformedResponse):
                    AsOf(self.chain, HEIGHT).final_block()

    def test_excluded_provider_is_unavailable_and_never_passes(self):
        with self.assertRaises(ValueError) as raised:
            incident_replay._NoNearBlocks().get("/account/old.near/keys")
        self.assertEqual(type(raised.exception).__name__, "ProviderExcluded")
        run = self.record()
        young, _, missing = run["accounts"]
        age = next(check for check in young["checks"] if check["id"] == "account_age")
        self.assertEqual(age["status"], "signal")
        self.assertNotIn("NearBlocks", age["detail"])
        age = next(check for check in missing["checks"] if check["id"] == "account_age")
        self.assertEqual(age["status"], "unknown")
        self.assertIn("ProviderExcluded: NearBlocks is excluded", age["detail"])
        self.assertNotEqual(missing["verdict"], "ALLOW")

    def test_malformed_run_always_reports_replay_mismatch(self):
        recorded = self.record()
        path = self.directory / "replay/example/run.json"
        missing_id = dict(recorded)
        del missing_id["incident_id"]
        for document in (missing_id, None, [], {**recorded, "height": None},
                         {**recorded, "accounts": None}, {**recorded, "fixtures": None},
                         {**recorded, "fixtures": []}, {**recorded, "fixtures": {"../other.json": "a" * 64}}):
            with self.subTest(document=document):
                path.write_text(json.dumps(document), encoding="utf-8")
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    self.assertEqual(gate_main.main(["incident-replay", "example"]), 1)
                self.assertIn("replay mismatch", error.getvalue())

    def test_recorded_numeric_fields_require_non_negative_integers_before_reads(self):
        recorded = self.record()
        path = self.directory / "replay/example/run.json"
        for field in ("height", "block_timestamp"):
            for value in (-1, True, False, float(recorded[field]), str(recorded[field]), None, [], {}):
                with self.subTest(field=field, value=value):
                    path.write_text(json.dumps({**recorded, field: value}), encoding="utf-8")
                    self.chain.calls.clear()
                    error = io.StringIO()
                    with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(gate_main.main(["incident-replay", "example"]), 1)
                    self.assertIn("replay mismatch", error.getvalue())
                    self.assertEqual(self.chain.calls, [])

    def test_failed_stale_fixture_refresh_preserves_previous_run(self):
        client, _ = self.fixture_run()
        params = {"request_type": "view_account", "account_id": "young.near", "block_id": HEIGHT}
        path = self.directory / f"{rpc.fixture_key(client.url, 'query', params)}.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        fixture["fetched_at"] = "2026-04-01T00:00:00Z"
        path.write_text(json.dumps(fixture), encoding="utf-8")
        run_path = self.directory / "replay/example/run.json"
        previous = run_path.read_bytes()
        client.mode = "record"
        error = io.StringIO()
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with mock.patch.object(rpc, "_fetch", side_effect=rpc.TransportError("refresh failed")) as fetch:
                with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate_main.main(["incident-replay", "example", "--record"]), 1)
        fetch.assert_called_once()
        self.assertIn("refresh failed", error.getvalue())
        self.assertEqual(run_path.read_bytes(), previous)

    def test_partial_refresh_failure_preserves_every_published_byte(self):
        client, recorded = self.fixture_run()
        previous = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        client.mode = "record"
        client.clock = lambda: datetime(2026, 4, 18, tzinfo=UTC).timestamp()
        refreshed = []
        def fetch(url, body, headers, errors):
            if body["params"].get("account_id") == "young.near":
                raise rpc.TransportError("collector refresh failed")
            refreshed.append(body)
            return {"jsonrpc": "2.0", "id": "gate", "result": self.chain.call(body["method"], body["params"])}
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with mock.patch.object(rpc, "_fetch", side_effect=fetch):
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate_main.main(["incident-replay", "example", "--record"]), 1)
        self.assertGreater(len(refreshed), 1)
        self.assertEqual({path: path.read_bytes() for path in self.directory.rglob("*.json")}, previous)
        self.assertEqual(sorted(path.name for path in self.directory.parent.glob(".incident-record-*")), [])
        client.mode = "replay"
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            self.assertEqual(incident_replay.run("example"), recorded)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)

    def test_interleaved_recorders_use_isolated_staging_and_publish_replayable_run(self):
        client, _ = self.fixture_run()
        clients = [Chain.__bases__[0](client.url, "record", fixtures_dir=self.directory,
                   clock=lambda day=day: datetime(2026, 4, day, tzinfo=UTC).timestamp())
                   for day in (18, 19)]
        local = threading.local()
        started = threading.Barrier(2)
        second_done = threading.Event()
        stages = []
        replacements = []
        replace = incident_replay.os.replace
        def fetch(url, body, headers, errors):
            if not getattr(local, "started", False):
                local.started = True
                stages.append(local.client.fixtures_dir)
                started.wait(timeout=10)
            try:
                return {"jsonrpc": "2.0", "id": "gate", "result": self.chain.call(body["method"], body["params"])}
            except rpc.RpcError as error:
                return {"jsonrpc": "2.0", "id": "gate", "error": error.error}
        def checked_replace(source, destination):
            replacements.append((Path(source), Path(destination)))
            if local.index == 0 and not getattr(local, "publishing", False):
                local.publishing = True
                second_done.wait(timeout=10)
            return replace(source, destination)
        def record(index):
            local.index = index
            local.client = clients[index]
            try:
                return incident_replay.run("example", record=True)
            except OSError as error:
                return error
            finally:
                if index == 1:
                    second_done.set()
        with mock.patch.object(incident_replay, "_FixtureClient", side_effect=lambda *args, **kwargs: local.client):
            with mock.patch.object(rpc, "_fetch", side_effect=fetch):
                with mock.patch.object(incident_replay.os, "replace", side_effect=checked_replace):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(record, range(2)))
        self.assertTrue(any(isinstance(result, dict) for result in results))
        self.assertEqual(len(set(stages)), 2)
        self.assertNotIn(self.directory, stages)
        self.assertTrue(all(not stage.exists() for stage in stages))
        self.assertEqual(len({source for source, _ in replacements}), len(replacements))
        self.assertTrue(all(source.parent == destination.parent for source, destination in replacements))
        client.mode = "replay"
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)

    def test_staged_bytes_changed_after_replay_are_never_published(self):
        client, _ = self.fixture_run()
        previous = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        publish = incident_replay._publish
        for changed in ("fixture", "run"):
            with self.subTest(changed=changed):
                client.mode = "record"
                def change_before_publish(staging, fixtures_dir, path, manifest, *args):
                    name = next(iter(manifest)) if changed == "fixture" else "run.json"
                    target = staging / name
                    target.write_bytes(target.read_bytes() + b"\n")
                    return publish(staging, fixtures_dir, path, manifest, *args)
                with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
                    with mock.patch.object(incident_replay, "_publish", side_effect=change_before_publish):
                        with self.assertRaisesRegex(incident_replay.ReplayMismatch, "replay mismatch"):
                            self.record()
                    self.assertEqual({path: path.read_bytes() for path in self.directory.rglob("*.json")}, previous)
                    client.mode = "replay"
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)

    def test_unlisted_preflight_read_preserves_previous_run(self):
        client, _ = self.fixture_run()
        run_path = self.directory / "replay/example/run.json"
        previous = run_path.read_bytes()
        client.mode = "record"
        judge = rules.judge
        def extra_read(request, policy, store, pinned, nearblocks, **kwargs):
            if client.mode == "replay":
                pinned.view_account("unlisted.near")
            return judge(request, policy, store, pinned, nearblocks, **kwargs)
        error = io.StringIO()
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with mock.patch.object(rules, "judge", side_effect=extra_read):
                with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate_main.main(["incident-replay", "example", "--record"]), 1)
        self.assertIn("unlisted fixture read", error.getvalue())
        self.assertEqual(run_path.read_bytes(), previous)

    def test_record_replays_before_atomic_replace(self):
        client, _ = self.fixture_run()
        run_path = self.directory / "replay/example/run.json"
        previous = run_path.read_bytes()
        client.mode = "record"
        replace = incident_replay.os.replace
        replacements = []
        def checked_replace(source, destination):
            self.assertNotEqual(Path(source), Path(destination))
            self.assertEqual(Path(source).parent, Path(destination).parent)
            self.assertEqual(run_path.read_bytes(), previous)
            self.assertEqual(client.mode, "replay")
            if Path(destination) == run_path:
                self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8"))["fixtures"],
                                 client.fixture_files)
                self.assertEqual(len(replacements), len(client.fixture_files))
            else:
                self.assertEqual(incident_replay.hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                                 client.fixture_files[Path(destination).name])
            replacements.append(Path(source))
            return replace(source, destination)
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with mock.patch.object(incident_replay.os, "replace", side_effect=checked_replace):
                recorded = self.record()
        self.assertEqual(len(replacements), len(recorded["fixtures"]) + 1)
        self.assertEqual(len(set(replacements)), len(replacements))
        self.assertTrue(all(not temporary.exists() for temporary in replacements))
        self.assertEqual(json.loads(run_path.read_text(encoding="utf-8")), recorded)

    def test_failed_atomic_replace_preserves_previous_run_and_removes_temp(self):
        client, _ = self.fixture_run()
        run_path = self.directory / "replay/example/run.json"
        previous = run_path.read_bytes()
        client.mode = "record"
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with mock.patch.object(incident_replay.os, "replace", side_effect=OSError("replace failed")):
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate_main.main(["incident-replay", "example", "--record"]), 1)
        self.assertEqual(run_path.read_bytes(), previous)
        self.assertEqual(list(run_path.parent.iterdir()), [run_path])

    def test_partial_publication_failure_restores_every_published_byte(self):
        client, _ = self.fixture_run()
        previous = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        client.mode = "record"
        client.clock = lambda: datetime(2026, 4, 18, tzinfo=UTC).timestamp()
        replace = incident_replay.os.replace
        replaced = []
        def fetch(url, body, headers, errors):
            try:
                return {"jsonrpc": "2.0", "id": "gate", "result": self.chain.call(body["method"], body["params"])}
            except rpc.RpcError as error:
                return {"jsonrpc": "2.0", "id": "gate", "error": error.error}
        def fail_second_replace(source, destination):
            replaced.append(Path(destination))
            if len(replaced) == 2:
                raise OSError("publication failed")
            return replace(source, destination)
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with mock.patch.object(rpc, "_fetch", side_effect=fetch):
                with mock.patch.object(incident_replay.os, "replace", side_effect=fail_second_replace):
                    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(gate_main.main(["incident-replay", "example", "--record"]), 1)
        self.assertEqual(len(replaced), 3)
        self.assertEqual({path: path.read_bytes() for path in self.directory.rglob("*.json")}, previous)
        self.assertEqual(list(self.directory.glob(".*")), [])
        client.mode = "replay"
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)

    def test_failed_run_replace_and_rollback_preserve_recovery_backups(self):
        client, recorded = self.fixture_run()
        run_path = self.directory / "replay/example/run.json"
        previous = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        client.mode = "record"
        client.clock = lambda: datetime(2026, 4, 18, tzinfo=UTC).timestamp()
        replace = incident_replay.os.replace
        failed = []
        def fetch(url, body, headers, errors):
            try:
                return {"jsonrpc": "2.0", "id": "gate", "result": self.chain.call(body["method"], body["params"])}
            except rpc.RpcError as error:
                return {"jsonrpc": "2.0", "id": "gate", "error": error.error}
        def fail_run_and_rollback(source, destination):
            if Path(destination) == run_path or failed:
                failed.append((Path(source), Path(destination)))
                raise PermissionError("replacement denied")
            return replace(source, destination)
        rmtree = incident_replay.shutil.rmtree
        def fail_staging_cleanup(target):
            self.assertEqual(Path(target).parent, self.directory.parent)
            self.assertTrue(Path(target).name.startswith(".incident-record-"))
            self.addCleanup(rmtree, target)
            raise PermissionError("cleanup denied")
        error = io.StringIO()
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with mock.patch.object(rpc, "_fetch", side_effect=fetch):
                with mock.patch.object(incident_replay.os, "replace", side_effect=fail_run_and_rollback):
                    with mock.patch.object(incident_replay.shutil, "rmtree", side_effect=fail_staging_cleanup):
                        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
                            status = gate_main.main(["incident-replay", "example", "--record"])
        self.assertEqual(status, 2)
        self.assertGreater(len(failed), 1)
        self.assertEqual(run_path.read_bytes(), previous[run_path])
        commands = [line.removeprefix("Restore: ") for line in error.getvalue().splitlines()
                    if line.startswith("Restore: ")]
        self.assertTrue(commands)
        self.assertIn("cleanup denied", error.getvalue())
        if incident_replay.os.name == "nt":
            restored = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "\n".join(commands)],
                                      capture_output=True, text=True)
            self.assertEqual(restored.returncode, 0, restored.stderr)
        else:
            for command in commands:
                restored = subprocess.run(command, shell=True, capture_output=True, text=True)
                self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual({path: path.read_bytes() for path in self.directory.rglob("*.json")}, previous)
        client.mode = "replay"
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)
            self.assertEqual(incident_replay.run("example"), recorded)
        self.assertEqual(list(self.directory.glob(".*")), [])

    def test_failed_published_replay_restores_before_backup_cleanup(self):
        client, _ = self.fixture_run()
        previous = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        client.mode = "record"
        evaluate, unlink = incident_replay._evaluate, Path.unlink
        evaluations = []
        def fail_published_replay(*args):
            evaluations.append(client.fixtures_dir)
            if len(evaluations) == 3:
                raise incident_replay.ReplayMismatch("published replay failed")
            return evaluate(*args)
        def checked_unlink(target, *args, **kwargs):
            if target.exists() and target.name.startswith(".run.json."):
                self.assertEqual(len(evaluations), 4)
            return unlink(target, *args, **kwargs)
        error = io.StringIO()
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with mock.patch.object(incident_replay, "_evaluate", side_effect=fail_published_replay):
                with mock.patch.object(Path, "unlink", checked_unlink):
                    with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(gate_main.main(["incident-replay", "example", "--record"]), 1)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)
        self.assertEqual(evaluations[2:], [self.directory, self.directory])
        self.assertIn("published replay failed", error.getvalue())
        self.assertEqual({path: path.read_bytes() for path in self.directory.rglob("*.json")}, previous)
        self.assertEqual(list(self.directory.glob(".*")), [])

    def test_cleanup_failures_after_publication_warn_and_record_succeeds(self):
        for cleanup in ("backup", "lock", "staging"):
            with self.subTest(cleanup=cleanup):
                client, _ = self.fixture_run()
                run_path = self.directory / "replay/example/run.json"
                previous = run_path.read_bytes()
                self.document["accounts"] = self.document["accounts"][1:]
                self.incident_path.write_text(json.dumps(self.document), encoding="utf-8")
                client.mode = "record"
                leftovers = []
                if cleanup == "backup":
                    owner, method, original = Path, "unlink", Path.unlink
                elif cleanup == "lock":
                    owner, method, original = Path, "rmdir", Path.rmdir
                else:
                    owner, method, original = incident_replay.shutil, "rmtree", incident_replay.shutil.rmtree
                def fail_cleanup(target, *args, **kwargs):
                    target = Path(target)
                    if ((cleanup == "backup" and target.name.startswith(".run.json.") and target.exists())
                            or (cleanup == "lock" and target.name == ".incident-record-publish.lock")
                            or (cleanup == "staging" and target.name.startswith(".incident-record-"))):
                        leftovers.append(target)
                        raise PermissionError("cleanup denied")
                    return original(target, *args, **kwargs)
                error = io.StringIO()
                with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
                    with mock.patch.object(owner, method, fail_cleanup):
                        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
                            status = gate_main.main(["incident-replay", "example", "--record"])
                self.assertEqual(status, 0)
                self.assertNotEqual(run_path.read_bytes(), previous)
                self.assertTrue(leftovers)
                self.assertIn("warning", error.getvalue())
                self.assertIn("cleanup denied", error.getvalue())
                for leftover in leftovers:
                    self.assertTrue(leftover.exists())
                    self.assertIn(str(leftover), error.getvalue())
                client.mode = "replay"
                with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)
                for leftover in leftovers:
                    if leftover.is_file():
                        leftover.unlink()
                    else:
                        incident_replay.shutil.rmtree(leftover)

    def test_new_destination_rollback_uses_only_prepared_renames(self):
        client, _ = self.fixture_run()
        previous = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        run_path = self.directory / "replay/example/run.json"
        name = rpc.fixture_key(client.url, "new_fixture", {}) + ".json"
        destination = self.directory / name
        replace, unlink = incident_replay.os.replace, Path.unlink
        replacements = []
        def fail_second_replace(source, target):
            replacements.append((Path(source), Path(target)))
            if len(replacements) == 2:
                raise OSError("publication failed")
            return replace(source, target)
        def temporary_unlink(target, *args, **kwargs):
            self.assertNotEqual(target, destination)
            return unlink(target, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            (staging / name).write_bytes(b"{}")
            candidate_data = run_path.read_bytes()
            (staging / "run.json").write_bytes(candidate_data)
            manifest = incident_replay.build_manifest(staging, [name])
            with mock.patch.object(incident_replay.os, "replace", side_effect=fail_second_replace):
                with mock.patch.object(Path, "unlink", temporary_unlink):
                    with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
                        with self.assertRaisesRegex(OSError, "publication failed"):
                            incident_replay._publish(staging, self.directory, run_path, manifest, candidate_data,
                                                     lambda: incident_replay.run("example"))
        self.assertEqual(len(replacements), 3)
        self.assertEqual(replacements[2], (destination, replacements[0][0]))
        self.assertFalse(destination.exists())
        self.assertEqual({path: path.read_bytes() for path in self.directory.rglob("*.json")}, previous)
        self.assertEqual(list(self.directory.glob(".*")), [])
        client.mode = "replay"
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)

    def test_publication_disk_full_rollback_uses_prepared_backups(self):
        for fail_restore in (False, True):
            with self.subTest(fail_restore=fail_restore):
                client, _ = self.fixture_run()
                previous = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
                client.mode = "record"
                client.clock = lambda: datetime(2026, 4, 18, tzinfo=UTC).timestamp()
                replace, prepare = incident_replay.os.replace, incident_replay._prepare_bytes
                replacements, backups = [], {}
                def fetch(url, body, headers, errors):
                    try:
                        return {"jsonrpc": "2.0", "id": "gate", "result": self.chain.call(body["method"], body["params"])}
                    except rpc.RpcError as error:
                        return {"jsonrpc": "2.0", "id": "gate", "error": error.error}
                def prepare_before_publication(destination, data):
                    if replacements:
                        raise OSError(errno.ENOSPC, "disk full")
                    temporary = prepare(destination, data)
                    if data == previous.get(destination):
                        backups[destination] = temporary
                    return temporary
                def fail_publication_then_restore(source, destination):
                    replacements.append((Path(source), Path(destination)))
                    if len(replacements) == 3:
                        raise OSError(errno.ENOSPC, "disk full")
                    if fail_restore and len(replacements) == 5:
                        raise PermissionError("restore denied")
                    return replace(source, destination)
                error = io.StringIO()
                with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
                    with mock.patch.object(rpc, "_fetch", side_effect=fetch):
                        with mock.patch.object(incident_replay, "_prepare_bytes", side_effect=prepare_before_publication):
                            with mock.patch.object(incident_replay.os, "replace", side_effect=fail_publication_then_restore):
                                with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
                                    status = gate_main.main(["incident-replay", "example", "--record"])
                    self.assertEqual(status, 2 if fail_restore else 1, error.getvalue())
                    if fail_restore:
                        self.assertEqual(len(replacements), 5)
                        self.assertEqual(set(backups), set(previous) - {self.incident_path})
                        self.assertTrue(all(backup.exists() for backup in backups.values()))
                        for destination, backup in backups.items():
                            self.assertEqual(backup.read_bytes(), previous[destination])
                        commands = [line for line in error.getvalue().splitlines() if line.startswith("Restore: ")]
                        self.assertEqual(len(commands), len(backups))
                        self.assertTrue(all("os.replace(" in command for command in commands))
                        self.assertTrue(all("write_bytes" not in command for command in commands))
                        for destination, backup in backups.items():
                            replace(backup, destination)
                    self.assertEqual({path: path.read_bytes() for path in self.directory.rglob("*.json")}, previous)
                    client.mode = "replay"
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)

    def test_backup_link_failure_preserves_previous_generation(self):
        client, _ = self.fixture_run()
        previous = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        client.mode = "record"
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with mock.patch.object(incident_replay.os, "link", side_effect=OSError(errno.ENOSPC, "disk full")):
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate_main.main(["incident-replay", "example", "--record"]), 1)
            self.assertEqual({path: path.read_bytes() for path in self.directory.rglob("*.json")}, previous)
            client.mode = "replay"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)

    def test_persistent_temp_file_exhaustion_preserves_every_published_byte(self):
        client, _ = self.fixture_run()
        previous = {path: path.read_bytes() for path in self.directory.rglob("*.json")}
        client.mode = "record"
        client.clock = lambda: datetime(2026, 4, 18, tzinfo=UTC).timestamp()
        mkstemp = incident_replay.tempfile.mkstemp
        created = []
        def fetch(url, body, headers, errors):
            try:
                return {"jsonrpc": "2.0", "id": "gate", "result": self.chain.call(body["method"], body["params"])}
            except rpc.RpcError as error:
                return {"jsonrpc": "2.0", "id": "gate", "error": error.error}
        def fail_after_first_temp(*args, **kwargs):
            if created:
                raise OSError(errno.ENOSPC, "disk full")
            result = mkstemp(*args, **kwargs)
            created.append(Path(result[1]))
            return result
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with mock.patch.object(rpc, "_fetch", side_effect=fetch):
                with mock.patch.object(incident_replay.tempfile, "mkstemp", side_effect=fail_after_first_temp):
                    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(gate_main.main(["incident-replay", "example", "--record"]), 1)
        self.assertEqual(len(created), 1)
        self.assertTrue(all(not temporary.exists() for temporary in created))
        self.assertEqual({path: path.read_bytes() for path in self.directory.rglob("*.json")}, previous)
        self.assertEqual(list(self.directory.glob(".*")), [])
        client.mode = "replay"
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)

    def test_height_must_be_a_non_negative_integer(self):
        for height in (-1, True, "10", None):
            with self.subTest(height=height), self.assertRaises(ValueError):
                AsOf(self.chain, height)

    def test_resolves_last_block_at_or_before_time(self):
        run = self.record()
        self.assertEqual(run["height"], HEIGHT)
        self.assertEqual(run["block_timestamp"], START_NS + HEIGHT * 10**9)
        self.assertEqual(run["as_of"], "2026-04-16T00:00:00Z")
        # 99 recorded: 90 before the two-block age probe, plus its 3 block reads in each of the 3 evaluations.
        self.assertLess(len([1 for method, _ in self.chain.calls if method == "block"]), 100)

    def test_skipped_block_height_is_bounded_and_uses_previous_block(self):
        self.chain.missing.add(HEIGHT)
        run = self.record()
        self.assertEqual(run["height"], HEIGHT - 1)

    def test_accounts_selected_by_reported_role_and_chain(self):
        run = self.record()
        self.assertEqual([(row["role"], row["account_id"]) for row in run["accounts"]],
                         [("collector", "young.near"), ("master", "old.near"), ("worker", "missing.near")])

    def test_empty_store_and_policy_and_pinned_clients_reach_real_judge(self):
        real_judge = rules.judge
        def checked(request, policy, store, client, nearblocks, **kwargs):
            self.assertEqual(store.assertions, [])
            self.assertEqual(store.by_subject(request["receiver_id"], "near"), [])
            self.assertEqual(policy, {"allowlist": [], "caps": []})
            self.assertIsInstance(client, AsOf)
            self.assertIs(client, kwargs["archival"])
            self.assertIsNone(kwargs["injection_scanner"])
            self.assertIsNone(kwargs["issuers"])
            self.assertIsNone(kwargs["reports_account"])
            self.assertEqual(kwargs["now_ms"], (START_NS + HEIGHT * 10**9) // 10**6)
            return real_judge(request, policy, store, client, nearblocks, **kwargs)
        with mock.patch.object(rules, "judge", side_effect=checked) as judge:
            run = self.record()
        self.assertEqual(judge.call_count, 9)
        self.assertEqual(run["label_store"], {"labels": [], "incidents": []})
        self.assertTrue(all(set(check) == {"id", "status", "detail"}
                            for row in run["accounts"] for check in row["checks"]))

    def test_young_state_absent_at_two_archival_blocks_is_deny(self):
        run = self.record()
        young, old, missing = run["accounts"]
        ages = [{check["id"]: check for check in row["checks"]} for row in (young, old)]
        self.assertEqual(ages[0]["account_age"]["status"], "signal")
        self.assertIn("young.near did not exist at block 86400 (2026-04-02T00:00:00Z) from archival.example or "
                      "block 993600 (2026-04-12T12:00:00Z) from archival.example, which is younger than 7 days, "
                      "so it was created less than 7 days ago", ages[0]["account_age"]["detail"])
        self.assertEqual((young["verdict"], young["outcome"]), ("DENY", "DENY"))
        self.assertEqual(ages[1]["account_age"]["status"], "pass")
        self.assertEqual((old["verdict"], old["outcome"]), ("ALLOW", "ALLOW"))
        self.assertEqual((missing["verdict"], missing["outcome"]), ("DENY", "DENY"))
        self.assertTrue(any(params.get("block_id") == HEIGHT - 7 * rules.BLOCKS_PER_DAY_MAX
                            for _, params in self.chain.calls))
        self.assertTrue(any(params.get("block_id") == HEIGHT - 7 * rules.BLOCKS_PER_DAY_MIN
                            for _, params in self.chain.calls))

    def test_replay_recomputes_recorded_resolution_from_fixtures(self):
        client, recorded = self.fixture_run()
        with mock.patch.object(client, "final_block", wraps=client.final_block) as final_block:
            with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
                self.assertEqual(incident_replay.run("example"), recorded)
        final_block.assert_called_once_with()

    def test_cli_tampered_run_exits_one_with_replay_mismatch(self):
        self.record()
        path = self.directory / "replay" / "example" / "run.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["accounts"][0]["outcome"] = "ALLOW"
        path.write_text(json.dumps(document), encoding="utf-8")
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            status = gate_main.main(["incident-replay", "example"])
        self.assertEqual(status, 1)
        self.assertIn("replay mismatch", error.getvalue())

    def test_replay_rejects_different_requested_time(self):
        self.record()
        with self.assertRaisesRegex(ValueError, "replay mismatch"):
            incident_replay.run("example", at="2026-04-15T00:00:00Z")

    def test_replay_missing_run_fails_without_reading_chain(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            status = gate_main.main(["incident-replay", "example"])
        self.assertEqual(status, 1)
        self.assertEqual(self.chain.calls, [])

    def test_cli_table_discloses_as_of_and_checks(self):
        self.record()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = gate_main.main(["incident-replay", "example"])
        self.assertEqual(status, 0)
        self.assertIn("collector", output.getvalue())
        self.assertRegex(output.getvalue(), r"(?m)^collector +young\.near +DENY ")
        self.assertIn("account_age signal", output.getvalue())
        self.assertIn(f"As of 2026-04-16T00:00:00 UTC (block {HEIGHT}), with no labels: on-chain state only.",
                      output.getvalue())

    def test_non_utc_or_naive_time_is_refused(self):
        for at in ("2026-04-16", "2026-04-16T00:00:00", "2026-04-16T01:00:00+01:00", "bad"):
            with self.subTest(at=at), self.assertRaises(ValueError):
                incident_replay.run("example", at=at, record=True)

    def test_cli_shortens_long_accounts_like_the_site(self):
        account = "a" * 64
        self.document["accounts"][0]["subject"] = account
        self.incident_path.write_text(json.dumps(self.document), encoding="utf-8")
        self.record()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(gate_main.main(["incident-replay", "example"]), 0)
        self.assertIn(account[:12] + "\u2026" + account[-10:], output.getvalue())

    def test_committed_role_shape_selects_seven_near_accounts(self):
        path = Path(__file__).resolve().parent.parent / "intel/incidents/rhea.json"
        self.incident_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        run = incident_replay.run("rhea", record=True)
        roles = [row["role"] for row in run["accounts"]]
        self.assertEqual(roles.count("master"), 1)
        self.assertEqual(roles.count("collector"), 1)
        self.assertEqual(roles.count("worker"), 5)
        self.assertEqual(len(roles), 7)

    def test_real_fixture_client_recomputes_recorded_run_offline(self):
        recorded = self.record()
        calls = list(self.chain.calls)
        for method, params in calls:
            try:
                response = {"jsonrpc": "2.0", "id": "gate", "result": self.chain.call(method, params)}
            except rpc.RpcError as error:
                response = {"jsonrpc": "2.0", "id": "gate", "error": error.error}
            key = rpc.fixture_key(self.chain.url, method, params)
            fixture = {"request": {"method": method, "params": params}, "url": self.chain.url,
                       "response": response, "fetched_at": "2026-04-16T00:00:00Z"}
            (self.directory / f"{key}.json").write_text(json.dumps(fixture), encoding="utf-8")
        real = Chain.__bases__[0](self.chain.url, "replay", fixtures_dir=self.directory)
        recorded["fixtures"] = incident_replay.build_manifest(
            self.directory, [path.name for path in self.directory.glob("*.json") if path != self.incident_path])
        path = self.directory / "replay/example/run.json"
        path.write_text(json.dumps(recorded), encoding="utf-8")
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=real):
            self.assertEqual(incident_replay.run("example"), recorded)

    def fixture_run(self):
        self.record()
        for method, params in list(self.chain.calls):
            try:
                response = {"jsonrpc": "2.0", "id": "gate", "result": self.chain.call(method, params)}
            except rpc.RpcError as error:
                response = {"jsonrpc": "2.0", "id": "gate", "error": error.error}
            key = rpc.fixture_key(self.chain.url, method, params)
            fixture = {"request": {"method": method, "params": params}, "url": self.chain.url,
                       "response": response, "fetched_at": "2026-04-16T00:00:00Z"}
            (self.directory / f"{key}.json").write_text(json.dumps(fixture), encoding="utf-8")
        client = Chain.__bases__[0](self.chain.url, "record", fixtures_dir=self.directory,
                                   clock=lambda: datetime(2026, 4, 16, tzinfo=UTC).timestamp())
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            recorded = self.record()
        client.mode = "replay"
        return client, recorded

    def assert_fixture_mismatch(self, client):
        error = io.StringIO()
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate_main.main(["incident-replay", "example"]), 1)
        self.assertIn("replay mismatch", error.getvalue())

    def test_record_manifest_includes_resolution_fixtures(self):
        client, recorded = self.fixture_run()
        self.assertIn("fixtures", recorded)
        self.assertEqual(recorded["fixtures"], incident_replay.build_manifest(
            self.directory, [path.name for path in self.directory.glob("*.json") if path != self.incident_path]))
        key = rpc.fixture_key(client.url, "block", {"finality": "final"})
        self.assertIn(f"{key}.json", recorded["fixtures"])
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            self.assertEqual(incident_replay.run("example"), recorded)

    def test_record_manifest_keeps_a_fixture_whose_read_failed(self):
        client, _ = self.fixture_run()
        key = rpc.fixture_key(client.url, "query", {"request_type": "view_account",
                              "account_id": "young.near", "block_id": HEIGHT})
        path = self.directory / f"{key}.json"
        path.write_text("{", encoding="utf-8")
        client.mode = "record"
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
            recorded = self.record()
        self.assertIn(path.name, recorded["fixtures"])
        self.assertEqual(recorded["fixtures"][path.name], incident_replay.build_manifest(self.directory, [path.name])[path.name])

    def test_fixture_replacement_after_hash_cannot_change_parsed_balance(self):
        client, recorded = self.fixture_run()
        params = {"request_type": "view_account", "account_id": "young.near", "block_id": HEIGHT}
        key = rpc.fixture_key(client.url, "query", params)
        path = self.directory / f"{key}.json"
        original = path.read_bytes()
        fixture = json.loads(original)
        fixture["response"]["result"]["amount"] = "0"
        replacement = json.dumps(fixture).encode("utf-8")
        read_bytes = Path.read_bytes
        reads = []
        def replace_after_read(target):
            data = read_bytes(target)
            if target == path:
                reads.append(target)
                target.write_bytes(replacement)
            return data
        client.manifest = recorded["fixtures"]
        client.fixture_data.clear()
        with mock.patch.object(Path, "read_bytes", replace_after_read):
            response = client.call("query", params)
        self.assertEqual(response, json.loads(original)["response"]["result"])
        self.assertEqual(len(reads), 1)

    def test_record_digest_matches_consumed_bytes_after_replacement(self):
        client, _ = self.fixture_run()
        params = {"request_type": "view_account", "account_id": "young.near", "block_id": HEIGHT}
        key = rpc.fixture_key(client.url, "query", params)
        path = self.directory / f"{key}.json"
        original = path.read_bytes()
        expected = incident_replay.build_manifest(self.directory, [path.name])[path.name]
        fixture = json.loads(original)
        fixture["response"]["result"]["amount"] = "0"
        replacement = json.dumps(fixture).encode("utf-8")
        read_bytes, read_text = Path.read_bytes, Path.read_text
        replaced = []
        def bytes_then_replace(target):
            data = read_bytes(target)
            if target.name == path.name and target.parent != self.directory and not replaced:
                replaced.append(target)
                target.write_bytes(replacement)
            return data
        def text_then_replace(target, *args, **kwargs):
            data = read_text(target, *args, **kwargs)
            if target.name == path.name and target.parent != self.directory and not replaced:
                replaced.append(target)
                target.write_bytes(replacement)
            return data
        run_path = self.directory / "replay/example/run.json"
        previous = run_path.read_bytes()
        client.mode = "record"
        with mock.patch.object(Path, "read_bytes", bytes_then_replace):
            with mock.patch.object(Path, "read_text", text_then_replace):
                with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
                    with self.assertRaisesRegex(incident_replay.ReplayMismatch, "fixture content changed"):
                        self.record()
        self.assertTrue(replaced)
        self.assertEqual(client.manifest[path.name], expected)
        self.assertEqual(run_path.read_bytes(), previous)

    def test_new_record_digest_uses_serialized_response_without_rereading(self):
        directory = self.directory / "fresh"
        client = Chain.__bases__[0](self.chain.url, "record", fixtures_dir=directory,
                                   clock=lambda: datetime(2026, 4, 16, tzinfo=UTC).timestamp())
        params = {"request_type": "view_account", "account_id": "young.near", "block_id": HEIGHT}
        response = {"jsonrpc": "2.0", "id": "gate", "result": {"code_hash": rpc.EMPTY_CODE_HASH, "block_height": HEIGHT, "amount": "1",
                               "locked": "0", "storage_usage": 1, "block_hash": BLOCK_HASH}}
        write_bytes = Path.write_bytes
        written = {}
        def replace_after_write(target, data):
            written[target.name] = data
            result = write_bytes(target, data)
            fixture = json.loads(data)
            fixture["response"]["result"]["amount"] = "0"
            write_bytes(target, json.dumps(fixture).encode("utf-8"))
            return result
        with mock.patch.object(rpc, "_fetch", return_value=response) as fetch, \
                mock.patch.object(client, "_block_hash", return_value=BLOCK_HASH):
            with mock.patch.object(Path, "write_bytes", replace_after_write):
                with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("fixture reread")):
                    self.assertEqual(client.call("query", params), response["result"])
        fetch.assert_called_once()
        self.assertEqual(client.fixture_files, {name: incident_replay.hashlib.sha256(data).hexdigest()
                                              for name, data in written.items()})

    def test_replay_reads_each_fixture_once(self):
        client, recorded = self.fixture_run()
        read_bytes, read_text = Path.read_bytes, Path.read_text
        reads = {}
        def bytes_count(target):
            if target.parent == self.directory:
                reads[target.name] = reads.get(target.name, 0) + 1
            return read_bytes(target)
        def text_count(target, *args, **kwargs):
            if target.parent == self.directory and target != self.incident_path:
                reads[target.name] = reads.get(target.name, 0) + 1
            return read_text(target, *args, **kwargs)
        with mock.patch.object(Path, "read_bytes", bytes_count):
            with mock.patch.object(Path, "read_text", text_count):
                with mock.patch.object(incident_replay, "_FixtureClient", return_value=client):
                    self.assertEqual(incident_replay.run("example"), recorded)
        self.assertEqual(reads, dict.fromkeys(recorded["fixtures"], 1))

    def test_resolution_read_removed_from_manifest_is_replay_mismatch(self):
        client, recorded = self.fixture_run()
        key = rpc.fixture_key(client.url, "block", {"finality": "final"})
        del recorded["fixtures"][f"{key}.json"]
        (self.directory / "replay/example/run.json").write_text(json.dumps(recorded), encoding="utf-8")
        self.assert_fixture_mismatch(client)

    def test_tampered_balance_is_replay_mismatch(self):
        client, _ = self.fixture_run()
        key = rpc.fixture_key(client.url, "query", {"request_type": "view_account",
                              "account_id": "young.near", "block_id": HEIGHT})
        path = self.directory / f"{key}.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        fixture["response"]["result"]["amount"] = "0"
        path.write_text(json.dumps(fixture), encoding="utf-8")
        self.assert_fixture_mismatch(client)

    def test_tampered_block_hash_is_replay_mismatch(self):
        client, _ = self.fixture_run()
        key = rpc.fixture_key(client.url, "block", {"block_id": HEIGHT})
        path = self.directory / f"{key}.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        fixture["response"]["result"]["header"]["hash"] = OTHER_BLOCK_HASH
        path.write_text(json.dumps(fixture), encoding="utf-8")
        self.assert_fixture_mismatch(client)

    def test_tampered_resolution_fixture_is_replay_mismatch(self):
        client, _ = self.fixture_run()
        key = rpc.fixture_key(client.url, "block", {"finality": "final"})
        path = self.directory / f"{key}.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        fixture["response"]["result"]["header"]["hash"] = OTHER_BLOCK_HASH
        path.write_text(json.dumps(fixture), encoding="utf-8")
        self.assert_fixture_mismatch(client)

    def test_deleted_resolution_fixture_is_replay_mismatch(self):
        client, _ = self.fixture_run()
        key = rpc.fixture_key(client.url, "block", {"finality": "final"})
        (self.directory / f"{key}.json").unlink()
        self.assert_fixture_mismatch(client)

    def test_malformed_fixture_reports_replay_mismatch(self):
        client, recorded = self.fixture_run()
        key = rpc.fixture_key(client.url, "block", {"block_id": HEIGHT})
        path = self.directory / f"{key}.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        for header in (None, [], "block", {}):
            with self.subTest(header=header):
                fixture["response"]["result"]["header"] = header
                path.write_text(json.dumps(fixture), encoding="utf-8")
                recorded["fixtures"] = incident_replay.build_manifest(self.directory, recorded["fixtures"])
                (self.directory / "replay/example/run.json").write_text(json.dumps(recorded), encoding="utf-8")
                self.assert_fixture_mismatch(client)

    def test_rule_read_removed_from_manifest_is_replay_mismatch(self):
        client, recorded = self.fixture_run()
        key = rpc.fixture_key(client.url, "query", {"request_type": "view_account",
                              "account_id": "young.near", "block_id": HEIGHT})
        del recorded["fixtures"][f"{key}.json"]
        (self.directory / "replay/example/run.json").write_text(json.dumps(recorded), encoding="utf-8")
        self.assert_fixture_mismatch(client)

    def test_unlisted_read_is_replay_mismatch(self):
        client, _ = self.fixture_run()
        params = {"request_type": "view_account", "account_id": "unlisted.near", "block_id": HEIGHT}
        key = rpc.fixture_key(client.url, "query", params)
        fixture = {"request": {"method": "query", "params": params}, "url": client.url,
                   "response": {"jsonrpc": "2.0", "id": "gate", "result": self.chain.call("query", params)},
                   "fetched_at": "2026-04-16T00:00:00Z"}
        (self.directory / f"{key}.json").write_text(json.dumps(fixture), encoding="utf-8")
        judge = rules.judge
        def extra_read(request, policy, store, pinned, nearblocks, **kwargs):
            pinned.view_account("unlisted.near")
            return judge(request, policy, store, pinned, nearblocks, **kwargs)
        with mock.patch.object(rules, "judge", side_effect=extra_read):
            self.assert_fixture_mismatch(client)

    def test_replay_always_uses_replay_mode_despite_environment(self):
        self.record()
        with mock.patch.dict("os.environ", {"GATE_FIXTURES_MODE": "off"}):
            incident_replay.run("example")
        self.assertEqual(incident_replay._FixtureClient.call_args.args[1], "replay")

    def test_real_replay_client_missing_fixtures_never_fetches(self):
        self.record()
        real = Chain.__bases__[0]("https://archival.example", "replay", fixtures_dir=self.directory)
        with mock.patch.object(incident_replay, "_FixtureClient", return_value=real):
            with self.assertRaises((rpc.FixtureMiss, ValueError, incident_replay.ReplayMismatch)):
                incident_replay.run("example")


if __name__ == "__main__":
    unittest.main()
