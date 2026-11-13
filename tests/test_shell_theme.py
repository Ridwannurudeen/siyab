import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ThemeControlTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_compact_theme_control_keeps_name_and_choice_after_each_toggle(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.children = []; this.listeners = {}; this.textContent = ''; }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this[name] = value; }
}
for (const storageFails of [false, true]) {
  for (const systemLight of [true, false]) {
    for (const stored of [null, 'light', 'dark']) {
      const nodes = new Map();
      const document = new Element();
      document.documentElement = {dataset: {}};
      document.getElementById = id => {
        if (!nodes.has(id)) nodes.set(id, new Element());
        return nodes.get(id);
      };
      document.createElement = () => new Element();
      const metas = [new Element(), new Element()];
      document.querySelectorAll = selector => selector === 'meta[name="theme-color"]' ? metas : [];
      const saved = [];
      const localStorage = {
        getItem() { if (storageFails) throw new Error('storage unavailable'); return stored; },
        setItem(key, value) { if (storageFails) throw new Error('storage unavailable'); saved.push([key, value]); }
      };
      vm.runInNewContext(fs.readFileSync('intel/ui/shell.js', 'utf8'), {
        document, localStorage, matchMedia: () => ({matches: systemLight}), location: {search: ''}, URLSearchParams,
        fetch: () => new Promise(() => {})
      });
      const toggle = nodes.get('theme-toggle');
      const label = toggle.children.find(child => child.className === 'theme-toggle-label');
      assert.ok(label, 'The compact control needs a separate label so mobile can retain an icon without clipping its text');
      const light = !storageFails && stored === 'light';
      assert.equal(document.documentElement.dataset.theme, light ? 'light' : 'dark');
      for (const meta of metas) assert.equal(meta.content, light ? '#F5F7FA' : '#0A0D16');
      assert.equal(label.textContent, light ? 'Use dark theme' : 'Use light theme');
      assert.equal(toggle['aria-label'], light ? 'Switch to dark theme' : 'Switch to light theme');
      toggle.listeners.click();
      assert.equal(document.documentElement.dataset.theme, light ? 'dark' : 'light');
      for (const meta of metas) assert.equal(meta.content, light ? '#0A0D16' : '#F5F7FA');
      assert.equal(label.textContent, light ? 'Use light theme' : 'Use dark theme');
      assert.equal(toggle['aria-label'], light ? 'Switch to light theme' : 'Switch to dark theme');
      toggle.listeners.click();
      assert.equal(document.documentElement.dataset.theme, light ? 'light' : 'dark');
      for (const meta of metas) assert.equal(meta.content, light ? '#F5F7FA' : '#0A0D16');
      if (storageFails) assert.match(toggle.title, /storage is unavailable/);
      else assert.deepEqual(saved, [['siyab-theme', light ? 'dark' : 'light'], ['siyab-theme', light ? 'light' : 'dark']]);
    }
  }
}
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
