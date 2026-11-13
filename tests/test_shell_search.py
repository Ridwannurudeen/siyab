import shutil
import subprocess
import unittest
from pathlib import Path


class ShellSearchTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_live_account_result_preserves_recorded_matches_and_caution(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) {this.tag = tag; this.children = []; this.listeners = {}; this.textContent = ''; this.value = '';}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children = children;}
  addEventListener(name, callback) {this.listeners[name] = callback;}
  setAttribute(name, value) {this[name] = value;}
  querySelectorAll(tag) {return this.children.flatMap(child => [...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)]);}
  querySelector(tag) {return this.querySelectorAll(tag)[0] || null;}
  focus() {}
  click() {this.clicked = true;}
}
async function main() {
  const nodes = new Map(), document = new Element('document');
  document.documentElement = {dataset: {}};
  document.getElementById = id => {if (!nodes.has(id)) nodes.set(id, new Element('div')); return nodes.get(id);};
  document.createElement = tag => new Element(tag); document.querySelectorAll = () => [];
  vm.runInNewContext(fs.readFileSync('intel/ui/shell.js', 'utf8'), {
    document, localStorage: {getItem: () => null, setItem() {}}, location: {search: ''}, URLSearchParams,
    matchMedia: () => ({matches: false}),
    fetch: async () => ({ok: true, json: async () => ({accounts: [{subject: 'alice.near', labels: ['Recorded label'], url: 'accounts/alice.near.html'}], entities: []})})
  });
  await new Promise(resolve => setImmediate(resolve));
  const input = nodes.get('global-search-input'), results = nodes.get('global-search-results');
  input.value = 'alice.near'; input.listeners.input();
  let links = results.querySelectorAll('a');
  assert.equal(links.length, 2); assert.equal(links[0].href, '/investigate.html?account=alice.near');
  assert.equal(links[0].children[0].textContent, 'Investigate alice.near');
  assert.equal(links[1].href, '/accounts/alice.near.html', 'Recorded matches remain accessible');
  document.activeElement = input;
  nodes.get('global-search').listeners.keydown({key: 'Enter', preventDefault() {}});
  assert.equal(links[0].clicked, true, 'Enter opens the first investigation result');
  input.value = 'new.near'; input.listeners.input();
  assert.match(results.children.find(child => child.tag === 'p').textContent, /No recorded match.*does not imply a favorable payment result/);
  assert.equal(results.querySelectorAll('a')[0].href, '/investigate.html?account=new.near');
  for (const query of ['testnet', 'alice.testnet', 'bad..near', '-bad.near', 'bad_.near', '<img src=x>', 'a'.repeat(65), 'a']) {
    input.value = query; input.listeners.input();
    assert.equal(results.querySelectorAll('a').filter(link => link.href.startsWith('/investigate.html')).length, 0, query);
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run([shutil.which("node"), "-e", script],
                                cwd=Path(__file__).resolve().parent.parent, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
