"""0.21.0 Agenda (docs/agenda.md; mockup agenda-projects-v1, approved 2026-11-13).

Day, Week and Month from the calendar store (msgstore events) plus the ledger: timed items and deadlines found only in
messages, the owner's own promises ("You said"), asks of the owner, invites and the clashes conflicts.py found. Every label is
made here (12-hour times, "in 3 days", "Free 2:00 to 5:00 pm · 3 h"), so the page only draws text. Rules only: no model,
nothing is sent. "Add to calendar" is calwrite.py's flow; Yes / No / Maybe on an invite is rsvp.py's.

The owner's answers (2026-11-13): a promise with a date and no time shows "By 5 pm"; the week keeps past days (dimmed);
month cells are dots only; when an item has no end time, Add asks for it.
"""
import datetime as dt
import re

import ledger as L
import today as T

MODES = ("day", "week", "month")
BY_HOUR = 17                 # a promise with a date and no time: "By 5 pm" (the end of the workday)
DAY_FROM, DAY_TO = 8, 20     # free stretches are told between 8 am and 8 pm, then "Free from …"
LATE = 23                    # no "Free from …" after 11 pm
FREE_MIN_S = 3600
SAID_DAYS = 14
STRIP_DAYS = 30
LEGEND = [["cal", "On your calendar"], ["msg", "Only in a message"], ["yes", "You said"], ["ask", "Asks you"],
          ["clash", "Clash"], ["free", "Free"]]
ITEM_TYPES = ("ask", "commitment", "event")


class Invalid(ValueError):
    pass


def _arg(body, k):
    v = body.get(k) if isinstance(body, dict) else None
    return v[-1] if isinstance(v, list) and v else v


def _date(v):
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise Invalid("Pick a date (YYYY-MM-DD).")
    try:
        return dt.date.fromisoformat(v)
    except ValueError:
        raise Invalid("Pick a date (YYYY-MM-DD).") from None


def t12s(d):
    """'7 pm', '7:30 pm' (the week's short words)."""
    return f"{d.hour % 12 or 12}{f':{d.minute:02d}' if d.minute else ''} {'am' if d.hour < 12 else 'pm'}"


def span_words(a, b, short=False):
    """'2:00 to 5:00 pm', '10:15 am to 1:00 pm'; short: '12 to 7 pm'."""
    f = t12s if short else T.t12
    sa, sb = f(a), f(b)
    if sa[-2:] == sb[-2:]:
        sa = sa[:-3]
    return f"{sa} to {sb}"


def _cap(s):
    s = " ".join(str(s or "").split())
    return s[:1].upper() + s[1:]


class Agenda:
    def __init__(self, ms, lv, today, clock, cal=None, rsvp=None):
        self.ms, self.lv, self.today, self.clock, self.cal, self.rsvp = ms, lv, today, clock, cal, rsvp

    def tz(self):
        return self.lv.tz()

    # --- the things in a range ---------------------------------------------------------------------------------------
    def _events(self, first, last, now, tz):
        s0, _ = L.day_bounds(first, tz)
        _, s1 = L.day_bounds(last, tz)
        out, seen = [], set()
        res = self.today.area_res()   # 0.22.0: each event's area (its calendar's connection then)
        for e in self.ms.events(since=s0, until=s1):
            if e["start_ts"] is None and not e["all_day"]:
                continue
            key = (e["ical_uid"] or e["event_id"], e["start"])
            if key in seen:                       # the same event on two of the owner's calendars
                continue
            seen.add(key)
            st = self.rsvp.state(e) if self.rsvp is not None else None
            if st and st["answer"] == "no":
                continue                          # the owner said no: not on the owner's day
            base = {"kind": "item", "k": "cal", "event": e["id"], "item": None, "title": _cap(e["title"]) or "(No title)",
                    "icon": "cal", "from": "", "tag": "", "on": False, "link": T.safe_cal_link(e["link"]),
                    "rsvp": st if st and st["answer"] in ("", "maybe") else None, "likely": False,
                    "area": T.Today.area_of(res, {"source": "calendar", "account": e["account"], "ts": e["start_ts"]},
                                            e.get("calendar"))}
            if base["rsvp"]:
                base["tag"] = "Invite"
            if e["all_day"]:
                try:
                    d0 = dt.date.fromisoformat(str(e["start"])[:10])
                    d1 = dt.date.fromisoformat(str(e["end"])[:10]) - dt.timedelta(days=1) if e["end"] else d0
                except ValueError:
                    continue
                d = max(d0, first)
                while d <= min(max(d0, d1), last):
                    past = d < T.local(now, tz).date()
                    out.append(dict(base, date=d, s=None, e=None, sort=L.day_bounds(d, tz)[0] - 1, time="All day",
                                    meta="", k="past" if past else "cal"))
                    d += dt.timedelta(days=1)
                continue
            s, en = e["start_ts"], max(e["end_ts"] or e["start_ts"] + 3600, e["start_ts"])
            d0, d1 = T.local(s, tz), T.local(en, tz)
            on = s <= now < en
            if on:
                meta = f"On now · until {T.t12(d1)}"
            elif en - s <= 3 * 3600:
                meta = T.Today._dur(en - s)
            else:
                meta = f"Until {T.t12(d1)}"
            if e.get("location"):
                meta += " · " + " ".join(str(e["location"]).split())[:40]
            if base["rsvp"]:
                meta = "Invite · " + ("you said Maybe" if base["rsvp"]["answer"] == "maybe" else "not answered") \
                    + " · " + meta
            if first <= d0.date() <= last:
                out.append(dict(base, date=d0.date(), s=s, e=en, sort=s, time=T.t12(d0), meta=meta, on=on,
                                k="past" if en <= now else "cal"))
        return out

    def _items(self, first, last, now, tz):
        s0, _ = L.day_bounds(first, tz)
        _, s1 = L.day_bounds(last, tz)
        rows = self.lv.items(
            "status='open' AND merged_into IS NULL AND cal_ref IS NULL AND type IN ('ask','commitment','event') AND "
            "((all_day=0 AND start_ts IS NOT NULL AND start_ts < ? AND coalesce(end_ts, start_ts) >= ?) OR "
            "(all_day=1 AND start_date IS NOT NULL AND start_date <= ? AND coalesce(end_date, start_date) >= ?))",
            (s1, s0, last.isoformat(), first.isoformat()))
        rows = [r for r in rows if r.get("subtype") != "security"]
        srcs = self.today._srcs([r["id"] for r in rows])
        res = self.today.area_res()   # 0.22.0
        today = T.local(now, tz).date()
        out = []
        for it in rows:
            ss = srcs.get(it["id"], [])
            o = self.today._origin(ss) or {}
            src = o.get("source") or "gmail"
            mine = it["type"] == "commitment" and it.get("who") == L.OWNER
            ask = it["type"] == "ask" and it.get("who") != L.OWNER
            who = self.today._person(it, ss)
            meta = []
            if ask and who:
                meta.append(f"{T.first(who)} asked {T.ago(o.get('ts'), now, tz)}".strip())
            if mine and it.get("phrase"):
                meta.append(f"“{it['phrase']}”")
            elif it.get("purpose"):
                meta.append(it["purpose"])
            word = T.SOURCES.get(src, ("", "Message"))[1].lower()
            frm = (f"You said in {'email' if src == 'gmail' else word}, {T.ago(o.get('ts'), now, tz)}"
                   if mine and o.get("ts") else T.FROM.get(src, "From a message"))
            base = {"kind": "item", "k": "yes" if mine else "msg", "item": it["id"], "event": None,
                    "title": _cap(it.get("what")) or "(No title)", "meta": " · ".join(m for m in meta if m),
                    "icon": T.SOURCES.get(src, ("mail", ""))[0], "from": frm,
                    "tag": "You said" if mine else "Asks you" if ask else "", "on": False, "link": None, "rsvp": None,
                    "likely": it.get("confidence") != "sure", "subtype": it.get("subtype") or "",
                    "area": T.Today.area_of(res, o)}
            if it.get("all_day") and it.get("start_date"):
                try:
                    d0 = dt.date.fromisoformat(it["start_date"])
                    d1 = dt.date.fromisoformat(it.get("end_date") or it["start_date"])
                except ValueError:
                    continue
                d = max(d0, first)
                while d <= min(max(d0, d1), last):
                    day0 = L.day_bounds(d, tz)[0]
                    if mine or ask:      # a deadline with no time: "By 5 pm" (decided 2026-11-13)
                        tm, sort = ("By 5 pm" if mine else "Due"), day0 + BY_HOUR * 3600
                    else:
                        tm, sort = "All day", day0 - 1
                    out.append(dict(base, date=d, s=None, e=None, sort=sort, time=tm,
                                    k="past" if d < today else base["k"]))
                    d += dt.timedelta(days=1)
                continue
            s = it["start_ts"]
            en = max(it.get("end_ts") or s + L.EVENT_MIN * 60, s)
            d0 = T.local(s, tz)
            if first <= d0.date() <= last:
                out.append(dict(base, date=d0.date(), s=s, e=en, sort=s, time=T.t12(d0), on=s <= now < en,
                                k="past" if en <= now else base["k"]))
        return out

    def _clashes(self, first, last, now, tz):
        s0, _ = L.day_bounds(first, tz)
        _, s1 = L.day_bounds(last, tz)
        rows = [c for c in self.today._read(
            "SELECT * FROM ledger_conflicts WHERE state='open' AND verdict IN ('clash','maybe') AND slot_start IS NOT NULL AND "
            "slot_start < ? AND "
            "coalesce(slot_end, slot_start) >= ? ORDER BY slot_start", (s1, s0))
            if (c["slot_end"] or c["slot_start"]) > now]
        if not rows:
            return []
        sides = self.today._clash_sides(rows, now, tz)
        out = []
        for c in rows:
            ss = sides[c["id"]]
            cv = self.today._clash(c, ss, None, [], now, tz, True)
            group = cv.get("group")
            at = {e["id"]: e["when"] for e in group["entries"]} if group else {}   # 0.22.x: each entry's day and time
            boxes = []
            for s in ss:
                if s["kind"] == "event":
                    boxes.append({"k": "cal", "title": _cap(s["title"]) or "Calendar", "meta": at.get(s["id"]) or
                                  s["until"], "tag": "", "icon": "cal", "from": ""})
                else:
                    mine = bool(s.get("mine"))
                    about = self.today._about(s)
                    boxes.append({"k": "yes" if mine else "msg", "title": _cap(s.get("what")) or about,
                                  "meta": (f"You told {s['first']} yes" if mine and s.get("first") else
                                           f"{s['first']}: {s.get('for')}" if s.get("for") and s.get("first") else ""),
                                  "tag": "You said" if mine else "Asks you" if s.get("type") == "ask" else "",
                                  "icon": s["icon"], "from": "From " + s["chip"].lower().replace("text", "text")
                                  if s["chip"] else ""})
            d = T.local(c["slot_start"], tz)
            when = cv["when"][:1].lower() + cv["when"][1:] if cv["when"] else ""
            out.append({"kind": "clash", "id": c["id"], "level": "al" if cv["level"] == "red" else "wn",
                        "when": when, "short": t12s(d), "until": cv["until"], "date": d.date(), "s": c["slot_start"],
                        "e": c["slot_end"] or c["slot_start"] + 3600, "sort": c["slot_start"], "time": T.t12(d),
                        "sides": boxes, "rows": cv["rows"], "reply": cv["reply"], "items": cv["items"],
                        "events": [s["id"] for s in ss if s["kind"] == "event"], "away": cv.get("away") or "",
                        # 0.22.x: the owner's trip's grouped clash: the trip and every entry during it (today._clash_sides)
                        "group": group["plain"] if group else "",
                        "verdict": c["verdict"] if "verdict" in c.keys() else "clash"})   # 0.23.0: Maybe is quiet
        return out

    def _gather(self, first, last, now, tz):
        ents = self._events(first, last, now, tz) + self._items(first, last, now, tz)
        return ents, self._clashes(first, last, now, tz)

    # --- one day's rows ------------------------------------------------------------------------------------------------
    def _free(self, d, busy, now, tz, short):
        """The free stretches of day d between DAY_FROM and DAY_TO (short: the longest one only, in short words)."""
        day0 = L.day_bounds(d, tz)[0]
        lo, hi = max(day0 + DAY_FROM * 3600, now), day0 + DAY_TO * 3600
        if lo >= day0 + LATE * 3600:
            return []
        gaps, cur = [], lo
        for a, b in sorted(busy):
            if a - cur >= FREE_MIN_S and cur < hi:
                gaps.append((cur, min(a, hi), False))
            cur = max(cur, b)
        if cur < day0 + LATE * 3600:
            if cur < hi and hi - cur >= FREE_MIN_S and busy and max(b for _, b in busy) <= hi:
                gaps.append((cur, None, True))
            elif not busy or cur >= hi or max(b for _, b in busy) > hi:
                gaps.append((cur, None, True))
        rows = []
        for a, b, tail in gaps:
            da = T.local(a, tz)
            if short:
                if tail:
                    txt = "Free all day" if a <= day0 + DAY_FROM * 3600 and not busy else \
                        "Free all afternoon" if da.hour == 12 and da.minute == 0 else f"Free after {t12s(da)}"
                else:
                    txt = "Free " + span_words(da, T.local(b, tz), short=True)
                rows.append({"kind": "free", "sort": a + 0.5, "len": (b or day0 + LATE * 3600) - a, "text": [[txt, False]]})
            elif tail:
                rows.append({"kind": "free", "sort": a + 0.5, "len": day0 + LATE * 3600 - a,
                             "text": T.seg(("Free ", False), ("from " + T.t12(da), True))})
            else:
                rows.append({"kind": "free", "sort": a + 0.5, "len": b - a,
                             "text": T.seg(("Free ", False), (span_words(da, T.local(b, tz)), True),
                                           (" · " + T.Today._dur(b - a), False))})
        if short and rows:
            best = max(rows, key=lambda r: r["len"])
            return [best]
        return rows

    def _day_rows(self, d, ents, clashes, now, tz, short=False):
        cl = [c for c in clashes if c["date"] == d]
        hide_items = {i for c in cl for i in c["items"]}
        hide_events = {i for c in cl for i in c["events"]}
        rows = [e for e in ents if e["date"] == d and e["item"] not in hide_items and e["event"] not in hide_events]
        busy = [(e["s"], e["e"]) for e in rows if e["s"] is not None and e["k"] != "past"] + \
            [(c["s"], c["e"]) for c in cl]
        today = T.local(now, tz).date()
        free = self._free(d, busy, now, tz, short) if d >= today else []
        out = sorted(rows + cl + free, key=lambda r: (r["sort"], r.get("title") or ""))
        return [self._public(r) for r in out]

    @staticmethod
    def _public(r):
        out = {k: v for k, v in r.items() if k not in ("s", "e", "sort", "date", "len")}
        return out

    def _adds(self, rows):
        """Add to calendar per item row (calwrite.states): add {on, why, title, date, time, end, ask_end, minutes} or
        added {link, calendar}."""
        ids = {r["item"] for r in rows if r.get("item") is not None}
        if self.cal is None or not ids:
            return
        try:
            st = self.cal.states(sorted(ids))
        except Exception:  # noqa: BLE001 (the Agenda always shows; Add stays off)
            st = {}
        for r in rows:
            s = st.get(r.get("item")) if r.get("item") is not None else None
            if s and "added" in s:
                r["added"] = s["added"]
            elif s and "add" in s:
                r["add"] = s["add"]

    # --- the side cards -----------------------------------------------------------------------------------------------
    def _said(self, now, tz, clash_items):
        today = T.local(now, tz).date()
        end = today + dt.timedelta(days=SAID_DAYS)
        s0 = L.day_bounds(today, tz)[0]
        s1 = L.day_bounds(end, tz)[1]
        rows = self.lv.items("status='open' AND merged_into IS NULL AND type='commitment' AND who=? AND "
                             "((all_day=0 AND start_ts IS NOT NULL AND coalesce(end_ts, start_ts) >= ? AND start_ts < ?) "
                             "OR (all_day=1 AND start_date >= ? AND start_date <= ?))",
                             (L.OWNER, max(s0, now - 3600), s1, today.isoformat(), end.isoformat()))
        srcs = self.today._srcs([r["id"] for r in rows])
        out = []
        for it in rows:
            o = self.today._origin(srcs.get(it["id"], [])) or {}
            src = o.get("source") or "gmail"
            if it.get("all_day"):
                d = dt.date.fromisoformat(it["start_date"])
                rel, date, sort = T.rel_day(d, today), f"{d:%a} {d:%b} {d.day}", L.day_bounds(d, tz)[0] + BY_HOUR * 3600
            else:
                d = T.local(it["start_ts"], tz)
                sec = it["start_ts"] - now
                rel = T.until(sec) if d.date() == today else T.rel_day(d.date(), today)
                date, sort = T.slot_word(it["start_ts"], now, tz), it["start_ts"]
            out.append({"item": it["id"], "rel": rel, "date": date, "what": _cap(it.get("what")), "sort": sort,
                        "icon": T.SOURCES.get(src, ("mail", ""))[0],
                        "from": f"{T.SOURCES.get(src, ('', 'Message'))[1]}, {T.ago(o.get('ts'), now, tz)}"
                        if o.get("ts") else T.SOURCES.get(src, ("", "Message"))[1],
                        "clash": it["id"] in clash_items, "cal": it.get("cal_ref") is not None})
        out.sort(key=lambda r: r.pop("sort"))
        return out

    def _invites(self, now, tz):
        rows = self.lv.items("status='open' AND merged_into IS NULL AND subtype='invite'")
        srcs = self.today._srcs([r["id"] for r in rows])
        res = self.today.area_res()   # 0.22.0
        today = T.local(now, tz).date()
        out = []
        for it in rows:
            sp = L.span(it, tz)
            if sp is not None and sp[1] <= now:
                continue
            ss = srcs.get(it["id"], [])
            cal = it.get("cal_ref") is not None
            o = self.today._origin(ss) or {}
            src = "calendar" if cal else (o.get("source") or "gmail")
            who = it.get("who_name") or ""
            when = L.when_label(it, tz) if sp is not None else "the time is in the message"
            if cal:
                sub = f"{when} · invite{' from ' + who if who else ''}, not answered"
            else:
                sub = f"{when} · only in {'email' if src == 'gmail' else T.SOURCES.get(src, ('', 'a message'))[1]}, " \
                      "not on your calendar"
            rel = T.rel_day(T.local(sp[0], tz).date(), today) if sp is not None else ""
            out.append({"item": it["id"], "title": _cap(it.get("what")) or "An invite", "rel": rel, "sub": sub,
                        "icon": T.SOURCES.get(src, ("mail", ""))[0], "calendar": cal,
                        "rsvp": self.rsvp.state_of_item(it) if cal and self.rsvp is not None else None,
                        "link": self.today._invite_link(it, ss), "sort": sp[0] if sp else 9e12})
        out.sort(key=lambda r: r.pop("sort"))
        return out

    # --- the views ----------------------------------------------------------------------------------------------------
    def view(self, body=None):
        """GET api/agenda?view=day|week|month&date=YYYY-MM-DD[&sel=YYYY-MM-DD]."""
        mode = _arg(body, "view") or "day"
        if mode not in MODES:
            raise Invalid("Pick Day, Week or Month.")
        now = self.clock()
        tz = self.tz()
        n = T.local(now, tz)
        today = n.date()
        d = _date(_arg(body, "date")) or today
        out = {"view": mode, "date": d.isoformat(), "today": today.isoformat(), "legend": LEGEND,
               "can_add": bool(self.cal.can_add()) if self.cal is not None else False,
               "cal_targets": self.cal.targets() if self.cal is not None else [], "day": None, "week": None,
               "month": None, "said": [], "invites": [], "only_msgs": []}
        head = {"time": T.t12(n), "fresh": self.today._fresh()}
        if mode == "day":
            ents, cl = self._gather(d, d, now, tz)
            rows = self._day_rows(d, ents, cl, now, tz)
            self._adds(rows)
            rel = {0: "today", 1: "tomorrow", -1: "yesterday"}.get((d - today).days, "")
            out["day"] = {"date": d.isoformat(), "label": f"{d:%a} {d:%b} {d.day}" + (f" · {rel}" if rel else ""),
                          "time": T.t12(n) if d == today else "", "rows": rows,
                          "empty": not any(r["kind"] != "free" for r in rows)}
            out["day"]["lanes"] = self.today.day_lanes(d, now, tz)   # 0.23.0 the same drawing as Today's Day strip
            head.update(title=f"{d:%A}, {d:%B} {d.day}", short=f"{d:%a}, {d:%b} {d.day}",
                        label="Today" if d == today else f"{d:%b} {d.day}",
                        prev=(d - dt.timedelta(days=1)).isoformat(), next=(d + dt.timedelta(days=1)).isoformat(),
                        clashes=sum(1 for c in cl))
        elif mode == "week":
            mon = d - dt.timedelta(days=d.weekday())
            sun = mon + dt.timedelta(days=6)
            ents, cl = self._gather(mon, sun, now, tz)
            days, allrows = [], []
            for i in range(7):
                x = mon + dt.timedelta(days=i)
                rows = self._day_rows(x, ents, cl, now, tz, short=True)
                allrows += rows
                days.append({"date": x.isoformat(), "dow": f"{x:%a}", "n": x.day, "label": f"{x:%a} {x.day}",
                             "past": x < today, "today": x == today,
                             "rel": T.rel_day(x, today) if x >= today else "", "rows": rows})
            self._adds(allrows)
            future = [e for e in ents if e["k"] != "past"]
            msgs = [e for e in future if e["k"] == "msg"]
            free_days = [(r["len"] if "len" in r else 0, dd) for dd in days if not dd["past"] for r in dd["rows"]
                         if r["kind"] == "free"]
            most = ""
            best = None
            for dd in days:
                if dd["past"]:
                    continue
                x = dt.date.fromisoformat(dd["date"])
                busy = [(e["s"], e["e"]) for e in ents if e["date"] == x and e["s"] is not None and e["k"] != "past"]
                busy += [(c["s"], c["e"]) for c in cl if c["date"] == x]
                for g in self._free(x, busy, now, tz, short=True):
                    if best is None or g["len"] > best[0]:
                        a = T.local(g["sort"], tz)
                        best = (g["len"], f"{x:%a}" + (" afternoon" if a.hour >= 12 else ""))
            most = best[1] if best else ""
            del free_days
            same_month = mon.month == sun.month
            title = f"{mon:%B} {mon.day} to {sun.day}" if same_month else f"{mon:%B} {mon.day} to {sun:%B} {sun.day}"
            short = f"{mon:%b} {mon.day} to {sun.day}" if same_month else f"{mon:%b} {mon.day} to {sun:%b} {sun.day}"
            out["week"] = {"label": f"Week of {mon:%b} {mon.day}", "days": days,
                           "summary": {"clashes": len(cl), "only_msgs": len(msgs),
                                       "promises": sum(1 for e in future if e["k"] == "yes"), "most_free": most},
                           "done": sum(1 for dd in days if dd["past"] for r in dd["rows"] if r["kind"] != "free")}
            om = [dict(self._public(e), rel=T.rel_day(e["date"], today), date=f"{e['date']:%a} " + (
                e["time"] if e["s"] is not None else f"{e['date']:%b} {e['date'].day}")) for e in msgs]
            self._adds(om)
            out["only_msgs"] = om
            this_week = mon <= today <= sun
            head.update(title=title, short=short, label="This week" if this_week else f"Week of {mon:%b} {mon.day}",
                        prev=(mon - dt.timedelta(days=7)).isoformat(), next=(mon + dt.timedelta(days=7)).isoformat(),
                        clashes=len(cl))
        else:
            first = d.replace(day=1)
            nxt = (first + dt.timedelta(days=32)).replace(day=1)
            g0 = first - dt.timedelta(days=first.weekday())
            g1 = g0 + dt.timedelta(days=41)
            ents, cl = self._gather(g0, g1, now, tz)
            sel = _date(_arg(body, "sel")) or (today if first <= today < nxt else first)
            if not g0 <= sel <= g1:
                sel = first
            tw = today - dt.timedelta(days=today.weekday())
            weeks = []
            for w in range(6):
                ws = g0 + dt.timedelta(days=7 * w)
                k = (ws - tw).days // 7
                rl = {0: "this week", 1: "next week", -1: "last week"}.get(k, f"in {k} weeks" if k > 1 else "")
                cells = []
                for i in range(7):
                    x = ws + dt.timedelta(days=i)
                    es = [e for e in ents if e["date"] == x]
                    cs = [c for c in cl if c["date"] == x]
                    kinds = [("xa" if c["level"] == "al" else "x") for c in cs]
                    if any(e["k"] in ("cal",) or (e["k"] == "past" and e["event"]) for e in es):
                        kinds.append("c")
                    if any(e["item"] is not None for e in es):
                        kinds.append("m")
                    kinds = list(dict.fromkeys(kinds))[:3]
                    label = f"{x:%b} {x.day}" if x.day == 1 and x.month != first.month else str(x.day)
                    names = [c["when"] and f"clash {c['short']}" for c in cs] + [
                        ("from a message, " if e["item"] is not None else "") + e["title"] for e in es]
                    cells.append({"date": x.isoformat(), "n": x.day, "label": label, "out": x.month != first.month,
                                  "past": x < today, "today": x == today, "sel": x == sel, "dots": kinds,
                                  "aria": f"{x:%A}, {x:%B} {x.day}" + (": " + ", ".join(names[:5]) if names else
                                                                        ": nothing")})
                weeks.append({"label": rl, "cur": k == 0, "cells": cells})
            inm = [e for e in ents if first <= e["date"] < nxt]
            out["month"] = {
                "weeks": weeks, "dows": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                "figs": {"on_cal": len({e["event"] for e in inm if e["event"]}),
                         "only_msgs": sum(1 for e in inm if e["k"] == "msg"),
                         "promises": sum(1 for e in inm if e["k"] == "yes"),
                         "clashes": sum(1 for c in cl if first <= c["date"] < nxt)},
                "name": f"{first:%B}",
                "sel": {"date": sel.isoformat(), "label": f"{sel:%a} {sel:%b} {sel.day}",
                        "rel": T.rel_day(sel, today) if sel >= today else "",
                        "rows": self._day_rows(sel, ents, cl, now, tz)}}
            self._adds(out["month"]["sel"]["rows"])
            # the 30-day strip from today (filled: something that day; outlined: today)
            s_ents, s_cl = self._gather(today, today + dt.timedelta(days=STRIP_DAYS - 1), now, tz)
            on = {e["date"] for e in s_ents} | {c["date"] for c in s_cl}
            marks = ["td" if i == 0 else "ev" if today + dt.timedelta(days=i) in on else "" for i in range(STRIP_DAYS)]
            wk, i = [], 0
            while i < STRIP_DAYS:
                x = today + dt.timedelta(days=i)
                span = min(7 - x.weekday() if i == 0 else 7, STRIP_DAYS - i)
                wk.append({"col": i + 1, "span": span, "label": "Today" if i == 0 else f"{x:%b} {x.day}"})
                i += span
            end = today + dt.timedelta(days=STRIP_DAYS - 1)
            out["month"]["strip"] = {"marks": marks, "weeks": wk, "to": f"to {end:%b} {end.day}",
                                     "days": sum(1 for m in marks if m)}
            head.update(title=f"{first:%B} {first.year}", short=f"{first:%B}",
                        label="This month" if first <= today < nxt else f"{first:%B}",
                        prev=(first - dt.timedelta(days=1)).replace(day=1).isoformat(), next=nxt.isoformat(),
                        clashes=sum(1 for c in cl if first <= c["date"] < nxt))
        if mode in ("day", "week"):
            all_cl = self._clashes(today, today + dt.timedelta(days=SAID_DAYS), now, tz)
            out["said"] = self._said(now, tz, {i for c in all_cl for i in c["items"]})
            out["invites"] = self._invites(now, tz)
            self._adds([r for r in out["invites"] if not r["calendar"]])
        out["header"] = head
        return out
