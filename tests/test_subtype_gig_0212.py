"""0.21.2: the event subtype "shoot" is now "gig" (extract.SUBTYPES). A model that still answers "shoot" gets "gig"
(extract.SUBTYPE_ALIASES), and ledger.ensure renames stored rows once they are opened. In-process only."""
import pathlib
import shutil
import sys
import tempfile
import unittest

APP = pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"
sys.path.insert(0, str(APP))
import conflicts  # noqa: E402
import extract  # noqa: E402
import ledger  # noqa: E402
import msgstore  # noqa: E402


class GigTest(unittest.TestCase):
    def test_the_names(self):
        self.assertIn("gig", extract.SUBTYPES)
        self.assertNotIn("shoot", extract.SUBTYPES)
        self.assertEqual(extract.SUBTYPE_ALIASES, {"shoot": "gig"})
        self.assertIn("gig", conflicts.OUT_SUBTYPES)
        self.assertIn("gig", ledger.ATTEND_SUBTYPES)
        self.assertNotIn("shoot", conflicts.OUT_SUBTYPES | ledger.ATTEND_SUBTYPES | conflicts.CALL)

    def test_stored_rows_are_renamed(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        ms = msgstore.MessageStore(d)
        self.addCleanup(ms.close)
        ledger.ensure(ms)

        def add(db):
            cols = [r for r in db.execute("PRAGMA table_info(ledger_items)") if r[3] and r[4] is None and not r[5]]
            for i, sub in enumerate(("shoot", "job")):
                vals = {c[1]: (0 if "INT" in (c[2] or "").upper() or "REAL" in (c[2] or "").upper() else "x")
                        for c in cols}
                vals["subtype"] = sub
                db.execute(f"INSERT INTO ledger_items ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                           list(vals.values()))
        ms.write(add)
        ledger.ensure(ms)
        rows = ms.write(lambda db: [r[0] for r in db.execute("SELECT subtype FROM ledger_items ORDER BY rowid")])
        self.assertEqual(rows, ["gig", "job"])


if __name__ == "__main__":
    unittest.main()
