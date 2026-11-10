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
  const nodes = new Map();
  const document = new Element();
  document.documentElement = {dataset: {}};
  document.getElementById = id => {
    if (!nodes.has(id)) nodes.set(id, new Element());
    return nodes.get(id);
  };
  document.createElement = () => new Element();
  document.querySelectorAll = () => [];
  const saved = [];
  const localStorage = {
    getItem() { if (storageFails) throw new Error('storage unavailable'); return 'light'; },
    setItem(key, value) { if (storageFails) throw new Error('storage unavailable'); saved.push([key, value]); }
  };
  vm.runInNewContext(fs.readFileSync('intel/ui/shell.js', 'utf8'), {
    document, localStorage, location: {search: ''}, URLSearchParams,
    fetch: () => new Promise(() => {})
  });
  const toggle = nodes.get('theme-toggle');
  const label = toggle.children.find(child => child.className === 'theme-toggle-label');
  assert.ok(label, 'The compact control needs a separate label so mobile can retain an icon without clipping its text');
  assert.equal(label.textContent, storageFails ? 'Use light theme' : 'Use dark theme');
  assert.equal(toggle['aria-label'], storageFails ? 'Switch to light theme' : 'Switch to dark theme');
  toggle.listeners.click();
  assert.equal(document.documentElement.dataset.theme, storageFails ? 'light' : 'dark');
  assert.equal(label.textContent, storageFails ? 'Use dark theme' : 'Use light theme');
  assert.equal(toggle['aria-label'], storageFails ? 'Switch to dark theme' : 'Switch to light theme');
  toggle.listeners.click();
  assert.equal(document.documentElement.dataset.theme, storageFails ? 'dark' : 'light');
  if (storageFails) assert.match(toggle.title, /storage is unavailable/);
  else assert.deepEqual(saved, [['siyab-theme', 'dark'], ['siyab-theme', 'light']]);
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
