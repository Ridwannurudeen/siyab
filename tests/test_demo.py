import contextlib
import io
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from gate import demo, rpc

DENIED = ("DENIED. vendor-pay.testnet is a lookalike of vendor.testnet. Created 3 h ago by a 0.01 NEAR deposit. "
          "Not on the owner's allowlist.")


def no_network(*args, **kwargs):
    raise AssertionError("the demo opened the network")


class DemoTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("urllib.request.urlopen", side_effect=no_network)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_demo(self):
        lines = []
        results = demo.run(out=lines.append)
        self.addCleanup(shutil.rmtree, Path(results[0]["evidence_path"]).parent)
        return results, lines

    def test_three_verdicts(self):
        results, lines = self.run_demo()
        self.assertEqual([r["outcome"] for r in results], ["ALLOW", "DENY", "UNKNOWN"])
        self.assertIsNotNone(results[0]["verdict"])
        self.assertIsNone(results[1]["verdict"])
        self.assertIsNone(results[2]["verdict"])
        self.assertIn(f"  {DENIED}", lines)
        self.assertIn("  ALLOW, signed with the TEST key (key id cur); signature verified with gate.ed25519: True",
                      lines)
        self.assertIn("  UNKNOWN, not signed: account_age, dust_funded could not run", lines)
        self.assertEqual(sum(line.startswith("  evidence sha256: ") for line in lines), 3)
        self.assertEqual(sum(line.startswith("  latency: ") and line.endswith(" ms") for line in lines), 3)
        self.assertTrue(any(line.startswith("  canonical message: GATEv1|gate.testnet|") for line in lines))
        for result in results:
            self.assertTrue((Path(result["evidence_path"])).exists())

    def test_command_exit_codes(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(demo.main(offline=True), 0)
        self.addCleanup(shutil.rmtree, output.getvalue().splitlines()[-1].split(": ", 1)[1])
        self.assertIn(DENIED, output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(demo.main(offline=False), 2)
        with mock.patch("gate.verdict.request", side_effect=RuntimeError("broken")), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            demo.main(offline=True)

    def test_the_fixtures_are_synthetic_and_in_the_recorded_shape(self):
        paths = sorted(demo.DEMO_DIR.iterdir())
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                self.assertTrue(path.name.startswith("synthetic-") and path.suffix == ".json")
                fixture = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(sorted(fixture), ["fetched_at", "request", "response", "url"])
                self.assertTrue(fixture["url"].startswith((rpc.TESTNET_RPC_URL, rpc.NEARBLOCKS_TESTNET_URL)))


if __name__ == "__main__":
    unittest.main()
