import contextlib
import io
import json
import unittest
from unittest.mock import patch

from intel import issuers, labels


class IssuerRowsTest(unittest.TestCase):
    def test_status_rows_cover_only_the_named_sets_and_carry_blocks(self):
        store_subjects = {a.subject for a in labels.load().assertions
                          if a.chain == "near" and a.superseded_by is None
                          and a.category in issuers.STATUS_CATEGORIES}
        receiver_subjects = {item["subject"] for item in json.loads(
            issuers.RECEIVERS_PATH.read_text(encoding="utf-8"))["subjects"]}
        reads = issuers.reads()
        self.assertEqual({read["subject"] for read in reads}, store_subjects | receiver_subjects)
        self.assertEqual(len(reads), len(store_subjects | receiver_subjects) * len(issuers.issuer_entries()))
        self.assertTrue(all(isinstance(read["block"], int) and read["block"] > 0 for read in reads))
        self.assertTrue(all("answer" in read for read in reads))

    def test_a_banned_status_makes_a_watch_row_never_a_denylist_row(self):
        rows = issuers.rows()
        reads = {(read["subject"], read["method"]): read for read in issuers.reads()}
        for row in rows:
            self.assertEqual(row["category"], "watch")
            self.assertEqual(row["provenance"], "onchain")
            read = reads[(row["subject"], row["source"]["method"])]
            self.assertIn(json.dumps(read["answer"], ensure_ascii=False), row["label"])
            self.assertIn(str(read["block"]), row["label"])
        self.assertFalse(any(row["category"] == "denylist" for row in rows))

    def test_rows_rebuild_from_the_fixtures(self):
        self.assertEqual(issuers.STATUS_PATH.read_text(encoding="utf-8"), issuers.rows_jsonl())

    def test_append_command_delegates_to_seed_append(self):
        rows = [{"id": "fixture row"}]
        output = io.StringIO()
        with patch.object(issuers, "rows", return_value=rows), \
                patch.object(issuers.seed, "append", return_value=1) as append, \
                contextlib.redirect_stdout(output):
            self.assertEqual(issuers.main(["append"]), 0)
        append.assert_called_once_with(rows)
        self.assertIn("1 issuer status rows appended", output.getvalue())


if __name__ == "__main__":
    unittest.main()
