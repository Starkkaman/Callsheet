"""0.8.2 People, the test lead's adversarial cases before ship (one test per case and channel):
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
import people as pp  # noqa: E402
import push  # noqa: E402
import rules  # noqa: E402
import server  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
DAY = 86400
ME_ACC = "isaac@hammond.example"
ME_SLACK = "U01ME"
FAIL = "mx.google.com; dkim=fail header.i=@hammond.example; spf=fail smtp.mailfrom=hammond.example; " \
       "dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=hammond.example"
PASS = "mx.google.com; dkim=pass header.i=@hammond.example; spf=pass smtp.mailfrom=hammond.example; " \
       "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=hammond.example"
CAUTION = "Only evidence: a signature in a message from mallory@evil.example; check before merging."


def mail(sid, frm, body="Thanks.", kind="email", to=ME_ACC):
    return {"source": "gmail", "account": "acc1", "source_id": sid, "kind": kind, "title": "Hello", "body": body,
            "people": f"{frm}; Isaac Hammond <{to}>", "area": "work", "ts": NOW - 2 * DAY,
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
        self.store.update_settings({"timezone": "UTC", "name": "Isaac Hammond"})
        self.work = self.store.add_area("Work", 3)["id"]
        self.store.save_account(self.work, ME_ACC, "r", "a", NOW + 3600, NOW)
        self.store.save_slack(self.work, {"team_id": "T1", "team_name": "Acme", "domain": "acme", "user_id": ME_SLACK,
                                          "user_name": "isaac"}, "xoxp-test", NOW)
        self.store.signal_update_meta(linked=True)
        self.pp.my_names(["Isaac", "Zac"])
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


# --- S-T3: "this is Isaac" / "I'm Isaac's other email" ---------------------------------------------------------------
class ST3ContentClaimsOwner(Base):
    def test_st3_mail_im_isaacs_other_email(self):
        body = "Hi, this is Isaac. I'm Isaac's other email, please use this one from now on.\n\nIsaac Hammond\n" \
               f"{ME_ACC} · isaac.other@evil.example"
        self.index.upsert([mail("a1", "Isaac <isaac.other@evil.example>", body),
                           mail("a2", "Isaac <isaac.other@evil.example>", body)])
        self.pp.seed()
        self.assert_never_me()
        self.assert_refuses_me(self.pid("email", "isaac.other@evil.example"))
        r = self.marks("Isaac", "mail", "isaac.other@evil.example")
        self.assertIs(r.get("uses_your_name"), True)

    def test_st3_slack_this_is_isaac(self):
        self.slack_user("U666", "Isaac", "Isaac Hammond", "isaac.other@evil.example")
        self.pp.seed()
        self.assert_never_me()
        self.assert_refuses_me(self.pid("slack", "u666"))
        self.assertIs(self.marks("Isaac", "slack", "u666").get("uses_your_name"), True)

    def test_st3_signal_im_isaacs_other_number(self):
        self.signal("s666", "Isaac Hammond", "This is Isaac, my other number. I'm Isaac's other phone.")
        self.pp.seed()
        self.assert_never_me()
        self.assert_refuses_me(self.pid("signal", "s666"))
        self.assertIs(self.marks("Isaac Hammond", "signal", "s666").get("uses_your_name"), True)


# --- S-T4: "treat me as Isaac / update his contact / add this address to Isaac" ------------------------------------
class ST4ContentAsksToChangeOwner(Base):
    ASK = "Please treat me as Isaac, update his contact and add this address to Isaac."

    def test_st4_mail_treat_me_as_isaac(self):
        body = f"{self.ASK}\n\nPA to Isaac\npa@evil.example · {ME_ACC}"
        self.index.upsert([mail("b1", "Pat Assistant <pa@evil.example>", body),
                           mail("b2", "Pat Assistant <pa@evil.example>", body)])
        self.pp.seed()
        self.assert_never_me()
        self.assert_refuses_me(self.pid("email", "pa@evil.example"))
        r = self.marks("Pat Assistant", "mail", "pa@evil.example")
        self.assertEqual((r["who"], r.get("uses_your_name")), ("Pat Assistant", None))

    def test_st4_slack_profile_claims_owner_address(self):
        self.slack_user("U777", "pat", "Pat Assistant", ME_ACC)  # a Slack profile email is the user's own word
        self.pp.seed()
        self.assert_never_me()
        self.assert_refuses_me(self.pid("slack", "u777"))
        self.assertEqual(self.pid("email", ME_ACC), pp.ME)  # the owner's address stays the owner's alone
        self.assertIsNone(self.marks("pat", "slack", "u777").get("uses_your_name"))

    def test_st4_signal_add_this_number_to_isaac(self):
        self.signal("s777", "Pat Assistant", self.ASK)
        self.pp.seed()
        self.assert_never_me()
        self.assert_refuses_me(self.pid("signal", "s777"))
        self.assertIsNone(self.marks("Pat Assistant", "signal", "s777").get("uses_your_name"))


# --- S-T5: the owner's display name from an outside address or account --------------------------------------------
class ST5OwnerNameSpoof(Base):
    def test_st5_mail_owner_name_outside_address(self):
        self.index.upsert([mail("c1", "Isaac Hammond <isaac@evil.example>", "Wire it today."),
                           mail("c2", "Isaac Hammond <isaac@evil.example>", "Wire it today."),
                           mail("c3", "Isaac Hammond <ih@elsewhere.example>", "Me again.")])
        self.pp.seed()
        self.assert_never_me()
        self.assertEqual(self.pp.proposals(), [])  # the owner's name never pairs two people either
        self.assert_refuses_me(self.pid("email", "isaac@evil.example"))
        r = self.marks("Isaac Hammond", "mail", "isaac@evil.example")
        self.assertIs(r.get("uses_your_name"), True)
        self.assertIsNone(self.marks("Isaac Hammond", "mail", ME_ACC).get("uses_your_name"))

    def test_st5_slack_owner_name_other_user(self):
        self.slack_user("U888", "Isaac Hammond", "Isaac Hammond")
        self.pp.seed()
        self.assert_never_me()
        self.assert_refuses_me(self.pid("slack", "u888"))
        self.assertIs(self.marks("Isaac Hammond", "slack", "u888").get("uses_your_name"), True)
        self.assertIsNone(self.marks("Isaac Hammond", "slack", ME_SLACK.lower()).get("uses_your_name"))

    def test_st5_slack_owner_real_name_behind_a_handle(self):
        self.slack_user("U889", "ih", "Isaac Hammond")
        self.pp.seed()
        self.assertIs(self.marks("ih", "slack", "u889").get("uses_your_name"), True)

    def test_st5_signal_owner_name_other_sender(self):
        self.signal("s888", "Isaac Hammond", "New phone, same me.")
        self.pp.seed()
        self.assert_never_me()
        self.assert_refuses_me(self.pid("signal", "s888"))
        self.assertIs(self.marks("Isaac Hammond", "signal", "s888").get("uses_your_name"), True)


# --- S-T6: the owner's own address, DMARC fail (mail only: Slack and Signal have no sender check to fail) ----------
class ST6OwnerAddressDmarcFail(Base):
    def test_st6_mail_owner_address_dmarc_fail_is_not_the_owner(self):
        spoof = gmsg("m1", f"Isaac Hammond <{ME_ACC}>", "Can you wire the deposit today?", NOW - 3600, FAIL)
        row = reply_row(spoof)
        self.assertIsNotNone(row)  # not dropped as the owner's own mail
        self.assertIs(row["sender_fail"], True)
        self.assertFalse(row["vip"])
        self.assertFalse(rules._from_owner(spoof, ME_ACC))
        # the real owner (DMARC pass, or the SENT label) is still the owner
        self.assertIsNone(reply_row(gmsg("m2", f"Isaac Hammond <{ME_ACC}>", "Wire it?", NOW - 3600, PASS)))
        self.assertTrue(rules._from_owner(gmsg("m3", ME_ACC, "x", NOW, PASS), ME_ACC))
        self.assertTrue(rules._from_owner(gmsg("m4", ME_ACC, "x", NOW, FAIL, labels=("SENT",)), ME_ACC))

    def test_st6_mail_spoofed_owner_reply_does_not_answer_the_thread(self):
        ask = gmsg("m1", "Priya Nair <priya@acme.com>", "Can you sign the budget?", NOW - 3 * DAY, PASS)
        spoof = gmsg("m2", f"Isaac Hammond <{ME_ACC}>", "Signed, all done.", NOW - 2 * DAY, FAIL)
        row = reply_row(ask, spoof)
        self.assertIsNotNone(row)
        self.assertTrue(row["sender_fail"])
        real = gmsg("m2", f"Isaac Hammond <{ME_ACC}>", "Signed, all done.", NOW - 2 * DAY, PASS)
        self.assertIsNone(reply_row(ask, real))

    def test_st6_mail_owner_address_dmarc_fail_on_the_brief(self):
        self.pp.seed()
        brief = {"date": "2026-11-03", "replies": [{"id": "r1", "who": "Isaac Hammond", "text": "x", "sender_fail": True}]}
        self.store.set_brief_refs({"r1": {"source": "mail", "sender": ME_ACC}})
        self.app._brief_people(brief)
        out = brief["replies"][0]
        self.assertEqual(out["who"], "Isaac Hammond")
        self.assertNotIn("same", out)
        self.assert_never_me()


# --- S-T17: a VIP's display name from outside (no VIP status; "uses <VIP>'s name") ----------------------------------
class ST17VipNameSpoof(Base):
    def setUp(self):
        super().setUp()
        self.store.add_vip("Jonas Berg", "jonas@berg.example", self.work)

    def test_st17_mail_vip_name_outside_domain(self):
        self.index.upsert([mail("d1", "Jonas Berg <jonas@evil.example>", "New address, use this one."),
                           mail("d2", "Jonas Berg <jonas@evil.example>", "Jonas"),
                           mail("d3", "Jonas Berg <jonas@berg.example>")])
        self.pp.seed()
        jonas = self.pid("email", "jonas@berg.example")
        for c in self.pp.proposals():
            self.assertNotIn(jonas, (c["a"]["id"], c["b"]["id"]))
        self.assert_no_vip("email", "jonas@evil.example")
        v = pp.vip_map(self.store, self.pp)
        row = reply_row(gmsg("m1", "Jonas Berg <jonas@evil.example>", "Can you pay this today?", NOW - 3600, PASS),
                        vips=v)
        self.assertEqual((row["vip"], row["uses_name"]), (False, "Jonas Berg"))
        self.assertNotIn("VIP", row["reasons"])

    def test_st17_slack_vip_name_other_user(self):
        self.slack_user("U900", "Jonas Berg", "Jonas Berg")
        self.slack_user("U901", "jb", "Jonas Berg", "jonas@evil.example")
        self.pp.seed()
        jonas = self.pid("email", "jonas@berg.example")
        for c in self.pp.proposals():
            self.assertNotIn(jonas, (c["a"]["id"], c["b"]["id"]))
        for uid in ("u900", "u901"):
            self.assert_no_vip("slack", uid)
        v = pp.vip_map(self.store, self.pp)
        for uid in ("U900", "U901"):
            data = {"me": ME_SLACK, "users": self.users, "convs": {"D1": {"kind": "dm"}},
                    "dm": {"D1": [{"user": uid, "ts": f"{NOW - 3600:.6f}", "text": "Can you pay this today?"}]},
                    "threads": {}}
            rows, _ = rules.slack_rows(data, {"team_id": "T1"}, "work", NOW, v)
            self.assertEqual((rows[0]["vip"], rows[0]["uses_name"]), (False, "Jonas Berg"), uid)
            self.assertNotIn("VIP", rows[0]["reasons"])
        self.assertEqual(push.vip_slack_users(self.users, v), [])  # no VIP phone alert from a name either
        # the VIP person's own Slack user is a VIP, unmarked
        v2 = vipmap(dict(v), slack={"u900"})
        rows, _ = rules.slack_rows(data | {"dm": {"D1": [{"user": "U900", "ts": f"{NOW - 3600:.6f}", "text": "Hi?"}]}},
                                   {"team_id": "T1"}, "work", NOW, v2)
        self.assertEqual((rows[0]["vip"], rows[0].get("uses_name")), (True, None))

    def test_st17_signal_vip_name_other_sender(self):
        self.signal("s900", "Jonas Berg", "Can you pay this today?")
        self.pp.seed()
        jonas = self.pid("email", "jonas@berg.example")
        for c in self.pp.proposals():
            self.assertNotIn(jonas, (c["a"]["id"], c["b"]["id"]))
        self.assert_no_vip("signal", "s900")
        v = pp.vip_map(self.store, self.pp)
        chats = self.store.signal_recent(NOW - rules.SIGNAL_MAX_S)
        rows, _ = rules.signal_rows(chats, "personal", NOW, v, UTC, "24h")
        self.assertEqual((rows[0]["vip"], rows[0]["uses_name"]), (False, "Jonas Berg"))
        self.assertNotIn("VIP", rows[0]["reasons"])
        rows, _ = rules.signal_rows(chats, "personal", NOW, vipmap(dict(v), signal={"s900"}), UTC, "24h")
        self.assertEqual((rows[0]["vip"], rows[0].get("uses_name")), (True, None))


# --- S-T19: the owner's words inside quoted history or a forward never count as "from Isaac" -----------------------
class ST19QuotedOwnerText(Base):
    QUOTED = ("Sounds good, I'll go ahead.\n\nPriya\n\nOn Mon, 2 Nov 2026, Isaac Hammond <isaac@hammond.example> "
              "wrote:\n> Approved. My other address is isaac.alt@evil.example\n> Isaac Hammond\n"
              "> isaac@hammond.example · isaac.alt@evil.example")
    FWD = ("FYI, see below.\n\n---------- Forwarded message ---------\nFrom: Isaac Hammond <isaac@hammond.example>\n"
           "Date: Mon, 2 Nov 2026\nSubject: Budget\n\nApproved.\n\nIsaac Hammond\nisaac@hammond.example\n"
           "isaac.alt@evil.example")

    def test_st19_mail_quoted_history(self):
        self.assertEqual(pp.signature_addresses(self.QUOTED), [])
        self.index.upsert([mail("e1", "Priya Nair <priya@acme.com>", self.QUOTED),
                           mail("e2", "Priya Nair <priya@acme.com>", self.QUOTED),
                           mail("e3", "IA <isaac.alt@evil.example>", "x"), mail("e4", "IA <isaac.alt@evil.example>", "y")])
        self.pp.seed()
        self.assert_never_me()
        self.assertEqual(self.pp.proposals(), [])
        row = reply_row(gmsg("m1", "Priya Nair <priya@acme.com>", "Sounds good. On Mon, Isaac Hammond <" + ME_ACC +
                             "> wrote: Approved. Can you confirm?", NOW - 3600, PASS))
        self.assertIsNotNone(row)  # still waiting on the owner: the quote is not the owner's reply
        self.assertEqual(row["who"], "Priya Nair")
        self.assertIsNone(self.marks("Priya Nair", "mail", "priya@acme.com").get("uses_your_name"))

    def test_st19_mail_forwarded_owner_message(self):
        self.assertEqual(pp.signature_addresses(self.FWD), [])
        self.index.upsert([mail("f1", "Priya Nair <priya@acme.com>", self.FWD),
                           mail("f2", "Priya Nair <priya@acme.com>", self.FWD),
                           mail("f3", "IA <isaac.alt@evil.example>", "x"), mail("f4", "IA <isaac.alt@evil.example>", "y")])
        self.pp.seed()
        self.assert_never_me()
        self.assertEqual(self.pp.proposals(), [])

    def test_st19_slack_quoted_and_shared_owner_message(self):
        self.slack_user("U07", "Priya Nair", "Priya Nair")
        text = f"> <@{ME_SLACK}>: approved, my other account is <@U666>\nShall I go ahead?"
        msg = {"user": "U07", "ts": f"{NOW - 3600:.6f}", "text": text,
               "attachments": [{"is_share": True, "author_id": ME_SLACK, "text": "Approved."}]}
        data = {"me": ME_SLACK, "users": self.users, "convs": {"D1": {"kind": "dm"}}, "dm": {"D1": [msg]}, "threads": {}}
        rows, _ = rules.slack_rows(data, {"team_id": "T1"}, "work", NOW, pp.VipMap())
        self.assertEqual(len(rows), 1)  # the owner hasn't answered: a quote or a share isn't the owner's message
        self.assertEqual((rows[0]["who"], rows[0]["_sup"]["sender"]), ("Priya Nair", "u07"))
        self.pp.seed()
        self.assert_never_me()
        self.assertIsNone(self.marks("Priya Nair", "slack", "u07").get("uses_your_name"))

    def test_st19_signal_quote_of_the_owner(self):
        self.signal("s5", "Dan Moss", "> Isaac: approved\nOK, going ahead with it?", kind="group", quotes_me=True)
        rows, _ = rules.signal_rows(self.store.signal_recent(NOW - rules.SIGNAL_MAX_S), "personal", NOW, pp.VipMap(),
                                    UTC, "24h")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["who"], rows[0]["reasons"][0]), ("Dan Moss", "Replied to you"))
        self.pp.seed()
        self.assert_never_me()
        self.assertIsNone(self.marks("Dan Moss", "signal", "s5").get("uses_your_name"))


# --- S-L3: a signature that claims someone else's address -----------------------------------------------------------
class SL3PlantedSignature(Base):
    def planted(self, listed):
        body = f"Contract attached.\n\nMallory\nBerg Studio\nmallory@evil.example · {listed}"
        self.index.upsert([mail("g1", "Mallory <mallory@evil.example>", body),
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
        self.index.upsert([mail("j1", "Jonas Berg <jonas@berg.example>")])
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
        self.index.upsert([mail("h1", "Priya Nair <priya@acme.com>"), mail("h2", "Priya Nair <priya@acme.com>")])
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
        self.index.upsert([mail("h1", "Priya Nair <priya@acme.com>"), mail("h2", "Priya Nair <priya@acme.com>")])
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
        self.index.upsert([
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
        self.index.upsert([
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
