import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gate import rpc
from intel import labels, registries, seed


class RegistriesTests(unittest.TestCase):
    def setUp(self):
        self.by_registry = registries.rows()
        self.all_rows = [row for group in self.by_registry.values() for row in group]

    def _call_function_fixture(self, contract, method):
        args = {}
        encoded = json.dumps(args, sort_keys=True, separators=(",", ":")).encode("utf-8")
        params = {
            "request_type": "call_function",
            "finality": "final",
            "account_id": contract,
            "method_name": method,
            "args_base64": base64.b64encode(encoded).decode("ascii"),
        }
        key = rpc.fixture_key(rpc.MAINNET_RPC_URL, "query", params)
        fixture = json.loads((registries.FIXTURES_DIR / f"{key}.json").read_text(encoding="utf-8"))
        result = fixture["response"]["result"]
        return json.loads(bytes(result["result"]))

    def test_every_dao_row_is_onchain_with_the_list_method_and_a_block(self):
        dao_rows = self.by_registry["sputnik_dao"]
        self.assertTrue(dao_rows)
        for row in dao_rows:
            self.assertEqual(row["category"], "organisation")
            self.assertEqual(row["provenance"], "onchain")
            self.assertEqual(row["source"]["method"], "get_dao_list")
            self.assertGreater(row["source"]["block"], 0)

    def test_registry_entity_names_quote_account_names(self):
        for row in self.by_registry["sputnik_dao"]:
            first_label = row["subject"].removesuffix(".sputnik-dao.near")
            self.assertEqual(row["entity"], f"Sputnik DAO: {seed._quoted(first_label)}")

        for row in self.by_registry["validators"]:
            self.assertEqual(row["entity"], f'validator {seed._quoted(row["subject"])}')

    def test_validator_rows_name_the_epoch_and_carry_a_block(self):
        validator_rows = self.by_registry["validators"]
        self.assertTrue(validator_rows)
        for row in validator_rows:
            self.assertEqual(row["category"], "organisation")
            self.assertEqual(row["label"], f"current validator at epoch {row['source']['block']}")
            self.assertGreater(row["source"]["block"], 0)
            self.assertEqual(row["source"]["method"], "validators")

    def test_whitelist_rows_are_catalog_tokens_from_the_ref_read(self):
        token_rows = self.by_registry["ref_whitelist"]
        self.assertTrue(token_rows)
        for row in token_rows:
            self.assertEqual(row["category"], "catalog_token")
            self.assertEqual(row["source"]["method"], "get_whitelisted_tokens")
            self.assertEqual(row["source"]["args"], {})
            self.assertGreater(row["source"]["block"], 0)

    def test_no_registry_row_is_incident_or_denylist(self):
        self.assertTrue(self.all_rows)
        self.assertFalse(any(row["category"] in ("incident", "denylist") for row in self.all_rows))
        for row in self.all_rows:
            labels.validate(row)

    def test_rows_rebuild_identically_from_the_recorded_reads(self):
        first = registries.rows()
        second = registries.rows()
        self.assertEqual(first, second)
        recorded = [json.loads(line) for line in registries.ROWS_PATH.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(recorded, [row for group in first.values() for row in group])

    def test_entities_cover_every_organisation_row(self):
        entity_rows = registries.entities(self.by_registry)
        recorded_entities = [
            json.loads(line) for line in registries.ENTITIES_PATH.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(entity_rows, recorded_entities)
        members = {member for entity in entity_rows if entity["kind"] in ("dao", "validator")
                   for member in entity["members"]}
        organisation_subjects = {row["subject"] for row in self.all_rows if row["category"] == "organisation"}
        self.assertEqual(organisation_subjects, members)

    def test_the_count_per_registry_matches_the_recorded_answer(self):
        dao_count = self._call_function_fixture("sputnik-dao.near", "get_number_daos")
        dao_accounts = self._call_function_fixture("sputnik-dao.near", "get_dao_list")
        whitelist = self._call_function_fixture("v2.ref-finance.near", "get_whitelisted_tokens")
        bridge_accounts = self._call_function_fixture("factory.bridge.near", "get_tokens_accounts")
        validators_key = rpc.fixture_key(rpc.MAINNET_RPC_URL, "validators", [None])
        validators_fixture = json.loads(
            (registries.FIXTURES_DIR / f"{validators_key}.json").read_text(encoding="utf-8")
        )
        validators = validators_fixture["response"]["result"]["current_validators"]
        self.assertEqual(len(self.by_registry["sputnik_dao"]), dao_count)
        self.assertEqual(len(self.by_registry["sputnik_dao"]), len(dao_accounts))
        self.assertEqual(len(self.by_registry["validators"]), len(validators))
        self.assertEqual(len(self.by_registry["ref_whitelist"]), len(whitelist))
        self.assertEqual(len(self.by_registry["bridge_factory"]), len(bridge_accounts))

    def test_a_missing_fixture_stops_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture_dir = Path(temporary) / "fixtures"
            output_dir = Path(temporary) / "output"
            client = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=fixture_dir)
            with patch.object(registries, "_client", return_value=client), \
                    patch.object(registries, "ROWS_PATH", output_dir / "rows.jsonl"), \
                    patch.object(registries, "ENTITIES_PATH", output_dir / "entities.jsonl"), \
                    self.assertRaises(rpc.FixtureMiss):
                registries.record()
            self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
