import hashlib
import json
import unittest

from intel import graph, render


class GraphLoadingTest(unittest.TestCase):
    def test_theme_label_is_present_before_javascript_initialization(self):
        page = render.page('Feed', '<h1>Feed</h1>')
        button = page.split('id="theme-toggle"', 1)[1].split('</button>', 1)[0]
        self.assertIn('aria-label="Switch to light theme"', button)
        self.assertIn('<span class="theme-toggle-label">Use light theme</span>', button)

    def test_graph_asset_is_repeatable_without_mutating_its_input(self):
        payload = graph.flow_payload(render._flows()[0][1], {'wrap.near': 'accounts/wrap.near.html'})
        before = json.dumps(payload, sort_keys=True)
        first = render._graph_asset(payload, '/flows/rhea-collector.html')
        second = render._graph_asset(payload, '/flows/rhea-collector.html')
        self.assertEqual(first[0], second[0])
        self.assertTrue(before == json.dumps(payload, sort_keys=True), 'Asset generation changed its input')

    def test_graph_shell_does_not_embed_the_recorded_dataset(self):
        document = render._flows()[0][1]
        payload = graph.flow_payload(document, {})
        section = render._graph_section(payload, '/flows/rhea-collector.html')
        self.assertIn('data-graph-src="/graph-data/', section)
        self.assertIn('Recorded investigation', section)
        self.assertIn('<noscript>', section)
        self.assertNotIn('"records":', section)
        self.assertLess(len(section.encode('utf-8')), 60000)

    def test_graph_asset_hash_covers_exact_bytes_and_retains_evidence(self):
        payload = graph.flow_payload(render._flows()[0][1], {'wrap.near': 'accounts/wrap.near.html'})
        url, encoded = render._graph_asset(payload, '/flows/rhea-collector.html')
        self.assertEqual(url, '/graph-data/' + hashlib.sha256(encoded.encode('utf-8')).hexdigest() + '.json')
        saved = json.loads(encoded)
        self.assertEqual(saved['total_records'], len(saved['records']))
        self.assertEqual(saved['source']['evidence_url'], '/flows/rhea-collector.html')
        self.assertEqual(saved['records'], payload['records'])
        self.assertTrue(all(node['url'] is None or node['url'].startswith('/accounts/') for node in saved['nodes']))
