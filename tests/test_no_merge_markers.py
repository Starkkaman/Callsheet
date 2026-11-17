"""No leftover merge-conflict markers in shipped files (a stray ======= line drops the CSS rule after it)."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent / "callsheet"
MARK = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.M)


class NoMergeMarkers(unittest.TestCase):
    def test_none(self):
        hits = [f"{p.relative_to(ROOT)}:{s.count(chr(10), 0, m.start()) + 1}"
                for p in ROOT.rglob("*") if p.suffix in {".py", ".js", ".css", ".html", ".md", ".yaml", ".json", ".sh"}
                for s in [p.read_text(errors="replace")] for m in MARK.finditer(s)]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
