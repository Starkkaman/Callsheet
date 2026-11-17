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
                     "dayAbout", "dayLook", "aiServer", "aiJobs", "aiAdvanced"):
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
        for want in ("key", "alerts", "writing", "memory", "signal", "startover"):
            self.assertIn(want, ids)
        for gone in ("server", "chat"):     # 0.21.0: the model server, Claude and Chat's limits live in Setup › AI
            self.assertNotIn(gone, ids)

    def test_ai_place_is_one_server_and_a_model_per_job(self):
        """0.21.0: one model server, a model per job from its list, one Advanced drawer; no presets, no writer."""
        ai = func(self.js, "placeAI")
        for part in ("aiServer(v)", "aiJobs(v)", "aiWeb()", "memoryBlock('ai')", "aiStyle()", "aiAdvanced(v)"):
            self.assertIn(part, ai)
        self.assertIn("api('GET', 'api/ai/models')", func(self.js, "aiList"))
        jobs = func(self.js, "aiJobs")
        for part in ("'Use one model for everything'", "'Chat'", "'Background'", "'▲ Two models. '", "api/background/check",
                     "'background_model'", "'chat_model'"):
            self.assertIn(part, jobs)
        adv = func(self.js, "aiAdvanced")
        for part in ("'If your model server fails, use Claude'", "'Save a Claude key first.'", "fallback_to_claude", "wsAdvanced(WS.v)"):
            self.assertIn(part, adv)
        for gone in ("function presetPlain(", "function writerBlock(", "function bgBlock(", "'Who writes'", "'AI writer'",
                     "Careful or Fast", "call_presets", "'Add one'", "Shadow", "same_as_writer", "Chat engine"):
            self.assertNotIn(gone, self.js)

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
        # 0.21.0: Chat has no server of its own; the writer's own numbers (one model per job now), Claude's effort and
        # Suggest replies are no longer shown (their saved values and defaults stay)
        # 2026-11-13: one look, so the look itself isn't a choice any more (the theme is "mode")
        retired = {"chat.local.base_url", "chat.local.api_key", "chat.claude.effort", "suggest_replies", "look"} | {
            "local." + x for x in ("context", "effort", "id_slot", "kwargs", "max_tokens", "reasoning_budget", "thinking", "timeout")}
        self.assertEqual(sorted(retired - want), [], "retired keys that no longer exist")
        want -= retired
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

    # ----- One at a time and Start your day -----
    def test_one_at_a_time_is_gone(self):
        # 0.21.0: One at a time lived on the old replies list (T_replies) and Start your day on the old Briefing; both
        # views are gone (Today and Replies from the ledger), with their keys, palette entry and Undo kinds
        for name in ("T_replies", "oatView", "oatAct", "oatStart", "oatExit", "oatEnd", "oatSwipe", "noteUndo",
                     "allClear", "meter", "doneRows", "legendBtn", "areaChips", "startCard", "T_briefing"):
            self.assertNotIn("function " + name + "(", self.js)
        for gone in ("U.oat", "OAT_UNDO", "KEY_START", "data-oatgo", "'One at a time'", "u.kind === 'later'"):
            self.assertNotIn(gone, self.js)
        undo = self.js[self.js.index("    else if (d.undo) {"):]
        undo = undo[:undo.index("    else if (d.toastx)")]
        for kind in ("done", "suppress", "horizon"):          # the rework: api/done, api/suppress, api/horizon went
            self.assertNotIn("u.kind === '" + kind + "'", undo)
        for name in ("markDone", "suppress", "hzMark", "rowEl", "panelReply", "presetsBlock", "renderEval", "neverReply"):
            self.assertNotIn("function " + name + "(", self.js)

    # ----- density, toast, chat off -----
    def test_density_is_gone(self):
        # 0.21.0: the per-device Replies density went (the Replies rows never read it), and so did "Needs a reply
        # means" (reply_age_days is read by no server code)
        for gone in ("KEY_DENSITY", "data-density", "setDensity", "densSeg", "Needs a reply means", "f-age"):
            self.assertNotIn(gone, self.js)
        self.assertNotIn("data-density", self.css)

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
    def test_sorting_screens_went_with_the_rework(self):
        block = self.js[self.js.index("var SETTINGS = ["):self.js.index("function setWhere(")]
        self.assertIn("'background.model'", block)
        for label in ("'Background model'", "'Local model health'"):
            self.assertIn("['ai', null, " + label, block)
        for gone in ("'sorting.mode'", "'AI sorting'", "'Check the sorting'", "'What it learned'", "'Read each message'"):
            self.assertNotIn(gone, block)
        for gone in ("function renderSorting(", "function sortSettings(", "function srLearned(", "function rdSettings(",
                     "api/sorting", "api/reading", "api/picker", "'#sorting'"):
            self.assertNotIn(gone, self.js)
        self.assertIn("aiHomeLine()", func(self.js, "placeLines"))   # 0.21.0: what each job uses
        self.assertNotIn("sorting", func(self.js, "placeLines"))

    def test_sorting_order_helpers_went_with_the_old_list(self):
        self.assertNotIn("function srSplit(", self.js)       # 0.21.0: only the old replies list and One at a time used it

    def test_version(self):
        cfg = (ROOT / "callsheet" / "config.yaml").read_text(encoding="utf-8")
        self.assertRegex(cfg, r'(?m)^version: "0\.21\.2"$')  # 0.21.0 = Talk it through (brief2.py)
        self.assertIn('APP_VERSION = "0.21.2"', (APP / "api.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
