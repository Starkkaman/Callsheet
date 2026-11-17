"""0.16.1 rework stage 7a: texts (texts.py), with fakes only (no network, no real /share).

The nightly SMS Backup & Restore XML (SMS and MMS, both directions, emoji written as UTF-16 halves, malformed XML,
entity attacks, a huge file, path safety like Reports), the phone's Last notification sensor through ha.py's two new
GETs (an in-process fake and a fake Core API over HTTP), linking a live notification to its backup copy, the Sources
rows, the Setup API, and the allowlist (everything else stays refused).
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
FIX = ROOT / "tests" / "fixtures" / "texts"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT / "tests"))

import fake_ha  # noqa: E402
import ha  # noqa: E402
import ledger as L  # noqa: E402
import live  # noqa: E402
import msgstore  # noqa: E402
import texts  # noqa: E402
from store import InputError  # noqa: E402

NOW = 1_794_400_000.0   # 2026-11-11 12:26:40 UTC
BACKUP = "sms-20261111031000.xml"
SENSOR = "sensor.pixel_8_last_notification"
GM = "com.google.android.apps.messaging"


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def parse(name):
    return texts.parse_bytes((FIX / name).read_bytes())


# --- the XML -------------------------------------------------------------------------------------------------------
class ParseTest(unittest.TestCase):
    def test_sms_and_mms_both_directions(self):
        recs, ps = parse(BACKUP)
        self.assertEqual(len(recs), 6)
        self.assertEqual(ps.skipped, {"draft or unsent": 2})          # a draft and a failed send: counted, not kept
        self.assertEqual(ps.backup_date, 1794366600.0)
        by = {r["text"][:12]: r for r in recs}
        a = by["Can you do t"]
        self.assertEqual((a["source"], a["account"], a["direction"], a["sender"], a["sender_name"]),
                         ("sms", "phone", "in", "+1 (555) 010-1234", "Dana Reyes"))
        self.assertEqual(a["ts"], 1794360000.0)
        self.assertTrue(a["text"].endswith("10? \U0001F600"))      # &#55357;&#56832; -> one emoji
        b = by["Yes 👍 see yo"]
        self.assertEqual((b["direction"], b["sender"]), ("out", "me"))
        self.assertEqual(b["text"], "Yes 👍 see you there & bring the <cards>")
        self.assertEqual(b["recipients"], [{"name": "Dana Reyes", "address": "5550101234"}])
        self.assertEqual(a["thread"], b["thread"])                    # "+1 (555) 010-1234" and "5550101234": one thread
        g = by["G-123456 is "]
        self.assertEqual((g["sender_name"], g["thread"]), ("", "sms:google"))   # "(Unknown)" is no name
        self.assertEqual(by["Lone half \ufffd "]["text"], "Lone half \ufffd here")   # a lone half: U+FFFD, file still read
        m = by["Location for"]
        self.assertEqual((m["direction"], m["sender"], m["sender_name"]), ("in", "+15550101234", "Dana Reyes"))
        self.assertEqual(m["text"], "Location for Thursday 📍 Pier 70")
        self.assertEqual(m["attachments"], [{"name": "IMG_0042.jpg", "type": "image/jpeg"}])   # no data, no smil
        self.assertEqual(m["labels"], ["mms", "group"])
        self.assertEqual(m["channel"], "Group text: Dana Reyes, Sam Lee")
        o = by["Got it, than"]
        self.assertEqual((o["direction"], o["sender"], o["thread"]), ("out", "me", m["thread"]))
        self.assertEqual(len(o["recipients"]), 2)
        self.assertNotIn("/9j/4AAQ", json.dumps(recs))               # attachment bytes are never kept

    def test_same_result_whatever_the_chunk_size(self):
        want = [r["msg_id"] for r in parse(BACKUP)[0]]
        for size in (1, 3, 7, 13, 64):
            with mock.patch.object(texts, "CHUNK", size):
                got = [r["msg_id"] for r in parse(BACKUP)[0]]
            self.assertEqual(got, want, size)

    def test_ids_are_stable(self):
        self.assertEqual([r["msg_id"] for r in parse(BACKUP)[0]], [r["msg_id"] for r in parse(BACKUP)[0]])

    def test_fix_refs(self):
        self.assertEqual(texts.fix_refs(b"&#55357;&#56832;"), b"&#128512;")
        self.assertEqual(texts.fix_refs(b"&#xD83D;&#xDE00;"), b"&#128512;")
        self.assertEqual(texts.fix_refs(b"a&#56832;b&#1;c&#65;"), b"a&#65533;b&#65533;c&#65;")
        self.assertEqual(texts.fix_refs(b"no refs &amp; here"), b"no refs &amp; here")

    def test_refusals(self):
        for name, why in (("sms-malformed.xml", "well-formed"), ("sms-entities.xml", "DTD"),
                          ("sms-external.xml", "DTD"), ("sms-undefined-entity.xml", "well-formed"),
                          ("calls-20261111031000.xml", "isn't an SMS Backup")):
            with self.assertRaises(texts.BadFile, msg=name) as cm:
                parse(name)
            self.assertIn(why, str(cm.exception), name)

    def test_entity_attack_is_refused_fast_and_reads_nothing(self):
        raw = (FIX / "sms-entities.xml").read_bytes().replace(b"&lol4;", b"&lol4;" * 1000)
        t0 = time.monotonic()
        got = []
        with self.assertRaises(texts.BadFile):
            texts.parse_bytes(raw, got.append)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(got, [])
        with mock.patch("builtins.open", side_effect=AssertionError("a file was opened")):
            with self.assertRaises(texts.BadFile):
                parse("sms-external.xml")

    def test_depth_and_count_caps(self):
        deep = b"<smses>" + b"<a>" * 20 + b"</a>" * 20 + b"</smses>"
        with self.assertRaises(texts.BadFile) as cm:
            texts.parse_bytes(deep)
        self.assertIn("nested", str(cm.exception))
        many = b"<smses>" + b'<sms address="1" date="1" type="1" body="x"/>' * 5 + b"</smses>"
        with mock.patch.object(texts, "MAX_MESSAGES", 4), self.assertRaises(texts.BadFile) as cm:
            texts.parse_bytes(many)
        self.assertIn("more than 4", str(cm.exception))

    def test_a_run_of_references_is_capped(self):
        raw = b'<smses><sms address="1" date="1" type="1" body="' + b"&#65;" * 1000 + b'"/></smses>'
        with mock.patch.object(texts, "MAX_CARRY", 100), mock.patch.object(texts, "CHUNK", 4096), \
                self.assertRaises(texts.BadFile):
            texts.parse_bytes(raw)
        self.assertEqual(texts.parse_bytes(raw)[0][0]["text"], "A" * 1000)

    def test_mms_seconds_and_address_keys(self):
        rec, _ = texts.mms_record({"date": "1794362000", "msg_box": "1", "address": "+15550101234"},
                                  [{"ct": "text/plain", "text": "hi"}], [{"address": "+15550101234", "type": "137"}])
        self.assertEqual(rec["ts"], 1794362000.0)
        self.assertEqual(texts.addr_key("+1 (555) 010-1234"), "5550101234")
        self.assertEqual(texts.addr_key("GOOGLE"), "google")
        self.assertEqual(texts.addr_key("Dana's 2nd phone"), "dana's 2nd phone")


# --- the folder ----------------------------------------------------------------------------------------------------
class FolderBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.share = os.path.join(self.root, "share")
        self.folder = os.path.join(self.share, "texts-backup")
        os.makedirs(self.folder)
        env = mock.patch.dict(os.environ, {"CALLSHEET_REPORTS_ROOTS": self.share})
        env.start()
        self.addCleanup(env.stop)
        self.clock = Clock()
        self.ms = msgstore.MessageStore(os.path.join(self.root, "data"), clock=self.clock)
        self.addCleanup(self.ms.close)
        texts.update_settings(self.ms, {"folder": self.folder})
        self.reader = texts.BackupReader(self.ms, self.clock)

    def put(self, name, src=BACKUP, data=None, age=3600):
        p = os.path.join(self.folder, name)
        pathlib.Path(p).write_bytes(data if data is not None else (FIX / src).read_bytes())
        os.utime(p, (NOW - age, NOW - age))
        return p


class FolderTest(FolderBase):
    def test_import_is_idempotent_and_rereads_only_changed_files(self):
        self.put(BACKUP)
        self.put("calls-20261111031000.xml", "calls-20261111031000.xml")
        res = self.reader.tick("phone")
        self.assertEqual((res["new"], res["backlog"]), (6, 0))
        self.assertEqual(self.ms.count("sms"), 6)
        self.assertEqual(self.ms.count("sms", "out"), 2)
        files = self.ms.cursor("sms", "phone", "files")
        self.assertEqual(list(files), [BACKUP])                       # calls-*.xml is never opened
        self.assertEqual(files[BACKUP]["status"], "read")
        with mock.patch.object(texts.Folder, "read", side_effect=AssertionError("read again")):
            self.assertEqual(self.reader.tick("phone")["new"], 0)     # unchanged: not read again
        p = self.put(BACKUP, age=1800)                                 # the same backup rewritten (newer time)
        res = self.reader.tick("phone")
        self.assertEqual(res["new"], 0)                                # read again, nothing new, nothing doubled
        self.assertEqual(self.ms.count("sms"), 6)
        # tomorrow's full backup: everything again plus one more
        raw = pathlib.Path(p).read_bytes().replace(b"</smses>", b'<sms address="+15550101234" date="1794390000000" '
                                                   b'type="1" body="One more" contact_name="Dana Reyes" /></smses>')
        self.put("sms-20261112031000.xml", data=raw, age=600)
        self.assertEqual(self.reader.tick("phone")["new"], 1)
        self.assertEqual(self.ms.count("sms"), 7)
        hit = self.ms.search("Pier 70", source="sms")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["title"], "Group text: Dana Reyes, Sam Lee")

    def test_bad_files_are_reported_not_hidden(self):
        self.put("sms-malformed.xml", "sms-malformed.xml")
        self.reader.tick("phone")
        f = self.ms.cursor("sms", "phone", "files")["sms-malformed.xml"]
        self.assertEqual(f["status"], "bad")
        self.assertIn("well-formed", f["why"])
        self.assertEqual(self.ms.count("sms"), 1)                      # what was read before the error is kept
        line, warn, stale = texts.backup_status(self.ms, NOW, live.dt.timezone.utc)
        self.assertTrue(stale)
        self.assertIn("Couldn't read sms-malformed.xml", warn)

    def test_huge_file_is_refused_with_a_reason(self):
        self.put(BACKUP)
        with mock.patch.object(texts, "MAX_FILE_BYTES", 1000):
            self.reader.tick("phone")
        f = self.ms.cursor("sms", "phone", "files")[BACKUP]
        self.assertEqual(f["status"], "bad")
        self.assertIn("bigger than", f["why"])
        self.assertEqual(self.ms.count("sms"), 0)
        self.put(BACKUP, age=100)                                       # a new copy under the cap is read
        self.reader.tick("phone")
        self.assertEqual(self.ms.count("sms"), 6)

    def test_links_young_and_odd_files(self):
        outside = os.path.join(self.root, "secret.xml")
        shutil.copy(FIX / BACKUP, outside)
        os.symlink(outside, os.path.join(self.folder, "sms-link.xml"))
        hard = self.put("sms-a.xml")
        os.link(hard, os.path.join(self.folder, "sms-b.xml"))
        self.put("sms-young.xml", age=5)
        self.put("sms-odd name.xml")
        self.put(".syncthing.sms-x.xml.tmp")
        res = self.reader.tick("phone")
        self.assertEqual(self.ms.count("sms"), 0)
        self.assertEqual(res["backlog"], 1)                             # the young one waits
        skipped = dict(map(tuple, self.ms.cursor("sms", "phone", "skipped")))
        self.assertIn("link", skipped["sms-link.xml"])
        self.assertIn("linked from another place", skipped["sms-a.xml"])
        self.assertIn("odd characters", skipped["sms-odd name.xml"])
        self.clock.t += 120
        self.reader.tick("phone")
        self.assertEqual(self.ms.count("sms"), 6)                       # the young one, once settled

    def test_folder_rules_like_reports(self):
        with self.assertRaises(InputError):
            texts.update_settings(self.ms, {"folder": self.root})       # outside /share
        with self.assertRaises(InputError):
            texts.update_settings(self.ms, {"folder": self.share})      # the root itself
        with self.assertRaises(InputError):
            texts.update_settings(self.ms, {"folder": self.folder + "/../../etc"})
        os.symlink(self.folder, os.path.join(self.share, "via-link"))
        with self.assertRaises(InputError):
            texts.update_settings(self.ms, {"folder": os.path.join(self.share, "via-link")})
        self.assertEqual(texts.settings(self.ms)["folder"], os.path.normpath(self.folder))
        # the folder later swapped for a link: the reader refuses it
        shutil.rmtree(self.folder)
        os.symlink(os.path.join(self.root, "data"), self.folder)
        with self.assertRaises(texts.TextsError):
            self.reader.tick("phone")

    def test_never_writes_to_the_folder(self):
        p = self.put(BACKUP)
        before = (os.stat(p).st_mtime_ns, pathlib.Path(p).read_bytes(), sorted(os.listdir(self.folder)))
        self.reader.tick("phone")
        self.assertEqual(before, (os.stat(p).st_mtime_ns, pathlib.Path(p).read_bytes(), sorted(os.listdir(self.folder))))
        src = (APP / "texts.py").read_text()
        for w in ("O_WRONLY", "O_RDWR", "O_CREAT", "os.unlink", "rename(", "os.remove(", "rmdir", "utime"):
            self.assertFalse(w in src, w)

    def test_sources_row_and_stale_warning(self):
        self.put(BACKUP)
        lv = live.Live(_StoreStub(), self.ms, clock=self.clock)
        lv.run_due("sms", force=True)
        row = next(r for r in lv.status()["sources"] if r["key"] == "sms")
        self.assertEqual((row["name"], row["state"], row["line"]), ("Texts", "current", "Nightly backup 03:10 · 6 messages"))
        self.clock.t = NOW + 36 * 3600   # the backup (03:10) is now 45 hours old; the folder still checked
        lv.run_due("sms", force=True)
        row = next(r for r in lv.status()["sources"] if r["key"] == "sms")
        self.assertEqual(row["state"], "behind")
        self.assertIn("45 hours old", row["warn"])
        self.assertTrue(row["line"].startswith("Nightly backup Wed Nov 11 03:10"))

    def test_missing_folder_is_an_error_row(self):
        shutil.rmtree(self.folder)
        lv = live.Live(_StoreStub(), self.ms, clock=self.clock)
        for _ in range(3):
            lv.run_due("sms", force=True)
        row = next(r for r in lv.status()["sources"] if r["key"] == "sms")
        self.assertEqual(row["state"], "error")
        self.assertIn("can't find", row["warn"])

    def test_no_row_until_the_owner_sets_it_up(self):
        ms = msgstore.MessageStore(os.path.join(self.root, "data2"), clock=self.clock)
        self.addCleanup(ms.close)
        lv = live.Live(_StoreStub(), ms, clock=self.clock)
        self.assertEqual([r["key"] for r in lv.status()["sources"]], [])    # no default folder, even one on disk
        texts.update_settings(ms, {"folder": self.folder})
        self.assertEqual([r["key"] for r in lv.status()["sources"]], ["sms"])
        texts.update_settings(ms, {"folder": ""})                         # cleared: off again
        self.assertEqual(texts.settings(ms)["folder"], "")
        self.assertEqual([r["key"] for r in lv.status()["sources"]], [])


class _StoreStub:
    """What live.Live asks of the main store when only texts are connected."""

    def settings(self):
        return {"timezone": "UTC"}

    def accounts(self):
        return []

    def slack_workspaces(self):
        return []


# --- live notifications --------------------------------------------------------------------------------------------
def note(ts, title, text, pkg=GM, big=None, **attrs):
    a = {"package": pkg, "post_time": int(ts * 1000), "android.title": title, "android.text": text,
         "friendly_name": "Pixel 8 Last notification"}
    if big is not None:
        a["android.bigText"] = big
    a.update(attrs)
    iso = live.dt.datetime.fromtimestamp(ts, live.dt.timezone.utc).isoformat()
    return {"entity_id": SENSOR, "state": text[:255], "attributes": a, "last_changed": iso, "last_updated": iso}


class FakeSensor:
    def __init__(self):
        self.history, self.state, self.calls, self.missing = [], None, [], False

    def notification_state(self, eid):
        self.calls.append(("state", eid))
        if self.missing:
            raise ha.HAError("Home Assistant doesn't have that sensor.", 404, "missing")
        return self.state or (self.history[-1] if self.history else {"state": "unknown"})

    def notification_history(self, eid, since):
        self.calls.append(("history", eid, since))
        return [h for h in self.history if live.dt.datetime.fromisoformat(h["last_updated"]).timestamp() >= since]


class LiveBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = Clock()
        self.ms = msgstore.MessageStore(self.dir, clock=self.clock)
        self.addCleanup(self.ms.close)
        self.fake = FakeSensor()
        texts.update_settings(self.ms, {"sensor": SENSOR})
        self.reader = texts.NotificationReader(self.ms, self.clock, self.fake)


class LiveTest(LiveBase):
    def test_keeps_only_allowed_apps_and_the_full_text(self):
        self.fake.history = [
            note(NOW - 600, "Dana Reyes", "Running 10 min late", big="Running 10 min late, start without me"),
            note(NOW - 500, "Mom", "Signal hello", pkg="org.thoughtcrime.securesms"),
            note(NOW - 400, "Gmail", "New mail", pkg="com.google.android.gm"),
            note(NOW - 300, "Messages", "Messages is running", is_ongoing=True),
            note(NOW - 200, "", "no title")]
        res = self.reader.tick(SENSOR)
        self.assertEqual(res["new"], 1)
        r = self.ms.search("", source="sms_live")[0]
        got = self.ms.get(r["id"])
        self.assertEqual(got["body"], "Running 10 min late, start without me")
        raw = self.ms._read("SELECT * FROM messages WHERE source='sms_live'")[0]  # noqa: SLF001
        self.assertEqual((raw["direction"], raw["sender_name"], raw["channel"]), ("in", "Dana Reyes", "Google Messages"))
        texts.update_settings(self.ms, {"apps": [GM, "org.thoughtcrime.securesms"]})
        self.ms.set_cursor("sms_live", SENSOR, "since", {"t": NOW - 3600})
        self.assertEqual(self.reader.tick(SENSOR)["new"], 1)          # Signal now allowed
        self.assertEqual(self.ms.count("sms_live"), 2)

    def test_reposts_and_overlap_are_one_message(self):
        self.fake.history = [note(NOW - 300, "Dana Reyes", "On my way"), note(NOW - 240, "Dana Reyes", "On my way")]
        self.assertEqual(self.reader.tick(SENSOR)["new"], 1)
        self.clock.t += 60
        self.fake.history.append(note(NOW + 30, "Dana Reyes", "On my way"))   # posted again
        self.reader.tick(SENSOR)
        self.assertEqual(self.ms.count("sms_live"), 1)
        self.fake.history.append(note(NOW + 45, "Dana Reyes", "Parking now"))
        self.reader.tick(SENSOR)
        self.assertEqual(self.ms.count("sms_live"), 2)

    def test_history_only_when_the_sensor_changed(self):
        self.fake.history = [note(NOW - 300, "Dana Reyes", "On my way")]
        self.reader.tick(SENSOR)
        n = len([c for c in self.fake.calls if c[0] == "history"])
        self.clock.t += 60
        self.reader.tick(SENSOR)
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "history"]), n)   # nothing new: state only
        since = self.ms.cursor("sms_live", SENSOR, "since")["t"]
        self.assertAlmostEqual(since, NOW - 300, delta=1)

    def test_first_read_looks_back_a_day(self):
        self.fake.history = [note(NOW - 300, "Dana Reyes", "x")]
        self.reader.tick(SENSOR)
        h = [c for c in self.fake.calls if c[0] == "history"][0]
        self.assertAlmostEqual(h[2], NOW - texts.LIVE_FIRST_S - texts.LIVE_OVERLAP_S, delta=1)

    def test_sources_row_and_unavailable_sensor(self):
        lv = live.Live(_StoreStub(), self.ms, clock=self.clock, ha_api=self.fake)
        # no backup folder saved: only the live row
        self.fake.history = [note(NOW - 120, "Dana Reyes", "On my way")]
        lv.run_due("sms_live", force=True)
        row = next(r for r in lv.status()["sources"] if r["key"] == "sms_live")
        self.assertEqual((row["name"], row["state"], row["line"]), ("Texts (live)", "current",
                                                                    "Last notification 2 min ago"))
        self.fake.state = {"entity_id": SENSOR, "state": "unavailable", "attributes": {},
                           "last_updated": live.dt.datetime.fromtimestamp(NOW, live.dt.timezone.utc).isoformat()}
        self.clock.t += 60
        lv.run_due("sms_live", force=True)
        row = next(r for r in lv.status()["sources"] if r["key"] == "sms_live")
        self.assertEqual((row["state"], row["word"]), ("behind", "Not receiving"))
        self.assertIn("unavailable", row["warn"])
        self.fake.missing = True
        for _ in range(3):
            lv.run_due("sms_live", force=True)
        row = next(r for r in lv.status()["sources"] if r["key"] == "sms_live")
        self.assertEqual(row["state"], "error")
        self.assertIn("doesn't have that sensor", row["warn"])

    def test_no_sensor_no_reader(self):
        texts.update_settings(self.ms, {"sensor": ""})
        self.assertEqual(self.reader.targets(), [])


# --- linking ---------------------------------------------------------------------------------------------------------
class LinkTest(LiveBase):
    def backup(self, ts, text, name="Dana Reyes", address="+15550101234"):
        rec, _ = texts.sms_record({"address": address, "date": str(int(ts * 1000)), "type": "1", "body": text,
                                   "contact_name": name})
        self.ms.upsert_message(rec)
        return self.ms.message("sms", "phone", rec["msg_id"])

    def live_row(self, ts, title, text):
        self.fake.history.append(note(ts, title, text))
        self.ms.set_cursor("sms_live", SENSOR, "since", {"t": ts - 10})
        self.reader.tick(SENSOR)
        return self.ms._read("SELECT * FROM messages WHERE source='sms_live' ORDER BY id DESC LIMIT 1")[0]  # noqa: SLF001

    def same_as(self, rid):
        return self.ms._read("SELECT same_as FROM messages WHERE id=?", (rid,))[0]["same_as"]  # noqa: SLF001

    def test_backup_copy_is_preferred(self):
        lv = self.live_row(NOW - 3600, "Dana Reyes", "Can you do the shoot Thursday at 10?")
        b = self.backup(NOW - 3600 + 40, "Can you do the shoot Thursday at 10?")
        self.assertEqual(texts.link(self.ms, NOW), 1)
        self.assertEqual(self.same_as(lv["id"]), b["id"])
        hits = self.ms.search("shoot Thursday")
        self.assertEqual([h["source"] for h in hits], ["sms"])          # the live copy is not shown twice
        self.assertEqual(self.ms.count("sms_live"), 1)                  # nothing deleted
        self.assertEqual(texts.link(self.ms, NOW), 0)                   # once

    def test_rules_of_a_match(self):
        a = self.live_row(NOW - 7200, "Dana Reyes", "Location for the shoot is Pier 70, gate B, come early pl")
        self.backup(NOW - 7200 + 30, "Location for the shoot is Pier 70, gate B, come early please")   # cut text
        b = self.live_row(NOW - 5000, "Dana Reyes", "See you")
        self.backup(NOW - 5000 + 400, "See you")                          # 6:40 apart: not the same
        c = self.live_row(NOW - 4000, "Dana Reyes", "ok")
        self.backup(NOW - 4000, "OK!", name="Sam Lee", address="+15550109999")   # someone else
        d = self.live_row(NOW - 3000, "+1 555-010-1234", "Number only")
        self.backup(NOW - 3000 - 100, "Number only", name="")
        e = self.live_row(NOW - 2000, "Shoot crew", "Sam Lee: bring the cards")   # a group notification
        self.backup(NOW - 2000, "bring the cards", name="Sam Lee", address="+15550109999")
        texts.link(self.ms, NOW)
        self.assertIsNotNone(self.same_as(a["id"]))
        self.assertIsNone(self.same_as(b["id"]))
        self.assertIsNone(self.same_as(c["id"]))
        self.assertIsNotNone(self.same_as(d["id"]))
        self.assertIsNotNone(self.same_as(e["id"]))

    def test_ledger_reads_the_backup_copy_or_hands_over(self):
        import extract
        L.ensure(self.ms)
        ex = extract.Extractor(self.ms, lambda: "UTC", lambda: "Alex", None, model=lambda: "m", clock=self.clock)
        # not read yet: the queue skips the linked live copy and reads the backup copy
        lv1 = self.live_row(NOW - 3600, "Dana Reyes", "Dinner Friday at 8?")
        b1 = self.backup(NOW - 3590, "Dinner Friday at 8?")
        texts.link(self.ms, NOW)
        ids = [r["id"] for r in ex.pending(limit=50, now=NOW)]
        self.assertIn(b1["id"], ids)
        self.assertNotIn(lv1["id"], ids)
        # already read from the live copy: the backup copy is marked with the reason and takes over its items' sources
        lv2 = self.live_row(NOW - 1800, "Dana Reyes", "Call me after the shoot")
        now = self.clock()

        def read(db):
            item = db.execute("INSERT INTO ledger_items (type, what, created_at, updated_at) VALUES "
                              "('ask', 'call Dana', ?, ?)", (now, now)).lastrowid
            L.add_source(db, item, lv2["id"], "created", now)
            L.mark_read(db, lv2["id"], "read", now, items=[item])
            return item
        item = self.ms.write(read)
        b2 = self.backup(NOW - 1790, "Call me after the shoot")
        texts.link(self.ms, NOW)
        got = self.ms._read("SELECT state, items, note FROM ledger_reads WHERE message=?", (b2["id"],))[0]  # noqa: SLF001
        self.assertEqual((got["state"], json.loads(got["items"])), ("skipped", [item]))
        self.assertIn("live notification", got["note"])
        self.assertNotIn(b2["id"], [r["id"] for r in ex.pending(limit=50, now=NOW)])
        led = L.Ledger(self.ms, lambda: "UTC", self.clock)
        self.assertEqual([s["source"] for s in led.sources(item)], ["sms"])   # shown as the backup copy


# --- ha.py: the two new GETs and nothing else ----------------------------------------------------------------------
class AllowlistTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": fake_ha.TOKEN})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CALLSHEET_HA_API_BASE", None)

    def test_exactly_the_sensor_state_and_history(self):
        base = "http://supervisor/core/api"
        ha.check_allowed("GET", base + "/states/" + SENSOR)
        ha.check_allowed("GET", base + "/history/period/2026-11-11T12:00:00+00:00?filter_entity_id=" + SENSOR)
        hist = base + "/history/period/2026-11-11T12:00:00+00:00"
        bad = [("GET", base + "/states"), ("GET", base + "/states/"), ("GET", base + "/states/sensor.pixel_8_battery"),
               ("GET", base + "/states/light.kitchen"), ("GET", base + "/states/sensor.x_last_notification_2"),
               ("GET", base + "/states/sensor._last_notification"), ("GET", base + "/states/SENSOR.X_LAST_NOTIFICATION"),
               ("POST", base + "/states/" + SENSOR), ("DELETE", base + "/states/" + SENSOR),
               ("GET", base + "/states/" + SENSOR + "?x=1"), ("GET", base + "/states/" + SENSOR + "/../light.x"),
               ("GET", base + "/states/sensor.a%2F..%2Fb_last_notification"),
               ("GET", hist), ("GET", hist + "?"), ("POST", hist + "?filter_entity_id=" + SENSOR),
               ("GET", hist + "?filter_entity_id=sensor.pixel_8_battery"),
               ("GET", hist + "?filter_entity_id=" + SENSOR + ",light.kitchen"),
               ("GET", hist + "?filter_entity_id=" + SENSOR + "&minimal_response"),
               ("GET", hist + "?filter_entity_id=" + SENSOR + "&end_time=2026-11-12T00:00:00+00:00"),
               ("GET", hist + "?x=1&filter_entity_id=" + SENSOR),
               ("GET", base + "/history/period?filter_entity_id=" + SENSOR),
               ("GET", base + "/history/period/2026-11-11?filter_entity_id=" + SENSOR),
               ("GET", base + "/history/period/2026-11-11T12:00:00-05:00?filter_entity_id=" + SENSOR),
               ("GET", base + "/history/period/2026-11-11T12:00:00+00:00/x?filter_entity_id=" + SENSOR),
               ("GET", base + "/logbook/2026-11-11T12:00:00+00:00?entity=" + SENSOR),
               ("GET", base + "/history/period/2026-11-11T12:00:00+00:00?filter_entity_id=" + SENSOR + "#x"),
               ("GET", "http://evil.example/core/api/states/" + SENSOR),
               ("GET", "http://supervisor/core/api2/states/" + SENSOR),
               ("GET", base + "/services?filter_entity_id=" + SENSOR),
               ("GET", base + "/template"), ("POST", base + "/services/notify/mobile_app_x?filter_entity_id=" + SENSOR)]
        for method, url in bad:
            with self.assertRaises(ha.NotAllowed, msg=f"{method} {url}"):
                ha.check_allowed(method, url)
        # the 0.5.1 pair still works, and nothing else of it changed
        ha.check_allowed("GET", base + "/services")
        ha.check_allowed("POST", base + "/services/notify/mobile_app_iphone")

    def test_helpers_refuse_other_entities_before_network(self):
        with mock.patch.object(ha._opener, "open", side_effect=AssertionError("network used")):  # noqa: SLF001
            for eid in ("light.kitchen", "sensor.battery", "sensor.x_last_notification?y", ""):
                with self.assertRaises(ha.NotAllowed):
                    ha.notification_state(eid)
                with self.assertRaises(ha.NotAllowed):
                    ha.notification_history(eid, NOW)


class FakeCore(fake_ha.FakeHA):
    """fake_ha plus the sensor's state and history, as Home Assistant answers them."""

    def __init__(self):
        super().__init__()
        self.states, self.history = {}, []

    def _handle(self, h):
        path, _, query = h.path.partition("?")
        if h.command == "GET" and path.startswith("/core/api/states/"):
            with self.lock:
                self.requests.append((h.command, h.path, None))
            st = self.states.get(path.rsplit("/", 1)[1])
            return self._send(h, 200, st) if st else self._send(h, 404, {"message": "Entity not found."})
        if h.command == "GET" and path.startswith("/core/api/history/period/"):
            with self.lock:
                self.requests.append((h.command, h.path, None))
            start = live.dt.datetime.fromisoformat(path.rsplit("/", 1)[1]).timestamp()
            eid = query.split("=", 1)[1]
            rows = [s for s in self.history if s["entity_id"] == eid
                    and live.dt.datetime.fromisoformat(s["last_updated"]).timestamp() >= start]
            return self._send(h, 200, [rows] if rows else [])
        return super()._handle(h)


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.core = FakeCore()
        self.addCleanup(self.core.close)
        env = mock.patch.dict(os.environ, self.core.env())
        env.start()
        self.addCleanup(env.stop)

    def test_discovery_and_reads_over_http(self):
        self.core.states[SENSOR] = note(NOW - 60, "Dana Reyes", "hi")
        self.core.states["sensor.iphone_last_notification"] = {"entity_id": "sensor.iphone_last_notification",
                                                                "state": "unavailable", "attributes": {}}
        got = texts.sensors()
        self.assertEqual(got, [{"id": "sensor.iphone_last_notification", "name": "iPhone Last notification",
                                "available": False},
                               {"id": SENSOR, "name": "Pixel 8 Last notification", "available": True}])
        self.core.history = [note(NOW - 120, "Dana Reyes", "hello"), note(NOW - 60, "Dana Reyes", "hi")]
        self.assertEqual(len(ha.notification_history(SENSOR, NOW - 3600)), 2)
        paths = [p for m, p, _ in self.core.requests]
        self.assertIn("/core/api/history/period/2026-11-11T11:26:40+00:00?filter_entity_id=" + SENSOR, paths)
        self.assertTrue(all(m == "GET" for m, _, _ in self.core.requests))
        for p in paths:   # only the services list, the phones' sensors and that history
            self.assertRegex(p, r"^/core/api/(services|states/sensor\.[a-z0-9_]+_last_notification|history/period/"
                                r"[0-9T:+-]+\?filter_entity_id=sensor\.[a-z0-9_]+_last_notification)$")

    def test_token_never_in_errors(self):
        try:
            ha.notification_state("sensor.nope_last_notification")
        except ha.HAError as e:
            self.assertEqual(e.code, "missing")
            self.assertNotIn(fake_ha.TOKEN, str(e))


# --- the Setup API -----------------------------------------------------------------------------------------------------
class ApiTest(unittest.TestCase):
    def test_get_patch_and_sensors(self):
        import api
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        share = os.path.join(d, "share")
        os.makedirs(os.path.join(share, "texts-backup"))
        with mock.patch.dict(os.environ, {"CALLSHEET_REPORTS_ROOTS": share, "SUPERVISOR_TOKEN": ""}):
            app = api.App(os.path.join(d, "data"))
            self.addCleanup(app.close)
            v = app.handle("GET", "texts", None)
            # 0.21.2: a new install has no folder (not even one on disk with the old default's name)
            self.assertEqual(v["settings"], {"folder": "", "folder_set": False, "sensor": "", "apps": [GM]})
            self.assertEqual(v["folder_hint"], "/share/<your folder>")
            self.assertEqual([a["id"] for a in v["apps"]], [GM, "org.thoughtcrime.securesms", "com.whatsapp"])
            v = app.handle("PATCH", "texts", {"folder": os.path.join(share, "texts-backup"), "sensor": SENSOR,
                                              "apps": ["com.whatsapp", GM]})
            self.assertEqual(v["settings"]["sensor"], SENSOR)
            self.assertEqual(v["settings"]["apps"], [GM, "com.whatsapp"])
            self.assertTrue(v["settings"]["folder_set"])
            for bad in ({"sensor": "light.kitchen"}, {"apps": ["com.evil"]}, {"apps": "x"}, {"folder": "/etc"},
                        {"other": 1}, {}):
                with self.assertRaises(InputError, msg=bad):
                    app.handle("PATCH", "texts", bad)
            self.assertEqual(app.handle("GET", "texts/sensors", None),
                             {"available": False, "sensors": [], "error": ha.NOT_IN_HA})
            st = app.handle("GET", "sources", None)
            self.assertEqual([r["key"] for r in st["sources"]], ["sms", "sms_live"])


if __name__ == "__main__":
    unittest.main()
