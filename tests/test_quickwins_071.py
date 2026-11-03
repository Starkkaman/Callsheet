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
        self.assertIn('function stepHome()', self.js)
        i = self.js.index("setupBtn")
        self.assertIn('ui.step = 0', self.js[i:i + 2000])
        rs = func(self.js, 'renderSetup')
        self.assertIn("'Home'", rs)
        self.assertIn('stepHome()', rs)
        self.assertNotIn('prail', self.js)
        self.assertNotIn('.prail', self.css)
        phone = re.findall(r'@media \(max-width:700px\)\{([^@]*)', self.css)
        self.assertTrue(any(re.search(r'\.rail\{display:flex', p) for p in phone), 'rail must show on phones')
        sh = func(self.js, 'stepHome')
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

    # 4. Tick and Copy on Top of the day
    def test_top_of_day_tick_copy(self):
        i = self.js.index('function T_briefing(')
        tb = self.js[i:self.js.index('function T_', i + 20)]
        self.assertIn("'data-ck'", tb)
        self.assertIn("'data-copyr'", tb)
        self.assertIn("'data-kr'", tb)
        self.assertIn("closest('.row, .trow')", self.js)
        self.assertIn('function copyReply(', self.js)
        self.assertIn('data-i="copy"', self.html)

    # 5. Legend until first use, then "?"; reasons as text labels
    def test_legend_hides_after_first_use(self):
        self.assertIn("KEY_LEGEND = 'callsheet.legend'", self.js)
        self.assertIn("load(KEY_LEGEND) === '1'", self.js)
        md = func(self.js, 'markDone')
        self.assertIn("save(KEY_LEGEND, '1')", md)
        i = self.js.index('function T_replies(')
        tr = self.js[i:self.js.index('function T_', i + 20)]
        self.assertRegex(tr, r'!used \|\| U\.legend')
        self.assertIn('legendBtn()', tr)
        lb = func(self.js, 'legendBtn')
        self.assertIn('aria-expanded', lb)
        self.assertIn('title: reasonWords(r)', self.js)

    # 6. Progress and All clear, once a day, no motion when reduced
    def test_progress_and_all_clear(self):
        self.assertIn('function meter()', self.js)
        self.assertIn(' done today', func(self.js, 'meter'))
        ac = func(self.js, 'allClear')
        self.assertIn('load(KEY_CLEAR) === day', ac)
        self.assertIn('save(KEY_CLEAR, day)', ac)
        self.assertIn("' still'", ac)
        self.assertIn('All clear', ac)
        self.assertRegex(self.block, r'prefers-reduced-motion:reduce\)\{\.bv \.clear \.shapes i,\.bv \.clear \.stamp\{animation:none')
        self.assertIn('.bv .clear.still .shapes i,.bv .clear.still .stamp{animation:none}', self.block)
        self.assertNotRegex(ac, r'Audio|\.play\(')

    # 7. Keyboard: never while typing, ? shows the sheet
    def test_keys_ignore_typing(self):
        ty = func(self.js, 'typing')
        for tag in ('INPUT', 'TEXTAREA', 'SELECT', 'isContentEditable'):
            self.assertIn(tag, ty)
        i = self.js.index("document.addEventListener('keydown'")
        kd = self.js[i:i + 4000]
        guard = kd.index('typing(t)')
        for mod in ('ctrlKey', 'metaKey', 'altKey', 'isComposing'):
            self.assertIn(mod, kd[:guard + 20])
        for key in ("'j'", "'k'", "'?'"):
            self.assertGreater(kd.index(key), guard, key + ' handled before the typing guard')
        self.assertIn('function keysEl()', self.js)
        ks = func(self.js, 'keysEl')
        self.assertIn("role: 'dialog'", ks)
        self.assertIn('nothing is sent', ks)

    # 8. Labels under the phone header icons
    def test_phone_header_labels(self):
        self.assertEqual(self.html.count('<span class="bl">'), 3)
        self.assertIn('.hbtn .bl{font-size:11px', self.css)
        self.assertIn('flex-direction:column', self.css)

    def test_tokens_only(self):
        self.assertIsNone(re.search(r'#[0-9a-fA-F]{3,8}\b', self.block), 'use theme tokens, not hex colours')
        self.assertNotRegex(self.block, r'rgba?\(')


if __name__ == '__main__':
    unittest.main()
