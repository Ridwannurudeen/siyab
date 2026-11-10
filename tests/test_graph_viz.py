import re
import shutil
import subprocess
import unittest
from collections import Counter
from pathlib import Path

from intel import render, trace


ROOT = Path(__file__).resolve().parent.parent


class GraphVizTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_exact_values_time_filtering_and_untrusted_view_parameters(self):
        result = subprocess.run(
            [shutil.which("node"), "tests/graph_viz_runtime.cjs"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_evidence_pagination_retains_every_transfer_once_and_full_context(self):
        name, document = render._flows()[0]
        counts = Counter()
        number_of_pages = (len(document["hops"]) + 49) // 50
        for number in range(1, number_of_pages + 1):
            page = render.flow_page(document, {}, evidence_url=f"/flows/{name}.html", page_number=number)
            cards = re.findall(r'<div class="card flow-transfer">(.*?)</div>', page, re.DOTALL)
            self.assertLessEqual(len(cards), 50)
            self.assertIn(f"Page {number} of {number_of_pages}", page)
            self.assertIn(document["fetched_at"], page)
            self.assertIn("#transfers", page)
            self.assertIn("Shares of the gross outbound transfer value", page)
            for card in cards:
                match = re.search(r'transaction ([^<]+)<br>receipt ([^<]+)', card)
                self.assertIsNotNone(match)
                counts[match.groups()] += 1
        expected = Counter((hop["transaction_hash"], hop["receipt_id"]) for hop in document["hops"])
        self.assertEqual(counts, expected)
        self.assertEqual(sum(counts.values()), len(document["hops"]))
        self.assertEqual(len(trace.hop_depths(document)), len(document["hops"]))
        with self.assertRaises(ValueError):
            render.flow_page(document, {}, evidence_url=f"/flows/{name}.html", page_number=number_of_pages + 1)

    def test_timeline_exposes_exact_intervals_and_keyboard_readouts(self):
        document = render._flows()[0][1]
        chart = render._flow_analytics(document, {})
        bins = render._flow_timeline(document)
        self.assertEqual(chart.count('data-bin-index='), len(bins))
        self.assertIn('class="chart-readout" role="status"', chart)
        self.assertIn('data-chart-inspect', chart)
        for row in bins:
            self.assertIn(f'data-chart-start="{row["from_ns"]}" data-chart-end="{row["to_ns"]}"', chart)
            self.assertIn(f'data-chart-count="{row["count"]}"', chart)
        self.assertIn('Full recorded dataset, not the current graph filter.', chart)
