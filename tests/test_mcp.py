import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = ROOT / "fixtures" / "evidence"
ALLOW_POLICY = {"allowlist": ["intents.near"], "caps": [{"asset": {"Ft": "wrap.near"}}]}
USDC_BASE_ASSET = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
DEMO_RECIPIENT = "0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107"
ONE_NEAR = "1000000000000000000000000"
KEPT = ("PATH", "SYSTEMROOT", "HOME", "LANG")


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

    def run_server(self, data):
        """The raw stdout bytes and stderr text of one server process fed the bytes."""
        env = {key: value for key, value in os.environ.items() if key in KEPT}
        env.update({"GATE_FIXTURES_MODE": "replay", "GATE_ACCOUNT_ID": "gate.near",
                    "GATE_POLICY": json.dumps(ALLOW_POLICY), "TMPDIR": self.tmp, "TEMP": self.tmp, "TMP": self.tmp})
        process = subprocess.run([sys.executable, "-m", "mcp.server"], input=data, capture_output=True, cwd=ROOT,
                                 env=env, timeout=120, check=False)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))
        return process.stdout, process.stderr.decode("utf-8", "replace")

    def session(self, messages, framed=False):
        bodies = [json.dumps(message).encode("utf-8") for message in messages]
        if framed:
            return self.run_server(b"".join(b"Content-Length: %d\r\n\r\n" % len(body) + body for body in bodies))
        return self.run_server(b"".join(body + b"\n" for body in bodies))

    def replies(self, messages):
        stdout, _ = self.session(messages)
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
        self.assertEqual(verdict["verdict"]["evidence_hash"], verdict["evidence_hash"])
        self.assertTrue(verdict["message"].startswith("GATEv1|gate.near|"))
        self.assertTrue(Path(verdict["evidence_path"]).is_relative_to(self.tmp))
        self.assertIn("TEST verifier key", verdict["signer"])

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

    def test_a_line_that_is_not_json_is_a_parse_error(self):
        stdout, _ = self.session([])
        self.assertEqual(stdout, b"")
        stdout, _ = self.run_server(b"not json\n[1]\n")
        first, second = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual((first["id"], first["error"]["code"]), (None, -32700))
        self.assertEqual((second["id"], second["error"]["code"]), (None, -32600))


if __name__ == "__main__":
    unittest.main()
