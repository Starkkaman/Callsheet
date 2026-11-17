"""0.21.0 Google Contacts, read-only (approved 2026-11-12; docs/people.md; contacts.py).

Checked here, with fakes only (a fake People API on 127.0.0.1, fake Google for the grant, a scripted model):
- the allowlist: exactly GET /v1/people/me/connections on the People host; every other method, path and host refused
  before any network I/O; contacts.readonly is never asked for at connect;
- the sync: paging, the sync token (only changes later, deletions), an expired token (one full read again), 403;
- normalising: phones to E.164 with the owner's country code (the owner's number, the owner's time zone, else +1), emails (case; Gmail
  dots and +tags);
- cross-source linking: a Signal number, a Gmail address, a text's number and a Slack user's email are one person;
  contacts first, then People rows;
- the ledger: one person's ask on Signal and by email is one item; a follow-up raised for an email contact is cleared
  by a text to the same person; older keys relinked (history "linked");
- birthdays in Coming up (yearly, source "contacts", Feb 29, not twice when the calendar has it);
- the API: Allow contacts (incremental grant), status, Sync now, the Sources row "Contacts · N people · Synced …"."""
import datetime as dt
import http.server
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT / "tests"))

import contacts  # noqa: E402
import extract  # noqa: E402
import google  # noqa: E402
import ledger  # noqa: E402
import msgstore  # noqa: E402
import today as today_mod  # noqa: E402
from store import Conflict, Store  # noqa: E402
from test_api_google import Base as ApiBase  # noqa: E402
from test_ledger_0160 import Script, item, ts, upd, when  # noqa: E402

TZ = "America/Los_Angeles"
CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts.readonly"


def person(rid, name, emails=(), phones=(), birthday=None, nick=None, deleted=False):
    p = {"resourceName": "people/" + rid, "etag": "e" + rid,
         "names": [{"displayName": name, "givenName": name.split()[0], "metadata": {"primary": True}}],
         "emailAddresses": [{"value": e} for e in emails],
         "phoneNumbers": [{"value": v} if isinstance(v, str) else v for v in phones]}
    if birthday:
        p["birthdays"] = [{"date": birthday}]
    if nick:
        p["nicknames"] = [{"value": nick}]
    if deleted:
        p = {"resourceName": "people/" + rid, "metadata": {"deleted": True}}
    return p


class FakePeople:
    """people.connections.list with pages of 2, sync tokens ("s<seq>") and a change log."""

    def __init__(self):
        self.people, self.log, self.seq = {}, [], 0
        self.requests, self.expired, self.deny, self.token = [], set(), False, "ya29.contacts"
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                fake._handle(self)
            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def put(self, p):
        self.seq += 1
        self.people[p["resourceName"]] = p
        self.log.append((self.seq, p["resourceName"]))

    def delete(self, rid):
        self.seq += 1
        self.people.pop("people/" + rid, None)
        self.log.append((self.seq, "people/" + rid))

    def _send(self, h, status, data):
        body = json.dumps(data).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _handle(self, h):
        u = urllib.parse.urlsplit(h.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.requests.append((h.command, u.path, q))
        if h.command != "GET" or u.path != "/v1/people/me/connections":
            return self._send(h, 404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        if h.headers.get("Authorization") != "Bearer " + self.token:
            return self._send(h, 401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        if self.deny:
            return self._send(h, 403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}})
        st = q.get("syncToken")
        if st in self.expired:
            return self._send(h, 400, {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                                                 "message": "Sync token is expired."}})
        if st:
            since = int(st[1:])
            rids = sorted({r for s, r in self.log if s > since})
            items = [self.people.get(r) or {"resourceName": r, "metadata": {"deleted": True}} for r in rids]
        else:
            items = [self.people[r] for r in sorted(self.people)]
        off = int(q.get("pageToken", "o0")[1:])
        page = items[off:off + 2]
        out = {"connections": page, "totalItems": len(items)}
        if off + 2 < len(items):
            out["nextPageToken"] = f"o{off + 2}"
        else:
            out["nextSyncToken"] = f"s{self.seq}"
        return self._send(h, 200, out)


def dana(**kw):
    return person("c1", "Dana Ruiz", emails=["Dana.Ruiz@gmail.com", "dana@park.example"],
                  phones=[{"value": "(555) 123-4567", "canonicalForm": "+15551234567"}],
                  birthday={"month": 11, "day": 20}, **kw)


def marco():
    return person("c2", "Marco Bell", emails=["marco@bellfilms.example"], phones=["555.987.6543"],
                  birthday={"year": 1988, "month": 2, "day": 29})


class NormaliseTest(unittest.TestCase):
    def test_phones(self):
        cases = [("+1 (555) 123-4567", "1", "+15551234567"), ("555-123-4567", "1", "+15551234567"),
                 ("1-555-123-4567", "1", "+15551234567"), ("(555) 123-4567 ext. 12", "1", "+15551234567"),
                 ("011 44 7700 900123", "1", "+447700900123"), ("07700 900123", "44", "+447700900123"),
                 ("+44 (0)7700 900123", "1", "+447700900123"),
                 ("0044 7700 900123", "49", "+447700900123"), ("030 1234567", "49", "+49301234567"),
                 ("06 1234 5678", "39", "+390612345678"), ("72975", "1", ""), ("AMAZON", "1", ""),
                 ("dana@park.example", "1", ""), ("1b3c5d7e-0000-4000-8000-123456789abc", "1", ""),
                 ("555-1234", "1", ""), ("", "1", ""), ("+0123456789", "1", "")]
        for raw, cc, want in cases:
            self.assertEqual(contacts.norm_phone(raw, cc), want, raw)

    def test_region(self):
        self.assertEqual(contacts.region_code("+447700900123", "America/New_York"), "44")   # the owner's number first
        self.assertEqual(contacts.region_code("+15551234567"), "1")
        self.assertEqual(contacts.region_code(None, "Europe/Berlin"), "49")
        self.assertEqual(contacts.region_code(None, "America/Los_Angeles"), "1")
        self.assertEqual(contacts.region_code(None, "UTC"), "1")
        self.assertEqual(contacts.region_code(None, None), "1")

    def test_emails(self):
        self.assertEqual(contacts.canon_email("Dana.Ruiz+films@GMAIL.com"), "danaruiz@gmail.com")
        self.assertEqual(contacts.canon_email("d.a.n.a@googlemail.com"), "dana@gmail.com")
        self.assertEqual(contacts.canon_email("Dana.Ruiz+x@Park.Example"), "dana.ruiz+x@park.example")  # not Gmail
        self.assertEqual(contacts.canon_email("mailto:Bo@X.example"), "bo@x.example")
        for bad in ("", "nobody", "a@b@c", "a b@c.d"):
            self.assertEqual(contacts.canon_email(bad), "", bad)

    def test_next_birthday(self):
        d = dt.date
        self.assertEqual(contacts.next_birthday("11-20", d(2026, 11, 12)), d(2026, 11, 20))
        self.assertEqual(contacts.next_birthday("1990-01-05", d(2026, 11, 12)), d(2027, 1, 5))
        self.assertEqual(contacts.next_birthday("02-29", d(2027, 2, 1)), d(2027, 2, 28))
        self.assertEqual(contacts.next_birthday("02-29", d(2028, 2, 1)), d(2028, 2, 29))


class AllowlistTest(unittest.TestCase):
    def setUp(self):
        for k in ("CALLSHEET_GOOGLE_PEOPLE_BASE", "CALLSHEET_GOOGLE_API_BASE"):
            p = mock.patch.dict(os.environ)
            p.start()
            self.addCleanup(p.stop)
            os.environ.pop(k, None)

    def test_only_connections_list(self):
        P = "https://people.googleapis.com"
        self.assertEqual(google.people_base(), P)
        google.check_allowed("GET", P + "/v1/people/me/connections")
        google.check_allowed("GET", P + "/v1/people/me/connections?personFields=names&syncToken=s1")
        refused = [("POST", "/v1/people/me/connections"), ("PUT", "/v1/people/me/connections"),
                   ("DELETE", "/v1/people/me/connections"), ("PATCH", "/v1/people/me/connections"),
                   ("GET", "/v1/people/me/connections/"), ("GET", "/v1/people/me/connections/x"),
                   ("GET", "/v1/people/me"), ("GET", "/v1/people/c123"), ("GET", "/v1/otherContacts"),
                   ("GET", "/v1/otherContacts:search"), ("GET", "/v1/people:searchContacts"),
                   ("GET", "/v1/people:batchGet"), ("POST", "/v1/people:createContact"),
                   ("PATCH", "/v1/people/c123:updateContact"), ("DELETE", "/v1/people/c123:deleteContact"),
                   ("POST", "/v1/people:batchCreateContacts"), ("POST", "/v1/people:batchDeleteContacts"),
                   ("GET", "/v1/contactGroups"), ("POST", "/v1/contactGroups"), ("GET", "/v1/people/me/connections#x"),
                   ("GET", "/v1/people/me/connections/../../otherContacts"), ("GET", "/v2/people/me/connections")]
        for method, path in refused:
            with self.assertRaises(google.NotAllowed, msg=f"{method} {path}"):
                google.check_allowed(method, P + path)
        for url in ("https://www.googleapis.com/v1/people/me/connections", "https://evil.example/v1/people/me/connections",
                    "http://people.googleapis.com/v1/people/me/connections",
                    "https://people.googleapis.com.evil.example/v1/people/me/connections"):
            with self.assertRaises(google.NotAllowed, msg=url):
                google.check_allowed("GET", url)

    def test_refused_before_any_io_and_scope_only_by_grant(self):
        with mock.patch.object(google._opener, "open") as op:   # noqa: SLF001
            for method, path in (("POST", "/v1/people:createContact"), ("DELETE", "/v1/people/c1:deleteContact"),
                                 ("GET", "/v1/otherContacts")):
                with self.assertRaises(google.NotAllowed):
                    google.request(method, google.people_base() + path, token="ya29.x")
            op.assert_not_called()
        self.assertEqual(google.CONTACTS_SCOPE, CONTACTS_SCOPE)
        self.assertNotIn(CONTACTS_SCOPE, google.SCOPES)
        self.assertNotIn("auth/contacts ", google.SCOPES + " ")
        app = "\n".join(p.read_text(encoding="utf-8") for p in APP.glob("*.py"))
        for word in ("auth/contacts\"", "auth/contacts.other", "otherContacts\"", "createContact", "updateContact",
                     "deleteContact", "people_base() + \"/v1/people:"):
            self.assertNotIn(word, app)
        src = (APP / "google.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("people_base() +"), 2)        # the allowlist line and the one GET


class StoreBase(unittest.TestCase):
    """A Store with one connected account whose token is valid, a fake People API and Contacts."""

    def setUp(self):
        self.fake = FakePeople()
        env = mock.patch.dict(os.environ, {"CALLSHEET_GOOGLE_PEOPLE_BASE": self.fake.base})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [ts("2026-11-12T09:00-08:00")]
        self.store = Store(self.tmp.name)
        self.acc = self.store.save_account("personal", "jordan@example.com", "1//rt", self.fake.token,
                                           self.now[0] + 10 ** 9, self.now[0])
        self.logs = []
        self.ct = contacts.Contacts(self.store, None, clock=lambda: self.now[0], log=self.logs.append,
                                    tz_name=lambda: TZ)

    def tearDown(self):
        self.store.close()
        self.fake.close()
        self.tmp.cleanup()

    def grant(self, acc=None):
        self.ct._acc_state(acc or self.acc, granted=True)   # noqa: SLF001

    def calls(self):
        return [r for r in self.fake.requests if r[1] == "/v1/people/me/connections"]


class SyncTest(StoreBase):
    def test_not_granted_reads_nothing(self):
        with self.assertRaises(Conflict):
            self.ct.sync(self.acc)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.ct.due(), [])

    def test_paging_then_sync_token_changes_and_deletions(self):
        for p in (dana(), marco(), person("c3", "Lena Ito", emails=["lena@x.example"]),
                  person("c4", "Theo Park", phones=["+44 7700 900123"]), person("c5", "Ana Lu")):
            self.fake.put(p)
        self.grant()
        self.assertEqual(self.ct.due(), [self.acc])
        r = self.ct.sync(self.acc)
        self.assertEqual((r["changed"], r["count"], r["full"]), (5, 5, True))
        c = self.calls()
        self.assertEqual(len(c), 3)                                         # 2 + 2 + 1
        self.assertEqual([q.get("pageToken") for _, _, q in c], [None, "o2", "o4"])
        for _, _, q in c:
            self.assertEqual(q["personFields"], "names,emailAddresses,phoneNumbers,birthdays,organizations,nicknames")
            self.assertEqual(q["requestSyncToken"], "true")
            self.assertNotIn("syncToken", q)
        self.assertEqual(self.ct.due(), [])
        # later: one changed, one deleted -> only the changes, with the token on every page
        self.fake.requests.clear()
        self.fake.put(person("c3", "Lena Ito-Park", emails=["lena@x.example"]))
        self.fake.delete("c5")
        self.now[0] += contacts.SYNC_EVERY_S + 1
        self.assertEqual(self.ct.due(), [self.acc])
        r = self.ct.sync(self.acc)
        self.assertEqual((r["changed"], r["deleted"], r["count"], r["full"]), (1, 1, 4, False))
        self.assertEqual([q.get("syncToken") for _, _, q in self.calls()], ["s5"])
        self.assertEqual(self.ct.resolve("gmail", "lena@x.example")[1], "Lena Ito-Park")
        # nothing changed: one request, nothing written
        self.fake.requests.clear()
        r = self.ct.sync(self.acc)
        self.assertEqual((r["changed"], r["deleted"], r["count"]), (0, 0, 4))
        self.assertEqual(len(self.calls()), 1)

    def test_expired_token_reads_everything_again(self):
        self.fake.put(dana())
        self.fake.put(marco())
        self.grant()
        self.ct.sync(self.acc)
        self.fake.expired.add("s2")
        # a contact deleted while the token was stale is gone after the full read
        self.fake.people.pop("people/c2")
        self.fake.requests.clear()
        r = self.ct.sync(self.acc)
        self.assertEqual((r["full"], r["count"], r["deleted"]), (True, 1, 1))
        self.assertEqual([q.get("syncToken") for _, _, q in self.calls()], ["s2", None])
        self.assertIsNone(self.ct.contact_of("gmail", "marco@bellfilms.example"))

    def test_refused_is_said_and_kept(self):
        self.grant()
        self.fake.deny = True
        with self.assertRaises(Conflict) as cm:
            self.ct.sync(self.acc)
        self.assertIn("didn't allow", str(cm.exception))
        st = self.ct.status()["accounts"][0]
        self.assertIs(st["granted"], False)
        self.assertTrue(st["error"])
        self.assertEqual(self.ct.due(), [])

    def test_error_waits_before_retry_and_tick_never_raises(self):
        self.grant()
        self.fake.token = "other"              # a 401 twice: the refresh needs the Google client, which isn't there
        self.ct.tick()
        self.assertTrue(any("contacts" in line for line in self.logs))
        self.assertEqual(self.ct.due(), [])                                  # retry waits RETRY_S
        self.now[0] += contacts.RETRY_S + 1
        self.assertEqual(self.ct.due(), [self.acc])
        row = self.ct.sources_row()
        self.assertEqual(row["state"], "error")

    def test_two_accounts_one_person_and_own_card_skipped(self):
        acc2 = self.store.save_account("personal", "jordan@work.example", "1//rt2", self.fake.token,
                                       self.now[0] + 10 ** 9, self.now[0])
        self.fake.put(person("c1", "Dana Ruiz", emails=["dana@park.example"]))
        self.fake.put(person("me", "Jordan Vale", emails=["Jordan@example.com"], phones=["+15550000000"]))
        self.grant()
        self.grant(acc2)
        self.ct.sync(self.acc)
        self.fake.people = {"people/x9": person("x9", "Dana R.", emails=["dana@park.example"], phones=["+15551234567"])}
        self.fake.log = [(self.fake.seq + 1, "people/x9")]
        self.ct.sync(acc2)
        a = self.ct.resolve("gmail", "dana@park.example")
        b = self.ct.resolve("signal", "+15551234567")
        self.assertEqual(a, b)                                      # same address in both accounts: one person
        self.assertEqual(a[1], "Dana Ruiz")
        self.assertIsNone(self.ct.contact_of("sms", "+15550000000"))   # the owner's own card is not a contact
        self.assertEqual(self.ct.people_count(), 1)


class LinkTest(StoreBase):
    def setUp(self):
        super().setUp()
        for p in (dana(), marco(), person("c3", "Dana Cho", emails=["cho@x.example"])):
            self.fake.put(p)
        self.grant()
        self.ct.sync(self.acc)

    def test_signal_gmail_sms_slack_are_one_person(self):
        key = self.ct.resolve("signal", "+15551234567", "D")
        self.assertEqual(key[1], "Dana Ruiz")
        self.assertTrue(key[0].startswith("person:c"))
        same = [("gmail", "dana.ruiz@gmail.com", ""), ("gmail", "DanaRuiz+films@googlemail.com", ""),
                ("gmail", "Dana@Park.Example", ""), ("sms", "(555) 123-4567", ""), ("sms", "+1 555 123 4567", ""),
                ("sms_live", "5551234567", ""), ("calendar", "dana@park.example", ""),
                ("signal", "1b3c5d7e-0000-4000-8000-123456789abc", "Dana Ruiz")]   # uuid only: the profile name
        for source, handle, name in same:
            self.assertEqual(self.ct.resolve(source, handle, name), key, (source, handle))
        # Slack: the user's email when Slack gave it
        with mock.patch.object(self.store, "slack_workspaces", return_value=[
                {"id": "w1", "state": {"users": {"U07DANA": {"name": "dana", "email": "Dana@park.example"},
                                                 "U07ELSE": {"name": "x", "email": ""}}}}]):
            self.ct._bump()   # noqa: SLF001
            self.assertEqual(self.ct.resolve("slack", "U07DANA"), key)
            self.assertIsNone(self.ct.resolve("slack", "U07ELSE"))
        # names only when a full name only one contact has; never a first name
        self.assertIsNone(self.ct.contact_of("signal", "uuid-x", "Dana"))
        self.assertIsNone(self.ct.contact_of("signal", "uuid-x", "dana ruiz jr"))
        self.assertIsNone(self.ct.contact_of("gmail", "other@x.example", "Dana Ruiz"))   # names aren't used for mail
        # strangers keep a normalised handle; the owner stays the owner
        self.assertEqual(self.ct.resolve("sms", "(555) 222-3333"), ("+15552223333", ""))
        self.assertEqual(self.ct.resolve("gmail", "Bo.X+y@Gmail.com"), ("box@gmail.com", ""))
        self.assertIsNone(self.ct.resolve("signal", "me"))
        self.assertIsNone(self.ct.resolve("sms", "SHORTCODE"))
        self.assertEqual(self.ct.name_of("sms", "5559876543"), "Marco Bell")
        self.assertEqual(self.ct.name_of("sms", "5550000001"), "")

    def test_owner_region_from_his_number(self):
        ct = contacts.Contacts(self.store, None, own_number=lambda: "+447700900999", clock=lambda: self.now[0])
        self.fake.requests.clear()
        self.fake.put(person("c9", "Theo Park", phones=["07700 900123"]))
        ct.sync(self.acc)
        self.assertEqual(ct.resolve("signal", "+447700900123")[1], "Theo Park")
        self.assertEqual(ct.region(), "44")

    def test_people_rows_after_contacts(self):
        class Dir:
            persons = {"pa1": {"id": "pa1", "name": "Dana R", "state": "suggested", "email": {"dana@park.example"},
                               "slack": {"u07dana"}},
                       "pb2": {"id": "pb2", "name": "Sam Lee", "state": "confirmed", "email": {"sam@lee.example"},
                               "slack": set()}}
            by = {("email", "dana@park.example"): "pa1", ("slack", "u07dana"): "pa1", ("email", "sam@lee.example"): "pb2"}

            def person_of(self, kind, value):
                pid = self.by.get((kind, str(value).lower()))
                return self.persons.get(pid) if pid else None

        class P:
            def directory(self):
                return Dir()
        ct = contacts.Contacts(self.store, None, people=P(), clock=lambda: self.now[0], tz_name=lambda: TZ)
        dana_key = ct.resolve("gmail", "dana@park.example")
        self.assertEqual(ct.resolve("slack", "U07DANA"), dana_key)          # the People row shares Dana's address
        self.assertEqual(ct.resolve("gmail", "sam@lee.example"), ("person:pb2", "Sam Lee"))
        self.assertEqual(ct.rekey("person:pa1"), dana_key)
        self.assertIsNone(ct.rekey("person:pb2"))


class LedgerTest(StoreBase):
    """The extractor with the resolver (as api.App wires it): one person across sources for items and follow-ups."""

    def setUp(self):
        super().setUp()
        self.fake.put(dana())
        self.fake.put(marco())
        self.grant()
        self.ct.sync(self.acc)
        self.ms = msgstore.MessageStore(self.tmp.name, clock=lambda: self.now[0])
        self.ms.name_of = self.ct.name_of
        self.model = Script()
        ct = self.ct

        def resolve(source, handle, name=""):
            return ct.resolve(source, handle, name) or (str(handle or "").lower(), "")
        resolve.takes_name = True
        self.ex = extract.Extractor(self.ms, TZ, "Jordan Vale", self.model, clock=lambda: self.now[0], person=resolve)
        self.lv = self.ex.lv

    def tearDown(self):
        self.ms.close()
        super().tearDown()

    def read(self, mid, at, answer, source="signal", sender="+15551234567", name="", direction="in", to=(),
             thread=None):
        self.model.answers[mid] = answer
        self.now[0] = ts(at)
        out = direction == "out"
        recips = [{"name": n, "address": a} for a, n in to] if out else [{"name": "Jordan Vale", "address": "me"}]
        self.ms.upsert_message({"source": source, "account": "acct", "msg_id": mid, "thread": thread or f"t-{sender}",
                                "direction": direction, "sender": "me" if out else sender,
                                "sender_name": "" if out else name, "recipients": recips, "ts": ts(at),
                                "subject": "", "text": "x", "labels": []})
        return self.ex.read(self.ms.message(source, "acct", mid))

    def test_same_ask_on_signal_and_by_email_is_one_item(self):
        w = when("Wednesday 10-1", "2026-11-18", "10:00", "13:00")
        a = self.read("s1", "2026-11-12T09:30-08:00", {"items": [item(what="truck prep", w=w)], "updates": []},
                      sender="+15551234567", name="D")["items"][0]
        self.read("g1", "2026-11-12T10:00-08:00", {"items": [item(what="truck prep", w=w)], "updates": []},
                  source="gmail", sender="dana.ruiz@gmail.com", name="Dana Ruiz", thread="mail-1")
        its = [i for i in self.lv.items() if i["merged_into"] is None and i["status"] not in ledger.CLOSED]
        self.assertEqual(len(its), 1)
        self.assertEqual(its[0]["id"], a)
        self.assertTrue(its[0]["who"].startswith("person:c"))
        self.assertEqual(its[0]["who_name"], "Dana Ruiz")

    def test_follow_up_cleared_by_a_text_to_the_same_person(self):
        e1 = self.read("m1", "2026-11-12T09:00-08:00", {"items": [item(what="client call", w=when(
            "Wednesday 10-12", "2026-11-18", "10:00", "12:00"))], "updates": []}, source="gmail",
            sender="marco@bellfilms.example", name="Marco", thread="mail-marco")["items"][0]
        e2 = self.read("m2", "2026-11-12T10:00-08:00", {"items": [item(what="truck prep", w=when(
            "Wednesday 10-1", "2026-11-18", "10:00", "13:00"))], "updates": []})["items"][0]
        self.assertEqual(len(self.lv.conflicts()), 1)
        self.read("m3", "2026-11-12T11:00-08:00", {"items": [item("commitment", "owner", "truck prep", when(
            "Wednesday 10-1", "2026-11-18", "10:00", "13:00"), "accepted")], "updates": [upd(f"L{e2}", "done")]},
            direction="out", to=[("+15551234567", "")], thread="t-+15551234567")
        f = self.lv.followups()
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["item"], e1)
        marco_key = self.ct.resolve("gmail", "marco@bellfilms.example")[0]
        self.assertEqual(f[0]["tell"], marco_key)
        # a text to Marco's number (written as the owner's phone shows it) about the call clears it
        self.read("m4", "2026-11-12T12:00-08:00", {"items": [], "updates": [upd(f"L{e1}", "declined")]},
                  source="sms", direction="out", to=[("(555) 987-6543", "")], thread="sms:marco")
        self.assertEqual(self.lv.followups(), [])
        self.assertEqual(self.lv.followups(state="cleared")[0]["cleared_message"],
                         self.ms.message("sms", "acct", "m4")["id"])

    def test_relink_moves_older_keys(self):
        now = self.now[0]

        def seed(db):
            ids = []
            for who, name in (("dana.ruiz@gmail.com", "dana.ruiz@gmail.com"), ("(555) 987-6543", "(555) 987-6543"),
                              ("name:dana ruiz", "Dana Ruiz"), ("name:dana", "Dana"), ("stranger@x.example", "S"),
                              ("owner", "Jordan")):
                ids.append(db.execute("INSERT INTO ledger_items (type, who, who_name, what, created_at, updated_at) "
                                      "VALUES ('ask', ?, ?, 'x', ?, ?)", (who, name, now, now)).lastrowid)
            db.execute("INSERT INTO ledger_followups (item, tell, tell_name, raised_at) VALUES (?, ?, ?, ?)",
                       (ids[0], "+15559876543", "+15559876543", now))
            return ids
        ids = self.ms.write(seed)
        n = self.ct.relink(self.ms)
        self.assertEqual(n, 4)                                           # 3 items and 1 follow-up
        rows = {r["id"]: r for r in self.ms._read("SELECT * FROM ledger_items")}   # noqa: SLF001
        dana_key = self.ct.resolve("gmail", "dana@park.example")
        marco_key = self.ct.resolve("sms", "5559876543")
        self.assertEqual((rows[ids[0]]["who"], rows[ids[0]]["who_name"]), dana_key)
        self.assertEqual((rows[ids[1]]["who"], rows[ids[1]]["who_name"]), marco_key)
        self.assertEqual(rows[ids[2]]["who"], dana_key[0])
        self.assertEqual(rows[ids[3]]["who"], "name:dana")              # a first name alone never links
        self.assertEqual(rows[ids[4]]["who"], "stranger@x.example")
        self.assertEqual(rows[ids[5]]["who"], "owner")
        fu = self.ms._read("SELECT * FROM ledger_followups")[0]   # noqa: SLF001
        self.assertEqual((fu["tell"], fu["tell_name"]), marco_key)
        h = self.ms._read("SELECT * FROM ledger_history WHERE kind='linked'")   # noqa: SLF001
        self.assertEqual(len(h), 3)
        self.assertEqual(self.ct.relink(self.ms), 0)                    # nothing left to move

    def test_contact_names_shown_for_numbers(self):
        self.read("s1", "2026-11-12T09:30-08:00", {"items": [], "updates": []}, source="sms", sender="5551234567")
        r = self.ms.get(msgstore.STORE_ID_BASE + self.ms.message("sms", "acct", "s1")["id"])
        self.assertEqual(r["sender_name"], "Dana Ruiz")
        th = self.ms.thread("sms", "acct", "t-5551234567")
        self.assertEqual(th[0]["sender_name"], "Dana Ruiz")


class BirthdayTest(StoreBase):
    def setUp(self):
        super().setUp()
        self.fake.put(dana())                                    # Nov 20
        self.fake.put(marco())                                   # Feb 29 (outside 30 days)
        self.fake.put(person("c3", "Lena Ito", birthday={"month": 11, "day": 25}))
        self.fake.put(person("c4", "Ana Lu", birthday={"month": 11, "day": 12}))   # today: not "coming up"
        self.grant()
        self.ct.sync(self.acc)
        self.ms = msgstore.MessageStore(self.tmp.name, clock=lambda: self.now[0])
        self.lv = ledger.Ledger(self.ms, lambda: TZ, lambda: self.now[0])
        self.td = today_mod.Today(self.ms, self.lv, lambda: self.now[0])
        self.td.contacts = self.ct

    def tearDown(self):
        self.ms.close()
        super().tearDown()

    def rows(self):
        return [r for g in self.td.view()["coming_up"]["groups"] for r in g["rows"]]

    def test_birthdays_in_coming_up(self):
        b = [r for r in self.rows() if r.get("birthday")]
        self.assertEqual([r["what"] for r in b], ["Dana Ruiz’s birthday", "Lena Ito’s birthday"])
        self.assertEqual((b[0]["from"], b[0]["icon"], b[0]["add"], b[0]["item"]), ("From contacts", "areas", None, None))
        self.assertEqual(b[0]["date"], today_mod.short_day(dt.date(2026, 11, 20)))
        # yearly: next year again
        self.now[0] = ts("2027-11-10T09:00-08:00")
        self.assertIn("Dana Ruiz’s birthday", [r["what"] for r in self.rows()])
        # Feb 29 shows on Feb 28 in 2027
        self.now[0] = ts("2027-02-10T09:00-08:00")
        self.assertIn(("Marco Bell’s birthday", today_mod.short_day(dt.date(2027, 2, 28))),
                      [(r["what"], r["date"]) for r in self.rows()])

    def test_not_twice_when_the_calendar_has_it(self):
        day = dt.date(2026, 11, 20)
        self.ms.upsert_events([{"account": "a", "calendar": "addressbook#contacts@group.v.calendar.google.com",
                                "event_id": "bd1", "start": day.isoformat(), "end": (day + dt.timedelta(days=1)).isoformat(),
                                "start_ts": ledger.day_bounds(day, ledger.zone(TZ))[0],
                                "end_ts": ledger.day_bounds(day, ledger.zone(TZ))[1], "all_day": True,
                                "title": "Dana Ruiz's birthday", "status": "confirmed"}])
        whats = [r["what"] for r in self.rows()]
        self.assertIn("Dana Ruiz's birthday", whats)
        self.assertNotIn("Dana Ruiz’s birthday", whats)
        self.assertIn("Lena Ito’s birthday", whats)

    def test_no_contacts_no_birthdays(self):
        self.td.contacts = None
        self.assertFalse([r for r in self.rows() if r.get("birthday")])


class ApiTest(ApiBase):
    def setUp(self):
        super().setUp()
        self.people = FakePeople()
        self.addCleanup(self.people.close)
        env = mock.patch.dict(os.environ, {"CALLSHEET_GOOGLE_PEOPLE_BASE": self.people.base})
        env.start()
        self.addCleanup(env.stop)
        self.acc = self.connect("jordan@example.com")
        self.people.put(dana())
        self.people.put(marco())
        self.people.token = None

    def test_grant_status_sync_and_sources_row(self):
        st = self.ok("GET", "/api/contacts")
        self.assertEqual(st["scope"], CONTACTS_SCOPE)
        self.assertEqual([(a["email"], a["granted"]) for a in st["accounts"]], [("jordan@example.com", None)])
        self.assertNotIn("contacts", [r["key"] for r in self.ok("GET", "/api/sources")["sources"]])
        code, body, _ = self.call("POST", "/api/contacts/sync", {})
        self.assertEqual(code, 409)
        # Allow contacts: an incremental grant for this account only
        flow = self.ok("POST", "/api/contacts/grant/start", {"account_id": self.acc["id"]})
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(flow["auth_url"]).query)
        self.assertIn(CONTACTS_SCOPE, q["scope"][0].split())
        self.assertEqual(q["login_hint"], ["jordan@example.com"])
        self.assertEqual(q["include_granted_scopes"], ["true"])
        self.fake.scope += " " + CONTACTS_SCOPE
        real = google.people_connections

        def fetch(token, page=None, sync=None, page_size=1000):
            self.people.token = token             # the access token Callsheet got from the (fake) refresh
            return real(token, page, sync, page_size)
        with mock.patch.object(google, "people_connections", fetch):
            pasted = self.fake.approve(flow["auth_url"], "jordan@example.com")
            done = self.ok("POST", "/api/contacts/grant/finish", {"flow_id": flow["flow_id"], "pasted": pasted})
            self.assertTrue(done["granted"])
            a = done["status"]["accounts"][0]
            self.assertEqual((a["granted"], a["count"], a["synced"]), (True, 2, "just now"))
            row = next(r for r in self.ok("GET", "/api/sources")["sources"] if r["key"] == "contacts")
            self.assertEqual((row["name"], row["line"], row["state"]), ("Contacts · 2 people", "Synced just now", "current"))
            self.app.contacts.clock = lambda: self.app.clock() + 2 * 3600
            row = next(r for r in self.ok("GET", "/api/sources")["sources"] if r["key"] == "contacts")
            self.assertEqual(row["line"], "Synced 2 h ago")
            self.people.put(person("c3", "Lena Ito", emails=["lena@x.example"]))
            st = self.ok("POST", "/api/contacts/sync", {"account_id": self.acc["id"]})
            self.assertEqual((st["accounts"][0]["count"], st["people"], st["said"]), (3, 3, ""))
        # the app's resolver: the extractor's person() and Chat's names use the contacts
        self.assertEqual(self.app.extractor.person("sms", "555-123-4567", "")[1], "Dana Ruiz")
        self.assertEqual(self.app.extractor.person("signal", "+15551234567", "")[0],
                         self.app.extractor.person("gmail", "dana.ruiz@gmail.com", "")[0])
        self.assertEqual(self.app.messages.name_of("sms", "5559876543", ""), "Marco Bell")
        # only the People API's connections were asked for (and never with a write method)
        self.assertTrue(self.people.requests)
        self.assertEqual({(m, p) for m, p, _ in self.people.requests}, {("GET", "/v1/people/me/connections")})
        # disconnecting the account forgets its contacts
        self.ok("DELETE", "/api/accounts/" + self.acc["id"])
        self.assertEqual(self.app.contacts.count(), 0)

    def test_grant_refused(self):
        flow = self.ok("POST", "/api/contacts/grant/start", {"account_id": self.acc["id"]})
        pasted = self.fake.approve(flow["auth_url"], "jordan@example.com")
        done = self.ok("POST", "/api/contacts/grant/finish", {"flow_id": flow["flow_id"], "pasted": pasted})
        self.assertFalse(done["granted"])
        self.assertEqual(self.people.requests, [])
        code, _, _ = self.call("POST", "/api/contacts/grant/start", {})
        self.assertEqual(code, 400)


class NoRulesTest(unittest.TestCase):
    """rw-int retires the rules brief: People and Contacts no longer import rules.py (rules.reply_row is already gone),
    and People's automated-sender check works on its own."""

    def test_people_and_contacts_import_no_rules(self):
        import ast
        for name in ("people.py", "contacts.py"):
            tree = ast.parse((APP / name).read_text(encoding="utf-8"))
            mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            self.assertNotIn("rules", mods, name)

    def test_is_role_without_rules(self):
        import people as people_mod
        for addr in ("no-reply@shop.example", "notifications@github.com", "alerts.billing@bank.example",
                     "mailer-daemon@x.example", "info@studio.example", "bounces+123@list.example"):
            self.assertTrue(people_mod.is_role(addr), addr)
        for addr in ("dana@kim.example", "marco.bellini@studio.example", "noreen@x.example", "alertson@x.example"):
            self.assertFalse(people_mod.is_role(addr), addr)


if __name__ == "__main__":
    unittest.main()
