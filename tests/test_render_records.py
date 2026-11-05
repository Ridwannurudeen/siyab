"""The incident desk pages: rendered from the committed records, and committed as bytes a reader can diff.

The pages carry no number that is not computed, and the action beside a record comes from
records.verdict_for, so a page cannot say a payment blocks when the signing paths would ask.
"""

import tempfile
import unittest
from pathlib import Path

from intel import records, render_records

ROOT = Path(__file__).resolve().parent.parent
COMMITTED = ROOT / "demo" / "records"


def render_to_temp():
    with tempfile.TemporaryDirectory() as directory:
        written = render_records.render_records(directory)
        pages = {relative: (Path(directory) / relative).read_bytes() for relative in written}
    return written, pages


class ReproducibleTest(unittest.TestCase):
    def test_the_committed_pages_are_a_fresh_render(self):
        written, pages = render_to_temp()
        committed = sorted(path.relative_to(COMMITTED).as_posix()
                           for path in COMMITTED.rglob("*") if path.is_file())
        self.assertEqual(committed, sorted(written))
        for relative, content in pages.items():
            with self.subTest(page=relative):
                self.assertEqual((COMMITTED / relative).read_bytes(), content)

    def test_every_committed_record_has_a_page(self):
        written, _ = render_to_temp()
        for record in render_records.load():
            with self.subTest(record=record["record_id"]):
                self.assertIn(f"records/{record['record_id']}.html", written)


class ActionTest(unittest.TestCase):
    def test_an_accepted_report_of_a_code_kind_blocks(self):
        for record in render_records.load():
            if record["state"] == "accepted" and record["kind"] in records.DENY_KINDS:
                with self.subTest(record=record["record_id"]):
                    self.assertEqual(render_records.action_for(record), "blocked")

    def test_an_accepted_social_report_asks(self):
        social = [record for record in render_records.load() if record["kind"] == "social"]
        self.assertTrue(social)
        for record in social:
            with self.subTest(record=record["record_id"]):
                self.assertEqual(render_records.action_for(record), "asks a human")

    def test_a_pending_report_only_asks_and_never_blocks(self):
        pending = [record for record in render_records.load() if record["state"] == "pending"]
        self.assertTrue(pending)
        for record in pending:
            with self.subTest(record=record["record_id"]):
                page = render_records.record_page(record)
                self.assertIn("asks a human", page)
                self.assertNotIn("<strong>blocked</strong>", page)


class PageTest(unittest.TestCase):
    def setUp(self):
        self.loaded = render_records.load()
        self.index = render_records.index_page(self.loaded)

    def test_the_index_lists_every_subject_with_its_state_and_action(self):
        for record in self.loaded:
            for subject in record["subjects"]:
                with self.subTest(subject=subject):
                    self.assertIn(subject, self.index)
        self.assertIn(f"{len(self.loaded)} records", self.index)

    def test_the_index_says_a_subject_is_never_a_victim(self):
        self.assertIn("never a victim", self.index)

    def test_a_record_page_shows_the_timeline_evidence_and_reason(self):
        for record in self.loaded:
            with self.subTest(record=record["record_id"]):
                page = render_records.record_page(record)
                for entry in record["timeline"]:
                    self.assertIn(entry["at"], page)
                for entry in record["evidence"]:
                    self.assertIn(entry["sha256"], page)
                    self.assertIn(entry["url"].replace("&", "&amp;"), page)
                for entry in record["moderation"]:
                    self.assertIn(entry["reason"], page)

    def test_a_record_page_navigates_one_directory_up(self):
        for record in self.loaded:
            with self.subTest(record=record["record_id"]):
                page = render_records.record_page(record)
                self.assertIn('<a href="../index.html">Home</a>', page)
                self.assertIn('<a href="../feed.html">Feed</a>', page)

    def test_the_index_navigates_from_the_site_root(self):
        self.assertIn('<a href="index.html">Home</a>', self.index)
        self.assertNotIn("../index.html", self.index)

    def test_a_page_carries_no_external_script(self):
        for record in self.loaded:
            page = render_records.record_page(record)
            self.assertNotIn("<script", page)

    def test_a_superseding_page_names_the_record_it_replaces(self):
        original = self.loaded[0]
        correction = dict(original, supersedes=original["record_id"], record_id="0" * 64)
        page = render_records.record_page(correction)
        self.assertIn(f"Supersedes <code>{original['record_id']}</code>", page)
