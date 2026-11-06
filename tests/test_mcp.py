import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import base58, canon, ed25519, rpc, signer
from tests.test_verdict import nacl, no_network, write_key_file

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = ROOT / "fixtures" / "evidence"
ALLOW_POLICY = {"allowlist": ["intents.near"], "caps": [{"asset": {"Ft": "wrap.near"}}]}
USDC_BASE_ASSET = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
DEMO_RECIPIENT = "0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107"
ONE_NEAR = "1000000000000000000000000"
KEPT = ("PATH", "SYSTEMROOT", "HOME", "LANG")
PAYMENT = {"kind": "ft", "token": "wrap.near", "receiver": "intents.near", "amount": "1000", "memo": "invoice 7"}
LIVE_GATE = "gate.testnet"
# Any read a refused live call makes by mistake fails on this machine instead of reaching testnet.
UNREACHABLE = "http://127.0.0.1:9"
NOTHING_SIGNED = "; nothing was signed"


def initialize(version, number=1):
    return {"jsonrpc": "2.0", "id": number, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


def call(number, name, arguments):
    return {"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


class McpSessionCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def run_server(self, data, overrides=None):
        """The raw stdout bytes and stderr text of one server process fed the bytes; overrides sets
        environment variables over the replay environment, and a None value removes one."""
        env = {key: value for key, value in os.environ.items() if key in KEPT}
        env.update({"GATE_FIXTURES_MODE": "replay", "GATE_ACCOUNT_ID": "gate.near",
                    "GATE_POLICY": json.dumps(ALLOW_POLICY), "TMPDIR": self.tmp, "TEMP": self.tmp, "TMP": self.tmp})
        for key, value in (overrides or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        process = subprocess.run([sys.executable, "-m", "mcp.server"], input=data, capture_output=True, cwd=ROOT,
                                 env=env, timeout=120, check=False)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))
        return process.stdout, process.stderr.decode("utf-8", "replace")

    def session(self, messages, framed=False, overrides=None):
        bodies = [json.dumps(message).encode("utf-8") for message in messages]
        if framed:
            return self.run_server(b"".join(b"Content-Length: %d\r\n\r\n" % len(body) + body for body in bodies), overrides)
        return self.run_server(b"".join(body + b"\n" for body in bodies), overrides)

    def replies(self, messages, overrides=None):
        stdout, _ = self.session(messages, overrides=overrides)
        lines = stdout.split(b"\n")
        self.assertEqual(lines[-1], b"")
        replies = [json.loads(line) for line in lines[:-1]]
        for reply in replies:
            self.assertEqual(reply["jsonrpc"], "2.0")
        return {reply["id"]: reply for reply in replies}, replies


class McpServerTest(McpSessionCase):
    def test_a_scripted_session(self):
        before = sorted(path.name for path in EVIDENCE_DIR.iterdir())
        by_id, replies = self.replies([
            initialize("2025-06-18"),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            call(3, "check_near_payment", {"kind": "ft", "token": "wrap.near", "receiver": "intents.near",
                                           "amount": "1000", "memo": "invoice 7"}),
            call(4, "check_intents_quote", {"origin_asset": "nep141:wrap.near", "destination_asset": USDC_BASE_ASSET,
                                            "amount": ONE_NEAR, "recipient": DEMO_RECIPIENT}),
            call(5, "scan_near_account", {"account_id": "near"}),
            {"jsonrpc": "2.0", "id": 6, "method": "ping"},
            call(7, "no_such_tool", {}),
            {"jsonrpc": "2.0", "id": 8, "method": "resources/list"},
            call(9, "check_near_payment", {"kind": "ft", "token": "wrap.near", "receiver": "intents.near",
                                           "amount": "0100"}),
        ])
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(by_id[1]["result"], {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                              "serverInfo": {"name": "shieldbot-near", "version": "0.1.0"}})
        tools = by_id[2]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools],
                         ["check_near_payment", "check_intents_quote", "scan_near_account"])
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(set(tool["inputSchema"]["required"]) <= set(tool["inputSchema"]["properties"]))

        payment = by_id[3]["result"]
        self.assertFalse(payment["isError"])
        self.assertEqual(payment["content"][0]["type"], "text")
        verdict = json.loads(payment["content"][0]["text"])
        self.assertEqual(verdict["outcome"], "ALLOW")
        self.assertEqual(verdict["action"], "sign")
        self.assertEqual(verdict["ask"], [])
        self.assertEqual(verdict["verdict"]["evidence_hash"], verdict["evidence_hash"])
        self.assertTrue(verdict["message"].startswith("GATEv1|gate.near|"))
        self.assertTrue(Path(verdict["evidence_path"]).is_relative_to(self.tmp))
        self.assertEqual(verdict["signer"]["kind"], "TEST")

        quote = json.loads(by_id[4]["result"]["content"][0]["text"])
        self.assertFalse(by_id[4]["result"]["isError"])
        self.assertEqual(quote["verdict"], "DENY")
        self.assertIn("recipient_theft_label", [c["id"] for c in quote["checks"] if c["status"] == "signal"])

        scan = json.loads(by_id[5]["result"]["content"][0]["text"])
        self.assertEqual(scan["findings"]["keys_error"], "RPC error TOO_MANY_ACCESS_KEYS")
        self.assertIn("full access keys: unknown", scan["lines"])

        self.assertEqual(by_id[6]["result"], {})
        self.assertEqual(by_id[7]["error"]["code"], -32602)
        self.assertIn("no_such_tool", by_id[7]["error"]["message"])
        self.assertEqual(by_id[8]["error"]["code"], -32601)
        self.assertTrue(by_id[9]["result"]["isError"])
        self.assertTrue(by_id[9]["result"]["content"][0]["text"].startswith("invalid request: "))
        self.assertEqual(sorted(path.name for path in EVIDENCE_DIR.iterdir()), before)

    def test_only_json_frames_reach_stdout(self):
        stdout, stderr = self.session([initialize("2025-06-18"), call(2, "scan_near_account", {"account_id": "near"}),
                                       {"jsonrpc": "2.0", "method": "notifications/initialized"}])
        for line in stdout.decode("utf-8").splitlines():
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0")
        self.assertEqual(len(stdout.decode("utf-8").splitlines()), 2)
        self.assertIn("MCP server on stdio", stderr)

    def test_the_protocol_version_is_echoed_or_falls_back(self):
        for asked, answered in (("2024-11-05", "2024-11-05"), ("2025-03-26", "2025-03-26"),
                                ("2025-06-18", "2025-06-18"), ("2099-01-01", "2024-11-05")):
            with self.subTest(asked=asked):
                by_id, _ = self.replies([initialize(asked)])
                self.assertEqual(by_id[1]["result"]["protocolVersion"], answered)

    def test_the_content_length_framing_is_answered_in_kind(self):
        stdout, _ = self.session([initialize("2025-03-26"), {"jsonrpc": "2.0", "id": 2, "method": "ping"}],
                                 framed=True)
        replies = []
        while stdout:
            header, _, rest = stdout.partition(b"\r\n\r\n")
            self.assertTrue(header.startswith(b"Content-Length: "))
            length = int(header.split(b": ")[1])
            replies.append(json.loads(rest[:length]))
            stdout = rest[length:]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(replies[1]["result"], {})

    def test_the_mcp_call_site_uses_the_default_signing_policy(self):
        source = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("sign_actions", source)

    def test_negative_and_oversized_content_lengths_are_refused_before_body_read(self):
        from mcp import server

        class TrackingStream:
            def __init__(self, data):
                self.stream = io.BufferedReader(io.BytesIO(data))
                self.read_calls = []

            def peek(self, size):
                return self.stream.peek(size)

            def readline(self):
                return self.stream.readline()

            def read(self, size=-1):
                self.read_calls.append(size)
                return self.stream.read(size)

        for length in (-1, 1048577):
            with self.subTest(length=length), contextlib.redirect_stderr(io.StringIO()):
                stream = TrackingStream(f"Content-Length: {length}\r\n\r\nbody".encode("ascii"))
                self.assertEqual(list(server._messages(stream)), [])
                self.assertEqual(stream.read_calls, [])

    def test_the_maximum_content_length_is_accepted(self):
        from mcp import server

        class TrackingStream:
            def __init__(self, data):
                self.stream = io.BufferedReader(io.BytesIO(data))
                self.read_calls = []

            def peek(self, size):
                return self.stream.peek(size)

            def readline(self):
                return self.stream.readline()

            def read(self, size=-1):
                self.read_calls.append(size)
                return self.stream.read(size)

        stream = TrackingStream(b"Content-Length: 1048576\r\n\r\n" + b"x" * 1048576)
        self.assertEqual(list(server._messages(stream)), [(True, b"x" * 1048576)])
        self.assertEqual(stream.read_calls, [1048576])

    def test_a_line_that_is_not_json_is_a_parse_error(self):
        stdout, _ = self.session([])
        self.assertEqual(stdout, b"")
        stdout, _ = self.run_server(b"not json\n[1]\n")
        first, second = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual((first["id"], first["error"]["code"]), (None, -32700))
        self.assertEqual((second["id"], second["error"]["code"]), (None, -32600))

    def test_params_that_are_not_an_object_count_as_empty(self):
        by_id, _ = self.replies([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": [1]},
                                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": "scan"}])
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(by_id[2]["error"]["code"], -32602)

    def test_a_header_that_does_not_parse_is_skipped(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}).encode("utf-8")
        data = (b"Content-Length: %d\r\nX-Note: caf\xe9\r\nContent-Length: abc\r\n\r\n" % len(body)) + body
        stdout, stderr = self.run_server(data)
        header, _, rest = stdout.partition(b"\r\n\r\n")
        self.assertEqual(header, b"Content-Length: %d" % len(json.dumps({"jsonrpc": "2.0", "id": 5, "result": {}})))
        self.assertEqual(json.loads(rest), {"jsonrpc": "2.0", "id": 5, "result": {}})
        self.assertEqual(stderr.count("skipped a header that does not parse"), 2)

    def test_the_evidence_directory_is_removed_unless_kept(self):
        self.replies([initialize("2025-06-18"), call(2, "scan_near_account", {"account_id": "near"})])
        self.assertEqual(os.listdir(self.tmp), [])
        payment = call(1, "check_near_payment", {"kind": "ft", "token": "wrap.near", "receiver": "intents.near",
                                                 "amount": "1000"})
        by_id, _ = self.replies([payment])
        self.assertEqual(json.loads(by_id[1]["result"]["content"][0]["text"])["outcome"], "ALLOW")
        self.assertEqual(os.listdir(self.tmp), [])
        _, stderr = self.session([payment], overrides={"GATE_KEEP_EVIDENCE": "1"})
        self.assertIn("removed at exit unless GATE_KEEP_EVIDENCE=1", stderr)
        [kept] = os.listdir(self.tmp)
        self.assertEqual(len([name for name in os.listdir(Path(self.tmp) / kept) if name.endswith(".json")]), 1)


class McpHandlerTest(unittest.TestCase):
    def test_an_unexpected_tool_failure_is_error_content_not_a_crash(self):
        from mcp import server

        def broken(arguments, evidence_dir):
            raise RuntimeError("disk full")

        errors = io.StringIO()
        with mock.patch.dict(server.HANDLERS, {"scan_near_account": broken}), contextlib.redirect_stderr(errors):
            reply = server.handle(call(1, "scan_near_account", {"account_id": "near"}), "unused")
        self.assertEqual(reply["result"], {"content": [{"type": "text", "text": "the tool failed: RuntimeError: disk "
                                                                                 "full"}], "isError": True})
        self.assertIn("tool scan_near_account failed: RuntimeError: disk full", errors.getvalue())
        self.assertTrue(Path(server.__file__).is_relative_to(ROOT))

    def test_a_non_string_tool_name_returns_invalid_params_and_the_server_continues(self):
        from mcp import server

        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": [], "arguments": {}}},
            call(2, "scan_near_account", {}),
        ]
        stdin = io.BytesIO(b"".join(json.dumps(message).encode("utf-8") + b"\n" for message in messages))
        stdout = io.BytesIO()

        def valid_tool(arguments, evidence_dir):
            return "valid call completed"

        with (mock.patch.dict(os.environ, {"GATE_KEEP_EVIDENCE": "0"}),
              mock.patch.dict(server.HANDLERS, {"scan_near_account": valid_tool}),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(server.serve(stdin, stdout), 0)
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32602)
        self.assertEqual(replies[1]["result"]["content"][0]["text"], "valid call completed")


class McpLiveSigningTest(McpSessionCase):
    def setUp(self):
        super().setUp()
        keys = tempfile.TemporaryDirectory()
        self.addCleanup(keys.cleanup)
        self.seed = os.urandom(32)
        self.public = ed25519.public_from_seed(self.seed)
        self.key_file = Path(keys.name) / "throwaway-verifier.json"
        write_key_file(self.key_file, self.seed)
        self.agent = ed25519.public_from_seed(os.urandom(32))
        self.agent_pub = base58.encode_key(self.agent)
        self.live = {"GATE_LIVE_SIGNING": "1", "VERIFIER_KEY_FILE": str(self.key_file), "GATE_FIXTURES_MODE": "off",
                     "GATE_ACCOUNT_ID": LIVE_GATE, "GATE_POLICY": None, "AGENT_PUB": self.agent_pub,
                     "NEAR_RPC_URL": UNREACHABLE, "NEAR_MAINNET_RPC_URL": UNREACHABLE,
                     "NEARBLOCKS_API_URL": UNREACHABLE, "NEAR_ARCHIVAL_RPC_URL": UNREACHABLE}

    def payment(self, overrides):
        by_id, _ = self.replies([call(1, "check_near_payment", PAYMENT)], overrides)
        return by_id[1]["result"]

    def refusal(self, overrides):
        result = self.payment({**self.live, **overrides})
        self.assertTrue(result["isError"])
        self.assertEqual(os.listdir(self.tmp), [])
        return result["content"][0]["text"]

    def test_every_partial_live_configuration_is_refused_and_nothing_is_signed(self):
        cases = (
            ({"GATE_FIXTURES_MODE": "replay"}, ("live signing needs GATE_FIXTURES_MODE=off, not replay: a live key "
                                                "never signs over fixtures")),
            ({"GATE_FIXTURES_MODE": None}, ("live signing needs GATE_FIXTURES_MODE=off, not replay: a live key never "
                                            "signs over fixtures")),
            ({"GATE_FIXTURES_MODE": "record"}, ("live signing needs GATE_FIXTURES_MODE=off, not record: a live key "
                                                "never signs over fixtures")),
            ({"GATE_ACCOUNT_ID": "gate.near"}, "live signing is for a .testnet Gate only, not gate.near"),
            ({"GATE_ACCOUNT_ID": None}, "live signing is for a .testnet Gate only, not gate.near"),
            ({"GATE_POLICY": json.dumps(ALLOW_POLICY)}, ("live signing reads the policy from get_agent; unset "
                                                         "GATE_POLICY")),
            ({"VERIFIER_KEY_FILE": None}, "live signing needs VERIFIER_KEY_FILE"),
            ({"AGENT_PUB": None}, "live signing needs AGENT_PUB, the agent public key as ed25519:<base58>"),
            ({"AGENT_PUB": "ed25519:abc"}, "AGENT_PUB is not a 32 byte ed25519 key: ed25519:abc"),
            ({"AGENT_PUB": self.agent.hex()}, f"AGENT_PUB is not a 32 byte ed25519 key: {self.agent.hex()}"),
            ({"AGENT_PUB": "ed25519:0OIl"}, "AGENT_PUB is not a 32 byte ed25519 key: ed25519:0OIl"),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.refusal(overrides), expected + NOTHING_SIGNED)

    def test_a_key_file_the_signer_cannot_use_is_refused(self):
        missing = str(self.key_file.with_name("missing.json"))
        text = self.refusal({"VERIFIER_KEY_FILE": missing})
        self.assertTrue(text.startswith("no live signer: "), text)
        self.assertTrue(text.endswith(NOTHING_SIGNED), text)
        self.assertTrue("PyNaCl is not installed" in text or f"{missing}: cannot read a key file" in text, text)

    def test_without_the_flag_a_key_file_still_signs_with_the_test_key(self):
        from mcp import server

        for flag in (None, "0"):
            with self.subTest(flag=flag):
                result = self.payment({"GATE_LIVE_SIGNING": flag, "VERIFIER_KEY_FILE": str(self.key_file),
                                       "AGENT_PUB": self.agent_pub})
                self.assertFalse(result["isError"])
                verdict = json.loads(result["content"][0]["text"])
                self.assertEqual(verdict["outcome"], "ALLOW")
                test_key = signer.TestSigner().public_key_hex
                self.assertEqual(verdict["signer"], {
                    "kind": "TEST", "key_id": "cur", "public_key": base58.encode_key(bytes.fromhex(test_key)),
                    "public_key_hex": test_key, "note": server.TEST_KEY_NOTE})
                self.assertEqual(verdict["agent_pk"], ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex())
                self.assertEqual(verdict["policy_source"], "GATE_POLICY")
                self.assertNotIn(self.agent.hex(), verdict["message"])

    @unittest.skipIf(nacl is None, "PyNaCl is not installed; run under ~/.venvs/near/bin/python")
    def test_a_live_allow_is_signed_with_the_configured_key_for_the_configured_agent(self):
        from mcp import server

        clients = (rpc.Client(rpc.MAINNET_RPC_URL, mode="replay"),
                   rpc.Client(rpc.NEARBLOCKS_URL, mode="replay", kind="http"), None,
                   rpc.Client(rpc.ARCHIVAL_RPC_URL, mode="replay"))
        env = {key: value for key, value in self.live.items() if value is not None}
        errors = io.StringIO()
        with (mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen", side_effect=no_network),
              mock.patch.object(server, "_clients", return_value=clients),
              mock.patch.object(server, "_live_policy", return_value=ALLOW_POLICY) as live_policy,
              contextlib.redirect_stderr(errors)):
            os.environ.pop("GATE_POLICY", None)
            result = server._check_near_payment(PAYMENT, self.tmp)
        live_policy.assert_called_once_with(clients[0], LIVE_GATE, self.agent_pub)
        self.assertEqual(result["outcome"], "ALLOW")
        self.assertEqual(result["gate"], LIVE_GATE)
        self.assertEqual(result["signer"], {"kind": "live", "key_id": "cur", "public_key": base58.encode_key(self.public),
                                            "public_key_hex": self.public.hex()})
        self.assertEqual(result["agent_pk"], self.agent.hex())
        self.assertEqual(result["policy_source"], "get_agent")
        self.assertTrue(result["message"].startswith(f"GATEv1|{LIVE_GATE}|{self.agent.hex()}|"))
        self.assertTrue(ed25519.verify(self.public, canon.digest(result["message"].encode("utf-8")),
                                       bytes.fromhex(result["verdict"]["signature"])))
        logged = errors.getvalue()
        self.assertEqual(len(logged.splitlines()), 1)
        self.assertIn(self.agent_pub, logged)
        self.assertIn(base58.encode_key(self.public), logged)
        self.assertNotIn(self.seed.hex(), logged)
        self.assertNotIn(base58.encode(self.seed), logged)

    def test_a_live_read_of_a_paused_gate_asks_without_a_signature(self):
        from mcp import server

        class Client:
            def __init__(self):
                self.calls = []

            def call_function(self, gate, method, arguments):
                self.calls.append((gate, method, arguments))
                if method == "get_agent":
                    return {"enabled": True, "allowlist": ["intents.near"], "unknown_caps": None}
                if method == "get_policy":
                    return {"paused": True, "denylist": []}
                raise AssertionError(method)

        client = Client()
        clients = (client, rpc.Client(rpc.MAINNET_RPC_URL, mode="replay"), None,
                   rpc.Client(rpc.ARCHIVAL_RPC_URL, mode="replay"))
        judgement = server.rules.Judgement(verdict="ALLOW", checks=[], reasons=[], labels=[], observed={})
        env = {key: value for key, value in self.live.items() if value is not None}
        with (mock.patch.dict(os.environ, env),
              mock.patch.object(server, "_live_signing", return_value=(signer.TestSigner(), self.agent_pub,
                                                                        self.agent.hex())),
              mock.patch.object(server, "_clients", return_value=clients),
              mock.patch.object(server.rules, "judge", return_value=judgement),
              mock.patch.object(server.labels, "load", return_value=None),
              contextlib.redirect_stderr(io.StringIO())):
            result = server._check_near_payment(PAYMENT, self.tmp)
        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["ask"], ["the Gate is paused, so it refuses every transfer (REFUSED:paused)"])
        self.assertIsNone(result["verdict"])
        self.assertIsNone(result["message"])
        document = json.loads(Path(result["evidence_path"]).read_bytes())
        self.assertEqual([document[key] for key in ("issued_ms", "expires_ms", "verdict_id")], [None, None, None])
        self.assertEqual(client.calls, [(LIVE_GATE, "get_agent", {"public_key": self.agent_pub}),
                                        (LIVE_GATE, "get_policy", {})])

    def test_a_live_signer_that_cannot_start_is_refused_before_any_read(self):
        from mcp import server

        env = {key: value for key, value in self.live.items() if value is not None}
        missing = signer.SignerError("PyNaCl is not installed; the live signer runs from the venv ~/.venvs/near")
        with (mock.patch.dict(os.environ, env), mock.patch.object(server.signer, "NaClSigner", side_effect=missing),
              mock.patch.object(server, "_clients", side_effect=AssertionError("read before the signer started"))):
            os.environ.pop("GATE_POLICY", None)
            with self.assertRaises(server.ToolError) as raised:
                server._check_near_payment(PAYMENT, self.tmp)
        self.assertEqual(str(raised.exception), f"no live signer: {missing}" + NOTHING_SIGNED)

    def test_the_policy_is_read_from_get_agent_and_an_unregistered_key_is_refused(self):
        from mcp import server

        class Client:
            def __init__(self, answer, gate_policy=None):
                self.answer, self.gate_policy, self.calls = answer, gate_policy or {"paused": False, "denylist": []}, []

            def call_function(self, contract, method, args):
                self.calls.append((contract, method, args))
                if isinstance(self.answer, Exception):
                    raise self.answer
                return self.answer if method == "get_agent" else self.gate_policy

        gate_policy = {"paused": True, "denylist": ["blocked.testnet"]}
        registered = Client(ALLOW_POLICY, gate_policy)
        self.assertEqual(server._live_policy(registered, LIVE_GATE, self.agent_pub),
                         {**ALLOW_POLICY, "gate_paused": True, "gate_denylist": ["blocked.testnet"]})
        self.assertEqual(registered.calls, [(LIVE_GATE, "get_agent", {"public_key": self.agent_pub}),
                                            (LIVE_GATE, "get_policy", {})])
        with self.assertRaises(server.ToolError) as raised:
            server._live_policy(Client(None), LIVE_GATE, self.agent_pub)
        self.assertEqual(str(raised.exception),
                         f"the agent key {self.agent_pub} is not registered on {LIVE_GATE}" + NOTHING_SIGNED)
        for error in (rpc.RpcError({"name": "HANDLER_ERROR"}), rpc.TransportError("timed out"),
                      rpc.MalformedResponse("the call_function result holds no byte list")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(server.ToolError) as raised:
                    server._live_policy(Client(error), LIVE_GATE, self.agent_pub)
                self.assertEqual(str(raised.exception),
                                 f"cannot read Gate policy on {LIVE_GATE}: {error}" + NOTHING_SIGNED)
    def test_a_failed_get_policy_after_a_good_get_agent_is_refused(self):
        from mcp import server

        class Client:
            def __init__(self, error):
                self.error, self.calls = error, []

            def call_function(self, contract, method, args):
                self.calls.append((contract, method, args))
                if method == "get_policy":
                    raise self.error
                return ALLOW_POLICY

        for error in (rpc.RpcError({"name": "HANDLER_ERROR"}), rpc.TransportError("timed out"),
                      rpc.MalformedResponse("the call_function result holds no byte list")):
            with self.subTest(error=type(error).__name__):
                client = Client(error)
                with self.assertRaises(server.ToolError) as raised:
                    server._live_policy(client, LIVE_GATE, self.agent_pub)
                self.assertEqual(str(raised.exception),
                                 f"cannot read Gate policy on {LIVE_GATE}: {error}" + NOTHING_SIGNED)
                self.assertEqual(client.calls, [(LIVE_GATE, "get_agent", {"public_key": self.agent_pub}),
                                                (LIVE_GATE, "get_policy", {})])


if __name__ == "__main__":
    unittest.main()
