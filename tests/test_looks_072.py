"""0.7.2: the nine JamDash-matched looks (harmony.md). Look ids and names, the per-device prefs shape, CSS token
completeness per look and mode, the data-base family selectors, dark-only looks, bundled fonts with their licences,
and no external URLs. Static checks over app.js / app.css / index.html plus the server's look setting."""
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

OWN = ["bold", "clean", "easy"]
JAM = ["depth", "braun", "nerv", "lcars", "olympia", "transit", "grid", "newwave", "pulsar"]   # JamDash looks.ts order
NAMES = {"bold": "Bold", "clean": "Clean", "easy": "Easy read", "depth": "Depth", "braun": "Braun", "nerv": "NERV",
         "lcars": "LCARS", "olympia": "Olympia", "transit": "Transit", "grid": "Grid", "newwave": "New Wave", "pulsar": "Pulsar"}
DARK_ONLY = ["nerv", "lcars"]
# the 44 colour tokens every look defines in every mode (harmony gen.py KEYS; --slab is Bold-only and unused)
COLOUR = ("bg surface surface-2 text muted rule accent on-accent alert ok area-1 on-area-1 area-2 on-area-2 area-3 on-area-3 "
          "area-4 on-area-4 sel on-sel masthead on-masthead eyebrow box on-box callbox on-callbox panel on-panel panel-head "
          "on-panel-head panel-mark track fill field field-line phone bezel tabbar on-tabbar tab-on on-tab-on focus").split()
SHAPE = ("font-display font-body display-case display-weight display-lh display-track label-case label-track label-weight "
         "radius tag-radius chip-radius phone-radius rule-w rule-mid rule-heavy frame-w gap-tight base-size lh focus-w cut "
         "cut-bar cut-torn fs-xs fs-sm fs-md fs-lg fs-time fs-h3 fs-h2 fs-h1 fs-stat fs-hero bezel-w").split()


def js_list(name):
    m = re.search(r"var %s = \[([^\]]*)\]" % name, JS)
    return re.findall(r"'([a-z]+)'", m.group(1))


def js_map(name):
    m = re.search(r"var %s = \{(.*?)\};" % name, JS, re.S)
    return dict(re.findall(r"([a-z]+): '([^']*)'", m.group(1)))


def blocks(selector_re):
    """Declarations (name -> value) of every rule whose selector list contains a selector matching selector_re."""
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)):
        if any(re.fullmatch(selector_re, s.strip()) for s in sel.split(",")):
            out.append(dict(re.findall(r"--([\w-]+):([^;]+);", body)))
    return out


class LookList(unittest.TestCase):
    def test_ids_and_order(self):
        self.assertEqual(js_list("OWN_LOOKS"), OWN)
        self.assertEqual(js_list("JAM_LOOKS"), JAM)
        self.assertEqual(list(store.LOOKS), OWN + JAM)

    def test_names_match_jamdash(self):
        names = dict(re.findall(r"([a-z]+): '([^']+)'", re.search(r"var LOOKNAME = \{(.*?)\}", JS, re.S).group(1)))
        self.assertEqual(names, NAMES)

    def test_every_look_has_a_family(self):
        base = js_map("BASE")
        self.assertEqual(set(base), set(OWN + JAM))
        self.assertTrue(all(base[k] == k for k in OWN))
        self.assertEqual({k for k, v in base.items() if v == "clean"}, {"clean", "depth", "braun", "pulsar", "newwave"})
        self.assertEqual({k for k, v in base.items() if v == "bold"}, {"bold", "nerv", "lcars", "transit", "olympia", "grid"})

    def test_header_picker_groups_and_swatches(self):
        own, jam = HTML.index('id="lbl-look">Callsheet<'), HTML.index('id="lbl-jam">Jam looks<')
        mode = HTML.index('id="lbl-mode"')
        for look in OWN:
            self.assertTrue(own < HTML.index('data-set-look="%s"' % look) < jam, look)
        for look in JAM:
            self.assertTrue(jam < HTML.index('data-set-look="%s"' % look) < mode, look)
            self.assertIn('class="app lsw" data-look="%s"' % look, HTML)
        self.assertEqual(len(re.findall(r'data-set-look="', HTML)), 12)

    def test_server_accepts_every_look_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            s = store.Store(d)
            try:
                for look in OWN + JAM:
                    s.update_settings({"look": look})
                    self.assertEqual(s.settings()["look"], look)
                with self.assertRaises(store.InputError):
                    s.update_settings({"look": "neon"})
            finally:
                s.close()


class Prefs(unittest.TestCase):
    def test_prefs_shape_is_jamdash_shape_under_a_callsheet_key(self):
        self.assertIn("KEY_PREFS = 'callsheet.prefs'", JS)
        m = re.search(r"function savePrefs\(\) \{ save\(KEY_PREFS, JSON\.stringify\(\{([^}]*)\}\)\); \}", JS)
        self.assertIsNotNone(m)
        self.assertEqual(re.findall(r"(\w+):", m.group(1)), ["look", "mode", "autoSource"])
        self.assertIn("autoSource: p.autoSource === 'sunset' ? 'sunset' : 'device'", JS)
        self.assertNotIn("jd.prefs", JS)   # never reads JamDash's key

    def test_sunset_is_depth_only_and_local(self):
        self.assertIn("PREFS.mode === 'auto' && PREFS.look === 'depth' && PREFS.autoSource === 'sunset'", JS)
        self.assertNotIn("geolocation", JS)


class Tokens(unittest.TestCase):
    def test_every_token_in_every_jam_look_and_mode(self):
        for look in JAM:
            base = [b for b in blocks(r"\.app\[data-look=%s\]" % look) if "font-display" in b]   # not the phone sizes
            self.assertEqual(len(base), 1, look)
            missing = [k for k in COLOUR + SHAPE if k not in base[0]]
            self.assertEqual(missing, [], look)
            if look in DARK_ONLY:
                continue
            forced = blocks(r"\.app\[data-look=%s\]\[data-mode=dark\]" % look)
            auto = blocks(r":root:not\(\[data-theme=light\]\) \.app\[data-look=%s\]\[data-mode=auto\]" % look)
            self.assertEqual((len(forced), len(auto)), (1, 1), look)
            for b in forced + auto:
                self.assertEqual([k for k in COLOUR if k not in b], [], look)
            self.assertEqual(forced[0], auto[0], look)   # the two dark copies stay identical

    def test_callsheet_looks_keep_their_tokens(self):
        for look in OWN:
            light = [b for b in blocks(r"\.app\[data-look=%s\]" % look) if "font-display" in b][0]
            dark = blocks(r"\.app\[data-look=%s\]\[data-mode=dark\]" % look)[0]
            self.assertEqual([k for k in COLOUR + SHAPE if k not in light], [], look)
            self.assertEqual([k for k in COLOUR if k not in dark], [], look)

    def test_dark_only_looks(self):
        self.assertEqual(js_list("DARK_ONLY"), DARK_ONLY)
        for look in DARK_ONLY:
            self.assertEqual(blocks(r"\.app\[data-look=%s\]\[data-mode=dark\]" % look), [])
            rule = re.search(r"\.app\[data-look=%s\]\{\s*color-scheme:(\w+);" % look, CSS)
            self.assertEqual(rule.group(1), "dark")
        self.assertIn("'Always dark'", JS)

    def test_phone_sizes_for_every_look(self):
        phone = CSS[CSS.index("/* ================= phone sizes ================= */"):]
        for look in OWN + JAM:
            self.assertRegex(phone, r"\.app\[data-look=%s\]\{--fs-h1:" % look)

    def test_structural_overrides_key_on_the_family(self):
        rest = CSS[CSS.index("/* ================= base (tokens only, see above) ================= */"):]
        for look in OWN:
            self.assertNotIn("[data-look=%s]" % look, rest.replace(",[data-look=%s]" % look, "").replace(":not([data-look=%s]" % look, ""))
        self.assertGreaterEqual(len(re.findall(r"\[data-base=(?:bold|clean|easy)\]", rest)), 30)
        self.assertIn("app.dataset.base = BASE[v]", JS)

    def test_signatures(self):
        self.assertIn("--btn-shadow:4px 4px 0 var(--text)", CSS)            # New Wave hard offset
        self.assertIn(".app:is([data-look=pulsar],[data-look=newwave]) *{border-radius:0!important}", CSS)
        self.assertIn("polygon(0 0,calc(100% - 14px) 0,100% 14px,100% 100%,14px 100%,0 calc(100% - 14px))", CSS)  # NERV TR + BL
        self.assertIn("--radius:26px;--tag-radius:20px;--chip-radius:20px", CSS)   # Depth
        self.assertIn("--btn-radius:14px", CSS)
        self.assertIn(".app[data-look=braun] :is(.btn,.act).critical{background:var(--critical)", CSS)
        self.assertEqual(JS.count("critical'"), 4)                           # only the delete / clear confirmations
        self.assertIn("--m:16px", CSS)                                       # Grid module
        self.assertRegex(CSS, r"@media \(max-width:700px\)\{\s*/\* LCARS phone")


class Fonts(unittest.TestCase):
    LICENCE = {"instrument-sans": "instrumentsans", "archivo": "archivo", "antonio": "antonio", "saira": "saira",
               "noto-serif": "notoserif", "share-tech-mono": "sharetechmono", "inter": "inter", "albert-sans": "albertsans",
               "hanken-grotesk": "hankengrotesk", "bricolage": "bricolage-silkscreen", "silkscreen": "bricolage-silkscreen",
               "ibm-plex-mono": "ibmplexmono", "ibm-plex-sans": "ibmplexsans", "atkinson": "atkinsonhyperlegible",
               "jost": "jost", "londrina": "londrinasolid"}

    def test_every_font_face_file_exists_with_a_licence(self):
        files = re.findall(r"@font-face\{[^}]*src:url\(([^)]+)\)", CSS)
        self.assertGreaterEqual(len(files), 17)
        for f in files:
            self.assertTrue(f.startswith("fonts/"), f)
            self.assertTrue((STATIC / f).is_file(), f)
            key = next(k for k in self.LICENCE if f[6:].startswith(k))
            lic = STATIC / "fonts" / ("OFL-%s.txt" % self.LICENCE[key])
            self.assertTrue(lic.is_file(), lic)
            self.assertIn("SIL Open Font License", lic.read_text(encoding="utf-8"), lic)

    def test_preload_map_covers_every_look(self):
        m = re.search(r"var LOOKFONTS = \{(.*?)\};", JS, re.S).group(1)
        fonts = dict((k, re.findall(r"'([^']+)'", v)) for k, v in re.findall(r"(\w+): \[([^\]]*)\]", m))
        self.assertEqual(set(fonts), set(OWN + JAM))
        for look, fs in fonts.items():
            for f in fs:
                self.assertTrue((STATIC / "fonts" / f).is_file(), (look, f))
        self.assertNotIn('rel="preload"', HTML)   # only the active look is preloaded, by app.js

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
    """0.7.2 follow-up: 44 x 44 px phone targets in every look, and the two Bold contrast fixes."""

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
        self.assertIn("H = phone ? 88 : 64", JS)                      # 30 minutes = 44 px
        self.assertIn("phone ? 44 : 26", JS)
        self.assertIn("lastPin + (phone ? 46 : 28)", JS)

    def test_bold_contrast_fixes(self):
        def lum(h):
            c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            c = [x / 12.92 if x <= .03928 else ((x + .055) / 1.055) ** 2.4 for x in c]
            return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]

        def ratio(a, b):
            x, y = sorted([lum(a), lum(b)])
            return (y + .05) / (x + .05)
        tok = {k: v.strip() for k, v in next(b for b in blocks(r"\.app\[data-look=bold\]") if "font-display" in b).items()}
        self.assertGreaterEqual(ratio(tok["alert"], tok["bg"]), 4.5)        # the Delivery note (syncst.bad)
        self.assertGreaterEqual(ratio(tok["alert"], tok["surface-2"]), 4.5)
        self.assertEqual(tok["accent"], "#b52e14")                          # Bold's red itself is unchanged
        self.assertIn(".app .vnav .badge{color:var(--bg)}", CSS)            # the Projects badge, every look


if __name__ == "__main__":
    unittest.main()
