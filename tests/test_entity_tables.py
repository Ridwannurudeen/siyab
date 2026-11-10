import dataclasses
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from intel import labels, render


class EntityTablesTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_runtime_search_and_failure_boundaries(self):
        result = subprocess.run([shutil.which("node"), "tests/entity_tables_runtime.cjs"],
                                cwd=Path(__file__).resolve().parent.parent,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_collection_html_is_bounded_and_remaining_rows_have_static_pages(self):
        store = labels.load()
        rows = [row for row in store.assertions if row.category == "organisation" and row.superseded_by is None]
        with tempfile.TemporaryDirectory() as temporary:
            paths = render.render(temporary, store=store)
            root = Path(temporary)
            first = (root / "entities/organisations.html").read_text(encoding="utf-8")
            self.assertEqual(first.count('data-assertion-id='), 50)
            self.assertLess(len(first.encode()), 100000)
            dataset = json.loads((root / "entities/organisations.table.json").read_text(encoding="utf-8"))
            self.assertEqual({row["id"] for row in dataset["rows"]}, {row.id for row in rows})
            pages = [path for path in paths if re.fullmatch(r"entities/organisations(?:\.page-\d+)?\.html", path)]
            rendered_ids = []
            for path in pages:
                document = (root / path).read_text(encoding="utf-8")
                rendered_ids.extend(re.findall(r'data-assertion-id="([0-9a-f]+)"', document))
                self.assertLessEqual(document.count('data-assertion-id='), 50)
            self.assertCountEqual(rendered_ids, [row.id for row in rows])
            self.assertIn('href="organisations.page-2.html"', first)

    def test_table_headers_and_phone_cells_have_explicit_labels(self):
        document = render.entity_page("Treasuries", ("treasury",), labels.load())
        for column in ("Subject", "Chain", "Label", "Category", "Provenance", "Note"):
            self.assertIn(f'<th scope="col" role="columnheader">{column}</th>', document)
            self.assertIn(f'<span class="cell-label" aria-hidden="true">{column}</span>', document)
        self.assertIn('class="entity-table" role="table"', document)

    def test_descriptor_cells_keep_source_quotations_and_escape_markup(self):
        row = next(row for row in labels.load().assertions if row.category == "organisation")
        row = dataclasses.replace(row, label='label "<img src=x onerror=alert(1)>"')
        store = labels.Store([row], "0" * 64)
        document = render.entity_page("Organisations", ("organisation",), store)
        self.assertIn('<q class="data">&lt;img src=x onerror=alert(1)&gt;</q>', document)
        self.assertNotIn('<img src=x', document)


if __name__ == "__main__":
    unittest.main()
