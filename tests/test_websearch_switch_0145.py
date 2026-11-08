"""0.14.5 Setup › AI › Web search: the On/Off switch.

0.14.4 showed "Off  Off" (the switch reads its own state, and the row's caption was the state again) and, with no
address saved, the switch was disabled with no message, so pressing it did nothing. Now the row names the setting,
the switch is never disabled, On with no address focuses the field and says "Add your SearXNG address first.", and an
address typed in the field is saved and checked, with web search turning on only when the Check passes.

The static checks always run; the page test clicks the real page in Chromium (Playwright under node) against the real
server with a fake SearXNG (no network), and reads the stored setting after each click. It is skipped when node or
Playwright isn't installed (PLAYWRIGHT_NODE_PATH, else NODE_PATH, else /home/node/.local/opt/playwright/node_modules)."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path.insert(0, str(APP))
import api  # noqa: E402
import net  # noqa: E402
import server  # noqa: E402
import websearch as W  # noqa: E402

JS = (APP / "static" / "app.js").read_text(encoding="utf-8")
OK = "http://192.168.10.157:30053"
BAD = "http://192.168.10.158:30053"


def ai_web_src():
    a = JS.index("function aiWeb()")
    return JS[a:JS.index("function wsAdvanced(", a)]


class Static(unittest.TestCase):
    def test_the_row_names_the_setting_not_its_state(self):
        src = ai_web_src()
        self.assertIn("h('span', { 'aria-hidden': 'true' }, 'Chat can search the web')", src)
        self.assertNotIn(": 'Off')", src)

    def test_the_switch_is_never_disabled_and_says_why(self):
        src = ai_web_src()
        self.assertNotIn("s.disabled", src)
        self.assertIn("'Add your SearXNG address first.'", src)
        self.assertIn("url.focus()", src)

    def test_no_switch_row_in_setup_ai_repeats_the_state(self):
        """Setup › AI's switch rows (Chat, Web search, the writer's) name the setting; the switch shows Off / On."""
        for m in re.finditer(r"h\('div', \{ class: 'swrow' \}, \[\w+, h\('span', \{ 'aria-hidden': 'true' \}, ([^)]*)\)", JS):
            self.assertNotRegex(m.group(1), r"\?\s*'[^']*'\s*:\s*'Off'", m.group(0))


class FakeSearx:
    """SearXNG for the page test: answers at OK, refuses anything else (no network)."""
    def __init__(self):
        self.seen = []

    def search(self, base, query):
        self.seen.append((base, query))
        if base != OK:
            raise net.NetError("Callsheet couldn't reach your SearXNG. Check that it's running.", 0, "unreachable")
        return {"results": [{"title": "Test", "url": "https://example.org/test", "content": "A test page."}]}


def _playwright():
    for d in (os.environ.get("PLAYWRIGHT_NODE_PATH"), *(os.environ.get("NODE_PATH") or "").split(os.pathsep),
              "/home/node/.local/opt/playwright/node_modules"):
        if d and (pathlib.Path(d) / "playwright").is_dir():
            return d
    return None


PW = _playwright()
LIBS = ["/home/node/.local/opt/chromium-libs/root/usr/lib/x86_64-linux-gnu",
        "/home/node/.local/opt/chromium-libs/root/lib/x86_64-linux-gnu"]

# The steps run in one page; after each, the page reads api/web-search (the stored setting) and the switch row.
CLICKS = r"""
const { chromium } = require('playwright');
const url = process.argv[2], OK = process.argv[3], BAD = process.argv[4];
(async () => {
  const b = await chromium.launch(); const out = { steps: [], errs: [] };
  try {
    const p = await (await b.newContext({ viewport: { width: 400, height: 860 } })).newPage(); p.setDefaultTimeout(8000);
    p.on('pageerror', e => out.errs.push(String(e)));
    await p.addInitScript(() => { localStorage.setItem('callsheet.prefs', JSON.stringify({ look: 'jam', mode: 'light', autoSource: 'device' }));
      localStorage.setItem('callsheet.looknote', '1'); });
    await p.goto(url + '#setup/ai');
    await p.waitForSelector('[data-fk="ws-on"]', { timeout: 15000 });
    const step = async name => {
      await p.waitForFunction(() => { const s = document.querySelector('[data-fk="ws-on"]'); return s && !s.hasAttribute('aria-busy'); });
      await p.waitForTimeout(150);
      out.steps.push(Object.assign({ name }, await p.evaluate(u => fetch(u + 'api/web-search', { headers: { 'X-Callsheet': '1' } }).then(r => r.json()).then(v => {
        const s = document.querySelector('[data-fk="ws-on"]');
        return { on: v.on, base_url: v.base_url, pressed: s.getAttribute('aria-pressed'), disabled: s.disabled,
          row: s.closest('.swrow').innerText.replace(/\s+/g, ' ').trim(), before: getComputedStyle(s, '::before').content,
          focus: document.activeElement ? document.activeElement.id : '',
          err: [...document.querySelectorAll('.aiweb .err')].map(e => e.textContent).join('|'),
          meta: (document.querySelector('.aiweb .wsmeta') || {}).textContent || '' };
      }), url)));
    };
    await step('fresh');
    await p.click('[data-fk="ws-on"]'); await step('on, no address');
    await p.fill('#ws-url', BAD); await p.click('[data-fk="ws-on"]'); await step('on, check fails');
    await p.fill('#ws-url', OK); await p.click('[data-fk="ws-on"]'); await step('on, check passes');
    await p.click('[data-fk="ws-on"]'); await step('off');
    await p.click('[data-fk="ws-on"]'); await step('on again');
  } catch (e) { out.fatal = String(e); }
  await b.close(); console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node") and PW, "node or Playwright is not installed")
class Page(unittest.TestCase):
    def test_clicking_the_switch_changes_the_stored_setting(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = api.App(tmp.name)
        self.addCleanup(app.close)
        sx = FakeSearx()
        app.web.searx = sx
        httpd = server.make_server("127.0.0.1", 0, app=app)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        script = pathlib.Path(tmp.name) / "clicks.js"
        script.write_text(CLICKS, encoding="utf-8")
        env = dict(os.environ, NODE_PATH=PW)
        env["LD_LIBRARY_PATH"] = os.pathsep.join([*LIBS, env.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)
        r = subprocess.run(["node", str(script), f"http://127.0.0.1:{httpd.server_address[1]}/", OK, BAD],
                           capture_output=True, text=True, timeout=120, env=env, cwd=tmp.name)
        lines = [x for x in r.stdout.splitlines() if x.startswith("{")]
        self.assertTrue(lines, r.stderr[-2000:])
        out = json.loads(lines[-1])
        if "Executable doesn't exist" in (out.get("fatal") or "") or "error while loading shared" in (out.get("fatal") or ""):
            self.skipTest("Chromium for Playwright is not installed")
        self.assertNotIn("fatal", out, out.get("fatal"))
        self.assertEqual(out["errs"], [])
        st = {s["name"]: s for s in out["steps"]}
        for s in out["steps"]:   # one label: the setting's name; the switch alone shows Off / On
            self.assertEqual(s["row"], "Chat can search the web", s["name"])
            self.assertFalse(s["disabled"], s["name"])
            self.assertEqual(s["pressed"], "true" if s["on"] else "false", s["name"])
            self.assertIn("On" if s["on"] else "Off", s["before"], s["name"])
        f = st["fresh"]
        self.assertEqual((f["on"], f["base_url"]), (False, ""))
        n = st["on, no address"]
        self.assertEqual((n["on"], n["base_url"], n["focus"], n["err"]), (False, "", "ws-url", "Add your SearXNG address first."))
        b = st["on, check fails"]
        self.assertEqual((b["on"], b["base_url"]), (False, BAD))
        self.assertIn("couldn't reach your SearXNG", b["meta"])
        o = st["on, check passes"]
        self.assertEqual((o["on"], o["base_url"], o["err"]), (True, OK, ""))
        self.assertIn("SearXNG answered with 1 result", o["meta"])
        self.assertEqual((st["off"]["on"], st["off"]["base_url"]), (False, OK))
        self.assertEqual(st["on again"]["on"], True)
        self.assertEqual([q for _, q in sx.seen], ["test", "test"])   # a passed Check isn't repeated
        saved = W.load(app.store)
        self.assertEqual((saved["on"], saved["base_url"], saved["check_ok"]), (True, OK, True))


if __name__ == "__main__":
    unittest.main()
