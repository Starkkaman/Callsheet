"""Signal (0.5.1): the signal-cli method allow-list, the subprocess against the fake signal-cli in
fake_signal_cli.py (link, receive, watchdog, unlink), the QR code SVG and the vendored qrcodegen, received
messages -> rows, retention, the history import parser (both formats), the import API, the Dockerfile and
config.yaml. Stdlib only, no network: nothing here talks to Signal."""
import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import threading
import time
import unittest
import uuid
import xml.etree.ElementTree as ET
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
FIX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT / "tests"))
import qr  # noqa: E402
import rules  # noqa: E402
import signal_import  # noqa: E402
import signalcli  # noqa: E402
from store import InputError, Store  # noqa: E402
from test_api_google import Base  # noqa: E402
from vendor import qrcodegen  # noqa: E402

FAKE = ROOT / "tests" / "fake_signal_cli.py"
ME = {"number": "+15551234567", "aci": "11111111-2222-4333-8444-555555555555", "name": "Sam"}
QR_SHA256 = "b089855caf16185c61421ea4927c1b213cf9468940d71fa8ab11ef83662dcc84"
H = 3600


def wait_for(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def calls(conf):
    p = pathlib.Path(conf) / "calls.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def methods(conf):
    return [c["method"] for c in calls(conf) if "method" in c]


def dm_env(text, ts, name="Alice", uuid_="aaaaaaaa-0000-4000-8000-000000000001", number="+15550000001", **dm):
    return {"source": number, "sourceNumber": number, "sourceUuid": uuid_, "sourceName": name, "sourceDevice": 1,
            "timestamp": ts, "dataMessage": dict({"timestamp": ts, "message": text, "expiresInSeconds": 0,
                                                  "viewOnce": False}, **dm)}


def group_env(text, ts, gid="R3JvdXBJZA==", gname="Climbing crew", name="Carol",
              uuid_="cccccccc-0000-4000-8000-000000000003", **dm):
    e = dm_env(text, ts, name=name, uuid_=uuid_, number="+15550000003", **dm)
    e["dataMessage"]["groupInfo"] = {"groupId": gid, "groupName": gname, "revision": 3, "type": "DELIVER"}
    return e


def sent_env(text, ts, dest_uuid="aaaaaaaa-0000-4000-8000-000000000001", group=None):
    sm = {"timestamp": ts, "message": text, "expiresInSeconds": 0, "viewOnce": False,
          "destination": None if group else "+15550000001", "destinationNumber": None if group else "+15550000001",
          "destinationUuid": None if group else dest_uuid}
    if group:
        sm["groupInfo"] = {"groupId": group, "type": "DELIVER"}
    return {"source": ME["number"], "sourceNumber": ME["number"], "sourceUuid": ME["aci"], "sourceDevice": 1,
            "timestamp": ts, "syncMessage": {"sentMessage": sm}}


class TempStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)

    def add(self, env, now, me=ME):
        act = signalcli.from_envelope(env, me, self.store.signal_hash, now)
        self.assertIsNotNone(act)
        kind, rec = act
        if kind == "add":
            self.store.signal_add(rec, now)
        elif kind == "edit":
            self.store.signal_edit(rec)
        else:
            self.store.signal_delete(rec["chat"], rec["sender"], rec["ts"])
        return rec


# --- the allow-list ---------------------------------------------------------------------------
class AllowListTest(unittest.TestCase):
    def test_exact_list(self):
        self.assertEqual(set(signalcli.ALLOWED_METHODS), {"startLink", "finishLink", "subscribeReceive",
                                                          "unsubscribeReceive", "listAccounts", "listContacts",
                                                          "listGroups"})
        for m in signalcli.ALLOWED_METHODS:
            signalcli.check_method(m)

    def test_send_and_profile_methods_refused(self):
        for bad in ("send", "sendReceipt", "sendTyping", "sendReaction", "sendSyncRequest", "sendMessageRequestResponse",
                    "sendContacts", "sendPollCreate", "updateProfile", "updateAccount", "updateGroup", "updateContact",
                    "joinGroup", "quitGroup", "block", "unblock", "remoteDelete", "register", "verify", "unregister",
                    "deleteLocalAccountData", "addDevice", "removeDevice", "trust", "setPin", "submitRateLimitChallenge",
                    "receive", "version", "", None, "listaccounts", "startLink "):
            with self.assertRaises(signalcli.NotAllowed, msg=repr(bad)):
                signalcli.check_method(bad)

    def test_refused_before_anything_is_written(self):
        written = []

        class Pipe:
            def write(self, b):
                written.append(b)

            def flush(self):
                pass

            def readline(self, *_):
                time.sleep(0.2)
                return b""

        proc = mock.Mock(stdin=Pipe(), stdout=Pipe(), stderr=Pipe())
        rpc = signalcli.Rpc(proc, lambda p: None, log=lambda s: None)
        for bad in ("send", "sendReceipt", "sendTyping", "updateProfile"):
            with self.assertRaises(signalcli.NotAllowed):
                rpc.call(bad, {"recipient": ["+15550000001"], "message": "hi"})
        self.assertEqual(written, [])

    def test_source_calls_only_allowed_methods(self):
        called = set()
        for f in APP.glob("*.py"):
            called |= set(re.findall(r'\.call\(\s*"(\w+)"', f.read_text()))
        self.assertTrue(called)
        self.assertLessEqual(called, set(signalcli.ALLOWED_METHODS))
        self.assertNotIn('"send', (APP / "signalcli.py").read_text().split("ALLOWED_METHODS = ")[1].split(")")[0])

    def test_command_line(self):
        cmd = signalcli.command("/usr/bin/signal-cli", "/data/signal-cli")
        self.assertEqual(cmd, ["/usr/bin/signal-cli", "--config", "/data/signal-cli", "--trust-new-identities=always",
                               "--disable-send-log", "--scrub-log", "jsonRpc", "--receive-mode=manual",
                               "--ignore-attachments", "--ignore-stories", "--ignore-avatars", "--ignore-stickers"])
        self.assertNotIn("-a", cmd)
        self.assertEqual(signalcli.command([sys.executable, "x.py"], "d")[:2], [sys.executable, "x.py"])

    def test_scrub_and_mask(self):
        line = signalcli.scrub("WARN failed for +15551234567 / 11111111-2222-4333-8444-555555555555 ok")
        self.assertNotIn("5551234567", line)
        self.assertNotIn("11111111-2222", line)
        self.assertEqual(signalcli.mask_number("+15551234567"), "+•••••••••67")
        self.assertIsNone(signalcli.mask_number(None))
        self.assertIsNone(signalcli.mask_number("12"))


# --- the subprocess, against the fake signal-cli ------------------------------------------
class ServiceTest(TempStore):
    def setUp(self):
        super().setUp()
        self.logs = []
        self.svc = signalcli.SignalService(self.store, self.tmp.name, log=self.logs.append,
                                           binary=[sys.executable, str(FAKE)])
        self.svc.backoff = (0.2,)
        self.addCleanup(self.svc.close)
        self.conf = pathlib.Path(self.tmp.name) / "signal-cli"

    def scan(self):
        (self.conf / "scan").write_text("")

    def link(self):
        st = self.svc.start_link()
        self.assertEqual(st["link"]["state"], "waiting")
        self.scan()
        self.assertTrue(wait_for(lambda: self.svc.status()["linked"]), self.logs)
        self.assertTrue(wait_for(lambda: methods(self.conf).count("subscribeReceive") >= 1))
        return st

    def deliver(self, *envs):
        with open(self.conf / "inbox.jsonl", "a") as f:
            for e in envs:
                f.write(json.dumps(e) + "\n")

    def test_link_flow(self):
        st = self.link()
        svg = st["link"]["qr_svg"]
        self.assertTrue(svg.startswith("<svg "))
        self.assertNotIn("sgnl", svg)  # the SVG is drawn from the module grid only
        self.assertGreater(st["link"]["expires_in"], 200)
        st = self.svc.status()
        self.assertEqual(st["number"], "+•••••••••67")
        self.assertEqual(st["link"], {"state": "linked"})
        self.assertNotIn("qr_svg", json.dumps(st))
        fin = [c for c in calls(self.conf) if c.get("method") == "finishLink"][0]
        self.assertEqual(fin["params"], {"deviceLinkUri": "sgnl://linkdevice?uuid=FAKE-uuid_1&pub_key=BQ%2Bfake%2Fkey%3D",
                                         "deviceName": "Callsheet"})
        argv = calls(self.conf)[0]["argv"]
        self.assertEqual(argv[:2], ["--config", str(self.conf)])
        self.assertIn("--receive-mode=manual", argv)
        self.assertEqual(oct(self.conf.stat().st_mode & 0o777), "0o700")
        # nothing outside the allow-list ever reached the process
        self.assertLessEqual(set(methods(self.conf)), set(signalcli.ALLOWED_METHODS))
        # the full number and the uuid are not stored, and not in the logs
        db = b"".join(p.read_bytes() for p in pathlib.Path(self.tmp.name).glob("callsheet.db*"))
        self.assertNotIn(b"5551234567", db)
        self.assertNotIn("5551234567", "\n".join(self.logs))
        self.assertNotIn("11111111-2222", "\n".join(self.logs))
        with self.assertRaises(signalcli.LinkError):
            self.svc.start_link()

    def test_receive_and_unlink(self):
        self.link()
        now = time.time()
        ts = int((now - 4 * H) * 1000)
        self.deliver(dm_env("Can you call me back?", ts), {"envelope": "junk"},
                     {"sourceUuid": "x", "receiptMessage": {"isDelivery": True, "timestamps": [ts]}},
                     {"sourceUuid": "x", "typingMessage": {"action": "STARTED"}},
                     group_env("hello all", ts + 1000))
        self.assertTrue(wait_for(lambda: self.store.signal_counts()["live"] == 2))
        chats = self.store.signal_recent(0)
        self.assertEqual(sorted(c["kind"] for c in chats), ["dm", "group"])
        self.assertTrue(self.store.signal_meta().get("last_message_at"))
        db = b"".join(p.read_bytes() for p in pathlib.Path(self.tmp.name).glob("callsheet.db*"))
        self.assertNotIn(b"aaaaaaaa-0000", db)
        self.assertNotIn(b"15550000001", db)
        # imported history survives unlinking, live messages and signal-cli's folder don't
        self.store.signal_add({"chat": "gi1", "kind": "group", "title": "Old", "sender": "s1", "ts": 1.0,
                               "text": "old"}, now, imported=True)
        proc = self.svc._proc
        st = self.svc.unlink()
        self.assertFalse(st["linked"])
        self.assertFalse(self.conf.exists())
        self.assertIsNotNone(proc.poll())
        self.assertEqual(self.store.signal_counts(), {"live": 0, "imported": 1})
        self.assertNotIn("number", self.store.signal_meta())
        self.assertIsNone(self.svc._proc)

    def test_link_code_expires(self):
        self.svc.link_wait = 1
        st = self.svc.start_link()
        self.assertEqual(st["link"]["state"], "waiting")
        self.assertTrue(wait_for(lambda: self.svc.status()["link"]["state"] == "expired"))
        self.assertIn("expired", self.svc.status()["link"]["error"])
        self.assertFalse(self.svc.status()["linked"])
        self.assertTrue(wait_for(lambda: self.svc._proc is None))

    def test_refused_link(self):
        self.conf.mkdir(mode=0o700, parents=True)
        (self.conf / "refuse").write_text("")
        with self.assertRaises(signalcli.LinkError):
            self.svc.start_link()
        self.assertIsNone(self.svc._proc)

    def test_cancel_link(self):
        self.svc.start_link()
        proc = self.svc._proc
        st = self.svc.cancel_link()
        self.assertEqual(st["link"], {"state": "idle"})
        self.assertIsNotNone(proc.poll())

    def test_watchdog_restarts_and_close_stops(self):
        self.link()
        first = self.svc._proc
        first.kill()
        self.assertTrue(wait_for(lambda: methods(self.conf).count("listAccounts") >= 1
                                 and self.svc._proc is not None and self.svc._proc is not first), self.logs)
        self.assertTrue(wait_for(lambda: methods(self.conf).count("subscribeReceive") >= 2))
        self.assertTrue(any("starting it again" in x for x in self.logs))
        proc = self.svc._proc
        self.svc.close()
        self.assertIsNotNone(proc.poll())

    def test_start_when_linked(self):
        self.conf.mkdir(mode=0o700, parents=True)
        (self.conf / "account.json").write_text(json.dumps({"number": "+15551234567", "aci": ME["aci"]}))
        self.store.signal_update_meta(linked=True, number="+•••••••••67")
        self.svc.start()
        self.assertTrue(wait_for(lambda: "subscribeReceive" in methods(self.conf)))
        self.assertEqual(self.svc._me["aci"], ME["aci"])  # memory only
        self.deliver(group_env("￼ can you bring the rope?", int(time.time() * 1000),
                               mentions=[{"name": "+15551234567", "number": "+15551234567", "uuid": ME["aci"],
                                          "start": 0, "length": 1}]))
        self.assertTrue(wait_for(lambda: self.store.signal_counts()["live"] == 1))
        m = self.store.signal_recent(0)[0]["messages"][0]
        self.assertTrue(m["mentions_me"])

    def test_link_removed_on_phone(self):
        self.conf.mkdir(mode=0o700, parents=True)  # no account.json: listAccounts is empty
        self.store.signal_update_meta(linked=True)
        self.svc.start()
        self.assertTrue(wait_for(lambda: not self.store.signal_meta().get("linked")))
        self.assertTrue(wait_for(lambda: not self.svc._watch.is_alive()))

    def test_not_installed(self):
        svc = signalcli.SignalService(self.store, self.tmp.name, binary="/nonexistent/signal-cli")
        self.assertFalse(svc.available())
        with self.assertRaises(signalcli.LinkError):
            svc.start_link()
        self.assertFalse(svc.status()["available"])


# --- QR code -----------------------------------------------------------------------------------
class QrTest(unittest.TestCase):
    def test_vendored_file_unchanged(self):
        data = (APP / "vendor" / "qrcodegen.py").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), QR_SHA256)
        head = data[:2000].decode()
        self.assertIn("Copyright (c) Project Nayuki", head)
        self.assertIn("MIT License", head)

    def test_qrcodegen_self_check(self):
        code = qrcodegen.QrCode.encode_text("Hello, world!", qrcodegen.QrCode.Ecc.LOW)
        self.assertEqual((code.get_version(), code.get_size()), (1, 21))
        n = code.get_size()
        for ox, oy in ((0, 0), (n - 7, 0), (0, n - 7)):  # finder patterns
            for y in range(7):
                for x in range(7):
                    ring = max(abs(x - 3), abs(y - 3))
                    self.assertEqual(code.get_module(ox + x, oy + y), ring != 2, (ox, oy, x, y))
        for i in range(8, n - 8):  # timing patterns
            self.assertEqual(code.get_module(i, 6), i % 2 == 0)
            self.assertEqual(code.get_module(6, i), i % 2 == 0)
        self.assertTrue(code.get_module(8, n - 8))  # the dark module
        # the library's own segment and capacity checks
        segs = qrcodegen.QrSegment.make_segments("sgnl://linkdevice?uuid=x&pub_key=y")
        self.assertEqual(len(segs), 1)
        with self.assertRaises(qrcodegen.DataTooLongError):
            qrcodegen.QrCode.encode_text("x" * 3000, qrcodegen.QrCode.Ecc.HIGH)

    def test_svg_structure(self):
        text = "sgnl://linkdevice?uuid=" + "A" * 22 + "&pub_key=" + "B%2B" * 11 + '"<&>'
        grid = qr.matrix(text)
        svg = qr.svg(text)
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        n = len(grid) + 8
        self.assertEqual(root.get("viewBox"), f"0 0 {n} {n}")
        kids = list(root)
        self.assertEqual([k.tag.split("}")[1] for k in kids], ["rect", "path"])
        self.assertEqual(kids[0].get("fill"), "#ffffff")
        d = kids[1].get("d")
        self.assertRegex(d, r"^(M\d+,\d+h1v1h-1z)+$")
        squares = re.findall(r"M(\d+),(\d+)", d)
        self.assertEqual(len(squares), sum(sum(r) for r in grid))
        self.assertIn(("4", "4"), squares)  # quiet zone of 4, finder corner dark
        self.assertNotIn("sgnl", svg)
        self.assertNotIn("<script", svg)


# --- envelopes -> messages ---------------------------------------------------------------------
class EnvelopeTest(TempStore):
    def act(self, env, me=ME):
        return signalcli.from_envelope(env, me, self.store.signal_hash, 1000.0)

    def test_dm_and_sent(self):
        kind, rec = self.act(dm_env("Hi there", 1_700_000_000_000))
        self.assertEqual(kind, "add")
        self.assertEqual((rec["kind"], rec["title"], rec["sender_name"], rec["ts"], rec["outgoing"]),
                         ("dm", "Alice", "Alice", 1_700_000_000.0, False))
        self.assertTrue(rec["chat"].startswith("d") and rec["sender"].startswith("s"))
        self.assertNotIn("aaaaaaaa", json.dumps(rec))
        self.assertNotIn("15550000001", json.dumps(rec))
        kind, out = self.act(sent_env("Sure", 1_700_000_100_000))
        self.assertEqual((out["chat"], out["sender"], out["outgoing"], out["title"]), (rec["chat"], "me", True, None))
        g = self.act(sent_env("hi group", 1_700_000_200_000, group="R3JvdXBJZA=="))[1]
        self.assertEqual((g["kind"], g["chat"][0], g["outgoing"]), ("group", "g", True))

    def test_mentions_quotes_utf16(self):
        env = group_env("\U0001F600 ￼ and ￼: rope?", 1_700_000_000_000, mentions=[
            {"name": "Sam", "number": ME["number"], "uuid": ME["aci"], "start": 3, "length": 1},
            {"name": "Dan", "number": None, "uuid": "dddddddd-0000-4000-8000-000000000004", "start": 9, "length": 1}])
        rec = self.act(env)[1]
        self.assertEqual(rec["text"], "\U0001F600 @Sam and @Dan: rope?")
        self.assertTrue(rec["mentions_me"])
        self.assertFalse(rec["quotes_me"])
        q = group_env("agreed", 1_700_000_000_000, quote={"id": 1, "authorUuid": ME["aci"], "text": "plan"})
        self.assertTrue(self.act(q)[1]["quotes_me"])
        other = group_env("x", 1_700_000_000_000, quote={"id": 1, "authorUuid": "zzz", "text": "plan"})
        self.assertFalse(self.act(other)[1]["quotes_me"])

    def test_skipped(self):
        ts = 1_700_000_000_000
        for env in (dm_env("x", ts, viewOnce=True), dm_env(None, ts, reaction={"emoji": "x", "targetSentTimestamp": 1}),
                    dm_env("", ts), dm_env(None, ts, isExpirationUpdate=True),
                    {"sourceUuid": "x", "receiptMessage": {"isRead": True}}, {"sourceUuid": "x", "typingMessage": {}},
                    {"sourceUuid": "x", "callMessage": {}}, {"sourceUuid": "x", "storyMessage": {}},
                    {"sourceUuid": "x", "syncMessage": {"readMessages": []}}, None, "junk",
                    dm_env("from me elsewhere", ts, uuid_=ME["aci"], number=ME["number"])):
            self.assertIsNone(self.act(env), env)
        upd = group_env(None, ts)
        upd["dataMessage"]["groupInfo"]["type"] = "UPDATE"
        self.assertIsNone(self.act(upd))

    def test_edit_delete_disappearing(self):
        ts = 1_700_000_000_000
        rec = self.add(dm_env("frist", ts), 1000.0)
        edit = {"sourceUuid": "aaaaaaaa-0000-4000-8000-000000000001", "sourceName": "Alice",
                "editMessage": {"targetSentTimestamp": ts, "dataMessage": {"timestamp": ts + 5, "message": "first"}}}
        self.assertEqual(self.act(edit)[0], "edit")
        self.add(edit, 1000.0)
        self.assertEqual(self.store.signal_recent(0)[0]["messages"][0]["text"], "first")
        dele = dm_env(None, ts + 10, remoteDelete={"timestamp": ts})
        self.assertEqual(self.act(dele)[0], "delete")
        self.add(dele, 1000.0)
        self.assertEqual(self.store.signal_counts()["live"], 0)
        exp = self.act(dm_env("secret", ts, expiresInSeconds=60))[1]
        self.assertEqual(exp["expires_at"], 1060.0)
        self.assertIsNone(rec["expires_at"])


# --- rows -------------------------------------------------------------------------------------
class RowsTest(TempStore):
    def setUp(self):
        super().setUp()
        self.now = time.time()

    def at(self, hours_ago):
        return int((self.now - hours_ago * H) * 1000)

    def rows(self, vips=None, ignored=()):
        return rules.signal_rows(self.store.signal_recent(self.now - rules.SIGNAL_MAX_S), "personal", self.now,
                                 vips or {}, dt.timezone.utc, "24h", ignored)

    def test_dm_other_person_last(self):
        self.add(dm_env("Are you free Friday?", self.at(5)), self.now)
        rows, heads = self.rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["who"], r["source"], r["via"], r["link"]), ("Alice", "Signal · DM", "signal", None))
        self.assertEqual(r["reasons"], ["Direct question", "Waiting 5h"])
        self.assertEqual(r["_sup"]["source"], "signal")
        self.assertTrue(r["_done"].startswith("signal:d"))
        self.assertEqual(r["_facts"]["count"], 1)
        self.assertEqual(r["_messages"][-1]["text"], "Are you free Friday?")
        self.add(sent_env("Yes", self.at(4)), self.now)
        self.assertEqual(self.rows()[0], [])

    def test_dm_without_reason_or_too_old(self):
        self.add(dm_env("ok", self.at(1)), self.now)
        self.assertEqual(self.rows()[0], [])  # no question, not waiting yet
        self.add(dm_env("Ping?", self.at(15 * 24), name="Bob", uuid_="b", number="+15550000002"), self.now)
        self.assertEqual(self.rows()[0], [])

    def test_group_mention_quote(self):
        self.add(group_env("hello all", self.at(6)), self.now)
        self.assertEqual(self.rows()[0], [])  # a plain group message
        self.add(group_env("￼ bring the rope", self.at(5), mentions=[
            {"name": "Sam", "uuid": ME["aci"], "start": 0, "length": 1}]), self.now)
        rows = self.rows()[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "Signal · Climbing crew")
        self.assertEqual(rows[0]["reasons"][0], "Mentioned you")
        self.assertIn("@Sam bring the rope", rows[0]["text"])
        self.add(sent_env("will do", self.at(4), group="R3JvdXBJZA=="), self.now)
        self.assertEqual(self.rows()[0], [])  # the owner answered in the group
        self.add(group_env("good point", self.at(3), name="Dan", uuid_="d",
                           quote={"id": 1, "authorUuid": ME["aci"], "text": "will do"}), self.now)
        rows = self.rows()[0]
        self.assertEqual(rows[0]["reasons"][0], "Replied to you")
        self.assertEqual(rows[0]["who"], "Dan")

    def test_vip_and_ignore(self):
        self.add(dm_env("thanks", self.at(1), name="Alice Example"), self.now)
        rows = self.rows(vips={"alice@example.com": "Alice Example"})[0]
        self.assertEqual(rows[0]["reasons"], ["VIP"])
        self.assertTrue(rows[0]["vip"])
        chat = self.store.signal_chat_list()[0]["id"]
        self.assertEqual(self.rows(vips={"alice@example.com": "Alice Example"}, ignored=[chat])[0], [])

    def test_busy_group_heads_up(self):
        for i in range(26):
            self.add(group_env(f"msg {i}", self.at(20) + i * 1000), self.now)
        heads = self.rows()[1]
        self.assertEqual(heads, ["Busy Signal group: Climbing crew (26 messages in the last day)"])


class RetentionTest(TempStore):
    def test_prune(self):
        now = time.time()
        old, new = int((now - 31 * 86400) * 1000), int((now - 86400) * 1000)
        self.add(dm_env("old", old), now)
        self.add(dm_env("new", new), now)
        self.add(dm_env("vanishing", new + 1, expiresInSeconds=60), now - 120)
        self.store.signal_add({"chat": "gi1", "kind": "group", "title": "Old", "sender": "s1",
                               "ts": now - 400 * 86400, "text": "imported"}, now, imported=True)
        self.assertEqual(self.store.signal_counts(), {"live": 3, "imported": 1})
        self.assertEqual(self.store.signal_prune(now), 2)
        self.assertEqual([m["text"] for m in self.store.signal_recent(0)[0]["messages"]], ["new"])
        self.assertEqual(self.store.signal_counts(), {"live": 1, "imported": 1})
        self.assertEqual(self.store.signal_clear_imported(), 1)
        self.assertEqual(len(self.store.signal_chat_list()), 1)  # the empty chat went too

    def test_db_mode(self):
        self.assertEqual(oct(os.stat(self.store.path).st_mode & 0o777), "0o600")


# --- history import --------------------------------------------------------------------------
SECRETS = ("MEDIAROOT", "ACCTPROFILE", "ALICEPROFILE", "ALICEIDENTITY", "BOBPROFILE", "CAROLPROFILE",
           "CAROLIDENTITY", "GROUPMASTER", "INVITELINK", "ATTACHKEY", "DIGEST", "STICKERPACK", "PACKID", "PACKKEY")


def secret_b64(tag):
    return base64.b64encode((tag + "-SECRET-KEY-0123456789abcdef").encode()[:32]).decode()


class ImportParserTest(TempStore):
    def parse(self, path, step=None, title=None):
        p = signal_import.Parser(self.store.signal_hash, dt.timezone.utc, title)
        data = path.read_bytes()
        recs = []
        for i in range(0, len(data), step or len(data)):
            recs += p.feed(data[i:i + (step or len(data))])
        return p, recs + p.finish()

    def test_main_jsonl(self):
        p, recs = self.parse(FIX / "signal_main.jsonl")
        self.assertEqual(p.format, "backup")
        self.assertEqual((p.kept, p.bad, p.skipped), (7, 1, 6))
        texts = [r["text"] for r in recs]
        self.assertEqual(texts, ["Are we still on for Saturday?", "Yes, 10am at the gym.", "Hi, it's Bob",
                                 "@Sam Owner can you bring the rope?", "Agreed, good plan", "ask @Alice Example about it",
                                 "\U0001F44D @Carol thanks"])
        alice = recs[0]
        self.assertEqual((alice["kind"], alice["title"], alice["sender_name"], alice["outgoing"]),
                         ("dm", "Alice Example", "Alice Example", False))
        self.assertEqual((recs[1]["sender"], recs[1]["outgoing"], recs[1]["chat"]), ("me", True, alice["chat"]))
        self.assertEqual(recs[2]["title"], "+•••••••••43")  # no name: a masked number
        self.assertTrue(recs[3]["mentions_me"])
        self.assertTrue(recs[4]["quotes_me"])
        self.assertEqual({r["title"] for r in recs[3:]}, {"Climbing crew"})
        self.assertTrue(recs[3]["chat"].startswith("gi"))
        self.assertEqual(recs[0]["ts"], 1761800000.0)
        # a direct chat imported and the same chat received live share their ids (hashes of the ACI)
        live = signalcli.from_envelope(dm_env("x", 1, uuid_="aaaaaaaa-0000-4000-8000-000000000001"), ME,
                                       self.store.signal_hash, 0)[1]
        self.assertEqual((live["chat"], live["sender"]), (alice["chat"], alice["sender"]))
        # every key and number is dropped
        blob = json.dumps(recs, ensure_ascii=False)
        for tag in SECRETS:
            self.assertNotIn(secret_b64(tag), blob, tag)
        for digits in ("15551110001", "15559876543", "sam.01"):
            self.assertNotIn(digits, blob)

    def test_chunk_boundaries(self):
        whole = self.parse(FIX / "signal_main.jsonl")[1]
        for step in (1, 7, 64, 4096):
            self.assertEqual(self.parse(FIX / "signal_main.jsonl", step)[1], whole, step)

    def test_signal_export(self):
        p, recs = self.parse(FIX / "signal_export_data.json", title="Alice Example")
        self.assertEqual(p.format, "export")
        self.assertEqual((p.kept, p.bad, p.skipped), (3, 1, 1))
        self.assertEqual([(r["outgoing"], r["text"]) for r in recs],
                         [(False, "Morning! Coffee later?"), (True, "Sure, 3pm?"), (False, "See you then")])
        self.assertEqual({r["title"] for r in recs}, {"Alice Example"})
        self.assertEqual(recs[0]["ts"], dt.datetime(2025, 10, 1, 9, 15, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(p.export_kind(), "dm")
        p2 = signal_import.Parser(self.store.signal_hash, dt.timezone(dt.timedelta(hours=2)), "x")
        r2 = p2.feed(b'{"date": "2025-10-01 09:15:00", "sender": "A", "body": "hi"}\n')
        self.assertEqual(r2[0]["ts"], recs[0]["ts"] - 2 * H)  # naive times are the owner's time zone

    def test_malformed_and_long_lines(self):
        with mock.patch.object(signal_import, "MAX_LINE", 100):
            p = signal_import.Parser(self.store.signal_hash, None)
            out = p.feed(b'{"date": "2025-01-01 10:00", "sender": "A", "body": "ok"}\n' + b"x" * 150)
            out += p.feed(b"y" * 150 + b'\n[1,2]\n"str"\n{bad\n')
            out += p.feed(b'{"date": "2025-01-01 10:01", "sender": "A", "body": "after"}')
            out += p.finish()
        self.assertEqual([r["text"] for r in out], ["ok", "after"])
        self.assertEqual(p.bad, 4)
        p = signal_import.Parser(self.store.signal_hash, None)
        self.assertEqual(p.feed(b'{"hello": 1}\n{"date": 1}\n'), [])
        self.assertIsNone(p.format)

    def test_stage_pick_commit(self):
        _, recs = self.parse(FIX / "signal_main.jsonl")
        self.store.signal_stage_add(recs)
        staged = self.store.signal_stage_summary()
        self.assertEqual([(s["title"], s["count"]) for s in staged],
                         [("Climbing crew", 4), ("Alice Example", 2), ("+•••••••••43", 1)])
        group = staged[0]["chat"]
        with self.assertRaises(InputError):
            self.store.signal_stage_commit([], 0)
        with self.assertRaises(InputError):
            self.store.signal_stage_commit(["nope"], 0)
        self.assertEqual(self.store.signal_stage_commit([group], time.time()), 4)
        self.assertEqual(self.store.signal_stage_summary(), [])
        self.assertEqual(self.store.signal_counts(), {"live": 0, "imported": 4})
        self.assertEqual([c["title"] for c in self.store.signal_chat_list()], ["Climbing crew"])
        self.assertEqual(self.store.signal_recent(0), [])  # imported history never makes rows
        self.store.signal_stage_add(recs)
        self.assertEqual(self.store.signal_stage_commit([group], time.time()), 0)  # no duplicates
        self.store.close()
        db = b"".join(p.read_bytes() for p in pathlib.Path(self.tmp.name).glob("callsheet.db*"))
        for tag in SECRETS:
            self.assertNotIn(secret_b64(tag).encode(), db, tag)
        self.assertNotIn(b"15559876543", db)
        self.store = Store(self.tmp.name)


# --- the API --------------------------------------------------------------------------------
class SignalApiTest(Base):
    def setUp(self):
        super().setUp()
        svc = signalcli.SignalService(self.app.store, self.tmp.name, binary=[sys.executable, str(FAKE)],
                                      log=lambda s: None)
        svc.backoff = (0.2,)
        self.app.signal.service = svc
        self.conf = pathlib.Path(self.tmp.name) / "signal-cli"

    def upload(self, path, name=None, chat=None, step=10_000):
        data = path.read_bytes()
        up = self.ok("POST", "/api/signal/import/start", {"name": name or path.name, "size": len(data), "chat": chat})
        for i in range(0, len(data), step):
            st, body, text = self.call("POST", f"/api/signal/import/{up['id']}/chunk", raw=data[i:i + step])
            self.assertEqual(st, 200, text)
        return self.ok("POST", f"/api/signal/import/{up['id']}/finish")

    def head_only(self, path, length):
        """The status for a POST that announces `length` bytes (the server answers before reading them)."""
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=30)
        c.putrequest("POST", path)
        c.putheader("X-Callsheet", "1")
        c.putheader("Content-Length", str(length))
        c.endheaders()
        status = c.getresponse().status
        c.close()
        return status

    def test_status_link_unlink(self):
        st = self.ok("GET", "/api/signal")
        self.assertEqual((st["linked"], st["link"]["state"], st["keep_days"]), (False, "idle", 30))
        st = self.ok("POST", "/api/signal/link")
        self.assertTrue(st["link"]["qr_svg"].startswith("<svg"))
        (self.conf / "scan").write_text("")
        self.assertTrue(wait_for(lambda: self.ok("GET", "/api/signal")["linked"]))
        self.assertEqual(self.call("POST", "/api/signal/link")[0], 409)
        state = self.ok("GET", "/api/state")
        personal = [a for a in state["areas"] if a["id"] == "personal"][0]
        self.assertTrue(personal["signal"] and personal["chat_available"])
        self.assertEqual(self.call("POST", "/api/signal/unlink", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/signal/unlink", {"confirm": True}, header=False)[0], 403)
        st = self.ok("POST", "/api/signal/unlink", {"confirm": True})
        self.assertFalse(st["linked"])
        self.assertFalse(self.conf.exists())

    def test_settings(self):
        self.app.store.signal_add({"chat": "dabc", "kind": "dm", "title": "Alice", "sender": "s1", "ts": time.time(),
                                   "text": "hi"}, time.time())
        st = self.ok("PATCH", "/api/signal", {"ignored": ["dabc"]})
        self.assertEqual(st["ignored"], ["dabc"])
        self.assertEqual(st["chats"][0]["title"], "Alice")
        self.assertEqual(self.call("PATCH", "/api/signal", {"ignored": ["nope"]})[0], 400)
        self.assertEqual(self.call("PATCH", "/api/signal", {"area_id": "nope"})[0], 404)
        self.assertEqual(self.call("PATCH", "/api/signal", {"number": "x"})[0], 400)

    def test_import_flow(self):
        res = self.upload(FIX / "signal_main.jsonl", step=333)
        self.assertEqual(res["last"]["format"], "backup")
        self.assertEqual(res["last"]["kept"], 7)
        chats = {s["title"]: s["chat"] for s in res["staged"]}
        res = self.upload(FIX / "signal_export_data.json", name="data.json", chat="Alice (signal-export)")
        self.assertEqual(len(res["staged"]), 4)  # a data.json adds its chat to what is waiting
        res = self.ok("POST", "/api/signal/import/commit", {"chats": [chats["Climbing crew"]]})
        self.assertEqual((res["added"], res["staged"]), (4, []))  # the rest is discarded
        res = self.upload(FIX / "signal_export_data.json", name="data.json", chat="Alice (signal-export)")
        self.assertEqual(len(res["staged"]), 1)
        self.assertEqual(res["staged"][0]["title"], "Alice (signal-export)")
        res = self.ok("POST", "/api/signal/import/commit", {"chats": [res["staged"][0]["chat"]]})
        self.assertEqual(res["added"], 3)
        self.assertEqual(res["counts"]["imported"], 7)
        res = self.upload(FIX / "signal_main.jsonl")
        self.assertEqual(len(res["staged"]), 3)
        self.assertEqual(set(chats), {s["title"] for s in res["staged"]})
        res = self.ok("DELETE", "/api/signal/import")
        self.assertEqual(res["staged"], [])
        self.assertEqual(self.call("POST", "/api/signal/history/clear", {})[0], 400)
        self.assertEqual(self.ok("POST", "/api/signal/history/clear", {"confirm": True})["counts"]["imported"], 0)

    def test_import_limits(self):
        st, body, _ = self.call("POST", "/api/signal/import/start", {"name": "main.jsonl", "size": 201 * 1024 * 1024})
        self.assertEqual(st, 400)
        self.assertIn("200 MB", body["error"])
        self.assertEqual(self.call("POST", "/api/signal/import/start", {"name": "x.zip", "size": 10})[0], 400)
        self.assertEqual(self.call("POST", "/api/signal/import/up_00000000/chunk", raw=b"abc")[0], 404)
        up = self.ok("POST", "/api/signal/import/start", {"name": "main.jsonl", "size": 10})
        up2 = self.ok("POST", "/api/signal/import/start", {"name": "main.jsonl", "size": 10})  # replaces it
        self.assertEqual(self.call("POST", f"/api/signal/import/{up['id']}/chunk", raw=b"x")[0], 404)
        self.assertEqual(self.call("POST", f"/api/signal/import/{up2['id']}/chunk", raw=b"x" * 11)[0], 400)
        # only the chunk route takes more than 64 KB, and only up to 8 MB
        self.assertEqual(self.call("POST", "/api/signal/import/start", raw=b"{" + b" " * 70_000 + b"}")[0], 413)
        up = self.ok("POST", "/api/signal/import/start", {"name": "main.jsonl", "size": 9 * 1024 * 1024})
        self.assertEqual(self.head_only(f"/api/signal/import/{up['id']}/chunk", 8 * 1024 * 1024 + 1), 413)
        self.assertEqual(self.head_only("/api/signal/import/start", 64 * 1024 + 1), 413)
        up = self.ok("POST", "/api/signal/import/start", {"name": "notes.txt", "size": 30})
        self.call("POST", f"/api/signal/import/{up['id']}/chunk", raw=b'{"a": 1}\n{"b": 2}\n')
        st, body, _ = self.call("POST", f"/api/signal/import/{up['id']}/finish")
        self.assertEqual(st, 400)
        self.assertIn("isn't a Signal export", body["error"])

    def test_rows_in_brief(self):
        store = self.app.store
        store.signal_update_meta(linked=True, number="+•••••••••67")
        now = time.time()
        env = dm_env("Can you approve the budget by Friday?", int((now - 5 * H) * 1000))
        rec = signalcli.from_envelope(env, ME, store.signal_hash, now)[1]
        store.signal_add(rec, now)
        self.app.store.update_writer({"provider": "off"})
        self.ok("POST", "/api/sync")
        brief = self.ok("GET", "/api/brief")
        rows = [r for r in brief["replies"] if r.get("via") == "signal"]
        self.assertEqual(len(rows), 1, brief["replies"])
        r = rows[0]
        self.assertEqual(r["source"], "Signal · DM")
        self.assertIn("Mentions a deadline", r["reasons"])
        self.assertIn("claim", r)
        self.assertIn("wait", r)
        self.assertIn("Signal", brief["sources"])
        refs = store.brief_refs()
        self.assertEqual(refs[r["id"]]["source"], "signal")
        self.assertNotIn("messages", refs[r["id"]])
        # done, then a newer message reopens the row
        self.ok("POST", "/api/done", {"row_id": r["id"], "key": r["key"], "done": True})
        brief = self.ok("GET", "/api/brief")
        self.assertFalse([x for x in brief["replies"] if x.get("via") == "signal"])
        rec2 = signalcli.from_envelope(dm_env("Any news?", int((now - 4 * H) * 1000)), ME, store.signal_hash, now)[1]
        store.signal_add(rec2, now)
        self.ok("POST", "/api/sync")
        brief = self.ok("GET", "/api/brief")
        self.assertEqual(len([x for x in brief["replies"] if x.get("via") == "signal"]), 1)
        # the writer gets the chat's messages in its data
        data, ids, ask, _ = self.app.syncer.writer_data(dict(store.writer_config(), suggest_replies=True))
        sig = [x for x in data["replies"] if x["source"] == "Signal · DM"][0]
        self.assertEqual(sig["messages"][-1]["text"], "Any news?")
        # hide this conversation
        r = [x for x in brief["replies"] if x.get("via") == "signal"][0]
        self.ok("POST", "/api/suppress", {"row_id": r["id"], "key": r["key"], "scope": "thread"})
        self.assertEqual([x["source"] for x in store.suppress_rules()], ["signal"])
        self.assertFalse([x for x in self.ok("GET", "/api/brief")["replies"] if x.get("via") == "signal"])


# --- packaging ------------------------------------------------------------------------------
class PackagingTest(unittest.TestCase):
    def test_dockerfile(self):
        text = (ROOT / "callsheet" / "Dockerfile").read_text()
        self.assertIn("ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24\n", text)
        self.assertIn('ENV JAVA_OPTS="-Xmx256m -XX:+UseSerialGC"\n', text)
        # one RUN: the packages, then the checks, then the pinned SDK (no curl/wget, no runtime downloads)
        runs = re.findall(r"^RUN (.*?)(?<!\\)\n", text, re.M | re.S)
        apk = [r for r in runs if "apk add" in r]
        self.assertEqual(len(apk), 1)
        run = apk[0]
        self.assertIn("apk add --no-cache tzdata openjdk25-jre-headless 'signal-cli>=0.14.9' "
                      "'java-libsignal-client>=0.103.0'", run)
        self.assertIn("test -f /usr/lib/libsignal_jni.so && signal-cli --version", run)
        self.assertLess(run.index("apk add"), run.index("test -f /usr/lib/libsignal_jni.so"))
        self.assertIn("pip install --no-cache-dir anthropic==2.3.0", run)
        self.assertIn("grep -q '/community$' /etc/apk/repositories", run)
        self.assertLess(text.index("ENV JAVA_OPTS"), text.index("RUN "))
        for bad in ("curl", "wget", "http://", "--allow-untrusted", "edge/"):
            self.assertNotIn(bad, run)
        self.assertNotIn("\r", text)

    def test_config_backup_exclude_and_version(self):
        text = (ROOT / "callsheet" / "config.yaml").read_text()
        m = re.search(r"^backup_exclude:\n((?:  - .*\n)+)", text, re.M)
        self.assertIsNotNone(m)
        items = re.findall(r'  - "([^"]+)"', m.group(1))
        self.assertIn("signal-cli", items)
        self.assertIn("signal-cli/**", items)
        import api
        self.assertIn(f'version: "{api.APP_VERSION}"', text)

    def test_signal_dir_name(self):
        self.assertEqual(signalcli.DIR_NAME, "signal-cli")
        self.assertEqual(signalcli.DEVICE_NAME, "Callsheet")


if __name__ == "__main__":
    unittest.main()
