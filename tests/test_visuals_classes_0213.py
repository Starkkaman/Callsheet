"""0.21.3: the bills ladder rows used the class "row", which the brief view's own ".bv .row" grid (30px columns)
also styles, so names were squeezed into a 30 px column. Picture blocks use vz- classes for their layout."""
import pathlib
import re
import unittest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "callsheet" / "app" / "static"


class VisualClasses(unittest.TestCase):
    def test_no_app_layout_classes_in_visuals(self):
        js = (STATIC / "visuals.js").read_text()
        used = set()
        for m in re.finditer(r"el\('[a-z0-9]+', '([^']+)'", js):
            used.update(m.group(1).split())
        app = (STATIC / "app.css").read_text()
        styled = set(re.findall(r"\.bv \.([a-z][a-z0-9-]*)\b", app))
        self.assertEqual(sorted(used & styled - {"note"}), [])


if __name__ == "__main__":
    unittest.main()
