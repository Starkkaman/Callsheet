"""0.22.0 Areas (mockup areas-v1, owner approved 2026-11-16: "I like all this! make it so!"): Setup › Connections ›
Areas. Rename an area, change its colour (palette.py), add one, delete one (its connections move to the area the
owner picks, Personal by default; the built-in Personal can't be deleted), and move a connection between areas,
with or without the items it already read. Texts, GitHub, feeds and reports folders get areas too, and one Google
account's calendars can go to different areas.

Where an item's area comes from: its connection's area now, unless the connection moved with "Leave as it is", in
which case items older than that move keep the area they had (area_history). "Move it too" drops that history, so
every past item follows: one transaction in callsheet.db, and nothing in the ledger or the message store is
rewritten (neither keeps an area of its own).

Undo: every change stores what it changed ({key: [before, after]}); Undo puts each back only while it still reads
as it was left (else a plain sentence and nothing changes). Connection keys: google:<account>, cal:<account>:
<calendar>, slack:<workspace>, signal, texts, github, feed:<connector>."""
import json
import secrets
import time

import palette

UNDO_KEEP = 100
LOOSE_KINDS = ("texts", "github", "feed")
FEED_NOUNS = {"news": "News feed", "youtube": "YouTube channel", "reports": "Reports folder",
              "reports_drive": "Reports from Drive"}
# a message's source -> its connection (feeds and reports keep their connector id as the account)
_SRC = {"gmail": "google", "calendar": "google", "drive": "google", "slack": "slack", "signal": "signal",
        "sms": "texts", "sms_live": "texts", "github": "github", "news": "feed", "youtube": "feed", "reports": "feed"}


class AreaError(ValueError):
    """A plain sentence for the owner (the API turns it into a 400)."""


def conn_of(source, account, calendar=None):
    k = _SRC.get(str(source or ""))
    if k == "google":
        return f"cal:{account}:{calendar}" if calendar else f"google:{account}"
    if k in ("slack", "feed"):
        return f"{k}:{account}"
    return k or ""


class Resolver:
    """One read of the areas' state; at(source, account, ts, calendar) -> the item's area id ("" when none)."""

    def __init__(self, state):
        self.cur = {k[2:]: v for k, v in state.items() if k.startswith("c:")}
        self.hist = {k[2:]: v for k, v in state.items() if k.startswith("h:")}

    def _at(self, conn, ts):
        for before, area in self.hist.get(conn) or []:
            if ts is not None and ts < before:
                return area
        return self.cur.get(conn)

    def at(self, source, account, ts=None, calendar=None):
        if calendar:
            c = conn_of(source, account, calendar)
            if c in self.cur or c in self.hist:
                return self._at(c, ts) or ""
        return self._at(conn_of(source, account), ts) or ""


class Areas:
    def __init__(self, store, ms=None, clock=time.time):
        self.store, self.ms, self.clock = store, ms, clock

    # --- state, as {key: value}: area:<id>, c:<conn>, h:<conn>, vip:<id> --------------------------------------------
    def _state(self):
        db, out = self.store.db, {}
        for r in db.execute("SELECT id, name, colour, reads, fixed, position FROM areas"):
            out["area:" + r["id"]] = [r["name"], r["colour"], r["reads"], r["fixed"], r["position"]]
        for r in db.execute("SELECT id, area_id FROM accounts"):
            out["c:google:" + r["id"]] = r["area_id"]
        for r in db.execute("SELECT id, area_id FROM slack"):
            out["c:slack:" + r["id"]] = r["area_id"]
        row = db.execute("SELECT value FROM meta WHERE key='signal'").fetchone()
        out["c:signal"] = (json.loads(row["value"]) if row else {}).get("area_id") or "personal"
        for r in db.execute("SELECT conn, area_id FROM conn_areas"):
            out["c:" + r["conn"]] = r["area_id"]
        for r in db.execute("SELECT conn, before_ts, area_id FROM area_history ORDER BY conn, before_ts"):
            out.setdefault("h:" + r["conn"], []).append([r["before_ts"], r["area_id"]])
        for r in db.execute("SELECT id, area_id FROM vips"):
            out["vip:" + r["id"]] = r["area_id"]
        return out

    def _put(self, key, val):
        db = self.store.db
        kind, _, rest = key.partition(":")
        if kind == "area":
            if val is None:
                db.execute("DELETE FROM areas WHERE id=?", (rest,))
            else:
                db.execute("INSERT OR REPLACE INTO areas (id, name, colour, reads, fixed, position) VALUES (?,?,?,?,?,?)",
                           [rest] + list(val))
        elif kind == "vip":
            if val is not None:
                db.execute("UPDATE vips SET area_id=? WHERE id=?", (val, rest))
        elif kind == "h":
            db.execute("DELETE FROM area_history WHERE conn=?", (rest,))
            for before, area in val or []:
                db.execute("INSERT INTO area_history (conn, before_ts, area_id) VALUES (?,?,?)", (rest, before, area))
        elif kind == "c":
            ck, _, cid = rest.partition(":")
            if ck == "google" and val is not None:
                db.execute("UPDATE accounts SET area_id=? WHERE id=?", (val, cid))
            elif ck == "slack" and val is not None:
                db.execute("UPDATE slack SET area_id=? WHERE id=?", (val, cid))
            elif ck == "signal":
                row = db.execute("SELECT value FROM meta WHERE key='signal'").fetchone()
                m = json.loads(row["value"]) if row else {}
                m["area_id"] = val or "personal"
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('signal', ?)", (json.dumps(m),))
            elif val is None:
                db.execute("DELETE FROM conn_areas WHERE conn=?", (rest,))
            else:
                db.execute("INSERT OR REPLACE INTO conn_areas (conn, area_id) VALUES (?,?)", (rest, val))

    def _change(self, kind, label, fn):
        """Runs fn() in one transaction and keeps what it changed for Undo. Returns (fn's result, undo token)."""
        st = self.store
        with st.lock:
            st.db.execute("BEGIN IMMEDIATE")
            try:
                before = self._state()
                res = fn()
                after = self._state()
                diff = {k: [before.get(k), after.get(k)] for k in set(before) | set(after) if before.get(k) != after.get(k)}
                token = secrets.token_urlsafe(12)
                st.db.execute("INSERT INTO area_undo (token, at, kind, label, diff) VALUES (?,?,?,?,?)",
                              (token, self.clock(), kind, label, json.dumps(diff)))
                st.db.execute("DELETE FROM area_undo WHERE token NOT IN (SELECT token FROM area_undo ORDER BY at DESC, "
                              "rowid DESC LIMIT ?)", (UNDO_KEEP,))
                st.db.execute("COMMIT")
            except Exception:
                st.db.execute("ROLLBACK")
                raise
        return res, token

    def undo(self, token):
        """Puts back what the change with this token changed. Nothing changes when any of it changed again since."""
        st = self.store
        if not isinstance(token, str) or not 8 <= len(token) <= 40:
            raise AreaError("That change can't be undone.")
        with st.lock:
            row = st.db.execute("SELECT * FROM area_undo WHERE token=?", (token,)).fetchone()
            if row is None or row["undone"]:
                raise AreaError("That change was already undone, or is too old to undo.")
            diff = json.loads(row["diff"])
            st.db.execute("BEGIN IMMEDIATE")
            try:
                now = self._state()
                if any(now.get(k) != after for k, (_before, after) in diff.items()):
                    raise AreaError("Something in Areas changed since, so Undo can't put this back. Change it again "
                                    "instead.")
                for k in sorted(diff, key=lambda k: not k.startswith("area:")):
                    self._put(k, diff[k][0])
                st.db.execute("UPDATE area_undo SET undone=1 WHERE token=?", (token,))
                st.db.execute("COMMIT")
            except Exception:
                st.db.execute("ROLLBACK")
                raise
        return {"ok": True, "undone": row["label"]}

    def resolver(self):
        with self.store.lock:
            return Resolver(self._state())

    # --- the changes ------------------------------------------------------------------------------------------------
    def _area(self, area_id):
        if not isinstance(area_id, str):
            raise AreaError("Pick an area.")
        return self.store.area(area_id)   # store.NotFound (a 404) when it doesn't exist

    def update(self, area_id, name=None, colour=None):
        """Rename and/or recolour in one change (one Undo). Returns (area, token)."""
        a = self._area(area_id)
        patch = {}
        if name is not None:
            patch["name"] = name
        if colour is not None:
            patch["colour"] = colour
        if not patch:
            raise AreaError("Say the new name or colour.")
        new_name = self.store._check_name(name) if name is not None else a["name"]  # noqa: SLF001
        new_col = palette.info(self.store._check_colour(colour))["name"] if colour is not None else ""  # noqa: SLF001
        bits = ([f"{a['name']} renamed {new_name}"] if name is not None and new_name != a["name"] else []) + \
            ([f"{new_name} is now {new_col}"] if colour is not None else [])
        return self._change("area", "; ".join(bits) or a["name"], lambda: self.store.update_area(area_id, patch))

    def add(self, name, colour=None):
        if colour in (None, ""):
            colour = palette.first_free({a["colour"] for a in self.store.areas()})
        nm = self.store._check_name(name)  # noqa: SLF001
        return self._change("add", f"Added {nm}", lambda: self.store.add_area(nm, colour))

    def delete(self, area_id, to="personal"):
        """Deletes the area; its connections, their past items' area and its VIPs go to `to` (Personal by default)."""
        a = self._area(area_id)
        if a["fixed"]:
            raise AreaError(f"{a['name']} can't be deleted: it's where connections go when an area is deleted.")
        to = to or "personal"
        if to == area_id:
            raise AreaError("Pick another area for its connections.")
        dest = self._area(to)

        def run():
            st = self._state()
            for k, v in st.items():
                if k.startswith(("c:", "vip:")) and v == area_id:
                    self._put(k, to)
                elif k.startswith("h:") and any(x[1] == area_id for x in v):
                    self._put(k, [[b, to if ar == area_id else ar] for b, ar in v])
            self.store.db.execute("DELETE FROM areas WHERE id=?", (area_id,))
            return {"deleted": a["name"], "to": dest["name"]}
        return self._change("delete", f"Deleted {a['name']}; its connections are in {dest['name']}", run)

    def move(self, conn, to, past=True):
        """Moves one connection to the area `to`. past: its past items go too (else they keep the area they had)."""
        c = self.connection(conn)
        dest = self._area(to)
        if not isinstance(past, bool):
            raise AreaError("Say whether what it already read moves too.")
        if c["area_id"] == dest["id"]:
            raise AreaError(f"{c['name']} is in {dest['name']} already.")

        def run():
            st = self._state()
            old, hist = c["area_id"], st.get("h:" + conn) or []
            if c["kind"] == "calendar" and dest["id"] == c["account_area"] and past:
                self._put("c:" + conn, None)   # back with its account
            else:
                self._put("c:" + conn, dest["id"])
            if past:
                self._put("h:" + conn, None)
            elif old:
                self._put("h:" + conn, [x for x in hist if x[0] < self.clock()] + [[self.clock(), old]])
            return {"moved": c["name"], "to": dest["name"], "past": past, "counts": c["past"]}
        what = f"Moved {c['name']} to {dest['name']}" + (f", with {c['past']['text']}" if past and c["past"]["n"] else "")
        return self._change("move", what, run)

    # --- the board (GET api/areas) ----------------------------------------------------------------------------------
    def _count(self, sql, args):
        if self.ms is None:
            return 0
        try:
            return int(self.ms._read(sql, args)[0][0])  # noqa: SLF001
        except Exception:  # noqa: BLE001 (a count is a nicety)
            return 0

    def _past(self, kind, cid, cal=None):
        msgs = evs = 0
        if kind == "google":
            msgs = self._count("SELECT count(*) FROM messages WHERE source='gmail' AND account=?", (cid,))
            evs = self._count("SELECT count(*) FROM events WHERE account=?", (cid,))
        elif kind == "calendar":
            evs = self._count("SELECT count(*) FROM events WHERE account=? AND calendar=?", (cid, cal))
        elif kind in ("slack", "signal"):
            msgs = self._count("SELECT count(*) FROM messages WHERE source=? AND account=?", (kind, cid))
        elif kind == "texts":
            msgs = self._count("SELECT count(*) FROM messages WHERE source IN ('sms','sms_live')", ())
        else:
            msgs = self._count("SELECT count(*) FROM docs WHERE " + ("source='github'" if kind == "github" else
                                                                       "account=?"), () if kind == "github" else (cid,))
        bits = ([f"{msgs:,} message{'s' * (msgs != 1)}" if kind not in ("github", "feed") else
                 f"{msgs:,} item{'s' * (msgs != 1)}"] if msgs else []) + \
            ([f"{evs:,} event{'s' * (evs != 1)}"] if evs else [])
        return {"messages": msgs, "events": evs, "n": msgs + evs, "text": " and ".join(bits)}

    def connections(self):
        """Every connection: {conn, kind, name, line, area_id ("" = not in an area), past, …}."""
        st = self._state()
        out = []
        for acc in self.store.accounts():
            svc = acc.get("services") or {}
            what = " and ".join(w for w, k in (("mail", "gmail"), ("calendar", "calendar")) if svc.get(k, True)) or "off"
            row = {"conn": "google:" + acc["id"], "kind": "google", "name": acc["email"], "icon": "@",
                   "line": f"Google · {what}", "area_id": acc["area_id"], "past": self._past("google", acc["id"]),
                   "calendars": []}
            out.append(row)
            for cal in acc.get("calendars") or []:
                if not isinstance(cal, dict) or not cal.get("id") or not cal.get("selected", True):
                    continue
                ck = f"cal:{acc['id']}:{cal['id']}"
                nm = str(cal.get("summary") or cal.get("name") or cal["id"])[:80]
                c = {"conn": ck, "kind": "calendar", "name": nm, "icon": "▦", "line": f"Calendar in {acc['email']}",
                     "area_id": st.get("c:" + ck) or acc["area_id"], "own_area": "c:" + ck in st,
                     "account_area": acc["area_id"], "past": self._past("calendar", acc["id"], cal["id"])}
                row["calendars"].append(c)
                if c["own_area"]:
                    out.append(c)
        for w in self.store.slack_workspaces():
            out.append({"conn": "slack:" + w["id"], "kind": "slack", "name": w["team_name"], "icon": "#",
                        "line": "Slack", "area_id": w["area_id"], "past": self._past("slack", w["id"])})
        sig = self.store._get_meta("signal", {}) or {}  # noqa: SLF001
        if sig.get("linked"):
            out.append({"conn": "signal", "kind": "signal", "name": "Signal", "icon": "S", "line": "Your phone",
                        "area_id": st.get("c:signal") or "personal", "past": self._past("signal", "signal")})
        out += self._loose(st)
        return out

    def _loose(self, st):
        out = []
        try:
            import texts as texts_mod
            ts = texts_mod.settings(self.ms) if self.ms is not None else {}
        except Exception:  # noqa: BLE001
            ts = {}
        if ts.get("folder") or ts.get("sensor"):
            out.append({"conn": "texts", "kind": "texts", "name": "Texts", "icon": "T", "line": "Your phone",
                        "area_id": st.get("c:texts") or "", "past": self._past("texts", "texts")})
        try:
            import github as github_mod
            gh = github_mod.config(self.store)
        except Exception:  # noqa: BLE001
            gh = {}
        if gh.get("token"):
            n = len(gh.get("repos") or [])
            out.append({"conn": "github", "kind": "github", "name": "GitHub", "icon": "<>",
                        "line": f"GitHub · {n} repo{'s' * (n != 1)}", "area_id": st.get("c:github") or "",
                        "past": self._past("github", "github")})
        try:
            rows = self.store.db.execute("SELECT id, kind, label FROM connectors ORDER BY added, id").fetchall()
        except Exception:  # noqa: BLE001 (connectors not set up)
            rows = []
        for r in rows:
            ck = "feed:" + r["id"]
            out.append({"conn": ck, "kind": "feed", "name": r["label"], "icon": "▤" if "reports" in r["kind"] else "≡",
                        "line": FEED_NOUNS.get(r["kind"], "Feed"), "area_id": st.get("c:" + ck) or "",
                        "past": self._past("feed", r["id"])})
        return out

    def connection(self, conn):
        if not isinstance(conn, str):
            raise AreaError("Pick a connection.")
        for c in self.connections():
            if c["conn"] == conn:
                return c
            for cal in c.get("calendars") or []:
                if cal["conn"] == conn:
                    return cal
        raise AreaError("That connection isn't here any more.")

    def board(self):
        areas = self.store.areas()
        conns = self.connections()
        ids = {a["id"] for a in areas}
        out = [dict(a, conns=[c for c in conns if c["area_id"] == a["id"]]) for a in areas]
        undo = self.store.db.execute("SELECT token, label FROM area_undo WHERE undone=0 ORDER BY at DESC, rowid DESC "
                                     "LIMIT 1"
                                     ).fetchone()
        return {"areas": out, "loose": [c for c in conns if c["area_id"] not in ids],
                "palette": [{"key": k, "name": n, "light": lt, "dark": dk, "light_ratio": round(palette.worst(lt, palette.LIGHT), 1),
                             "dark_ratio": round(palette.worst(dk, palette.DARK), 1)} for k, n, lt, dk in palette.PALETTE],
                "last_undo": {"token": undo["token"], "label": undo["label"]} if undo else None}

    # --- Chat: what the owner's words name (chatstore.build_setting_card) --------------------------------------------
    def find_area(self, words):
        """The area the owner's words name: (area, exact) or (None, False)."""
        w = str(words or "").strip().lower()
        if not w:
            return None, False
        areas = self.store.areas()
        for a in areas:
            if a["name"].lower() == w or a["id"] == w:
                return a, True
        hits = [a for a in areas if a["name"].lower() in w or w in a["name"].lower()]
        return (hits[0], False) if len(hits) == 1 else (None, False)

    def find_conn(self, words):
        """The connection the owner's words name: (connection, exact) or (None, False). exact: its whole address or
        name is in the words; otherwise one connection whose name shares a word with them (a guess)."""
        w = str(words or "").strip().lower()
        if not w:
            return None, False
        conns, parent = [], {}
        for c in self.connections():
            conns.append(c)
            for x in c.get("calendars") or []:
                if x not in conns:
                    conns.append(x)
                    parent[x["conn"]] = c["conn"]
        import re
        exact = [c for c in conns if re.search(r"(?<![\w.@-])" + re.escape(c["name"].lower()) + r"(?![\w.@-])", w)]
        if len(exact) == 1:
            return exact[0], True
        ws = {x for x in re.findall(r"[a-z0-9]+", w) if len(x) > 2 and x not in ("account", "the", "my", "and", "calendar",
                                                                                   "workspace", "feed", "com", "example")}
        guess = [c for c in conns if ws & set(re.findall(r"[a-z0-9]+", (c["name"] + " " + c["line"]).lower()))]
        ids = {c["conn"] for c in guess}   # an account and its own calendars: the account
        guess = [c for c in guess if parent.get(c["conn"]) not in ids]
        return (guess[0], False) if len(guess) == 1 else (None, False)
