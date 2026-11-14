import copy
import json
import unittest
from pathlib import Path

from gate import rpc
from gate.txcheck import check_transaction
from gate.txfetch import fetch_transaction
from intel import accounts, labels

ROOT = Path(__file__).resolve().parent.parent
RECORDED = ROOT / "fixtures/issuers/0e842edffd5a07ad0c9d66bc5b386d96ca836417eab18956a2cdefbb0f7c064f.json"


class StubClient:
    def __init__(self, response):
        self.response, self.posts = response, []

    def post(self, url, body):
        self.posts.append((url, body))
        return self.response


def recorded_transaction():
    """One recorded mainnet transaction, cut from a 20 hash response, with its hash."""
    item = json.loads(RECORDED.read_text(encoding="utf-8"))["response"]["transactions"][0]
    return item, item["transaction"]["hash"]


class FetchTransactionTest(unittest.TestCase):
    def test_recorded_mainnet_response_yields_actions(self):
        item, tx_hash = recorded_transaction()
        # The fixture key covers all 20 hashes, so a one hash request misses replay; a stub serves the cut.
        replay = rpc.Client(accounts.TX_URL, "replay", kind="http", fixtures_dir=RECORDED.parent)
        with self.assertRaises(rpc.FixtureMiss):
            replay.post("/v0/transactions", {"tx_hashes": [tx_hash]})
        client = StubClient({"transactions": [item]})
        transaction = fetch_transaction(tx_hash, client=client)
        self.assertEqual(client.posts, [("/v0/transactions", {"tx_hashes": [tx_hash]})])
        self.assertEqual(transaction, {"signer_id": item["transaction"]["signer_id"],
                                       "receiver_id": "tether.multisafe.near",
                                       "actions": item["transaction"]["actions"]})
        answer = check_transaction(transaction, store=labels.load())
        self.assertEqual(answer["signing"], "never")
        self.assertIsNone(answer["verdict"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_hash_mismatch_or_count_is_malformed(self):
        item, tx_hash = recorded_transaction()
        other = copy.deepcopy(item)
        other["transaction"]["hash"] = "1" * 32
        partial = copy.deepcopy(item)
        del partial["transaction"]["actions"]
        for response in ({"transactions": [other]}, {"transactions": []}, {"transactions": [item, item]},
                         {"transactions": [partial]}, {"transactions": [{"transaction": None}]}, {}, [item], None):
            with self.subTest(response=str(response)[:60]):
                with self.assertRaises(rpc.MalformedResponse):
                    fetch_transaction(tx_hash, client=StubClient(response))

    def test_bad_hash_never_reaches_the_client(self):
        _, tx_hash = recorded_transaction()
        for value in ("", "wrap.near", tx_hash + "1", tx_hash.replace(tx_hash[0], "0"),
                      "1" + tx_hash, "1" * 31, "z" * 45, None, 7):
            with self.subTest(value=value):
                client = StubClient({"transactions": []})
                with self.assertRaises(ValueError):
                    fetch_transaction(value, client=client)
                self.assertEqual(client.posts, [])


if __name__ == "__main__":
    unittest.main()
