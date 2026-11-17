"""0.8.2 People, the acceptance rules' adversarial cases before ship (one test per case and channel):
S-T3 content that claims to be the owner, S-T4 content that asks to change the owner's contact, S-T5 the owner's
display name from elsewhere, S-T6 the owner's own address with a failed sender check (DMARC), S-T17 a VIP's display
name from elsewhere, S-T19 the owner's words inside quoted history or a forward, S-L3 a signature that claims someone
else's address. Pass: nothing is ever merged or proposed into "me", name-only or signature-only evidence never gives
VIP status, and the row carries the right mark. Plus the second confirm step for a merge into a VIP and "Undo merge"
(Split) restoring everything exactly."""
import datetime as dt
import http.client
import json
import pathlib
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import api  # noqa: E402
import testindex as ix  # noqa: E402
import people as pp  # noqa: E402
import push  # noqa: E402
import rules  # noqa: E402
import server  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
DAY = 86400
ME_ACC = "alex@morgan.example"
ME_SLACK = "U01ME"
FAIL = "mx.google.com; dkim=fail header.i=@morgan.example; spf=fail smtp.mailfrom=morgan.example; " \
       "dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=morgan.example"
PASS = "mx.google.com; dkim=pass header.i=@morgan.example; spf=pass smtp.mailfrom=morgan.example; " \
       "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=morgan.example"
CAUTION = "Only evidence: a signature in a message from mallory@evil.example; check before merging."


def mail(sid, frm, body="Thanks.", kind="email", to=ME_ACC):
    return {"source": "gmail", "account": "acc1", "source_id": sid, "kind": kind, "title": "Hello", "body": body,
            "people": f"{frm}; Alex Morgan <{to}>", "area": "work", "ts": NOW - 2 * DAY,
            "url": "https://mail.google.com/mail/u/0/#all/" + sid}


def gmsg(mid, frm, snippet, ts, auth=None, labels=("INBOX",), subject="Deposit"):
    hd = [{"name": "From", "value": frm}, {"name": "To", "value": ME_ACC}, {"name": "Subject", "value": subject}]
    if auth:
        hd.insert(0, {"name": "Authentication-Results", "value": auth})
    return {"id": mid, "labelIds": list(labels), "internalDate": str(int(ts * 1000)), "snippet": snippet,
            "payload": {"headers": hd}}


def reply_row(*msgs, vips=None):
    return rules.reply_row({"id": "t1", "messages": list(msgs)}, ME_ACC, "work", NOW, 2, vips or pp.VipMap())


def vipmap(emails=None, slack=(), signal=()):
    v = pp.VipMap(emails or {})
    v.slack, v.signal = frozenset(slack), frozenset(signal)
    return v


class Base(unittest.TestCase):
    """An App (people, store, index) with the owner's Google account and Slack workspace connected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [NOW]
        self.app = api.App(self.tmp.name, clock=lambda: self.now[0])
        self.addCleanup(self.app.close)
        self.store, self.index = self.app.store, self.app.index
        self.pp = pp.People(self.store, self.index, clock=lambda: self.now[0])
        self.app.people = self.pp
        self.store.update_settings({"timezone": "UTC", "name": "Alex Morgan"})
        self.work = self.store.add_area("Work", 3)["id"]
        self.store.save_account(self.work, ME_ACC, "r", "a", NOW + 3600, NOW)
        self.store.save_slack(self.work, {"team_id": "T1", "team_name": "Acme", "domain": "acme", "user_id": ME_SLACK,
                                          "user_name": "alex"}, "xoxp-test", NOW)
        self.store.signal_update_meta(linked=True)
        self.pp.my_names(["Alex", "Al"])
        self.users = {}
        self.me = {("email", ME_ACC), ("slack", ME_SLACK.lower()), ("signal", "me")}

    # fixtures ------------------------------------------------------------------------------------------------------
    def slack_user(self, uid, name, real_name="", email=""):
        self.users[uid] = {"name": name, "real_name": real_name, "email": email}
        ws = self.store.slack_workspaces()[0]
        self.store.set_slack_state(ws["id"], {"users": self.users})

    def signal(self, sender, name, text, kind="dm", quotes_me=False, ts=None):
        self.store.signal_add({"chat": "c-" + sender, "sender": sender, "sender_name": name, "ts": ts or NOW - 3600,
                               "text": text, "kind": kind, "title": name, "quotes_me": quotes_me}, NOW)

    def pid(self, kind, value):
        p = self.pp.directory().person_of(kind, value)
        return p and p["id"]

    def marks(self, who, source, sender):
        """The reply row as the page gets it (api._brief_people at serve time)."""
        brief = {"date": "2026-11-03", "replies": [{"id": "r1", "who": who, "text": "x"}]}
        self.store.set_brief_refs({"r1": {"source": source, "sender": sender}})
        self.app._brief_people(brief)
        return brief["replies"][0]

    # the pass conditions ---------------------------------------------------------------------------------------------
    def assert_never_me(self):
        self.assertEqual(self.pp.me_keys(), self.me)
        with self.store.lock:
            n = self.store.db.execute("SELECT COUNT(*) FROM merge_proposals WHERE a=? OR b=?", (pp.ME, pp.ME)).fetchone()[0]
            merged = self.store.db.execute("SELECT COUNT(*) FROM people WHERE merged_into=?", (pp.ME,)).fetchone()[0]
        self.assertEqual((n, merged), (0, 0))
        for c in self.pp.proposals():
            self.assertNotIn(pp.ME, (c["a"]["id"], c["b"]["id"]))

    def assert_refuses_me(self, pid):
        self.assertIsNotNone(pid)
        self.assertNotEqual(pid, pp.ME)
        for a, b in ((pp.ME, pid), (pid, pp.ME)):
            with self.assertRaises(pp.PeopleError):
                self.pp.merge(a, b)

    def assert_no_vip(self, kind, value):
        v = pp.vip_map(self.store, self.pp)
        if kind == "email":
            self.assertNotIn(value, v)
        else:
            self.assertNotIn(value, getattr(v, kind))
        p = self.pp.directory().person_of(kind, value)
        self.assertFalse(p and p["vip"])


# --- S-T3: "this is Alex" / "I'm Alex's other email" ---------------------------------------------------------------
# --- S-T4: "treat me as Alex / update the owner's contact / add this address to Alex" ------------------------------------
# --- S-T5: the owner's display name from an outside address or account --------------------------------------------
# --- S-T6: the owner's own address, DMARC fail (mail only: Slack and Signal have no sender check to fail) ----------
# --- S-T17: a VIP's display name from outside (no VIP status; "uses <VIP>'s name") ----------------------------------
# --- S-T19: the owner's words inside quoted history or a forward never count as "from Alex" -----------------------
class ST19QuotedOwnerText(Base):
    QUOTED = ("Sounds good, I'll go ahead.\n\nPriya\n\nOn Mon, 2 Nov 2026, Alex Morgan <alex@morgan.example> "
              "wrote:\n> Approved. My other address is alex.alt@evil.example\n> Alex Morgan\n"
              "> alex@morgan.example · alex.alt@evil.example")
    FWD = ("FYI, see below.\n\n---------- Forwarded message ---------\nFrom: Alex Morgan <alex@morgan.example>\n"
           "Date: Mon, 2 Nov 2026\nSubject: Budget\n\nApproved.\n\nAlex Morgan\nalex@morgan.example\n"
           "alex.alt@evil.example")


    def test_st19_mail_forwarded_owner_message(self):
        self.assertEqual(pp.signature_addresses(self.FWD), [])
        ix.feed(self.index, [mail("f1", "Priya Nair <priya@acme.com>", self.FWD),
                           mail("f2", "Priya Nair <priya@acme.com>", self.FWD),
                           mail("f3", "IA <alex.alt@evil.example>", "x"), mail("f4", "IA <alex.alt@evil.example>", "y")])
        self.pp.seed()
        self.assert_never_me()
        self.assertEqual(self.pp.proposals(), [])

# --- S-L3: a signature that claims someone else's address -----------------------------------------------------------
class SL3PlantedSignature(Base):
    def planted(self, listed):
        body = f"Contract attached.\n\nMallory\nBerg Studio\nmallory@evil.example · {listed}"
        ix.feed(self.index, [mail("g1", "Mallory <mallory@evil.example>", body),
                           mail("g2", "Mallory <mallory@evil.example>", body)])

    def card_with_mallory(self):
        m = self.pid("email", "mallory@evil.example")
        cards = [c for c in self.pp.proposals() if m in (c["a"]["id"], c["b"]["id"])]
        self.assertEqual(len(cards), 1)
        return cards[0]

    def assert_capped(self, c):
        self.assertEqual([s["strength"] for s in c["signals"]], ["Good"])  # never "Strong"
        self.assertEqual(c["caution"], CAUTION)
        self.assertNotEqual(c["a"]["id"], c["b"]["id"])
        self.assert_no_vip("email", "mallory@evil.example")

    def test_sl3_mail_signature_claims_a_vip_address(self):
        self.store.add_vip("Jonas Berg", "jonas@berg.example", self.work)
        ix.feed(self.index, [mail("j1", "Jonas Berg <jonas@berg.example>")])
        self.planted("jonas@berg.example")
        self.pp.seed()
        c = self.card_with_mallory()
        self.assert_capped(c)
        out = self.pp.decide(c["id"], "same")  # the VIP merge stops at the second step, evidence shown again
        self.assertTrue(out["needs_confirm"])
        self.assertEqual((out["evidence"][0]["strength"], out["caution"]), ("Good", CAUTION))
        self.assertNotEqual(self.pid("email", "mallory@evil.example"), self.pid("email", "jonas@berg.example"))
        self.assert_no_vip("email", "mallory@evil.example")

    def test_sl3_mail_signature_claims_the_owner_address(self):
        self.planted(ME_ACC)
        self.pp.seed()
        self.assert_never_me()
        self.assertEqual(self.pp.proposals(), [])
        self.assert_refuses_me(self.pid("email", "mallory@evil.example"))

    def test_sl3_slack_signature_claims_a_slack_verified_person(self):
        self.slack_user("U07PN", "pn", "", "priya@acme.com")
        ix.feed(self.index, [mail("h1", "Priya Nair <priya@acme.com>"), mail("h2", "Priya Nair <priya@acme.com>")])
        self.pp.seed()
        for c in self.pp.proposals():
            self.pp.decide(c["id"], "same")  # Slack profile email: the owner merges (no VIP, one step)
        self.assertEqual(self.pid("slack", "u07pn"), self.pid("email", "priya@acme.com"))
        self.planted("priya@acme.com")
        self.pp.seed()
        self.assert_capped(self.card_with_mallory())
        self.assert_no_vip("slack", "u07pn")

    def test_sl3_signal_signature_claims_a_signal_verified_person(self):
        self.signal("s123", "Priya Nair", "Call me?")
        ix.feed(self.index, [mail("h1", "Priya Nair <priya@acme.com>"), mail("h2", "Priya Nair <priya@acme.com>")])
        self.pp.seed()
        self.pp.merge(self.pid("email", "priya@acme.com"), self.pid("signal", "s123"))
        self.planted("priya@acme.com")
        self.pp.seed()
        self.assert_capped(self.card_with_mallory())
        self.assert_no_vip("signal", "s123")


# --- the second step for a merge into a VIP, and Undo merge (Split) -----------------------------------------------
class ConfirmAndUndo(Base):
    def setUp(self):
        super().setUp()
        self.store.add_vip("Priya Nair", "priya@acme.com", self.work)
        self.personal = self.store.add_area("Personal", 2)["id"]
        ix.feed(self.index, [
            mail("m1", "Priya Nair <priya@acme.com>", "Budget.\n\nPriya Nair\npriya@acme.com · pn@gmail.com"),
            mail("m2", "Priya Nair <priya@acme.com>"), mail("m3", "PN <pn@gmail.com>"), mail("m4", "PN <pn@gmail.com>"),
            mail("m5", "PN <p.n@googlemail.com>"), mail("m6", "PN <p.n@googlemail.com>")])
        self.slack_user("U07PN", "pn", "", "pn@gmail.com")
        self.pp.seed()
        self.priya, self.pn = self.pid("email", "priya@acme.com"), self.pid("email", "pn@gmail.com")
        self.pn2, self.sl = self.pid("email", "p.n@googlemail.com"), self.pid("slack", "u07pn")

    def card(self, x, y):
        for c in self.pp.proposals():
            if {c["a"]["id"], c["b"]["id"]} == {x, y}:
                return c
        self.fail(f"no card for {x} and {y}")

    def snapshot(self):
        with self.store.lock:
            db = self.store.db
            people = sorted(tuple(r[k] for k in r.keys() if k != "updated") for r in db.execute("SELECT * FROM people"))
            handles = sorted(tuple(r) for r in db.execute("SELECT * FROM person_handles"))
            props = sorted(tuple(r) for r in db.execute("SELECT * FROM merge_proposals"))
        v = pp.vip_map(self.store, self.pp)
        d = self.pp.directory()
        notes = {e: d.note_people(emails=[e]) for e in ("priya@acme.com", "pn@gmail.com", "p.n@googlemail.com")}
        notes["name:Priya N."] = d.note_people(names=["Priya N."])
        return {"people": people, "handles": handles, "props": props, "vip_list": self.store.vips(),
                "vip_map": (dict(v), v.slack, v.signal), "notes": notes, "me": self.pp.me_keys()}

    def test_confirm_vip_merge_needs_second_step_with_evidence(self):
        c = self.card(self.priya, self.pn)
        before = self.snapshot()
        out = self.pp.decide(c["id"], "same")
        self.assertEqual((out["ok"], out["needs_confirm"], out["vip"]), (False, True, "Priya Nair"))
        self.assertEqual(out["evidence"], [{"strength": "Good", "why": "The signature in mail from priya@acme.com "
                                                                      "lists pn@gmail.com."}])
        self.assertTrue(out["confirm"])
        self.assertEqual(self.snapshot(), before)  # the first answer changes nothing
        with self.assertRaises(pp.PeopleError):
            self.pp.decide(c["id"], "same", "not-the-token")
        res = self.pp.decide(c["id"], "same", out["confirm"])
        self.assertEqual(res["state"], "same")
        self.assertEqual(self.pid("email", "pn@gmail.com"), self.priya)
        with self.assertRaises(pp.PeopleError):  # one use
            self.pp.decide(c["id"], "same", out["confirm"])

    def test_confirm_token_is_bound_to_the_pair_and_expires(self):
        c = self.card(self.priya, self.pn)
        other = self.pp.merge(self.priya, self.pn2)
        self.assertTrue(other["needs_confirm"])
        with self.assertRaises(pp.PeopleError):  # a token for one pair never merges another
            self.pp.decide(c["id"], "same", other["confirm"])
        out = self.pp.decide(c["id"], "same")
        self.now[0] += pp.CONFIRM_S + 1
        with self.assertRaises(pp.PeopleError):
            self.pp.decide(c["id"], "same", out["confirm"])
        self.assertNotEqual(self.pid("email", "pn@gmail.com"), self.priya)

    def test_confirm_not_needed_without_a_vip_and_never_enough_for_me(self):
        out = self.pp.decide(self.card(self.pn, self.sl)["id"], "same")
        self.assertEqual(out["state"], "same")
        with self.assertRaises(pp.PeopleError):
            self.pp.merge(pp.ME, self.priya, confirm="anything")

    def test_undo_merge_restores_vip_overrides_notes_and_handles_exactly(self):
        self.pp.decide(self.card(self.pn, self.sl)["id"], "same")  # PN + their Slack user: one person, no VIP
        pn = self.pid("email", "pn@gmail.com")
        self.pp.patch(pn, {"name": "Priya N.", "area": self.personal})  # the owner's overrides on PN
        before = self.snapshot()
        self.assertEqual(before["notes"]["pn@gmail.com"], {pn})
        self.assertNotIn("u07pn", before["vip_map"][1])
        c = self.card(self.priya, pn)
        out = self.pp.decide(c["id"], "same")
        self.pp.decide(c["id"], "same", out["confirm"])
        during = self.snapshot()
        self.assertIn("u07pn", during["vip_map"][1])  # VIP covers PN's handles while merged
        self.assertEqual(during["notes"]["pn@gmail.com"], {self.priya})
        self.assertEqual(self.card(self.priya, self.pn2)["id"], pp.pair_id(self.priya, self.pn2))  # moved over
        merge = next(h for h in self.pp.audit() if h["action"] == "merge" and h["can_split"] and "Priya N." in h["text"])
        self.pp.split(merge["id"])  # Undo merge
        after = self.snapshot()
        pair = pp.pair_id(self.priya, pn)
        for k in ("people", "handles", "vip_list", "vip_map", "notes", "me"):
            self.assertEqual(after[k], before[k], k)
        strip = [r for r in after["props"] if r[0] != pair]
        self.assertEqual(strip, [r for r in before["props"] if r[0] != pair])
        self.assertEqual(next(r for r in after["props"] if r[0] == pair)[6], "different")  # never asked again

    def test_undo_merge_after_later_edits_takes_off_only_what_the_merge_added(self):
        c = self.card(self.priya, self.pn)
        self.pp.decide(c["id"], "same", self.pp.decide(c["id"], "same")["confirm"])
        self.pp.patch(self.priya, {"name": "Priya Nair-Smith"})
        self.pp.split(self.pp.audit()[0]["id"])
        p = self.pp._row(self.priya)
        self.assertEqual((p["name"], p["vip"], p["aliases"]), ("Priya Nair-Smith", 1, []))
        self.assertEqual(self.pp._row(self.pn)["state"], "suggested")
        self.assertEqual(self.pid("email", "pn@gmail.com"), self.pn)


class ConfirmApi(Base):
    def setUp(self):
        super().setUp()
        self.store.add_vip("Priya Nair", "priya@acme.com", self.work)
        ix.feed(self.index, [
            mail("m1", "Priya Nair <priya@acme.com>", "Budget.\n\nPriya Nair\npriya@acme.com · pn@gmail.com"),
            mail("m2", "Priya Nair <priya@acme.com>"), mail("m3", "PN <pn@gmail.com>"), mail("m4", "PN <pn@gmail.com>")])
        self.httpd = server.make_server("127.0.0.1", 0, app=self.app)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=30)
        headers = {"X-Callsheet": "1", "Content-Type": "application/json"}
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        return r.status, (json.loads(text) if text else None)

    def test_api_vip_merge_two_posts_then_undo(self):
        s, v = self.call("GET", "/api/people")
        c = v["proposals"][0]
        s, first = self.call("POST", f"/api/people/proposals/{c['id']}", {"answer": "same"})
        self.assertEqual((s, first["needs_confirm"], first["open"]), (200, True, 1))
        self.assertEqual([e["strength"] for e in first["evidence"]], ["Good"])
        self.assertEqual(first["caution"], "Only evidence: a signature in a message from priya@acme.com; check before "
                                           "merging.")
        self.assertEqual(self.call("POST", f"/api/people/proposals/{c['id']}", {"answer": "same", "confirm": 7})[0], 400)
        self.assertEqual(self.call("POST", f"/api/people/proposals/{c['id']}",
                                   {"answer": "same", "confirm": "wrong"})[0], 400)
        s, out = self.call("POST", f"/api/people/proposals/{c['id']}", {"answer": "same", "confirm": first["confirm"]})
        self.assertEqual((s, out["state"], out["open"]), (200, "same", 0))
        merge = out["person"]["merges"][0]["id"]
        self.assertEqual(self.call("POST", f"/api/people/merges/{merge}/split", {})[0], 200)
        s, v = self.call("GET", "/api/people?q=pn%40gmail")
        self.assertEqual([(p["name"], p["vip"]) for p in v["people"]], [("PN", False)])


class ConfirmPage(unittest.TestCase):
    def test_page_second_step_shows_the_evidence(self):
        js = (ROOT / "callsheet" / "app" / "static" / "app.js").read_text()
        a = js.index("function sameCard(")
        card = js[a:js.index("function ppAnswered(", a)]
        self.assertNotIn("innerHTML", card)
        for s in ("r.needs_confirm", "askConfirm(r)", "'Merge into a VIP?'", "'Evidence'", "sigList(r.evidence)",
                  "r.caution ? h('p', { class: 'scaution' }, r.caution)", "{ answer: a, confirm: token }",
                  "'Yes, merge'", "'Cancel'"):
            self.assertIn(s, card)
        css = (ROOT / "callsheet" / "app" / "static" / "app.css").read_text()
        self.assertIn(".same .sconfirm{", css)


if __name__ == "__main__":
    unittest.main()
