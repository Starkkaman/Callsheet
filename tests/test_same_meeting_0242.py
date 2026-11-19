"""0.24.2 (the owner: an email and a calendar invite for the same meeting showed as a clash): the mail's item and the
calendar entry are one meeting, by the iCalendar UID when the mail carries the invite's calendar part, else by a word
of the title in common, the same time (within 15 minutes) and the entry's organizer as the sender. It's never a clash
with itself (made or kept open); a real clash (another organizer, nothing in common) still is. Fictional people and
example domains only; no model server, no network."""
import base64
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_ledger_0160 import TZ, item, ts, when  # noqa: E402
from test_need_0230 import NeedBase, att, ev, guest  # noqa: E402

import conflicts  # noqa: E402
import heads  # noqa: E402
import ledger as L  # noqa: E402
import live  # noqa: E402

UID = "7kq2plan-0242@cal.example"


def ics(uid=UID):
    text = f"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:{uid[:10]}\r\n {uid[10:]}\r\nEND:VEVENT\r\n"
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class SameBase(NeedBase):
    def mail(self, mid, sender, subject, it, uid="", at="2027-03-01T08:00-08:00"):
        self.now[0] = ts(at)
        files = [{"name": "", "type": "text/calendar", "size": 300, "uid": uid, "inline": True}] if uid else []
        self.ms.upsert_message({"source": "gmail", "account": "acct", "msg_id": mid, "thread": "t-" + mid,
                                "direction": "in", "sender": sender, "sender_name": sender.split("@")[0].title(),
                                "recipients": [{"name": "Sam Rowe", "address": "sam@home.example"}], "ts": ts(at),
                                "subject": subject, "text": subject, "labels": [], "attachments": files})
        self.fake.answers[mid] = {"items": [it], "updates": []}
        return self.ex.read(self.ms.message("gmail", "acct", mid))["items"][0]

    def meeting(self, eid, title, start, end, organizer="jonas@work.example", **kw):
        return self.put(ev(eid, title, start, end, organizer=organizer,
                           attendees=[att("accepted"), guest("Jonas Weber")], **kw))[0]

    def sweep(self):
        return self.ms.write(lambda db: conflicts.sweep(db, self.now[0], L.zone(TZ)))

    def cal_ref(self, iid):
        return self.ms._read("SELECT cal_ref FROM ledger_items WHERE id=?", (iid,))[0]["cal_ref"]  # noqa: SLF001


def meet(what, start, end, date="2027-03-02"):
    return item("event", "sender", what, when("Tue", date, start, end), "open", "sure", "meeting")


class TestOneMeeting(SameBase):
    def test_the_invite_uid_makes_them_one_whatever_the_title_and_sender(self):
        e = self.meeting("j1", "Q2 planning sync", "2027-03-02T10:00-08:00", "2027-03-02T10:30-08:00", ical_uid=UID)
        iid = self.mail("m1", "assistant@other.example", "Invitation: roadmap review",
                        meet("roadmap review", "10:00", "10:30"), uid=UID)
        self.assertEqual(self.cal_ref(iid), e["id"])
        self.sweep()
        self.assertEqual(self.lv.conflicts(state=None), [])

    def test_title_time_and_organizer_when_the_mail_comes_first(self):
        iid = self.mail("m1", "jonas@work.example", "Budget numbers before Tuesday",
                        meet("budget numbers", "10:05", "11:00"))
        self.assertIsNone(self.cal_ref(iid))
        e = self.meeting("j1", "Q1 budget review: finance team", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00")
        self.sweep()
        self.assertEqual(self.lv.conflicts(state=None), [])             # never a clash with itself
        self.assertEqual(self.cal_ref(iid), e["id"])                     # now one item: its calendar entry
        hist = self.ms._read("SELECT note FROM ledger_history WHERE item=?", (iid,))  # noqa: SLF001
        self.assertTrue(any("the same meeting" in (h["note"] or "") for h in hist))

    def test_an_open_clash_with_itself_resolves(self):
        iid = self.mail("m1", "jonas@work.example", "Budget numbers before Tuesday",
                        meet("budget numbers", "10:05", "11:00"))
        self.meeting("j1", "Q1 budget review: finance team", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00")
        real = L.same_meeting
        L.same_meeting = lambda *a, **k: ""                               # as on 0.24.1: a clash
        try:
            self.sweep()
        finally:
            L.same_meeting = real
        self.assertEqual(len(self.lv.conflicts()), 1)
        self.sweep()
        self.assertEqual(self.lv.conflicts(), [])
        self.assertEqual([c["resolved_why"] for c in self.lv.conflicts(state=None)], ["the same meeting"])
        self.assertIsNotNone(self.cal_ref(iid))

    def test_a_real_clash_stays_a_clash(self):
        self.mail("m1", "jonas@work.example", "Budget numbers before Tuesday",
                  meet("budget numbers", "10:05", "11:00"))
        self.meeting("j1", "Q1 budget review", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00",
                     organizer="maria@work.example")                     # another organizer
        self.sweep()
        self.assertEqual(len(self.lv.conflicts()), 1)

    def test_nothing_in_common_or_another_time_stays_a_clash(self):
        self.mail("m1", "jonas@work.example", "Hiring loop", meet("hiring loop", "10:00", "11:00"))
        self.meeting("j1", "Q1 budget review", "2027-03-02T10:00-08:00", "2027-03-02T11:00-08:00")
        self.sweep()
        self.assertEqual(len(self.lv.conflicts()), 1)                    # no word in common
        self.mail("m2", "jonas@work.example", "Budget prep", meet("budget prep", "10:30", "11:00"))
        self.sweep()
        with_entry = [c for c in self.lv.conflicts() if "event" in (c.get("a_kind"), c.get("b_kind"))]
        self.assertEqual(len(with_entry), 2, self.lv.conflicts())        # 30 minutes later: another meeting

    def test_another_uid_is_not_the_same_meeting(self):
        self.meeting("j1", "Q2 planning sync", "2027-03-02T10:00-08:00", "2027-03-02T10:30-08:00", ical_uid=UID)
        iid = self.mail("m1", "assistant@other.example", "Invitation: roadmap review",
                        meet("roadmap review", "10:00", "10:30"), uid="other-uid@cal.example")
        self.assertIsNone(self.cal_ref(iid))
        self.sweep()
        self.assertEqual(len(self.lv.conflicts()), 1)


class TestCalendarPart(unittest.TestCase):
    def test_gmail_text_reads_the_uid_of_an_inline_calendar_part(self):
        payload = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"See you then").decode()}},
            {"mimeType": "text/calendar", "body": {"data": ics(), "size": 120}},
            {"mimeType": "application/ics", "filename": "invite.ics", "body": {"attachmentId": "a1", "size": 120}}]}
        text, files = live.gmail_text(payload)
        self.assertEqual(text, "See you then")
        self.assertEqual(files[0], {"name": "", "type": "text/calendar", "size": 120, "uid": UID, "inline": True})
        self.assertEqual(files[1], {"name": "invite.ics", "type": "application/ics", "size": 120})   # not fetched
        self.assertEqual(live.ical_uid(""), "")
        self.assertEqual(live.ical_uid("!!notbase64"), "")

    def test_an_inline_part_is_not_a_calendar_file(self):
        inline = '[{"name": "", "type": "text/calendar", "size": 9, "uid": "x", "inline": true}]'
        self.assertFalse(heads._has_cal_file({"attachments": inline}))   # noqa: SLF001
        self.assertTrue(heads._has_cal_file({"attachments": '[{"name": "invite.ics", "type": ""}]'}))  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
