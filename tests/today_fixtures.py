"""0.18.0: a seeded ledger for Today (tests and the page checks). Every state of the approved v4
design comes from real readings through the extractor with a scripted fake model: no model
server, no network. The day is Wed 2026-11-11 in America/Los_Angeles; seed(..., stop=) stops at a moment:

  08:40  a red clash (tonight's grade review vs the class you said you'd teach), an amber one (Marco's call vs Dana's
         rigging, Thu 7:00 pm) and a far one (Sat 10:00 am); Needs you; Coming up from every source; Clean up
  09:05  the red clash cleared at 8:55 (you wrote to Ana); you told Dana yes at 9:02: a follow-up for Marco
  12:05  the noon reminder went out ("Marco still doesn't know")
  12:20  the follow-up cleared at 12:14 (you wrote to Marco)
"""
import datetime as dt

TZ = "America/Los_Angeles"
OWNER = "Jordan Vale"
STAGES = {"0840": "2026-11-11T08:40", "0905": "2026-11-11T09:05", "1205": "2026-11-11T12:05",
          "1220": "2026-11-11T12:20"}


def ts(s):
    if len(s) == 16:
        s += "-08:00"
    return dt.datetime.fromisoformat(s).timestamp()


def when(phrase="", date="", start="", end="", end_date="", all_day=False, approx=False):
    return {"phrase": phrase, "date": date, "start": start, "end": end, "end_date": end_date, "all_day": all_day,
            "approx": approx, "tz": "", "options": []}


def item(type="ask", who="sender", what="", purpose="", w=None, status="open", conf="sure", subtype=""):
    return {"type": type, "subtype": subtype, "who": who, "what": what, "for": purpose, "when": w or when(),
            "status": status, "says": "", "confidence": conf, "never_hide": False}


def upd(ref, status="", w=None, note="", tell="", due=""):
    return {"id": ref, "status": status, "when": w or when(), "note": note, "tell": tell, "due": due}


class Script:
    """The fake model: answers by message id."""

    def __init__(self):
        self.answers = {}

    def __call__(self, body, info):
        a = self.answers.get(info["msg_id"], {"items": [], "updates": []})
        return a(info) if callable(a) else a


class FakeAlerter:
    """Stands in for conflicts.Alerter.remind: records what would go to the owner's phones."""

    def __init__(self, state="sent"):
        self.sent, self.state = [], state

    def remind(self, title, message, tag, vip=False):
        self.sent.append({"title": title, "message": message, "tag": tag})
        return self.state


PEOPLE = {"marco": ("marco@bellini.example", "Marco Bellini", "gmail", "mail-marco"),
          "dana": ("+15550142", "Dana Kim", "signal", "sig-dana"),
          "ana": ("+15550101", "Ana Reyes", "signal", "sig-ana"),
          "priya": ("priya@lumen.example", "Priya Shah", "gmail", "mail-priya"),
          "kestrel": ("billing@kestrel.example", "Kestrel Rentals", "gmail", "mail-kestrel"),
          "lena": ("U02LENA", "Lena Ortiz", "slack", "sl-lena"),
          "sam_sms": ("+15550144", "Sam Okafor", "sms", "sms-sam"),
          "sam": ("+15550144", "Sam Okafor", "signal", "sig-sam"),
          "theo": ("+15550177", "Theo Park", "signal", "sig-theo"),
          "north": ("ap@northlight.example", "Northlight Studios", "gmail", "mail-north"),
          "mom": ("+15550110", "Mom", "sms", "sms-mom"),
          "skyway": ("noreply@skyway-airlines.example", "Skyway Airlines", "gmail", "mail-skyway"),
          "insure": ("policy@lensguard.example", "LensGuard", "gmail", "mail-lensguard"),
          "gallery": ("events@wavegallery.example", "Wave Gallery", "gmail", "mail-wave")}

LINKS = {"gmail": "https://mail.google.com/mail/u/0/#inbox/{mid}", "slack": "https://acme.slack.com/archives/C01/p{n}"}


class Seeder:
    def __init__(self, ms, ex, now, today=None):
        self.ms, self.ex, self.now, self.today = ms, ex, now, today
        self.fake = Script()
        ex.call = self.fake
        self.ids = {}
        self.n = 0

    def msg(self, mid, at, who, text, out=False, subject="", answer=None):
        handle, name, source, thread = PEOPLE[who]
        self.now[0] = ts(at)
        self.n += 1
        link = LINKS.get(source, "").format(mid=mid, n=1700000000 + self.n)
        recips = [{"name": name, "address": handle}] if out else [{"name": OWNER, "address": "me"}]
        self.ms.upsert_message({"source": source, "account": "acct", "msg_id": mid, "thread": thread,
                                "direction": "out" if out else "in", "sender": "me" if out else handle,
                                "sender_name": OWNER if out else name, "recipients": recips, "ts": ts(at),
                                "subject": subject, "text": text, "labels": [], "link": link})
        self.fake.answers[mid] = answer or {"items": [], "updates": []}
        res = self.ex.read(self.ms.message(source, "acct", mid))
        return (res.get("items") or [None])[0]


def events(ms):
    def ev(eid, title, start, end, all_day=False, location="", link=""):
        if all_day:
            a = dt.datetime.fromisoformat(start + "T00:00-08:00").timestamp()
            b = dt.datetime.fromisoformat(end + "T00:00-08:00").timestamp()
        else:
            a, b = ts(start), ts(end)
        return {"account": "acct", "calendar": "primary", "event_id": eid, "title": title, "start": start,
                "end": end, "start_ts": a, "end_ts": b, "all_day": all_day, "location": location, "link": link,
                "status": "confirmed", "tz": TZ}
    ms.upsert_events([
        ev("e-color", "Color session", "2026-11-11T08:30", "2026-11-11T09:30"),
        ev("e-lumen", "Lumen Films call", "2026-11-11T09:45", "2026-11-11T10:15", location="Google Meet",
           link="https://meet.google.com/abc-defg-hij"),
        ev("e-grade", "Cascade grade review", "2026-11-11T18:00", "2026-11-11T19:30"),
        ev("e-edit", "Edit review", "2026-11-23T10:00", "2026-11-23T11:00"),
        ev("e-party", "Holiday party", "2026-12-05", "2026-12-06", all_day=True),
        ev("e-dentist", "Dentist", "2026-12-09T15:30", "2026-12-09T16:30")])


CLEANUP = [("n1", "Filmmaker Weekly", "newsletter", "Filmmaker Weekly: festival season"),
           ("n2", "B&H Deals", "newsletter", "B&H: lens sale this week"),
           ("n3", "No Film School", "newsletter", "No Film School digest"),
           ("n4", "Frame.io", "newsletter", "Frame.io product update"),
           ("n5", "Criterion", "newsletter", "Criterion: new in the collection"),
           ("r1", "Adobe", "receipt", "Adobe receipt: Creative Cloud"),
           ("r2", "Lyft", "receipt", "Lyft ride receipt"),
           ("r3", "Amazon", "receipt", "Amazon order shipped: SD cards"),
           ("s1", "#general", "chatter", "#general: lunch order thread"),
           ("s2", "#random", "chatter", "#random: weekend photos"),
           ("s3", "#gear", "chatter", "#gear: tripod for sale"),
           ("s4", "#announcements", "chatter", "#announcements: office wifi")]


def seed(ms, ex, now, today, stop="1220"):
    """Fill the store and the ledger up to STAGES[stop]; now is the shared clock (a one-item list). Returns the ids."""
    end = ts(STAGES[stop])
    s = Seeder(ms, ex, now, today)
    i = s.ids
    now[0] = ts("2026-11-01T08:00")
    events(ms)
    s.msg("marco-0", "2026-11-02T10:00", "marco", "Loved the grade on the trailer. The night exteriors look great.",
          subject="Aster trailer")
    s.msg("marco-1", "2026-11-02T14:00", "marco", "Thanks! Glad it landed.", out=True, subject="Re: Aster trailer")
    i["priya"] = s.msg("priya-1", "2026-11-06T10:12", "priya",
                       "Hi! Are you free Dec 8-10 for a three-day shoot? It's for the Lumen campaign.",
                       subject="Dec 8-10 shoot?", answer={"items": [item(
                           what="your availability for a three-day shoot", purpose="the Lumen campaign",
                           w=when("Dec 8-10", "2026-12-08", end_date="2026-12-10", all_day=True))], "updates": []})
    i["ana_yes"] = s.msg("ana-1", "2026-11-06T11:20", "ana", "Yes, I can teach the 6 pm class Wednesday.", out=True,
                         answer={"items": [item("commitment", "owner", "teach the 6 pm class", "cover Ana's class",
                                                when("Wednesday 6 pm", "2026-11-11", "18:00", "19:30"), "accepted")],
                                 "updates": []})
    i["kestrel"] = s.msg("kestrel-1", "2026-11-07T09:30", "kestrel",
                         "I'll send the signed rental agreement over this week.", out=True,
                         subject="Re: Rental agreement", answer={"items": [item(
                             "commitment", "owner", "send the signed rental agreement", "the camera package rental",
                             status="accepted")], "updates": []})
    i["lena_shoot"] = s.msg("lena-1", "2026-11-09T09:05", "lena",
                            "Pickup shoot is Saturday 10am at the pier, can you come?", answer={"items": [item(
                                what="join the pickup shoot", purpose="the pier pickups",
                                w=when("Saturday 10am", "2026-11-14", "10:00", "14:00"))], "updates": []})
    i["calltime"] = s.msg("lena-2", "2026-11-09T09:10", "lena", "Call time is 6:30 am Tue the 17th.", answer={
        "items": [item("event", "sender", "Call time", "the Cascade reshoot",
                       when("6:30 am Tue the 17th", "2026-11-17", "06:30", "18:00"))], "updates": []})
    i["lena_lut"] = s.msg("lena-3", "2026-11-09T11:00", "lena", "Can you share the LUT you used for the pickup grade?",
                          answer={"items": [item(what="share the LUT", purpose="matching the pickup grade")],
                                  "updates": []})
    s.msg("priya-2", "2026-11-09T16:30", "priya", "Bumping this, we need to lock crew by Friday.",
          subject="Re: Dec 8-10 shoot?")
    i["bill"] = s.msg("kestrel-2", "2026-11-09T17:00", "kestrel", "Invoice 4471: $1,240 due Mon Nov 16.",
                      subject="Invoice 4471", answer={"items": [item(
                          "event", "sender", "Kestrel invoice due · $1,240", "the camera package", when(
                              "Mon Nov 16", "2026-11-16", all_day=True), subtype="deadline")], "updates": []})
    i["birthday"] = s.msg("mom-1", "2026-11-10T09:00", "mom", "Don't forget Jamie's birthday Saturday!", answer={
        "items": [item("event", "sender", "Jamie’s birthday", "", when("Saturday", "2026-11-14", all_day=True),
                       subtype="birthday")], "updates": []})
    i["flight"] = s.msg("skyway-1", "2026-11-10T09:30", "skyway", "Your flight to Austin: Thu Nov 19, 9:05 am.",
                        subject="Your trip to Austin", answer={"items": [item(
                            "event", "sender", "Flight to Austin", "the Austin job",
                            when("Thu Nov 19, 9:05 am", "2026-11-19", "09:05", "13:10"), subtype="trip")],
                            "updates": []})
    i["sam_slider"] = s.msg("sam-1", "2026-11-10T14:40", "sam_sms", "Hey can I borrow your slider this weekend?",
                            answer={"items": [item(what="borrow your slider", purpose="Sam's weekend shoot")],
                                    "updates": []})
    i["marco"] = s.msg("marco-2", "2026-11-10T16:10", "marco",
                       "Are you free Thursday around 7 for a call about the Aster edit? I have notes from the client.",
                       subject="Aster edit notes", answer={"items": [item(
                           what="a call about the Aster edit", purpose="going over the client's notes on the Aster edit",
                           w=when("Thursday around 7", "2026-11-12", "19:00", "20:00", approx=True))], "updates": []})
    i["sam_move"] = s.msg("sam-2", "2026-11-10T18:00", "sam", "Any chance you could help me move Sat at 10?", answer={
        "items": [item(what="help moving", purpose="Sam's move to the new place",
                       w=when("Sat at 10", "2026-11-14", "10:00", "13:00"))], "updates": []})
    s.msg("dana-0", "2026-11-10T20:00", "dana", "How's the week going?")
    i["screening"] = s.msg("theo-0", "2026-11-10T21:00", "theo", "Screening is Fri Nov 20 at 8, you're on the list.",
                           answer={"items": [item("event", "sender", "Theo’s screening", "", when(
                               "Fri Nov 20 at 8", "2026-11-20", "20:00", "22:00"))], "updates": []})
    i["insure"] = s.msg("ins-1", "2026-11-10T22:00", "insure", "Your camera insurance renews Dec 1.",
                        subject="Renewal notice", answer={"items": [item(
                            "event", "sender", "Camera insurance renews", "", when("Dec 1", "2026-12-01", all_day=True),
                            subtype="deadline")], "updates": []})
    i["gallery"] = s.msg("gal-1", "2026-11-10T22:30", "gallery", "You're invited: opening night Dec 3, 7 pm.",
                         subject="Opening night", answer={"items": [item(
                             "event", "sender", "Gallery opening", "", when("Dec 3, 7 pm", "2026-12-03", "19:00",
                                                                            "21:00"))], "updates": []})
    for n, (mid, name, kind, what) in enumerate(CLEANUP):
        src = "slack" if kind == "chatter" else "gmail"
        PEOPLE["c-" + mid] = (f"news{n}@example.com" if src == "gmail" else f"U0C{n}", name, src, "c-" + mid)
        i["c-" + mid] = s.msg("c-" + mid, f"2026-11-10T23:{n * 4:02d}", "c-" + mid, what, subject=what,
                              answer={"items": [item("cleanup", "sender", what, subtype=kind)], "updates": []})
    i["theo"] = s.msg("theo-1", "2026-11-11T02:40", "theo", "Could you give me notes on the cut before Friday?",
                      answer={"items": [item(what="your notes on the cut", purpose="Theo's festival submission")],
                              "updates": []})
    i["north"] = s.msg("north-1", "2026-11-11T05:40", "north", "Please send a W-9 so we can pay your invoice.",
                       subject="W-9 needed", answer={"items": [item(what="a W-9 form", purpose="paying your invoice")],
                                                     "updates": []})
    i["dana"] = s.msg("dana-1", "2026-11-11T07:52", "dana", "Can you help me rig the lights tomorrow at 7?", answer={
        "items": [item(what="help rigging the lights", purpose="Dana's gallery set",
                       w=when("tomorrow at 7", "2026-11-12", "19:00", "21:00"))], "updates": []})
    s.msg("dana-2", "2026-11-11T07:53", "dana", "It'll take two hours tops. Pizza after!")
    if today is not None:
        now[0] = ts("2026-11-11T08:20")
        today.note({"items": [i["theo"]], "text": "He's flexible, end of week is fine.", "chips": []})
    if end < ts("2026-11-11T08:55"):
        now[0] = end
        return i
    s.msg("ana-2", "2026-11-11T08:55", "ana", "So sorry, my grade review landed tonight. Can't teach the class.",
          out=True, answer={"items": [], "updates": [upd(f"L{i['ana_yes']}", "declined")]})
    i["dana_yes"] = s.msg("dana-3", "2026-11-11T09:02", "dana", "Yes! I'll be there at 7.", out=True, answer={
        "items": [item("commitment", "owner", "help rig the lights", "Dana's gallery set",
                       when("tomorrow at 7", "2026-11-12", "19:00", "21:00"), "accepted")],
        "updates": [upd(f"L{i['dana']}", "done")]})
    if today is not None and end >= ts("2026-11-11T09:10"):
        now[0] = ts("2026-11-11T09:10")
        f = [r for r in today._followups()]  # noqa: SLF001
        if f:
            i["followup"] = f[0]["id"]
            today.remind({"followup": f[0]["id"], "at": "noon"})
    if end < ts("2026-11-11T12:00"):
        now[0] = end
        return i
    now[0] = ts("2026-11-11T12:00")
    if today is not None:
        today.tick()
    if end < ts("2026-11-11T12:14"):
        now[0] = end
        return i
    s.msg("marco-3", "2026-11-11T12:14", "marco", "Thursday's out for me now, could we do Friday at 7 instead?",
          out=True, subject="Re: Aster edit notes", answer={"items": [], "updates": [upd(
              f"L{i['marco']}", "moved", when("Friday at 7", "2026-11-13", "19:00", "20:00"))]})
    now[0] = end
    return i
