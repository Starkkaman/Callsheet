"""0.7.1 quick wins (design audit): static page checks.

Setup home and the phone rail, help behind "?", stale area-card lines gone, Top of the day tick and copy,
the Replies legend hiding after first use, progress and All clear (once a day, still under reduced motion),
the keyboard (never while typing), labels under the phone header icons.
"""
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), '..', 'callsheet', 'app', 'static')


def read(name):
    with open(os.path.join(ROOT, name), encoding='utf-8') as f:
        return f.read()


def func(js, name):
    """The source of `function name(` up to the next top-level-ish `function ` declaration."""
    i = js.index('function ' + name + '(')
    j = js.find('\n  function ', i + 10)
    return js[i:j if j > 0 else len(js)]


class QuickWins071(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = read('app.js')
        cls.css = read('app.css')
        cls.html = read('index.html')
        i = cls.css.index('0.7.1 quick wins')
        cls.block = cls.css[i:]

    # 1. Setup opens on a home page; the rail shows on phones
    def test_setup_opens_home(self):
        self.assertIn('function setupHome()', self.js)  # 0.9.0: Setup home summarises the four places
        i = self.js.index("setupBtn")
        self.assertIn("openSetup('home')", self.js[i:i + 2000])
        rs = func(self.js, 'renderSetup')
        self.assertIn("'Home'", rs)
        self.assertIn('setupHome()', rs)
        self.assertNotIn('prail', self.js)
        self.assertNotIn('.prail', self.css)
        phone = re.findall(r'@media \(max-width:700px\)\{([^@]*)', self.css)
        self.assertTrue(any(re.search(r'\.rail\{display:flex', p) for p in phone), 'rail must show on phones')
        sh = func(self.js, 'setupHome')
        self.assertIn("'Edit'", sh)

    # 2. Help folds behind "?", remembered per device, one sentence stays
    def test_help_folds(self):
        self.assertIn("KEY_HELP = 'callsheet.help'", self.js)
        fh = func(self.js, 'foldHelp')
        self.assertIn('helpq', fh)
        self.assertIn('aria-expanded', fh)
        self.assertIn('aria-controls', fh)
        self.assertIn('save(KEY_HELP', fh)
        self.assertIn('firstSentence(', fh)
        self.assertIn('foldHelp(', func(self.js, 'renderSetup'))

    # 3. Stale area-card lines are gone
    def test_stale_area_lines_gone(self):
        self.assertNotIn('own column', self.js)
        self.assertNotIn("later('Tasks')", self.js)
        self.assertNotIn("Tasks · coming later", self.js)

    # 4. Tick and Copy on Top of the day (0.21.0: the old Briefing view, T_briefing, is gone; Copy stays for replies)
    def test_top_of_day_tick_copy(self):
        self.assertNotIn('function T_briefing(', self.js)
        self.assertNotIn('function copyReply(', self.js)     # the rework: the old reply rows (and C) are gone
        self.assertIn('data-i="copy"', self.html)

    # 5 and 6. The legend, progress and All clear lived on the old replies list (T_replies), gone in 0.21.0 (Replies
    # from the ledger); the reasons stay text labels
    def test_old_replies_list_is_gone(self):
        for name in ("T_replies", "legend", "legendBtn", "legendUsed", "meter", "allClear", "doneRows"):
            self.assertNotIn("function " + name + "(", self.js)
        self.assertNotIn("KEY_CLEAR", self.js)
        for name in ('rowEl', 'reasonWords', 'reasonOf'):   # the rework: the old reply rows are gone
            self.assertNotIn('function ' + name + '(', self.js)

    # 7. Keyboard: never while typing, ? shows the sheet
    def test_keys_ignore_typing(self):
        ty = func(self.js, 'typing')
        for tag in ('INPUT', 'TEXTAREA', 'SELECT', 'isContentEditable'):
            self.assertIn(tag, ty)
        self.assertIn("!e.isComposing && !typing(e.target)) { e.preventDefault(); faOpen(); }", self.js)   # Find's "/"
        # the rework: J/K, D, C, O, N and the "?" key sheet worked on the old reply rows only; they went with them
        for name in ('keysEl', 'openKeys', 'keyAct', 'keyMove'):
            self.assertNotIn('function ' + name + '(', self.js)

    # 8. Labels under the phone header icons
    def test_phone_header_labels(self):
        self.assertEqual(self.html.count('<span class="bl">'), 3)  # 0.20.0: Find, Setup, Look (Refresh went: reading is live)
        self.assertIn('.hbtn .bl{font-size:11px', self.css)
        self.assertIn('flex-direction:column', self.css)

    def test_tokens_only(self):
        self.assertIsNone(re.search(r'#[0-9a-fA-F]{3,8}\b', self.block), 'use theme tokens, not hex colours')
        self.assertNotRegex(self.block, r'rgba?\(')


if __name__ == '__main__':
    unittest.main()
