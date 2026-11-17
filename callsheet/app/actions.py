"""0.22.0 Chat does what the owner can (owner approved 2026-11-16: "chat should be able to do things like dismiss alerts
if I ask for it, and perform basically any task that I might ask for"; rework/chat-actions-plan.md).

- `REGISTRY`: one typed entry per in-app action. Each entry names its target kind, its class (`undoable`, `external`,
  `destructive`, `setup_only`), the button whose function it calls (the same one: `Today.done`, `Today.dismiss`,
  `Tidy.run`, `Store.add_vip`, `OldData.clear` ...), the date the owner approved it and its test module.
- `Actions.run` calls that function and journals the change in `action_log` (messages.db) with its before value;
  `Actions.undo` puts it back once, and refuses when the thing changed since. Labels come from Callsheet's own data
  (an item's who and what, a clash's why), never from model text and never message text.
- Nothing here sends: no entry reaches a send path (tests/test_chat_actions_0220.py checks the source).
- `related`: the open clashes that share an item with what the owner just dismissed, finished or filed ("Also
  dismiss N related clashes", one Undo for all; owner 2026-11-16).
"""
import json
import time

import ledger as L
import prefs as prefs_mod

UNDOABLE, EXTERNAL, DESTRUCTIVE, SETUP_ONLY = "undoable", "external", "destructive", "setup_only"
CLASSES = (UNDOABLE, EXTERNAL, DESTRUCTIVE, SETUP_ONLY)
BATCH_MAX = 100          # a batch is built by Callsheet from its own lists, never from model ids
RECENT = 30              # rows in Setup › Your rules › Recent changes
CHANGED = "It changed after, so Undo would overwrite that."
DONE_ALREADY = "That was already undone."
NO_UNDO = "This can't be undone here."
VIAS = ("chat", "today", "setup")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_log (
  id INTEGER PRIMARY KEY, action TEXT NOT NULL, targets TEXT NOT NULL DEFAULT '[]', args TEXT NOT NULL DEFAULT '{}',
  label TEXT NOT NULL DEFAULT '', before TEXT NOT NULL DEFAULT '{}', after TEXT NOT NULL DEFAULT '{}',
  cls TEXT NOT NULL, via TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '', at REAL NOT NULL, undone_at REAL)"""


class Invalid(ValueError):
    """An action that can't run or can't be undone; the message is a plain sentence."""


class Action:
    """One registry entry. `button`: the route and function the owner's button calls (this entry calls the same
    function); `chat`: the propose_action verb that reaches it ("" when Chat reaches it another way)."""
    __slots__ = ("name", "target", "cls", "button", "chat", "approved", "test", "batch")

    def __init__(self, name, target, cls, button, chat="", approved="2026-11-16",
                 test="tests/test_chat_actions_0220.py", batch=False):
        self.name, self.target, self.cls, self.button, self.chat = name, target, cls, button, chat
        self.approved, self.test, self.batch = approved, test, batch


REGISTRY = {a.name: a for a in (
    Action("item.done", "item", UNDOABLE, "today/done → Today.done", "done", batch=True),
    Action("item.not_for_me", "item", UNDOABLE, "today/notes {not_for_me} → Today.note", "not_for_me", batch=True),
    Action("item.note", "item", UNDOABLE, "today/notes {text} → Today.note", "note"),
    Action("item.snooze", "item", UNDOABLE, "Today.set_aside (today_reminders, like Remind me; hidden until then)",
           "snooze", approved="2026-11-11", test="tests/test_reminders_0220.py"),
    Action("item.remind", "item", UNDOABLE, "Today.set_aside (today_reminders, like Remind me; then Today.tick)",
           "remind", approved="2026-11-11", test="tests/test_reminders_0220.py"),
    Action("clash.dismiss", "clash", UNDOABLE, "today/dismiss → Today.dismiss (Undo: Today.reopen)", "dismiss",
           batch=True),
    Action("followup.told", "followup", UNDOABLE, "Today.told (new: the follow-up is marked told)", "told"),
    Action("cleanup.clear", "item", UNDOABLE, "today/done on Clean up rows → Today.done", "clear", batch=True),
    Action("older.clear", "item", UNDOABLE, "today/done on the Older group → Today.done", "clear_older", batch=True),
    Action("tidy", "none", UNDOABLE, "today/tidy → Tidy.run (Undo: Tidy.undo)", "tidy", approved="2026-11-15",
           test="tests/test_tidy_0214.py"),
    Action("vip.add", "person", UNDOABLE, "POST vips → Store.add_vip", "vip"),
    Action("vip.remove", "person", UNDOABLE, "DELETE vips/<id> → Store.delete_vip", "unvip"),
    Action("old_data.clear", "none", DESTRUCTIVE, "old-data/clear → OldData.clear", "clear_old_data"),
    # reached through their own cards (not propose_action): listed so the registry holds every Chat-reachable change
    Action("setting", "none", UNDOABLE, "PATCH settings and push → Prefs.apply (propose_setting)",
           approved="2026-11-11", test="tests/test_chat_cards_0170.py"),
    Action("area.rename", "area", UNDOABLE, "PATCH areas/<id> → Areas (propose_setting kind area)",
           approved="2026-11-16", test="tests/test_areas_0220.py"),
    Action("area.colour", "area", UNDOABLE, "PATCH areas/<id> → Areas (propose_setting kind area)",
           approved="2026-11-16", test="tests/test_areas_0220.py"),
    Action("area.move", "connection", UNDOABLE, "Move to → Areas (propose_setting kind area_move)",
           approved="2026-11-16", test="tests/test_areas_0220.py"),
    Action("calendar.add", "item", EXTERNAL, "calendar/add → CalWrite.add (Confirm card)", "calendar",
           approved="2026-11-12", test="tests/test_calwrite_0210.py"),
    Action("gmail.draft", "item", EXTERNAL, "today/items/<id>/gmail → GDrafts (Confirm card)",
           approved="2026-11-12", test="tests/test_gdrafts_0210.py"),
    Action("secrets", "none", SETUP_ONLY, "Setup only (keys, tokens, sign-ins): Chat opens Setup"),
)}
CHAT_VERBS = {a.chat: a.name for a in REGISTRY.values() if a.chat}


def ensure(ms):
    def run(db):
        for stmt in _SCHEMA.split(";"):
            if stmt.strip():
                db.execute(stmt)
    ms.write(run)


def _j(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def clash_words(c):
    """A clash's label from Callsheet's own data (its why), short."""
    w = " ".join(str(c.get("why") or "").split())
    return (w[:117] + "…") if len(w) > 118 else (w or f"Clash {c.get('id')}")


class Actions:
    """The registry's runner and journal. `today`: today.Today; `prefs`: prefs.Prefs; `tidy`: tidy.Tidy;
    `store`: store.Store (the Setup VIP list, the one VIP home); `oldata`: oldata.OldData."""

    def __init__(self, ms, clock=time.time, today=None, prefs=None, tidy=None, store=None, oldata=None, tz=None):
        self.ms, self.clock = ms, clock
        self.today, self.prefs, self.tidy, self.store, self.oldata = today, prefs, tidy, store, oldata
        self.tz = tz or (lambda: None)
        ensure(ms)

    # --- reads ---------------------------------------------------------------------------------------------------
    def _items(self, ids):
        rows = self.ms._read(f"SELECT * FROM ledger_items WHERE id IN ({','.join('?' * len(ids))})",  # noqa: SLF001
                             tuple(ids)) if ids else []
        return {r["id"]: dict(r) for r in rows}

    def _clashes(self, ids):
        rows = self.ms._read(f"SELECT * FROM ledger_conflicts WHERE id IN ({','.join('?' * len(ids))})",  # noqa: SLF001
                             tuple(ids)) if ids else []
        return {r["id"]: dict(r) for r in rows}

    def related(self, items=(), clash=None):
        """Open clashes that share an item with `items` or with clash `clash` (that clash left out): [{id, label}]."""
        ids = {int(i) for i in items}
        skip = set()
        if clash is not None:
            c = self._clashes([int(clash)]).get(int(clash))
            if c:
                skip.add(c["id"])
                ids |= {r for k, r in ((c["a_kind"], c["a_ref"]), (c["b_kind"], c["b_ref"])) if k == "item"}
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        rows = self.ms._read(  # noqa: SLF001
            f"SELECT * FROM ledger_conflicts WHERE state='open' AND ((a_kind='item' AND a_ref IN ({q})) OR "
            f"(b_kind='item' AND b_ref IN ({q}))) ORDER BY slot_start, id", tuple(ids) * 2)
        return [{"id": r["id"], "label": clash_words(dict(r))} for r in rows if r["id"] not in skip][:BATCH_MAX]

    def clash_labels(self, ids):
        """{id: label} for the open clashes among ids."""
        return {i: clash_words(c) for i, c in self._clashes(list(ids)).items() if c["state"] == "open"}

    def item_labels(self, ids):
        return {i: prefs_mod.item_words(it) for i, it in self._items(list(ids)).items()}

    def label(self, name, targets, args=None):
        """The card's and the journal's label, from Callsheet's own data."""
        args = args or {}
        n = len(targets)
        if name.startswith(("item.", "cleanup.", "older.")):
            its = self._items(targets)
            one = prefs_mod.item_words(its[targets[0]]) if n == 1 and targets[0] in its else f"{n} items"
            return {"item.done": f"Done: {one}", "item.not_for_me": f"Not for me: {one}",
                    "item.note": f"Note on {one}", "item.snooze": f"Snoozed: {one}",
                    "item.remind": f"Remind me: {one}", "cleanup.clear": f"Cleared from Clean up: {one}",
                    "older.clear": f"Cleared from Older: {one}"}[name]
        if name == "clash.dismiss":
            cs = self._clashes(targets)
            return f"Dismissed: {clash_words(cs[targets[0]])}" if n == 1 and targets[0] in cs else \
                f"Dismissed {n} clashes"
        if name == "followup.told":
            r = self.ms._read("SELECT * FROM ledger_followups WHERE id=?", (targets[0],))  # noqa: SLF001
            who = (r[0]["tell_name"] or r[0]["tell"]) if r else ""
            return f"Told {who}" if who else "Follow-up marked told"
        if name == "vip.add":
            return f"VIP: {args.get('name') or ''}".strip()
        if name == "vip.remove":
            return f"No longer a VIP: {args.get('name') or ''}".strip()
        if name == "tidy":
            return "Tidy up"
        if name == "old_data.clear":
            return "Clear old data"
        return name

    # --- run -----------------------------------------------------------------------------------------------------
    def run(self, name, targets=(), args=None, via="chat", ref="", label=None):
        """Run one registry action (the button's own function) and journal it. Returns the public log row with
        `result`. Raises Invalid with a plain sentence."""
        a = REGISTRY.get(name)
        if a is None or a.cls in (EXTERNAL, SETUP_ONLY) or name in ("setting",) or name.startswith("area."):
            raise Invalid("That isn't something Callsheet can do from here.")
        if via not in VIAS:
            raise Invalid("Unknown origin.")
        args = dict(args or {})
        targets = [int(t) for t in targets if not isinstance(t, bool) and isinstance(t, int)] if a.target in (
            "item", "clash", "followup") else list(targets)
        targets = list(dict.fromkeys(targets))
        if a.target in ("item", "clash", "followup") and not targets:
            raise Invalid("Which one?")
        if len(targets) > (BATCH_MAX if a.batch else 1):
            raise Invalid(f"At most {BATCH_MAX if a.batch else 1} at a time.")
        lab = label or self.label(name, targets, args)
        before, after, result = getattr(self, "_do_" + name.replace(".", "_"))(targets, args, via, ref)
        now = self.clock()
        lid = self.ms.write(lambda db: db.execute(
            "INSERT INTO action_log (action, targets, args, label, before, after, cls, via, ref, at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", (name, _j(targets), _j({k: v for k, v in args.items() if k != "note"}),
                                             lab[:200], _j(before), _j(after), a.cls, via, str(ref)[:80], now)).lastrowid)
        out = self.entry(lid)
        out["result"] = result
        return out

    def _do_item_done(self, ids, args, via, ref):
        if self.today is None:
            raise Invalid("Today isn't available just now.")
        got = self.today.done(ids, note={"chat": "you marked it done in Chat"}.get(via, "you marked it done"))
        return {}, {"items": got["done"]}, got

    _do_cleanup_clear = _do_older_clear = _do_item_done

    def _do_item_not_for_me(self, ids, args, via, ref):
        if self.today is None:
            raise Invalid("Today isn't available just now.")
        got = self.today.note({"items": ids, "chips": ["not_for_me"]})
        return {}, {"items": got["filed"], "note": got["note"]}, got

    def _do_item_note(self, ids, args, via, ref):
        if self.today is None:
            raise Invalid("Today isn't available just now.")
        got = self.today.note({"items": ids, "text": str(args.get("note") or "")})
        return {}, {"items": ids, "note": got["note"]}, got

    def _do_prefs(self, action, ids, args, ref):
        if self.prefs is None:
            raise Invalid("Callsheet can't do that just now.")
        try:
            rows = self.prefs.act(action, ids, {"until_ts": args.get("until_ts")}, ref, self.tz())
        except prefs_mod.Invalid as e:
            raise Invalid(str(e)) from None
        return {}, {"owner_actions": [r["id"] for r in rows]}, {"actions": [r["id"] for r in rows]}

    def _set_aside(self, ids, args, remind):
        """0.22.0 one reminder store: Chat's snooze and remind go through Today's (today_reminders, Today.tick), like
        its Remind me buttons; Undo cancels them."""
        if self.today is None:
            raise Invalid("Today isn't available just now.")
        try:
            got = self.today.set_aside(ids, args.get("until_ts"), remind=remind)
        except ValueError as e:   # today.Invalid: a plain sentence
            raise Invalid(str(e)) from None
        return {}, {"reminders": got["reminders"], "items": got["items"]}, got

    def _do_item_snooze(self, ids, args, via, ref):
        return self._set_aside(ids, args, False)

    def _do_item_remind(self, ids, args, via, ref):
        return self._set_aside(ids, args, True)

    def _do_clash_dismiss(self, cids, args, via, ref):
        if self.today is None:
            raise Invalid("Today isn't available just now.")
        cs = self._clashes(cids)
        if len(cs) != len(cids):
            raise Invalid("There's no such clash.")
        before = {}
        for cid in cids:
            c = cs[cid]
            ids = [r for k, r in ((c["a_kind"], c["a_ref"]), (c["b_kind"], c["b_ref"])) if k == "item"]
            fus = [r["id"] for r in self.ms._read(  # noqa: SLF001
                "SELECT id FROM ledger_followups WHERE state='open' AND (conflict=? OR item IN "
                f"({','.join('?' * len(ids)) or 'NULL'}))", tuple([cid] + ids))]
            rems = [r["id"] for r in self.ms._read(  # noqa: SLF001
                "SELECT id FROM today_reminders WHERE state='open' AND conflict=?", (cid,))]
            before[str(cid)] = {"state": c["state"], "followups": fus, "reminders": rems}
        done = []
        for cid in cids:
            if cs[cid]["state"] == "open":
                self.today.dismiss(cid)
                done.append(cid)
        return before, {"clashes": done}, {"dismissed": done}

    def _do_followup_told(self, fids, args, via, ref):
        if self.today is None:
            raise Invalid("Today isn't available just now.")
        got = self.today.told(fids[0])
        return {"state": "open"}, {"followup": fids[0]}, got

    def _do_tidy(self, _t, args, via, ref):
        if self.tidy is None:
            raise Invalid("Tidy up isn't available just now.")
        res = self.tidy.run(how=via)
        return {}, {"run": res["run"], "moved": res["moved"]}, res

    def _vip_store(self):
        if self.store is None:
            raise Invalid("The VIP list isn't available just now.")
        return self.store

    def _do_vip_add(self, _t, args, via, ref):
        st = self._vip_store()
        try:
            v = st.add_vip(args.get("name"), args.get("email"), args.get("area_id"))
        except Exception as e:  # noqa: BLE001 (store.InputError / NotFound: a plain sentence)
            raise Invalid(str(e)) from None
        return {}, {"vip": v["id"]}, v

    def _do_vip_remove(self, _t, args, via, ref):
        st = self._vip_store()
        v = next((x for x in st.vips() if x["id"] == args.get("id")), None)
        if v is None:
            raise Invalid("That VIP isn't on the list.")
        st.delete_vip(v["id"])
        return {"vip": {k: v.get(k) for k in ("name", "email", "area_id", "opts")}}, {}, {"removed": v["id"]}

    def _do_old_data_clear(self, _t, args, via, ref):
        if self.oldata is None:
            raise Invalid("Clear old data isn't available just now.")
        v = self.oldata.view()
        if not v["token"]:
            raise Invalid("There's no old data left to clear.")
        try:
            res = self.oldata.clear({"token": v["token"], "groups": [g["id"] for g in v["groups"]]})
        except Exception as e:  # noqa: BLE001
            raise Invalid(str(e)) from None
        return {}, {"cleared": (res.get("cleared") or {}).get("count", 0)}, res

    # --- undo ----------------------------------------------------------------------------------------------------
    def undo(self, lid, via="setup"):
        """Undo one journal row, once. Refused when it was already undone, has no Undo, or the thing changed since."""
        e = self._row(lid)
        if e is None:
            raise Invalid("There's no such change.")
        if e["undone_at"] is not None:
            raise Invalid(DONE_ALREADY)
        if e["cls"] != UNDOABLE:
            raise Invalid(NO_UNDO)
        after, before = e["after"], e["before"]
        name = e["action"]
        if name in ("item.done", "cleanup.clear", "older.clear", "item.not_for_me"):
            ids = after.get("items") or []
            its = self._items(ids)
            if not ids or any(its.get(i, {}).get("status") != "done" for i in ids):
                raise Invalid(CHANGED)
            self.today.undo(ids)
            if name == "item.not_for_me" and after.get("note"):
                self.ms.write(lambda db: db.execute("DELETE FROM today_notes WHERE id=?", (after["note"],)))
        elif name == "item.note":
            nid = after.get("note")
            now = self.clock()

            def run(db):
                db.execute("DELETE FROM today_notes WHERE id=?", (nid,))
                for i in after.get("items") or []:
                    L.history(db, i, "note", now, note="you removed your note")
            self.ms.write(run)
        elif name in ("item.snooze", "item.remind") and after.get("reminders") is not None:
            if not self.today.cancel_reminders(after["reminders"]):
                raise Invalid(CHANGED)   # already due (reminded or back on Today) or set again since
        elif name in ("item.snooze", "item.remind"):   # journaled before 0.22.0's one store (prefs moved them)
            for aid in after.get("owner_actions") or []:
                try:
                    self.prefs.undo_action(aid)
                except prefs_mod.Invalid:
                    pass
        elif name == "clash.dismiss":
            cids = after.get("clashes") or []
            cs = self._clashes(cids)
            if not cids or any(cs.get(c, {}).get("resolved_why") != "dismissed" for c in cids):
                raise Invalid(CHANGED)
            for c in cids:
                b = before.get(str(c)) or {}
                self.today.reopen(c, b.get("followups") or [], b.get("reminders") or [])
        elif name == "followup.told":
            if not self.today.untold(after["followup"]):
                raise Invalid(CHANGED)
        elif name == "tidy":
            if not self.tidy.undo(after["run"])["restored"]:
                raise Invalid(DONE_ALREADY)
        elif name == "vip.add":
            try:
                self._vip_store().delete_vip(after["vip"])
            except Exception:  # noqa: BLE001
                raise Invalid(CHANGED) from None
        elif name == "vip.remove":
            v = before["vip"]
            if v["email"] and any(x["email"] == v["email"] for x in self._vip_store().vips()):
                raise Invalid(CHANGED)
            self._vip_store().add_vip(v["name"], v["email"], v["area_id"], opts=v.get("opts") or {}, by_name=not v["email"])
        else:
            raise Invalid(NO_UNDO)
        now = self.clock()
        self.ms.write(lambda db: db.execute("UPDATE action_log SET undone_at=? WHERE id=? AND undone_at IS NULL",
                                            (now, e["id"])))
        return self.entry(e["id"])

    # --- the journal ---------------------------------------------------------------------------------------------
    def _row(self, lid):
        if isinstance(lid, bool) or not isinstance(lid, int):
            return None
        r = self.ms._read("SELECT * FROM action_log WHERE id=?", (lid,))  # noqa: SLF001
        if not r:
            return None
        e = dict(r[0])
        for k in ("targets", "args", "before", "after"):
            e[k] = json.loads(e[k] or "null")
        return e

    def entry(self, lid):
        """The public row: labels only, no message text."""
        e = self._row(lid)
        if e is None:
            return None
        undo = e["cls"] == UNDOABLE and e["undone_at"] is None
        return {"id": e["id"], "action": e["action"], "label": e["label"], "cls": e["cls"], "via": e["via"],
                "at": e["at"], "undone_at": e["undone_at"], "count": len(e["targets"]) or 1, "can_undo": undo,
                "ref": e["ref"],
                "note": "" if e["cls"] == UNDOABLE else ("Can't be undone here" if e["cls"] == DESTRUCTIVE else "")}

    def recent(self, limit=RECENT):
        rows = self.ms._read("SELECT id FROM action_log ORDER BY id DESC LIMIT ?", (int(limit),))  # noqa: SLF001
        return [self.entry(r["id"]) for r in rows]
