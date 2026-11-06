"""0.11.0: the Jam look (data-look=jam, data-base=panel) becomes the default. Only the enabled looks show in the
picker, an owner on a disabled look is shown Jam with a one-time notice, the Briefing tab reads "Today" (its id and
#briefing link unchanged), and Jam's tokens exist for light, forced dark and auto dark. Static checks over app.js /
app.css / index.html plus the server's default."""
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


class EnabledLooks(unittest.TestCase):
    def test_the_enabled_list_is_one_constant(self):
        self.assertIn("var ENABLED_LOOKS = ['jam', 'easy'];", JS)
        self.assertIn("var DEFAULT_LOOK = 'jam';", JS)
        self.assertIn("var LOOKS = ['jam'].concat(OWN_LOOKS, JAM_LOOKS);", JS)   # the others stay in code

    def test_disabled_looks_keep_their_code_and_chips(self):
        for look in ("bold", "clean", "depth", "braun", "nerv", "lcars", "olympia", "transit", "grid", "newwave", "pulsar"):
            self.assertIn('data-set-look="%s"' % look, HTML)
            self.assertIn(".app[data-look=%s]" % look, CSS)
        # the picker hides every chip (and a group left empty) that isn't enabled
        self.assertIn("b.hidden = ENABLED_LOOKS.indexOf(b.getAttribute('data-set-look')) < 0;", JS)
        self.assertIn("g.hidden = true", JS)

    def test_the_page_starts_on_jam(self):
        self.assertIn('data-look="jam" data-base="panel"', HTML)
        self.assertIn('data-set-look="jam" aria-pressed="true"', HTML)
        self.assertIn("p.look : DEFAULT_LOOK", JS)

    def test_server_default_is_jam_and_old_saved_looks_are_kept(self):
        self.assertEqual(store.LOOKS[0], "jam")
        with tempfile.TemporaryDirectory() as d:
            s = store.Store(d)
            try:
                self.assertEqual(s.settings()["look"], "jam")
                s.update_settings({"look": "bold"})            # an older owner's choice stays stored
                self.assertEqual(s.settings()["look"], "bold")
                s.update_settings({"look": "jam"})
                self.assertEqual(s.settings()["look"], "jam")
            finally:
                s.close()


class Fallback(unittest.TestCase):
    def test_set_look_falls_back_to_jam(self):
        f = func("setLook")
        self.assertIn("if (LOOKS.indexOf(v) < 0) v = DEFAULT_LOOK;", f)
        self.assertIn("if (ENABLED_LOOKS.indexOf(v) < 0) { lookFellBack = v; v = DEFAULT_LOOK; }", f)

    def test_server_look_that_is_disabled_falls_back_too(self):
        f = func("applyServerLook")
        self.assertIn("ENABLED_LOOKS.indexOf(st.look) < 0) lookFellBack = st.look;", f)
        self.assertIn("shownLook(st.look) !== app.dataset.look", f)

    def test_one_time_notice(self):
        self.assertIn("KEY_LOOKNOTE = 'callsheet.looknote'", JS)
        self.assertIn("function lookNoteDue() { return !!lookFellBack && load(KEY_LOOKNOTE) !== '1'; }", JS)
        self.assertIn("function lookNoteSeen() { save(KEY_LOOKNOTE, '1'); }", JS)
        self.assertEqual(JS.count("'Callsheet has a new look. Easy read is still in Setup.'"), 1)
        self.assertIn("lookNoteDue()", func("renderBrief"))
        i = JS.index("'Callsheet has a new look. Easy read is still in Setup.'")
        self.assertIn("lookNoteSeen();", JS[i:i + 300])                 # Got it dismisses it for good
        self.assertGreaterEqual(JS.count("lookNoteSeen();"), 2)          # picking a look counts as seeing it


class Today(unittest.TestCase):
    def test_tab_label_is_today_and_the_id_stays(self):
        self.assertIn("['briefing', 'Today', 'Today', 'brief']", JS)
        self.assertNotIn("['briefing', 'Briefing'", JS)
        self.assertIn("'briefing'", func("savedTab"))

    def test_no_visible_briefing_label_left(self):
        for s in ("'Briefing'", "top of Briefing.", "Name on your Briefing"):
            self.assertNotIn(s, JS, s)
        self.assertIn("Area summaries are at the top of Today.", JS)


class JamTokens(unittest.TestCase):
    def test_light_forced_dark_and_auto_dark(self):
        light = [b for b in blocks(r"\.app\[data-look=jam\]") if "font-display" in b]
        self.assertEqual(len(light), 1)
        self.assertEqual([k for k in COLOUR + SHAPE if k not in light[0]], [])
        forced = blocks(r"\.app\[data-look=jam\]\[data-mode=dark\]")
        theme = blocks(r":root\[data-theme=dark\] \.app\[data-look=jam\]\[data-mode=auto\]")
        auto = blocks(r":root:not\(\[data-theme=light\]\) \.app\[data-look=jam\]\[data-mode=auto\]")
        self.assertEqual((len(forced), len(theme), len(auto)), (1, 1, 1))
        for b in forced + auto:
            self.assertEqual([k for k in COLOUR if k not in b], [])
        self.assertEqual(forced[0], auto[0])   # the two dark copies stay identical

    def test_palette_and_type(self):
        light = [b for b in blocks(r"\.app\[data-look=jam\]") if "font-display" in b][0]
        dark = blocks(r"\.app\[data-look=jam\]\[data-mode=dark\]")[0]
        self.assertEqual((light["bg"], light["surface"], light["text"], light["accent"]), ("#ece7dd", "#f7f4ee", "#19202e", "#ec7c22"))
        self.assertEqual((dark["bg"], dark["surface"], dark["text"], dark["accent"]), ("#10141d", "#161c28", "#ece5d3", "#f08a2c"))
        self.assertIn('"Jost"', light["font-body"])
        self.assertIn('"IBM Plex Mono"', light["font-label"])
        self.assertIn("ibm-plex-mono-400.woff2", JS)
        self.assertRegex(CSS, r"\.app\[data-look=jam\]\{--fs-h1:")   # phone sizes

    def test_panel_structure(self):
        rest = CSS[CSS.index("0.11.0: the Jam look's structure (data-base=panel)"):]
        self.assertGreaterEqual(len(re.findall(r"\[data-base=panel\]", rest)), 40)
        self.assertIn("border:1px solid var(--rule);border-radius:var(--prad);box-shadow:none", rest)
        self.assertIn(".btn.primary,.banner .btn.primary,.bv .startday .btn.primary){background:var(--accent)", rest)
        self.assertIn(".btn.danger{background:var(--alert-key);color:var(--on-alert-key)", rest)
        self.assertIn('content:"▲"', rest)


if __name__ == "__main__":
    unittest.main()
