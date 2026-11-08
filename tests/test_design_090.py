"""0.9.0 (design audit 2026-11, "next release"): page tests for the UI-only changes.

Setup in four places (Connections, Your day, AI, Advanced) with a three-step first run and old deep links mapped;
the settings index; Find or ask (read, copy and open only); One at a time on Replies (Undo for every action that
changes something); the Start your day card; the per-device density; the 44 px toast Close; the version.
"""
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "callsheet" / "app"
STATIC = APP / "static"
sys.path.insert(0, str(APP))
import chat  # noqa: E402
import push  # noqa: E402
import store  # noqa: E402


def func(js, name):
    """The source of one top-level function in app.js (up to the next one at the same indent)."""
    i = js.index("  function " + name + "(")
    m = re.compile(r"\n  (?:function |var |/\*)").search(js, i + 10)
    return js[i:m.start() if m else len(js)]


class Design090(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.css = (STATIC / "app.css").read_text(encoding="utf-8")
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.block = cls.css[cls.css.index("/* ================= 0.9.0 next release"):]

    # ----- Setup in four places -----
    def test_four_places_and_first_run(self):
        self.assertIn("var PLACES = [['connections', 'Connections'], ['day', 'Your day'], ['ai', 'AI'], ['advanced', 'Advanced']]", self.js)
        self.assertIn("var WIZ = [['connections', 'Connect'], ['day', 'Your day'], ['knowme', 'Get to know me']]", self.js)
        for name in ("placeConnections", "placeDay", "placeAI", "placeAdvanced", "setupHome", "wizKnow", "dayBriefs",
                     "dayAbout", "dayLook", "presetPlain"):
            self.assertIn("function " + name + "(", self.js)
        self.assertNotIn("ui.step", self.js)
        rs = func(self.js, "renderSetup")
        self.assertIn("setupHome()", rs)
        self.assertIn("PLACE_FN[", rs)

    def test_advanced_closed_by_default(self):
        self.assertIn("adv: {},", self.js)
        pa = func(self.js, "placeAdvanced")
        self.assertIn("var open = !!ui.adv[x[0]]", pa)
        self.assertIn("hidden: !open", pa)
        self.assertIn("'aria-expanded': String(open)", pa)
        ids = re.findall(r"\['(\w+)', '[^']+', (?:function|adv)", self.js[self.js.index("var ADV = "):])
        for want in ("key", "server", "presets", "chat", "alerts", "writing", "memory", "eval", "signal", "startover"):
            self.assertIn(want, ids)

    def test_ai_place_has_plain_presets_and_a_sorting_slot(self):
        ai = func(self.js, "placeAI")
        self.assertIn("sortingSlot()", ai)
        self.assertIn("return sortingBlock();", func(self.js, "sortingSlot"))     # the 0.8.4/0.8.5 merge
        sb = func(self.js, "sortingBlock")
        for part in ("lmHealth(v)", "sortSettings(v)", "srLearned(v)", "'sr-check-open'", "location.hash = 'sorting'"):
            self.assertIn(part, sb)
        wb = func(self.js, "writerBlock")
        for word in ("'Off'", "'On this box'", "'Claude'"):
            self.assertIn(word, wb)
        pp = func(self.js, "presetPlain")
        self.assertIn("'Careful'", pp)
        self.assertIn("'Fast'", pp)
        self.assertIn("api('PATCH', 'api/writer', body)", pp)

    def test_urgent_check_days_row(self):
        db = func(self.js, "dayBriefs")
        self.assertIn("'Urgent checks'", db)
        self.assertIn("skip_checkins_days_off: true", db)
        self.assertIn("'Pick at least one day.", db)

    def test_old_deep_links_map_to_places(self):
        m = re.search(r"var OLD_STEPS = \{([^}]*)\}", self.js)
        self.assertIsNotNone(m)
        pairs = dict((k.strip("' "), v) for k, v in re.findall(r"('?[\w-]+'?): '(\w+)'", m.group(1)))
        places = {"home", "connections", "day", "ai", "advanced"}
        for old in ("1", "2", "3", "4", "5", "6", "about", "areas", "vips", "times", "call-times", "delivery", "review"):
            self.assertIn(old, pairs, old)
            self.assertIn(pairs[old], places, old)
        self.assertEqual(pairs["2"], "connections")
        self.assertEqual(pairs["areas"], "connections")
        self.assertEqual(pairs["delivery"], "ai")
        fh = func(self.js, "placeFromHash")
        self.assertIn(r"/^#setup(?:\/(.*))?$/", fh)
        self.assertIn(": 'home'", fh)  # an unknown step opens Setup home
        self.assertIn("var sp = placeFromHash(location.hash)", self.js)

    # ----- the settings index -----
    def test_settings_index_covers_every_setting(self):
        block = self.js[self.js.index("var SETTINGS = ["):self.js.index("function setWhere(")]
        rows = re.findall(r"\['(connections|day|ai|advanced)', (null|'\w+'), '([^']+)', '([^']+)', '([^']*)', \[([^\]]*)\]\]", block)
        self.assertGreater(len(rows), 40)
        keys = set()
        for row in rows:
            keys.update(re.findall(r"'([\w.]+)'", row[5]))
        want = set(store.default_settings()) | set(push.defaults())
        w = store.default_writer()
        for k, v in w.items():
            if k == "configured_by_owner":
                continue
            if isinstance(v, dict) and k in ("claude", "local"):
                want |= {k + "." + x for x in v}
            else:
                want.add(k)
        want |= {"chat.provider"} | {"chat.local." + x for x in chat.LOCAL_DEFAULTS} | {"chat.claude." + x for x in chat.CLAUDE_DEFAULTS}
        self.assertEqual(sorted(want - keys), [], "settings missing from the index")
        adv_ids = set(re.findall(r"\['(\w+)', '[^']+', (?:function|adv)", self.js[self.js.index("var ADV = "):]))
        for row in rows:
            if row[1] != "null":
                self.assertIn(row[1].strip("'"), adv_ids)

    # ----- Find or ask -----
    def test_find_or_ask_only_reads_copies_or_opens(self):
        self.assertIn("var FA_KINDS = ['open', 'copy', 'read'];", self.js)
        fi = func(self.js, "faItems")
        kinds = re.findall(r"item\('[^']+', '(\w+)'", fi)
        self.assertTrue(kinds)
        self.assertEqual(set(kinds) - {"open", "copy", "read"}, set())
        for bad in ("markDone", "suppress(", "dismiss(", "unDone", "api(", "'POST'", "'DELETE'", "'PATCH'", "mutate(", "setLater"):
            self.assertNotIn(bad, fi)
        self.assertIn("FA_KINDS.indexOf(x.kind) < 0) return;", func(self.js, "faRun"))

    def test_find_or_ask_is_a_combobox(self):
        fo = func(self.js, "faOpen")
        for a in ("role: 'combobox'", "'aria-expanded': 'true'", "'aria-controls': 'fa-list'", "'aria-autocomplete': 'list'",
                  "role: 'listbox'", "role: 'dialog'", "'aria-modal': 'true'"):
            self.assertIn(a, fo)
        fl = func(self.js, "faList")
        self.assertIn("role: 'option'", fl)
        self.assertIn("'aria-selected'", fl)
        self.assertIn("aria-activedescendant", fl)
        self.assertIn("role: 'group'", fl)
        fk = func(self.js, "faKeys")
        for k in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
            self.assertIn(k, fk)
        self.assertIn("FA.from.focus()", func(self.js, "faClose"))
        self.assertIn('id="find-btn"', self.html)
        self.assertIn('data-i="search"', self.html)

    def test_slash_is_ignored_while_typing(self):
        i = self.js.index("var FA_KINDS")
        kd = self.js[self.js.index("document.addEventListener('keydown'", i):]
        kd = kd[:kd.index("}, true);")]
        self.assertIn("(e.metaKey || e.ctrlKey)", kd)
        self.assertIn("e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey && !e.isComposing && !typing(e.target)", kd)

    # ----- One at a time -----
    def test_one_at_a_time_keys_ignore_inputs(self):
        i = self.js.index("if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing || typing(t)) return;")
        j = self.js.index("if (U.oat && U.tab === 'replies') { if (k && !e.shiftKey && 'codlnxjk'.indexOf(k) >= 0 && oatAct(k))")
        self.assertLess(i, j)
        self.assertIn("if (ui.view !== 'brief' || !B || FA.open) return;", self.js)

    def test_one_at_a_time_undo_for_every_action(self):
        self.assertIn("var OAT_UNDO = { d: 'done', l: 'later', x: 'suppress', n: 'note' };", self.js)
        self.assertIn("{ kind: 'done', id: id, row: r }", func(self.js, "markDone"))
        self.assertIn("{ kind: 'suppress', rule: rule && rule.id, rows: gone }", func(self.js, "suppress"))
        self.assertIn("{ kind: 'later', id: id }", func(self.js, "oatAct"))
        self.assertIn("{ kind: 'note', ref: ref, id: P.id, prev: nt ? nt.text : null }", func(self.js, "noteAction"))
        undo = self.js[self.js.index("    else if (d.undo) {"):]
        undo = undo[:undo.index("    else if (d.toastx)")]
        for kind in ("done", "later", "suppress", "note"):
            self.assertIn("u.kind === '" + kind + "'", undo)
        self.assertIn("if (ub) U.oat.cur = ub;", undo)  # Undo brings that card back
        self.assertIn("function noteUndo(u)", self.js)

    def test_one_at_a_time_card(self):
        ov = func(self.js, "oatView")
        self.assertIn("k + ' of ' + n", ov)
        self.assertIn("'data-oat': 'c'", ov)
        self.assertIn("'data-oat': 'd'", ov)
        for k in ("'o'", "'l'", "'n'", "'x'"):
            self.assertIn("oatQuiet(" + k, ov)
        self.assertIn("allClear(later)", func(self.js, "oatEnd"))
        sw = func(self.js, "oatSwipe")
        self.assertIn("oatAct(v > 0 ? 'd' : 'l')", sw)
        self.assertIn("e.pointerType === 'mouse'", sw)
        self.assertIn("touch-action:pan-y", self.block)
        self.assertIn("if (U.oat) return oatView();", func(self.js, "T_replies"))
        self.assertIn("'data-oatgo': '1'", func(self.js, "T_replies"))
        self.assertIn("U.pop = { kind: 'reply', id: id, anc: 'oat-x-' + id, sub: true }", func(self.js, "oatAct"))
        self.assertIn("window.open(link, '_blank', 'noopener,noreferrer')", func(self.js, "oatAct"))

    # ----- Start your day -----
    def test_start_your_day(self):
        sc = func(self.js, "startCard")
        self.assertIn("if (!B || B.sample || load(KEY_START) === todayKey()) return null;", sc)
        for w in ("'data-start': '1'", "'data-startno': '1'", "'Not now'", "clash", "invite", "'Next '"):
            self.assertIn(w, sc)
        self.assertIn("return [startCard(), h('div', { class: 'bf' }", func(self.js, "T_briefing"))
        self.assertIn("else if (d.startno) { save(KEY_START, todayKey());", self.js)
        self.assertIn("save(KEY_START, todayKey());", func(self.js, "oatStart"))

    # ----- density, toast, chat off -----
    def test_density_per_device(self):
        self.assertIn("KEY_DENSITY = 'callsheet.density'", self.js)
        self.assertIn("setAttribute('data-density', density())", self.js)
        self.assertIn("else if (t.dataset.dens) setDensity(t.dataset.dens);", self.js)
        self.assertIn(".app[data-density=comfortable] .bv .rmain .why{display:block", self.block)
        self.assertIn("h('span', { class: 'why' }, reasonWords(r))", func(self.js, "rowEl"))

    def test_toast_close_is_44px(self):
        self.assertIn(".bv-layer .toast .tx{display:grid;place-items:center;width:44px;height:44px", self.css)
        self.assertNotIn("width:28px;height:28px", self.css[self.css.index(".bv-layer .toast"):self.css.index(".bv-layer .toast") + 2000])
        self.assertIn(".bv-layer .toast [data-undo]{min-width:44px;min-height:44px", self.css)

    def test_chat_off_hides_the_tab_on_this_device(self):
        self.assertIn("function liveTabs()", self.js)
        self.assertIn("if (liveTabs().indexOf(t) < 0) return;", func(self.js, "setTab"))
        self.assertIn("if (U.tab === 'chat' && chatOff()) U.tab = 'briefing';", self.js)

    # ----- the rules every page change keeps -----
    def test_text_only_relative_tokens(self):
        self.assertNotIn("innerHTML", self.js)
        self.assertNotRegex(self.js, r"api\('[A-Z]+', '/")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", self.block), "use theme tokens, not hex colours")
        self.assertNotRegex(self.block, r"rgba?\(|hsl\(")

    # ----- the 0.8.4/0.8.5 merge: sorting in the AI place, the index, One at a time and Start your day -----
    def test_sorting_merged_into_the_ai_place(self):
        block = self.js[self.js.index("var SETTINGS = ["):self.js.index("function setWhere(")]
        for key in ("'sorting.mode'", "'sorting.model'", "'sorting.day_cards'", "'sorting.backfill'", "'sorting.learned'"):
            self.assertIn(key, block)
        for label in ("'AI sorting'", "'Check the sorting'", "'What it learned'", "'Local model health'"):
            self.assertIn("['ai', null, " + label, block)
        rs = func(self.js, "renderSorting")
        self.assertNotIn("sortSettings(", rs)                 # each setting in one place: Setup › AI
        self.assertNotIn("srLearned(", rs)
        self.assertIn("'Back: AI'", rs)
        self.assertIn("srHomeLine()", func(self.js, "placeLines"))
        self.assertNotIn("srHomeCard", self.js)
        self.assertIn("inPlace('ai')", func(self.js, "srRender"))

    def test_sorting_order_in_one_at_a_time_and_start(self):
        sp = func(self.js, "srSplit")
        self.assertIn("out.need.concat(rs.filter(function (r) { return !placed[r.id]; }))", sp)   # nothing dropped
        self.assertIn("sp.need.concat(sp.fyi, sp.sorted)", func(self.js, "oatStart"))
        self.assertIn("srSplit(rs)", func(self.js, "startCard"))
        self.assertIn("srMark(r)", func(self.js, "oatView"))

    def test_version(self):
        cfg = (ROOT / "callsheet" / "config.yaml").read_text(encoding="utf-8")
        self.assertRegex(cfg, r'(?m)^version: "0\.14\.3"$')  # 0.14.3 = Chat first, background requests capped by total time
        self.assertIn('APP_VERSION = "0.14.3"', (APP / "api.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
