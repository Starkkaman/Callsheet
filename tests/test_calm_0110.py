"""0.11.0: the look (data-look=calm, data-base=panel), since 2026-11-13 the only one. The page starts on it, a look saved
before (any of the retired ones) is shown as it silently (no notice), the Briefing tab reads "Today" (its id and #briefing
link unchanged), and its tokens exist for light, forced dark and auto dark. Static checks over app.js / app.css /
index.html plus the server's default."""
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "callsheet" / "app"
STATIC = APP / "static"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT / "tests"))
import store  # noqa: E402
from test_looks_072 import COLOUR, SHAPE, blocks  # noqa: E402

JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def func(name):
    i = JS.index("function %s(" % name)
    return JS[i:JS.index("\n  }\n", i)]


class OneLook(unittest.TestCase):
    def test_the_page_starts_on_calm(self):
        self.assertIn('data-look="calm" data-base="panel"', HTML)
        self.assertIn("var LOOK = 'calm';", JS)

    def test_server_default_is_calm_and_old_saved_looks_read_as_calm(self):
        self.assertEqual(store.LOOKS, ("calm",))
        with tempfile.TemporaryDirectory() as d:
            s = store.Store(d)
            try:
                self.assertEqual(s.settings()["look"], "calm")
                s.update_settings({"look": "bold", "mode": "light"})   # an old page's choice: saved as the one look
                self.assertEqual((s.settings()["look"], s.settings()["mode"]), ("calm", "light"))
            finally:
                s.close()


class NoFallbackNotice(unittest.TestCase):
    def test_the_server_look_only_carries_the_mode(self):
        f = func("applyServerLook")
        self.assertIn("if (MODES.indexOf(st.mode) >= 0 && st.mode !== PREFS.mode) setMode(st.mode, true);", f)
        self.assertNotIn("st.look", f.replace("st.look = PREFS.look;", ""))

    def test_no_one_time_notice(self):
        for gone in ("KEY_LOOKNOTE", "lookNoteDue", "lookNoteSeen", "lookFellBack", "Callsheet has a new look"):
            self.assertNotIn(gone, JS, gone)


class Today(unittest.TestCase):
    def test_tab_label_is_today_and_the_id_stays(self):
        self.assertIn("['briefing', 'Today', 'Today', 'brief']", JS)
        self.assertNotIn("['briefing', 'Briefing'", JS)
        self.assertIn("'briefing'", func("savedTab"))

    def test_no_visible_briefing_label_left(self):
        for s in ("'Briefing'", "top of Briefing.", "Name on your Briefing"):
            self.assertNotIn(s, JS, s)
        self.assertNotIn("Area summaries are at the top of", JS)  # 0.21.0: the area chips went with the old replies list


class CalmTokens(unittest.TestCase):
    def test_light_forced_dark_and_auto_dark(self):
        light = [b for b in blocks(r"\.app\[data-look=calm\]") if "font-display" in b]
        self.assertEqual(len(light), 1)
        self.assertEqual([k for k in COLOUR + SHAPE if k not in light[0]], [])
        forced = blocks(r"\.app\[data-look=calm\]\[data-mode=dark\]")
        theme = blocks(r":root\[data-theme=dark\] \.app\[data-look=calm\]\[data-mode=auto\]")
        auto = blocks(r":root:not\(\[data-theme=light\]\) \.app\[data-look=calm\]\[data-mode=auto\]")
        self.assertEqual((len(forced), len(theme), len(auto)), (1, 1, 1))
        for b in forced + auto:
            self.assertEqual([k for k in COLOUR if k not in b], [])
        self.assertEqual(forced[0], auto[0])   # the two dark copies stay identical

    def test_palette_and_type(self):
        light = [b for b in blocks(r"\.app\[data-look=calm\]") if "font-display" in b][0]
        dark = blocks(r"\.app\[data-look=calm\]\[data-mode=dark\]")[0]
        self.assertEqual((light["bg"], light["surface"], light["text"], light["accent"]), ("#ece7dd", "#f7f4ee", "#19202e", "#ec7c22"))
        self.assertEqual((dark["bg"], dark["surface"], dark["text"], dark["accent"]), ("#10141d", "#161c28", "#ece5d3", "#f08a2c"))
        self.assertIn('"Jost"', light["font-body"])
        self.assertIn('"IBM Plex Mono"', light["font-label"])
        self.assertIn("url(fonts/ibm-plex-mono-400.woff2)", CSS)
        self.assertRegex(CSS, r"\.app\[data-look=calm\]\{--fs-h1:")   # phone sizes

    def test_panel_structure(self):
        rest = CSS[CSS.index("0.11.0: the Calm look's structure (data-base=panel)"):]
        self.assertGreaterEqual(len(re.findall(r"\[data-base=panel\]", rest)), 40)
        self.assertIn("border:1px solid var(--rule);border-radius:var(--prad);box-shadow:none", rest)
        self.assertIn(".btn.primary,.banner .btn.primary,.bv .startday .btn.primary){background:var(--accent)", rest)
        self.assertIn(".btn.danger{background:var(--alert-key);color:var(--on-alert-key)", rest)
        self.assertIn('content:"▲"', rest)


if __name__ == "__main__":
    unittest.main()
