"""0.8.0 See everything, the page (app.js / app.css): everything renders as text, links come only from the server's
Google URL builders (rules.gmail_link, a Calendar event's htmlLink checked by gather.safe_cal_link) and pass gLink(),
invites found only in email never get a Calendar link and never sit in the timeline's calendar column (so never a
clash), Coming up posts to the relative api/horizon, and the 0.8.0 styles use theme tokens only. Stdlib; the gLink
behaviour checks run the real function under node when it is installed (skipped otherwise)."""
import json
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "callsheet" / "app" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
APP_CSS = (STATIC / "app.css").read_text(encoding="utf-8")
SINKS = r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function"
NEW_FNS = ("gLink", "extA", "mk8", "mks", "calGap", "gapCard")   # the rework: the old brief's invites, gaps and
GONE_FNS = ("invAnswer", "invCard", "invSection", "invPins", "goInvite", "gapHint", "hzMark", "nameMark")  # Coming up went


def fn(name):
    m = re.search(r"\n  function " + name + r"\(.*?\n  }\n", APP_JS, re.S)
    if m is None:
        raise AssertionError("no function " + name)
    return m.group(0)


def block_080():
    start = APP_JS.index("/* ---------- 0.8.0 shared pieces")
    end = APP_JS.index("/* ---------- 0.7.0 projects", start)
    return APP_JS[start:end]


class TextOnlyTest(unittest.TestCase):
    def test_no_html_sinks_in_the_080_code(self):
        self.assertNotRegex(block_080(), SINKS)
        for name in NEW_FNS:
            self.assertNotRegex(fn(name), SINKS, name)

    def test_no_url_is_built_on_the_page(self):
        b = block_080()
        self.assertNotIn("https://", b)
        self.assertNotIn("http://", b)
        self.assertNotIn("calendar.google", APP_JS)  # 0.5.0's rule: calendar URLs are the server's
        # every href is a gLink() result handed to extA()
        self.assertEqual(re.findall(r"href:\s*(\w+)", b), ["href"])
        for m in re.finditer(r"extA\('[^']*',\s*([\w.]+)", b):
            self.assertIn(m.group(1), ("ans", "open", "link"), m.group(0))
        self.assertIn("target: '_blank', rel: 'noopener noreferrer'", fn("extA"))

    def test_links_only_through_glink(self):
        self.assertIn("safeHttps(u)", fn("gLink"))
        for name in GONE_FNS:                           # the rework: the old brief's invites and Coming up are gone
            self.assertNotIn("function " + name + "(", APP_JS)
        for route in ("api/horizon", "api/done", "api/suppress", "api/sync", "api/eval", "api/writer/presets"):
            self.assertNotIn(route, APP_JS)                         # removed server routes: nothing calls them

class StyleTest(unittest.TestCase):
    def test_080_css_uses_theme_tokens_only(self):
        css = APP_CSS[APP_CSS.index("/* ================= 0.8.0 See everything"):]
        rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        self.assertNotRegex(rules, r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")
        self.assertNotRegex(rules, r"url\(")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class GLinkBehaviourTest(unittest.TestCase):
    def run_js(self, cases):
        src = "function safeHttps(u) { return typeof u === 'string' && /^https:/i.test(u) ? u : null; }\n" + \
            fn("gLink").strip() + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + \
            ".map(function (c) { return gLink(c[0], c[1]); })));"
        out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def test_glink(self):
        gm = "https://mail.google.com/mail/u/?authuser=me@example.com#all/thread-f:1"
        cal = "https://www.google.com/calendar/event?eid=abc"
        cases = [[gm, "mail"], [gm, "cal"], [cal, "cal"], [cal, "mail"],
                 ["https://calendar.google.com/calendar/event?eid=x", "cal"],
                 ["javascript:alert(1)", "mail"], ["http://mail.google.com/x", "mail"],
                 ["https://mail.google.com.evil.example/x", "mail"], ["https://evilgoogle.com/x", "cal"],
                 ["https://meet.example.com/x", "cal"], [None, "mail"], ["https://google.com@evil.example/", "cal"]]
        got = self.run_js(cases)
        self.assertEqual(got, [gm, None, cal, None, "https://calendar.google.com/calendar/event?eid=x",
                               None, None, None, None, None, None, None])


if __name__ == "__main__":
    unittest.main()
