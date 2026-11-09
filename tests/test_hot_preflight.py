import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import ed25519, intents, rpc
from integrations.hot.preflight import preflight_intents, preflight_near
from intel import labels


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


class HotPreflightTests(unittest.TestCase):
    def test_near_and_intents_preflights_replay_unsigned_verdicts(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch("urllib.request.urlopen", side_effect=no_network):
            near = preflight_near(
                "ft", "wrap.near", "intents.near", "1000", None, gate="gate.near",
                agent_pk_hex=ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex(),
                policy={"allowlist": ["intents.near"], "caps": [{"asset": {"Ft": "wrap.near"}}]},
                store=labels.load(), rpc=rpc.Client(rpc.MAINNET_RPC_URL, "replay"),
                nearblocks=rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http"), evidence_dir=Path(temp),
            )
            intents_result = preflight_intents(
                "nep141:wrap.near",
                "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near",
                "1000000000000000000000000", "0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107",
            )

        self.assertEqual(near["outcome"], "ALLOW")
        self.assertEqual(near["action"], "sign")
        self.assertIsNone(near["verdict"])
        self.assertIsNone(near["message"])
        self.assertEqual(intents_result["verdict"], "DENY")
        self.assertTrue(intents_result["evidence_hash"])


if __name__ == "__main__":
    unittest.main()
