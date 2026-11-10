import dataclasses
import html
import shutil
import subprocess
import unittest
from pathlib import Path

from intel import labels, render
from tests.test_render import forbidden, own_text


ROOT = Path(__file__).resolve().parent.parent


class RegistryOverviewTest(unittest.TestCase):
    def test_authored_investigation_scope_keeps_the_existing_word_rule(self):
        self.assertIsNone(forbidden(own_text(render.investigate_page())))

    def test_groups_are_exact_dated_reads_not_ownership_or_suffix_clusters(self):
        store = labels.load()
        groups = render._organisation_groups(store)
        self.assertEqual(sorted(group["assertions"] for group in groups), [374, 2060])
        self.assertEqual(sum(group["subjects"] for group in groups), 2434)
        self.assertEqual({group["source"]["method"] for group in groups}, {"get_dao_list", "validators"})
        self.assertTrue(all(group["observed_at"].startswith("2026-11-02T") for group in groups))
        rows = render._entity_rows(("organisation",), store)
        self.assertEqual({row["source_group"] for row in rows}, {group["id"] for group in groups})
        first = next(row for row in store.assertions if row.category == "organisation" and row.superseded_by is None)
        other = dataclasses.replace(first, id="f" * 64, subject="other.near", source={**first.source, "block": first.source["block"] + 1})
        self.assertEqual(len(render._organisation_groups(labels.Store([first, other], "a" * 64))), 2)

    def test_static_overview_preserves_every_existing_row_and_control(self):
        store = labels.load()
        page = render.entity_page("Organisations from on chain registries", ("organisation",), store)
        self.assertIn("2,434 assertions from 2 recorded registry reads", page)
        self.assertIn('id="individual-assertions"', page)
        self.assertIn('>Individual recorded assertions</summary>', page)
        self.assertEqual(page.count('data-assertion-id='), 50)
        self.assertIn('2434 current assertions from the label store.', page)
        for field in ('data-table-search', 'data-table-chain', 'data-table-sort', 'data-table-group', 'data-export-table="entity-collection-stated"'):
            self.assertIn(field, page)
        self.assertIn('href="organisations.page-2.html"', page)
        self.assertLess(len(page.encode()), 100000)
        self.assertNotIn('"rows":', page)
        original = next(row for row in store.assertions if row.category == "organisation" and row.superseded_by is None)
        self.assertIn('data-export-value="' + html.escape(original.note, quote=True) + '"', page)
        for group in render._organisation_groups(store):
            self.assertIn('?source=' + group['id'] + '#individual-assertions', page)
        later = render.entity_page("Organisations", ("organisation",), store, page_number=2)
        self.assertIn('id="individual-assertions" open', later)

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_group_filter_permalink_lazy_limits_and_export_rows(self):
        result = subprocess.run([shutil.which("node"), "tests/registry_groups_runtime.cjs"], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
