"""One look, three modes (2026-11-13). Was 0.7.2's looks test. The one look
(id calm, data-base=panel), the Theme choice (Light, Dark, Auto), saved looks from before read as the one look with their
mode kept, CSS tokens for light and both dark copies, no code left for the retired looks, bundled fonts with their
licences, no external URLs, 44 px phone targets and AA contrast in light and dark. Static checks over app.js / app.css /
index.html plus the server's look setting."""
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "callsheet" / "app"
STATIC = APP / "static"
sys.path.insert(0, str(APP))
import store  # noqa: E402

JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")

# earlier look ids (migration): each reads as "calm"
OLD = ["jam", "bold", "clean", "easy", "depth", "braun", "nerv", "lcars", "olympia", "transit", "grid", "newwave", "pulsar"]
# the 44 colour tokens the look defines in every mode (--slab is unused)
COLOUR = ("bg surface surface-2 text muted rule accent on-accent alert ok area-1 on-area-1 area-2 on-area-2 area-3 on-area-3 "
          "area-4 on-area-4 sel on-sel masthead on-masthead eyebrow box on-box callbox on-callbox panel on-panel panel-head "
          "on-panel-head panel-mark track fill field field-line phone bezel tabbar on-tabbar tab-on on-tab-on focus").split()
SHAPE = ("font-display font-body display-case display-weight display-lh display-track label-case label-track label-weight "
         "radius tag-radius chip-radius phone-radius rule-w rule-mid rule-heavy frame-w gap-tight base-size lh focus-w cut "
         "cut-bar cut-torn fs-xs fs-sm fs-md fs-lg fs-time fs-h3 fs-h2 fs-h1 fs-stat fs-hero bezel-w").split()


def blocks(selector_re):
    """Declarations (name -> value) of every rule whose selector list contains a selector matching selector_re."""
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)):
        if any(re.fullmatch(selector_re, s.strip()) for s in sel.split(",")):
            out.append(dict(re.findall(r"--([\w-]+):([^;]+);", body)))
    return out


def light_tokens():
    return {k: v.strip() for k, v in next(b for b in blocks(r"\.app\[data-look=calm\]") if "font-display" in b).items()}


def dark_tokens():
    return {k: v.strip() for k, v in blocks(r"\.app\[data-look=calm\]\[data-mode=dark\]")[0].items()}


class OneLook(unittest.TestCase):
    def test_the_server_has_one_look(self):
        self.assertEqual(store.LOOKS, ("calm",))
        self.assertEqual(list(store.OLD_LOOKS), OLD)
        self.assertEqual(store.MODES, ("auto", "light", "dark"))

    def test_server_saves_old_looks_as_the_one_look_and_refuses_others(self):
        with tempfile.TemporaryDirectory() as d:
            s = store.Store(d)
            try:
                for look in ["calm"] + OLD:
                    s.update_settings({"look": look, "mode": "dark"})
                    self.assertEqual((s.settings()["look"], s.settings()["mode"]), ("calm", "dark"), look)
                with self.assertRaises(store.InputError):
                    s.update_settings({"look": "neon"})
            finally:
                s.close()

    def test_a_saved_old_look_reads_as_the_one_look_with_its_mode(self):
        with tempfile.TemporaryDirectory() as d:
            s = store.Store(d)
            try:
                saved = s.settings()
                saved.update({"look": "easy", "mode": "light"})
                s._set_meta("settings", saved)
                self.assertEqual((s.settings()["look"], s.settings()["mode"]), ("calm", "light"))
            finally:
                s.close()

    def test_no_look_picker(self):
        for text in (JS, HTML):
            self.assertNotIn("data-set-look", text)
            self.assertNotIn("setLook", text)
            self.assertNotIn("ENABLED_LOOKS", text)
            self.assertNotIn("LOOKNAME", text)
        for gone in ("pressLook", "sunUi", "loadFonts", "LOOKFONTS", "DARK_ONLY", "shownLook"):   # nothing calls a removed helper
            self.assertNotIn(gone, JS, gone)
        self.assertIn('<div class="app" id="app" data-look="calm" data-base="panel" data-mode="auto">', HTML)
        self.assertIn("var LOOK = 'calm';", JS)
        self.assertIn("app.dataset.look = LOOK; app.dataset.base = 'panel';", JS)

    def test_the_theme_is_light_dark_auto(self):
        self.assertIn("var MODES = ['light', 'dark', 'auto'];", JS)
        panel = HTML[HTML.index('id="look-panel"'):HTML.index('id="look-err"')]
        self.assertEqual(re.findall(r'data-set-mode="(\w+)"', panel), ["light", "dark", "auto"])
        self.assertIn('id="lbl-mode">Theme<', panel)
        self.assertIn('<span class="bl">Theme</span>', HTML)
        self.assertIn("'Appearance and sound'", JS)
        self.assertIn("h('span', { class: 'label', id: 'theme-lbl' }, 'Theme')", JS)


class Prefs(unittest.TestCase):
    def test_prefs_shape_under_a_callsheet_key(self):
        self.assertIn("KEY_PREFS = 'callsheet.prefs'", JS)
        m = re.search(r"function savePrefs\(\) \{ save\(KEY_PREFS, JSON\.stringify\(\{([^}]*)\}\)\); \}", JS)
        self.assertIsNotNone(m)
        self.assertEqual(re.findall(r"(\w+):", m.group(1)), ["look", "mode"])
        self.assertNotIn("jd.prefs", JS)   # never reads another app's prefs key

    def test_the_reader_takes_any_saved_look_and_never_throws(self):
        i = JS.index("var PREFS = (function () {")
        reader = JS[i:JS.index("})();", i)]
        self.assertIn("try { p = JSON.parse(load(KEY_PREFS) || 'null'); } catch (e) { p = null; }", reader)
        self.assertIn("return { look: LOOK, mode: MODES.indexOf(m) >= 0 ? m : 'auto' };", reader)
        self.assertNotIn("p.look", reader)
        self.assertNotIn("geolocation", JS)
        self.assertNotIn("sunset", JS)


class Tokens(unittest.TestCase):
    def test_every_token_in_light_and_both_dark_copies(self):
        light = [b for b in blocks(r"\.app\[data-look=calm\]") if "font-display" in b]
        self.assertEqual(len(light), 1)
        self.assertEqual([k for k in COLOUR + SHAPE if k not in light[0]], [])
        forced = blocks(r"\.app\[data-look=calm\]\[data-mode=dark\]")
        auto = blocks(r":root:not\(\[data-theme=light\]\) \.app\[data-look=calm\]\[data-mode=auto\]")
        self.assertEqual((len(forced), len(auto)), (1, 1))
        for b in forced + auto:
            self.assertEqual([k for k in COLOUR if k not in b], [])
        self.assertEqual(forced[0], auto[0])   # the two dark copies stay identical

    def test_no_code_for_the_retired_looks(self):
        for name, text in (("app.css", CSS), ("visuals.css", (STATIC / "visuals.css").read_text(encoding="utf-8"))):
            for look in OLD:
                self.assertNotIn("[data-look=%s]" % look, text, name)
            for base in ("bold", "clean", "easy"):
                self.assertNotIn("[data-base=%s]" % base, text, name)
        for look in OLD:
            if look != "clean":   # 'clean' is also Today's clean-up group
                self.assertNotIn("'%s'" % look, JS)
        self.assertNotIn("looknote", JS + CSS)

    def test_phone_sizes(self):
        self.assertRegex(CSS, r"@media \(max-width:700px\)\{\n\.app\[data-look=calm\]\{--fs-h1:")


class Fonts(unittest.TestCase):
    LICENCE = {"ibm-plex-mono": "ibmplexmono", "jost": "jost"}

    def test_every_font_face_file_exists_with_a_licence(self):
        files = re.findall(r"@font-face\{[^}]*src:url\(([^)]+)\)", CSS)
        self.assertEqual(sorted(files), ["fonts/ibm-plex-mono-400.woff2", "fonts/jost.ttf"])
        for f in files:
            self.assertTrue((STATIC / f).is_file(), f)
            key = next(k for k in self.LICENCE if f[6:].startswith(k))
            lic = STATIC / "fonts" / ("OFL-%s.txt" % self.LICENCE[key])
            self.assertIn("SIL Open Font License", lic.read_text(encoding="utf-8"), lic)

    def test_no_unused_font_files(self):
        self.assertEqual(sorted(p.name for p in (STATIC / "fonts").iterdir()),
                         ["OFL-ibmplexmono.txt", "OFL-jost.txt", "ibm-plex-mono-400.woff2", "jost.ttf"])

    def test_latin_subsets_only(self):
        self.assertEqual([p.name for p in (STATIC / "fonts").glob("*latin-ext*")], [])


class Offline(unittest.TestCase):
    def test_no_external_urls(self):
        for name, text in (("app.css", CSS), ("app.js", JS), ("index.html", HTML)):
            urls = re.findall(r"url\(\s*['\"]?([^)'\"]+)", text)
            self.assertEqual([u for u in urls if re.match(r"(?:[a-z]+:)?//|/", u)], [], name)
            self.assertNotRegex(text, r"(?:src|href)=\"(?:https?:)?//", name)
        self.assertNotIn("fonts.googleapis", CSS + JS + HTML)


class PhoneTargetsAndContrast(unittest.TestCase):
    """0.7.2 follow-up: 44 x 44 px phone targets, and AA contrast for the look in light and dark."""

    def phone_block(self):
        i = CSS.find("0.7.2: 44 px tap targets on phones")
        self.assertGreater(i, 0)
        j = CSS.find("@media (max-width:700px){", i)
        return CSS[j:CSS.find("\n}", j)]

    def test_hit_area_and_fields(self):
        b = self.phone_block()
        self.assertIn("width:max(100%,44px);height:max(100%,44px)", b)
        self.assertIn(":is(button,a[href],summary,[role=tab])", b)   # every look: no data-look in the selector
        self.assertNotIn("data-look", b)
        self.assertRegex(b, r"input:not\(\[type=checkbox\],\[type=radio\],\[type=range\]\),select\)[^{]*\{min-height:44px")
        for sel in (".ck,.np:not(.wide)){width:44px;height:44px", ".rmain,.bopen,.tline,.dfold){min-height:44px", ".btn{min-height:44px"):
            self.assertIn(sel, b)

    def test_agenda_rows_are_44_on_phones(self):
        # 0.21.0: the Agenda (api/agenda) replaced the old timeline: its boxes, day headers, month days and invite keys
        self.assertIn(".ag .eb,.ag .rvt,.ag .w.agw,.ag .wdc .dh,.ag .md{min-height:44px}", CSS)
        self.assertIn(".tdy .rsvp .k{min-height:44px;", CSS)

    def test_aa_contrast_in_light_and_dark(self):
        def lum(h):
            c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            c = [x / 12.92 if x <= .03928 else ((x + .055) / 1.055) ** 2.4 for x in c]
            return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]

        def ratio(a, b):
            x, y = sorted([lum(a), lum(b)])
            return (y + .05) / (x + .05)
        for mode, tok in (("light", light_tokens()), ("dark", dark_tokens())):
            for fg, bg in (("text", "bg"), ("text", "surface"), ("text", "surface-2"), ("muted", "bg"), ("muted", "surface"),
                           ("alert", "bg"), ("alert", "surface"), ("on-accent", "accent"), ("on-alert-key", "alert-key"),
                           ("on-sel", "sel"), ("on-masthead", "masthead"), ("on-tab-on", "tab-on")):
                self.assertGreaterEqual(ratio(tok[fg], tok[bg]), 4.5, (mode, fg, bg))
        self.assertIn(".app .vnav .badge{color:var(--bg)}", CSS)            # the Projects badge


if __name__ == "__main__":
    unittest.main()
