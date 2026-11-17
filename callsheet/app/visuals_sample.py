"""0.19.0: the visuals gallery's sample store (Setup → Advanced → Visuals). Invented names and numbers only, as in the
v4 design; "now" is fixed at Wed Nov 11 2026, 9:00 am in the sample's zone,
so the gallery always looks the same. Each block is the JSON a brief model would write, filled through the same
visuals.compose() as a real turn."""
import datetime as dt
import re
import zoneinfo

import visuals

TZ = zoneinfo.ZoneInfo("UTC")
TODAY = dt.date(2026, 11, 11)
NOW = dt.datetime(2026, 11, 11, 9, 0, tzinfo=TZ).timestamp()


def at(day, hhmm="00:00"):
    """Epoch of a day offset from today (or a date) at a local time."""
    d = TODAY + dt.timedelta(days=day) if isinstance(day, int) else day
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).timestamp()


def rec(ref, title="", who="", type="event", source="mail", start=None, end=None, all_day=False, created=None,
        purpose="", text="", subject="", sender="", on_calendar=False, subtype="", link="", status="open",
        direction="in", amount="", stage=""):
    if all_day and start is not None and end is None:
        end = start + 86400
    return {"ref": ref, "kind": ref.split(":")[0], "type": type, "subtype": subtype, "title": title, "who": who,
            "purpose": purpose, "source": source, "start": start, "end": end, "all_day": all_day,
            "created": created if created is not None else (start or NOW) - 86400, "status": status,
            "on_calendar": on_calendar or source == "cal", "link": link, "msg_ts": created,
            "sender": sender or who, "hay": " \n".join(x for x in (title, purpose, who, subject, text) if x),
            "text": text, "subject": subject, "direction": direction,
            "amount": amount, "currency": "$" if amount.startswith("$") else "", "stage": stage}   # 0.20.0 ledger facts


def records():
    R = []
    # Day timeline (today) and the clash
    R += [rec("cal:118", "Team stand-up", source="cal", start=at(0, "08:30"), end=at(0, "09:30")),
          rec("cal:121", "Supplier call, Brightline Office", source="cal", start=at(0, "09:45"), end=at(0, "10:15"), purpose="video"),
          rec("cal:130", "Budget review", source="cal", start=at(0, "18:00"), end=at(0, "19:30")),
          rec("msg:5498", "Ana’s class", "Ana", "commitment", "signal", at(0, "18:00"), at(0, "20:00"),
              text="Class is at 6 tonight, see you there!"),
          rec("msg:5521", "Cover the front desk", "Dana", "ask", "signal", at(1, "19:00"), at(1, "21:00"),
              purpose="front desk cover", created=at(-1, "20:10")),
          rec("msg:88204", "Quarterly report review", "Marco", "ask", "mail", at(1, "19:00"), at(1, "20:00"),
              purpose="report review", created=at(0, "07:30"))]
    # Needs you
    R += [rec("msg:87710", "Quote for Dec 8 to 10", "Priya Shah", "ask", "mail", created=at(-5, "10:12"),
              purpose="Quote for Dec 8 to 10", sender="Priya Shah", subject="Dec 8 to 10",
              text="Hi! Could you send your quote for Dec 8 to 10? The office move: three days, mostly packing. "
                   "Thanks so much, Priya"),
          rec("msg:3302", "Inventory count on Saturday", "Lena Park", "ask", "slack", created=at(-2, "08:00"),
              purpose="Inventory count on Saturday"),
          rec("msg:87951", "Confirm the printer return", "Kestrel Rentals", "ask", "mail", created=at(-2, "07:00"),
              purpose="Confirm the printer return"),
          rec("msg:1180", "Help moving on Saturday", "Sam", "ask", "text", created=at(-1, "15:00"),
              purpose="Help moving on Saturday")]
    # Coming up, money, trip
    R += [rec("msg:1175", "Jamie’s birthday", "Jamie", "event", "text", at(3), all_day=True),
          rec("item:51", "Kestrel Rentals bill due", "Kestrel Rentals", "event", "mail", at(5), all_day=True,
              subtype="bill", text="Invoice 4471: $1,240 due Mon Nov 16.", amount="$1,240", stage="due"),
          rec("msg:3290", "Warehouse shift · Dock 2", "Lena Park", "event", "slack", at(6, "07:00"),
              at(6, "13:00")),
          rec("item:40", "Trip to Austin", "", "event", "mail", at(8, "09:05"), at(11, "21:40"), subtype="trip"),
          rec("item:41", "Flight to Austin", "", "event", "mail", at(8, "09:05"), at(8, "12:20"), subtype="flight",
              text="Your flight Chicago (ORD) to Austin (AUS) departs Thu Nov 19 at 9:05 am."),
          rec("item:42", "Flight home from Austin", "", "event", "mail", at(11, "16:15"), at(11, "21:40"),
              subtype="flight", text="Return: Austin (AUS) to Chicago (ORD), Sun Nov 22 at 4:15 pm."),
          rec("cal:150", "Contract review, Northlight", source="cal", start=at(12, "10:00"), end=at(12, "11:00")),
          rec("item:44", "Home insurance due", "Home insurance", "event", "mail", at(20), all_day=True,
              subtype="bill", text="Your premium of $86 is due Dec 1.", amount="$86", stage="due"),
          rec("msg:88300", "Gallery opening, Mira’s show", "Mira", "event", "mail", at(22, "19:00"), at(22, "21:00"))]
    # Clean up
    for i, k in enumerate(["newsletter"] * 5 + ["receipt"] * 3 + ["slack"] * 4):
        R.append(rec(f"item:{300 + i}", f"{k} {i}", type="cleanup", subtype=k))
    # Bills ladder
    for n, (who, purpose, day, text) in enumerate((
            ("Northbank Visa", "", 3, "Statement for card ending 4412: minimum payment $35, balance $1,284.50."),
            ("Pinecrest Power", "Electric", 7, "Your bill of $64.20 is due Nov 18."),
            ("Keystone CU Mastercard", "", 10, "Card ending 0937: minimum due $25, statement balance $412.08."),
            ("Fibernet", "Internet", 14, "Amount due: $55.00 on Nov 25."),
            ("Juniper store card", "", 22, "Account ending 2210. Minimum payment $29. Balance $318.77."))):
        one = re.findall(r"\$[0-9][0-9,.]*[0-9]", text)    # the ledger keeps an amount only from a one-amount sentence
        R.append(rec(f"item:{61 + n}", f"{who} payment due", who, "event", "mail", at(day), all_day=True,
                     purpose=purpose, subtype="bill", text=text, amount=one[0] if len(one) == 1 else ""))
    # Money in and out
    R += [rec("msg:90310", "", "Sam", "message", "mail", created=at(-2, "18:00"), sender="Venmo",
              text="Sam paid you $60.00. Moving help, thanks!"),
          rec("msg:90322", "", "Jamie", "message", "mail", created=at(-3, "12:00"), sender="Venmo",
              text="Jamie paid you $42.50. Concert tickets"),
          rec("msg:90290", "", "Pinecrest Power", "message", "mail", created=at(-5, "06:00"),
              text="Autopay: we charged $64.20 to your card."),
          rec("msg:90301", "", "Cloud storage", "message", "mail", created=at(-2, "03:00"),
              text="Your monthly plan renewed: $9.99.")]
    # Package, ticket, tasks, health
    R += [rec("msg:90340", "", "Gearbox Supply", "message", "mail", created=at(-3, "11:00"),
              text="Thanks for your order! Desk lamp, qty 1.", stage="ordered"),
          rec("msg:90355", "", "Gearbox Supply", "message", "mail", created=at(-1, "16:00"),
              text="Your Desk lamp has shipped. Tracking number 1Z999AA10123457781.", stage="shipped"),
          rec("item:70", "Desk lamp arrives", "Gearbox Supply", "event", "mail", at(2, "08:00"), at(2, "20:00"),
              subtype="delivery"),
          rec("item:80", "Evening workshop: Home budgeting", "Elmwood Community Center", "event", "mail",
              at(22, "18:00"), at(22, "20:00"), text="Join us at Elmwood Community Center for Home budgeting."),
          rec("item:81", "Register for Home budgeting", "Elmwood Community Center", "event", "mail", at(7),
              all_day=True),
          rec("item:90", "Replace your toll tag", "Metro Toll", "event", "mail", at(5), all_day=True,
              purpose="new tag mailed Nov 9"),
          rec("item:91", "Set up online access", "Pinecrest Water", "event", "mail", at(9), all_day=True,
              purpose="new account"),
          rec("item:92", "Log in to keep your account", "Snapbox Photos", "event", "mail", at(21), all_day=True,
              purpose="closes if unused"),
          rec("msg:90450", "", "Westside Health portal", "message", "mail", created=at(-1, "16:12"),
              subtype="health", subject="Your lab results are ready",
              text="Your lab results are ready. Sign in to read them.")]
    # Stat, local, price, leads, series, invite
    R += [rec("msg:90461", "", "StepCount", "message", "mail", created=at(-2, "07:00"),
              text="Your week, Nov 2 to 8: 7,840 steps a day, up 6% on the week before. Goal 8,000. "
                   "Mon 6,200 · Tue 8,100 · Wed 9,400 · Thu 5,300 · Fri 7,700 · Sat 11,200 · Sun 6,980"),
          rec("msg:90472", "", "Neighborhood Alerts", "message", "mail", created=at(0, "07:10"),
              text="ADVISORY: Road works on Main Street. North of you · expect delays · until 6:00 pm. "
                   "About 4 mi from your home."),
          rec("msg:90480", "", "Neighborhood Alerts", "message", "mail", created=at(-1, "09:00"),
              text="INFO: Riverside Park closed. Path repairs · through Sun Nov 15. 1.2 mi from your home."),
          rec("msg:90490", "", "eBay", "message", "mail", created=at(0, "06:00"),
              text="5 new matches for standing desk, used (your range $400 to $550): $380 fair; "
                   "$465 good condition, ends Sat; $520 like new, with case; $640; $725."),
          rec("msg:90502", "", "Job alerts", "message", "mail", created=at(-1, "08:00"),
              text="14 new remote office manager roles this week. Office manager, design studio: Lumen Works, "
                   "contract, 8 weeks, $450/day. Assistant office manager: Northpoint Media, 6 weeks, $32/h. "
                   "Front desk coordinator: Fieldnote Studio, part-time, $40/h."),
          rec("msg:90515", "", "Eastside Community College", "message", "mail", created=at(-1, "10:00"),
              text="You're enrolled in Beginner Spanish: Mon and Wed, 6:30 to 8:00 pm. Sessions: Nov 30, Dec 2, "
                   "Dec 7, Dec 9, Dec 14, Dec 16."),
          rec("msg:90523", "", "Kai Moreno", "message", "mail", created=at(-2, "10:00"),
              text="Kai Moreno invited you as an admin on Hillcrest Mesh, your building’s Wi-Fi app. "
                   "This invite expires in 6 days.")]
    # Subscriptions
    R += [rec("item:100", "Office suite renews", "Office suite", "event", "mail", at(17), all_day=True,
              text="Your plan renews at $54.99 a month."),
          rec("item:101", "Cloud storage renews", "Cloud storage", "event", "mail", at(28), all_day=True,
              text="$9.99 a month, paid Nov 9."),
          rec("item:102", "Password manager trial ends", "Password manager", "event", "mail", at(9), all_day=True,
              text="Free trial, then $15 a month."),
          rec("item:103", "Music library renews", "Music library", "event", "mail", at(62), all_day=True,
              text="Renews at $119 a year.")]
    # Composite
    R += [rec("msg:90600", "", "Lena Park", "message", "slack", created=at(0, "07:45"),
              text="Move prep for Tuesday: 4 of 6 boxes packed. Labels printed, two spare keys cut. Monitors packed, "
                   "printer at Kestrel. Rental total $1,240."),
          rec("item:120", "Van pickup", "Lena Park", "event", "slack", at(6, "06:00"), None)]
    return R


CONFLICTS = {2: {"slot_start": at(1, "19:00"), "slot_end": at(1, "20:00"), "state": "open",
                 "sides": ["msg:5521", "msg:88204"]}}


def facts():
    return visuals.DictFacts(TZ, NOW, records(), CONFLICTS)


BLOCKS = [
    {"type": "day", "from": "7 am", "to": "10 pm", "items": ["cal:118", "cal:121", "cal:130", "msg:5498"]},
    {"type": "clash", "ref": "clash:2"},
    {"type": "asks", "order": "oldest", "items": ["msg:87710", "msg:3302", "msg:87951", "msg:1180"]},
    {"type": "upcoming", "days": 30, "items": ["msg:1175", "item:51", "msg:3290", "item:40", "cal:150", "item:44",
                                               "msg:88300"]},
    {"type": "money", "bills": [{"ref": "item:51", "amount": "$1,240"}, {"ref": "item:44", "amount": "$86"}]},
    {"type": "trip", "to": "Austin", "legs": ["item:41", "item:42"]},
    {"type": "person", "ref": "msg:87710", "mark": "your quote for Dec 8 to 10"},
    {"type": "cleanup"},
    {"type": "bills", "days": 30, "items": [
        {"ref": "item:61", "amount": "$35", "min": True, "balance": "$1,284.50", "card": "4412"},
        {"ref": "item:62", "amount": "$64.20"},
        {"ref": "item:63", "amount": "$25", "min": True, "balance": "$412.08", "card": "0937"},
        {"ref": "item:64", "amount": "$55.00"},
        {"ref": "item:65", "amount": "$29", "min": True, "balance": "$318.77", "card": "2210"}]},
    {"type": "ledger", "range": "this week", "items": [
        {"ref": "msg:90310", "dir": "in", "amount": "$60.00", "memo": "Moving help, thanks!"},
        {"ref": "msg:90322", "dir": "in", "amount": "$42.50", "memo": "Concert tickets"},
        {"ref": "msg:90290", "dir": "out", "amount": "$64.20", "memo": "Autopay"},
        {"ref": "msg:90301", "dir": "out", "amount": "$9.99"}]},
    {"type": "package", "name": "Desk lamp", "steps": [{"ref": "msg:90340", "stage": "ordered"},
                                                            {"ref": "msg:90355", "stage": "shipped"}],
     "arrives": "item:70", "tracking": "1Z999AA10123457781"},
    {"type": "ticket", "ref": "item:80", "place": "Elmwood Community Center", "register_by": "item:81"},
    {"type": "tasks", "order": "deadline", "items": ["item:90", "item:91", "item:92"]},
    {"type": "health", "ref": "msg:90450"},
    {"type": "stat", "ref": "msg:90461", "metric": "steps",
     "values": ["6,200", "8,100", "9,400", "5,300", "7,700", "11,200", "6,980"], "goal": "8,000", "average": "7,840",
     "range": "Nov 2 to 8", "change": "up 6% on the week before"},
    {"type": "local", "items": [
        {"ref": "msg:90472", "level": "advisory", "title": "Road works on Main Street",
         "detail": "North of you · expect delays · until 6:00 pm", "distance": "4 mi"},
        {"ref": "msg:90480", "level": "info", "title": "Riverside Park closed", "detail": "Path repairs · through Sun Nov 15",
         "distance": "1.2 mi"}]},
    {"type": "price", "ref": "msg:90490", "name": "standing desk, used", "range": ["$400", "$550"], "new": "5",
     "matches": [{"price": "$380"}, {"price": "$465", "note": "good condition, ends Sat"},
                 {"price": "$520", "note": "like new, with case"}, {"price": "$640"}, {"price": "$725"}]},
    {"type": "leads", "ref": "msg:90502", "count": "14", "what": "new remote office manager roles this week",
     "top": [{"title": "Office manager, design studio", "org": "Lumen Works", "rate": "$450/day"},
             {"title": "Assistant office manager", "org": "Northpoint Media", "rate": "$32/h"},
             {"title": "Front desk coordinator", "org": "Fieldnote Studio", "rate": "$40/h"}]},
    {"type": "series", "ref": "msg:90515", "name": "Beginner Spanish", "time": "6:30 to 8:00 pm",
     "dates": ["Nov 30", "Dec 2", "Dec 7", "Dec 9", "Dec 14", "Dec 16"]},
    {"type": "invite", "ref": "msg:90523", "what": "as an admin", "app": "Hillcrest Mesh",
     "expires": "in 6 days"},
    {"type": "subs", "days": 30, "items": [{"ref": "item:100", "price": "$54.99 a month"},
                                           {"ref": "item:101", "price": "$9.99 a month"},
                                           {"ref": "item:102", "price": "Free trial, then $15 a month", "trial": True},
                                           {"ref": "item:103", "price": "$119 a year"}]},
    {"type": "composite", "name": "move_prep", "title": "Move prep", "ref": "msg:90600", "body": {
        "t": "column", "c": [
            {"t": "row", "c": [{"t": "icon", "name": "box"}, {"t": "text", "text": "Packing list from Lena",
                                                              "style": "label"}]},
            {"t": "progress", "ref": "msg:90600", "value": "4", "of": "6", "label": "boxes packed"},
            {"t": "time_axis", "from": 5, "to": 14, "c": [{"t": "range_bar", "ref": "msg:3290", "label": "Warehouse shift"},
                                                          {"t": "dot", "ref": "item:120", "label": "Van pickup"}]},
            {"t": "list", "items": ["Labels printed", "two spare keys cut"]},
            {"t": "table", "head": ["Item", "Where"], "rows": [["Monitors", "Packed"], ["Printer", "At Kestrel"]]},
            {"t": "number", "ref": "msg:90600", "value": "$1,240", "unit": "rental total"}]}},
]


def gallery():
    """GET api/visuals/gallery: every type, its JSON as the model writes it, and the filled block."""
    f = facts()
    out = []
    for b in BLOCKS:
        res = visuals.compose(b, f)
        name, desc = visuals.SCHEMAS[b["type"]]["title"], visuals.SCHEMAS[b["type"]]["description"]
        out.append({"type": b["type"], "name": name, "desc": desc, "json": b,
                    "block": res["blocks"][0] if res["blocks"] else None, "problems": res["problems"]})
    demo = visuals.compose([{"type": "weather", "city": "Austin"}, {"type": "asks", "items": ["msg:1"], "x": 1}], f)
    return {"version": visuals.VERSION, "as_of": "Wed Nov 11, 9:00 am", "blocks": out,
            "dropped_demo": {"json": [{"type": "weather", "city": "Austin"},
                                      {"type": "asks", "items": ["msg:1"], "x": 1}], "dropped": demo["dropped"]}}
