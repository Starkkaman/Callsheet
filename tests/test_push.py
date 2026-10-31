"""0.5.1 phone notifications through Home Assistant, against the fake Core API in fake_ha.py (and the fake Google
and Slack): the notify-only allowlist, target listing, payloads (no body text, clipping, deep link), the
scheduler's timing (quiet hours, days off, call-time windows, every 10 min), dedupe, rate limit and batching, the
urgent rules, settings validation, the test button, the token never in logs, and no writer or model in the
light check. Stdlib only, no network."""
import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import re
import sys
import time
import unittest
import zoneinfo
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "callsheet" / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT / "tests"))
import fake_google  # noqa: E402
import fake_ha  # noqa: E402
import ha  # noqa: E402
import push  # noqa: E402
import sync  # noqa: E402
import writer  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from store import Store  # noqa: E402
from test_api_google import Base  # noqa: E402

UTC = dt.timezone.utc
BODY = "SECRET-BODY-TEXT the full contract terms are in the attachment"


# --- the allowlist ------------------------------------------------------------------------
class AllowlistTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": fake_ha.TOKEN})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CALLSHEET_HA_API_BASE", None)

    def test_only_services_list_and_mobile_app_notify(self):
        base = "http://supervisor/core/api"
        self.assertEqual(ha.api_base(), base)
        ha.check_allowed("GET", base + "/services")
        ha.check_allowed("POST", base + "/services/notify/mobile_app_iphone")
        ha.check_allowed("POST", base + "/services/notify/mobile_app_pixel_9a")
        bad = [("POST", base + "/services"), ("GET", base + "/services/notify/mobile_app_iphone"),
               ("POST", base + "/services/notify/persistent_notification"), ("POST", base + "/services/notify/notify"),
               ("POST", base + "/services/notify/mobile_app_"), ("POST", base + "/services/notify/mobile_app_X"),
               ("POST", base + "/services/light/turn_on"), ("POST", base + "/services/lock/unlock"),
               ("GET", base + "/states"), ("GET", base + "/config"), ("POST", base + "/template"),
               ("POST", base + "/events/x"), ("GET", base + "/services?x=1"), ("GET", base + "/services#x"),
               ("POST", base + "/services/notify/mobile_app_iphone/../../services/lock/unlock"),
               ("POST", base + "/services/notify/mobile_app_a%2F..%2Fb"), ("DELETE", base + "/services"),
               ("GET", "http://supervisor/addons/self/info"), ("GET", "http://supervisor/core/api2/services"),
               ("GET", "https://supervisor/core/api/services"), ("GET", "http://evil.example/core/api/services"),
               ("POST", "http://user:pw@supervisor/core/api/services/notify/mobile_app_iphone")]
        for method, url in bad:
            with self.assertRaises(ha.NotAllowed, msg=f"{method} {url}"):
                ha.check_allowed(method, url)

    def test_refuses_before_network(self):
        with mock.patch.object(ha._opener, "open", side_effect=AssertionError("network used")):
            with self.assertRaises(ha.NotAllowed):
                ha.request("POST", ha.api_base() + "/services/lock/unlock", {})
            with self.assertRaises(ha.NotAllowed):
                ha.notify("persistent_notification", {"message": "x"})

    def test_env_override_moves_the_allowlist(self):
        with mock.patch.dict(os.environ, {"CALLSHEET_HA_API_BASE": "http://127.0.0.1:9/core/api"}):
            ha.check_allowed("GET", "http://127.0.0.1:9/core/api/services")
            with self.assertRaises(ha.NotAllowed):
                ha.check_allowed("GET", "http://supervisor/core/api/services")

    def test_source_uses_only_the_two_calls(self):
        src = (APP / "ha.py").read_text(encoding="utf-8")
        calls = re.findall(r'\brequest\("(\w+)", ([^\n]+?)(?:, payload)?\)$', src, re.M)
        self.assertEqual(sorted(calls), sorted([("GET", 'api_base() + "/services"'),
                                                ("POST", 'api_base() + "/services/notify/" + service')]))
        # nothing else in the app talks to Home Assistant
        for f in APP.glob("*.py"):
            if f.name != "ha.py":
                self.assertNotIn("supervisor/core", f.read_text(encoding="utf-8"), f.name)

    def test_no_token_no_request(self):
        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": ""}), \
                mock.patch.object(ha._opener, "open", side_effect=AssertionError("network used")):
            self.assertFalse(ha.available())
            with self.assertRaises(ha.HAError) as cm:
                ha.notify_targets()
            self.assertEqual(cm.exception.code, "unavailable")

    def test_config_yaml_asks_for_the_core_api(self):
        text = (ROOT / "callsheet" / "config.yaml").read_text()
        self.assertRegex(text, r"(?m)^homeassistant_api: true$")
        self.assertNotRegex(text, r"(?m)^hassio_api:")


class FriendlyTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(ha.friendly("mobile_app_iphone"), "iPhone")
        self.assertEqual(ha.friendly("mobile_app_pixel_9a"), "Pixel 9a")
        self.assertEqual(ha.friendly("mobile_app_isaac_ipad"), "Isaac iPad")
        self.assertEqual(ha.friendly("mobile_app_voyager"), "Voyager")
        self.assertEqual(ha.friendly("mobile_app_"), "Phone")


# --- payloads ------------------------------------------------------------------------------
def brief(replies=(), schedule=(), kind="Midday check-in"):
    return {"kind": kind, "replies": list(replies), "schedule": list(schedule), "written_at": "2026-10-30T12:30:00"}


def row(rid, who, text, via="mail", vip=False, reasons=("Direct question",)):
    return {"id": rid, "who": who, "text": text, "via": via, "vip": vip, "reasons": list(reasons),
            "gist": {"summary": BODY}}


class PayloadTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"HOSTNAME": "a1b2c3d4-callsheet"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CALLSHEET_APP_SLUG", None)

    def test_deep_link_for_both_apps(self):
        self.assertEqual(push.app_slug(), "a1b2c3d4_callsheet")
        p = push.test_payload()
        self.assertEqual(p["data"]["url"], "/hassio/ingress/a1b2c3d4_callsheet")
        self.assertEqual(p["data"]["clickAction"], "/hassio/ingress/a1b2c3d4_callsheet")
        with mock.patch.dict(os.environ, {"HOSTNAME": "local-callsheet"}):
            self.assertEqual(push.ingress_path(), "/hassio/ingress/local_callsheet")
        with mock.patch.dict(os.environ, {"HOSTNAME": "devbox"}), mock.patch("platform.node", return_value="devbox"):
            self.assertIsNone(push.ingress_path())
            self.assertNotIn("url", push.test_payload()["data"])
        with mock.patch.dict(os.environ, {"CALLSHEET_APP_SLUG": "x_callsheet; rm"}):
            self.assertIsNone(push.app_slug())

    def test_brief_summary_counts_and_top_without_body(self):
        b = brief([row("r1", "Dana Lee", "Budget sign-off", vip=True, reasons=["VIP"]),
                   row("r2", "Sam", "Lunch?")], [{"id": "s1", "time": "9:00 am", "title": "Standup"}])
        p = push.brief_payload(b)
        self.assertEqual(p["title"], "Midday check-in")
        self.assertEqual(p["message"], "2 to reply, 1 event today. Top: Dana Lee, Budget sign-off")
        self.assertEqual(p["data"]["tag"], "callsheet-brief")
        self.assertNotIn("SECRET", json.dumps(p))

    def test_brief_summary_never_uses_a_snippet_or_slack_text(self):
        p = push.brief_payload(brief([row("r1", "Ana", '"' + BODY + '"', vip=True, reasons=["VIP"])]))
        self.assertNotIn("SECRET", json.dumps(p))
        self.assertTrue(p["message"].endswith("Top: Ana"))
        p = push.brief_payload(brief([row("r1", "Ana", BODY, via="dm", vip=True, reasons=["VIP"])]))
        self.assertNotIn("SECRET", json.dumps(p))
        self.assertTrue(p["message"].endswith("Top: Ana, Slack"))

    def test_brief_summary_clash_and_empty(self):
        ev = [{"id": "s1", "time": "10:00 am", "title": "Board", "clash": True, "start_hhmm": "10:00", "end_hhmm": "11:00"},
              {"id": "s2", "time": "10:30 am", "title": "Dentist", "clash": True, "start_hhmm": "10:30", "end_hhmm": "11:00"}]
        self.assertEqual(push.brief_payload(brief([], ev))["message"],
                         "Nothing to reply to, 2 events today. Top: clash at 10:00 am Board")
        self.assertEqual(push.brief_payload(brief())["message"], "Nothing to reply to, 0 events today.")

    def test_clipping(self):
        long = "Q4 " + "budget review and sign-off " * 10
        p = push.brief_payload(brief([row("r1", "D" * 80, long, vip=True, reasons=["VIP"])], kind="K" * 80))
        self.assertLessEqual(len(p["message"]), push.TEXT_MAX)
        self.assertLessEqual(len(p["title"]), push.TITLE_MAX)
        self.assertTrue(p["message"].endswith("…"))
        a = push.alert_payload({"who": "Dana", "subject": long, "reason": "urgent", "source": "mail"})
        self.assertLessEqual(len(a["message"]), push.TEXT_MAX)
        self.assertTrue(a["message"].startswith("Dana: Q4 budget"))

    def test_alert_texts(self):
        a = push.alert_payload({"who": "Dana Lee", "subject": "Contract", "reason": "vip", "source": "mail"})
        self.assertEqual((a["title"], a["message"]), ("VIP message", "Dana Lee: Contract"))
        self.assertEqual(a["data"]["priority"], "high")
        self.assertEqual(a["data"]["push"], {"interruption-level": "time-sensitive"})
        self.assertNotIn("tag", a["data"])
        s = push.alert_payload({"who": "Tomás", "reason": "vip", "source": "slack"})
        self.assertEqual(s["message"], "Tomás sent you a Slack message")
        self.assertEqual(push.batch_payload(3)["message"], "3 more urgent messages. Open Callsheet to see them.")
        self.assertEqual(push.alert_payload({"who": "X", "subject": "", "reason": "urgent"})["message"], "X: (no subject)")


# --- the urgent rules ------------------------------------------------------------------------
class UrgentRulesTest(unittest.TestCase):
    def test_words(self):
        yes = ["Urgent: need this today", "URGENT - due Friday", "Need the deck today, due 5pm", "Emergency at the site",
               "Can you send it ASAP", "a.s.a.p. please", "urgent, by 3pm", "Urgently need sign-off by end of day",
               "Today: urgent review"]
        no = ["Your order arrives today", "Urgent: server down", "Is this urgent?x", "asapx", "today today",
              "Lunch tomorrow?", "due Friday", "by today"]
        for t in yes:
            self.assertTrue(push.urgent_words(t), t)
        for t in no:
            self.assertFalse(push.urgent_words(t), t)

    def test_reason_order(self):
        self.assertEqual(push.urgent_reason(True, "hello", "", "hello"), "vip")
        self.assertEqual(push.urgent_reason(False, "Urgent: today", "", ""), "urgent")
        self.assertEqual(push.urgent_reason(False, "Contract", "The contract has been signed.",
                                            "Contract\nThe contract has been signed."), "claim")
        self.assertIsNone(push.urgent_reason(False, "Lunch?", "Are you free?", "Lunch?\nAre you free?"))

    def _thread(self, sender, subject, snippet="", labels=("INBOX",), at=None, headers=None):
        at = at or dt.datetime.now(UTC)
        return fake_google.thread("t1", sender, subject, snippet, at, to="me@example.com", labels=labels,
                                  headers=headers)

    def test_mail_item_rules(self):
        vips = {"dana@example.com": "Dana"}
        since = int((time.time() - 600) * 1000)
        it = push.mail_item(self._thread("Dana Lee <dana@example.com>", "Hi", BODY), "me@example.com", "a1",
                            vips, [], since)
        self.assertEqual((it["reason"], it["who"], it["subject"]), ("vip", "Dana Lee", "Hi"))
        self.assertNotIn("SECRET", json.dumps(it))
        self.assertTrue(it["id"].startswith("m:a1:m"))
        f = lambda th, s=(): push.mail_item(th, "me@example.com", "a1", vips, list(s), since)  # noqa: E731
        self.assertIsNotNone(f(self._thread("Bo <bo@x.com>", "Urgent: sign today")))
        self.assertIsNone(f(self._thread("Bo <bo@x.com>", "Weekly notes")))
        # automated, newsletters, category tabs: skipped unless VIP
        self.assertIsNone(f(self._thread("Shop <noreply@shop.com>", "URGENT: sale ends today, due now")))
        self.assertIsNone(f(self._thread("Bo <bo@x.com>", "Urgent: today due", headers={"List-Id": "x"})))
        self.assertIsNone(f(self._thread("Bo <bo@x.com>", "Urgent: today due", labels=("INBOX", "CATEGORY_UPDATES"))))
        self.assertIsNotNone(f(self._thread("Dana <dana@example.com>", "x", labels=("INBOX", "CATEGORY_UPDATES"))))
        # own mail, sent, not in the inbox, older than the last check
        self.assertIsNone(f(self._thread("Me <me@example.com>", "Urgent: today due")))
        self.assertIsNone(f(self._thread("Bo <bo@x.com>", "Urgent: today due", labels=("SENT",))))
        self.assertIsNone(f(self._thread("Bo <bo@x.com>", "Urgent: today due", labels=("ARCHIVE",))))
        self.assertIsNone(f(self._thread("Bo <bo@x.com>", "Urgent: today due",
                                         at=dt.datetime.now(UTC) - dt.timedelta(hours=1))))
        # "doesn't need a reply" rules; VIPs win over sender and domain rules (not thread rules)
        sender = [{"source": "mail", "kind": "sender", "value": "bo@x.com"}]
        self.assertIsNone(f(self._thread("Bo <bo@x.com>", "Urgent: today due"), sender))
        self.assertIsNone(f(self._thread("Bo <bo@x.com>", "Urgent: today due"),
                            [{"source": "mail", "kind": "domain", "value": "x.com"}]))
        self.assertIsNotNone(f(self._thread("Dana <dana@example.com>", "x"),
                               [{"source": "mail", "kind": "sender", "value": "dana@example.com"}]))
        self.assertIsNone(f(self._thread("Dana <dana@example.com>", "x"),
                            [{"source": "mail", "kind": "thread", "value": "t1"}]))
        # a reply's subject isn't the sender's claim
        self.assertIsNone(f(self._thread("Bo <bo@x.com>", "Re: The invoice has been paid", "thanks")))
        self.assertEqual(f(self._thread("Bo <bo@x.com>", "Invoice", "The invoice has been paid."))["reason"], "claim")

    def test_vip_slack_users(self):
        users = {"U1": {"name": "Tom", "email": "tom@x.com"}, "U2": {"name": "Ana Costa", "email": ""},
                 "U3": {"name": "Bob", "email": "bob@x.com"}}
        self.assertEqual(push.vip_slack_users(users, {"tom@x.com": "Tom", "ana@y.com": "Ana Costa"}), ["U1", "U2"])


# --- rate limit and batching --------------------------------------------------------------------
def items(n, reason="urgent"):
    return [{"id": f"i{k}", "reason": reason, "at": k} for k in range(n)]


class PlanTest(unittest.TestCase):
    def test_room_for_all(self):
        singles, batch, held, sent = push.plan(items(2), [], 0, 1000, 3)
        self.assertEqual((len(singles), batch, held, len(sent)), (2, 0, 0, 2))

    def test_extras_batched(self):
        singles, batch, held, sent = push.plan(items(5), [], 0, 1000, 3)
        self.assertEqual((len(singles), batch, held, len(sent)), (2, 3, 0, 3))

    def test_no_room_holds_then_batches(self):
        sent = [1000, 1100, 1200]
        singles, batch, held, out = push.plan(items(2), sent, 0, 1500, 3)
        self.assertEqual((singles, batch, held, out), ([], 0, 2, sent))
        # an hour after the first alert: room for one -> one batch with the held and the new
        singles, batch, held, out = push.plan(items(1), out, held, 4601, 3)
        self.assertEqual((singles, batch, held, len(out)), ([], 3, 0, 3))

    def test_held_with_room(self):
        singles, batch, held, out = push.plan(items(1), [], 4, 10, 3)
        self.assertEqual((len(singles), batch, held, len(out)), (1, 4, 0, 2))

    def test_order_vip_first_then_newest(self):
        its = items(2, "claim") + items(1, "vip") + [{"id": "u", "reason": "urgent", "at": 9}]
        singles, *_ = push.plan(its, [], 0, 10, 10)
        self.assertEqual([i["reason"] for i in singles], ["vip", "urgent", "claim", "claim"])
        self.assertEqual(singles[2]["at"], 1)

    def test_never_more_than_limit_in_an_hour(self):
        sent, held, total = [], 0, 0
        for minute in range(0, 180, 10):
            now = minute * 60
            singles, batch, held, sent = push.plan(items(4), sent, held, now, 3)
            self.assertLessEqual(len([t for t in sent if now - t < 3600]), 3)


# --- timing ---------------------------------------------------------------------------------------
TZ = zoneinfo.ZoneInfo("America/Los_Angeles")


def at(day, h, m=0):
    return dt.datetime(2026, 11, day, h, m, tzinfo=TZ).timestamp()   # 2 Nov 2026 is a Monday


class TimingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.store.update_settings({"timezone": "America/Los_Angeles"})
        self.cfg = self.store.update_push({"targets": ["mobile_app_iphone"], "urgent": True})

    def due(self, ts, state=None, cfg=None):
        return push.check_due(self.store.settings(), cfg or self.cfg, state or {}, ts)

    def test_between_call_times(self):
        self.assertEqual(self.due(at(2, 9)), (True, "due"))
        self.assertEqual(self.due(at(2, 14, 45)), (True, "due"))
        self.assertEqual(self.due(at(2, 19)), (True, "due"))

    def test_off_or_no_phone(self):
        self.assertEqual(self.due(at(2, 9), cfg=dict(self.cfg, urgent=False))[1], "off")
        self.assertEqual(self.due(at(2, 9), cfg=dict(self.cfg, targets=[]))[1], "off")

    def test_quiet_hours(self):
        for h, m in ((22, 0), (23, 30), (3, 0), (6, 59)):
            self.assertEqual(self.due(at(3, h, m))[1], "quiet hours", (h, m))
        self.assertEqual(self.due(at(3, 7, 0))[0], True)
        self.assertEqual(self.due(at(3, 21, 59))[0], True)
        day = dict(self.cfg, quiet_start="12:00", quiet_end="14:00")
        self.assertEqual(self.due(at(3, 13), cfg=day)[1], "quiet hours")
        self.assertTrue(self.due(at(3, 23), cfg=day)[0])
        self.assertTrue(self.due(at(3, 23), cfg=dict(self.cfg, quiet_start="22:00", quiet_end="22:00"))[0])

    def test_days_off(self):
        self.assertEqual(self.due(at(7, 10))[1], "day off")  # Saturday
        self.store.update_settings({"skip_checkins_days_off": False})
        self.assertTrue(self.due(at(7, 10))[0])

    def test_call_time_window(self):
        for h, m in ((12, 20), (12, 30), (12, 40), (16, 51), (17, 10)):
            self.assertEqual(self.due(at(2, h, m))[1], "call time", (h, m))
        self.assertTrue(self.due(at(2, 12, 19))[0])
        self.assertTrue(self.due(at(2, 12, 41))[0])
        self.store.update_settings({"call_times": [{"id": "midday", "enabled": False}]})
        self.assertTrue(self.due(at(2, 12, 30))[0])

    def test_every_ten_minutes(self):
        self.assertEqual(self.due(at(2, 9, 9), {"last_check": at(2, 9)})[1], "not yet")
        self.assertTrue(self.due(at(2, 9, 10), {"last_check": at(2, 9)})[0])

    def test_settings_validation(self):
        good = self.store.update_push({"quiet_start": "23:15", "quiet_end": "06:00", "max_per_hour": 5,
                                       "at_call_times": True, "targets": ["mobile_app_a", "mobile_app_a", "mobile_app_b"]})
        self.assertEqual(good["targets"], ["mobile_app_a", "mobile_app_b"])
        from store import InputError
        for bad in ({"targets": "mobile_app_a"}, {"targets": ["light.kitchen"]}, {"targets": ["notify"]},
                    {"targets": [f"mobile_app_{i}" for i in range(6)]}, {"urgent": "yes"}, {"quiet_start": "7:00"},
                    {"quiet_end": "24:00"}, {"max_per_hour": 0}, {"max_per_hour": 11}, {"max_per_hour": True},
                    {"max_per_hour": 2.5}, {"other": 1}, ["x"]):
            with self.assertRaises(InputError, msg=bad):
                self.store.update_push(bad)
        self.assertEqual(self.store.push_config(), good)


class FakeNotifier:
    def __init__(self, due=True):
        self.calls, self._due = [], due

    def after_brief(self):
        self.calls.append("brief")

    def due(self, now):
        self.calls.append(("due", now))
        return self._due

    def check(self):
        self.calls.append("check")


class FakeSyncer:
    def __init__(self):
        self.runs = 0

    def run_now(self):
        self.runs += 1


class SchedulerTest(TimingTest):
    def loop(self, times, notifier, has_sources=True):
        syncer, clock = FakeSyncer(), iter(times)
        sch = Scheduler(self.store, syncer, clock=lambda: next(clock), tick=0, notifier=notifier)
        ticks = iter([False] * (len(times) - 1) + [True])
        sch.stop_event.wait = lambda t: next(ticks)
        sch.startup_due = lambda: False
        with mock.patch.object(self.store, "has_sources", return_value=has_sources):
            sch._loop()
        return syncer

    def test_brief_then_summary_else_urgent(self):
        n = FakeNotifier()
        syncer = self.loop([at(2, 12, 29), at(2, 12, 30), at(2, 12, 31)], n)
        self.assertEqual(syncer.runs, 1)
        self.assertEqual(n.calls, ["brief", ("due", at(2, 12, 31)), "check"])

    def test_not_due_no_check(self):
        n = FakeNotifier(due=False)
        self.loop([at(2, 9), at(2, 9, 1)], n)
        self.assertEqual(n.calls, [("due", at(2, 9, 1))])

    def test_startup_sync_sends_no_summary(self):
        n, syncer = FakeNotifier(), FakeSyncer()
        sch = Scheduler(self.store, syncer, notifier=n)
        with mock.patch.object(self.store, "has_sources", return_value=True):
            sch._sync()
        self.assertEqual((syncer.runs, n.calls), (1, []))

    def test_no_sources_no_check(self):
        n = FakeNotifier()
        self.loop([at(2, 9), at(2, 9, 1)], n, has_sources=False)
        self.assertEqual(n.calls, [])

    def test_failures_keep_the_scheduler(self):
        n = FakeNotifier()
        n.check = mock.Mock(side_effect=RuntimeError("boom " + fake_ha.TOKEN))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.loop([at(2, 9), at(2, 9, 1), at(2, 9, 2)], n)
        self.assertEqual(n.check.call_count, 2)
        self.assertIn("Callsheet urgent check failed: RuntimeError", err.getvalue())
        self.assertNotIn(fake_ha.TOKEN, err.getvalue())


# --- end to end against the fakes -------------------------------------------------------------------
class PushBase(Base):
    def setUp(self):
        super().setUp()
        self.ha = fake_ha.FakeHA()
        env = mock.patch.dict(os.environ, dict(self.ha.env(), HOSTNAME="a1b2c3d4-callsheet"))
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.ha.close)
        self.app.notifier.log = self.logs.append
        self.ok("PATCH", "/api/settings", {"timezone": "UTC", "name": "Sam"})

    def mail(self, email="sam@example.com"):
        self.connect(email)
        return self.fake.users[email]

    def thread(self, tid, sender, subject, snippet="", ago=60):
        return fake_google.thread(tid, sender, subject, snippet, dt.datetime.now(UTC) - dt.timedelta(seconds=ago),
                                  to="sam@example.com")

    def enable(self, **kw):
        body = dict({"targets": ["mobile_app_iphone"], "urgent": True}, **kw)
        return self.ok("PATCH", "/api/push", body)


class ApiTest(PushBase):
    def test_get_patch_and_state(self):
        r = self.ok("GET", "/api/push")
        self.assertEqual(r["settings"], push.defaults())
        self.assertTrue(r["available"])
        self.assertEqual(r["link"], "/hassio/ingress/a1b2c3d4_callsheet")
        r = self.enable(max_per_hour=2)
        self.assertEqual(r["settings"]["max_per_hour"], 2)
        self.assertEqual(self.ok("GET", "/api/state")["push"]["targets"], ["mobile_app_iphone"])
        status, data, _ = self.call("PATCH", "/api/push", {"targets": ["light.kitchen"]})
        self.assertEqual(status, 400)
        self.assertIn("Pick phones", data["error"])
        self.assertEqual(self.call("POST", "/api/push", {})[0], 405)

    def test_targets_from_the_core_api(self):
        r = self.ok("GET", "/api/push/targets")
        self.assertEqual(r["targets"], [{"id": "mobile_app_iphone", "name": "iPhone"},
                                        {"id": "mobile_app_isaac_ipad", "name": "Isaac iPad"},
                                        {"id": "mobile_app_pixel_9a", "name": "Pixel 9a"}])
        self.assertEqual(self.ha.requests, [("GET", "/core/api/services", None)])

    def test_targets_errors_in_plain_words(self):
        self.ha.fail = 500
        status, data, _ = self.call("GET", "/api/push/targets")
        self.assertEqual((status, data["error"]), (502, "Home Assistant had a problem. Try again in a minute."))
        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "wrong"}):
            status, data, _ = self.call("GET", "/api/push/targets")
        self.assertEqual(status, 502)
        self.assertIn("didn't let Callsheet in", data["error"])
        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": ""}):
            r = self.ok("GET", "/api/push/targets")
        self.assertEqual((r["available"], r["targets"]), (False, []))
        self.assertIn("Home Assistant app", r["error"])

    def test_test_button(self):
        status, data, _ = self.call("POST", "/api/push/test", {})
        self.assertEqual((status, data["error"]), (400, "Pick a phone first."))
        self.enable(targets=["mobile_app_iphone", "mobile_app_pixel_9a"])
        r = self.ok("POST", "/api/push/test", {})
        self.assertTrue(r["ok"])
        self.assertEqual(r["message"], "Sent to 2 phones. Check them.")
        self.assertEqual([s[0] for s in self.ha.sent], ["mobile_app_iphone", "mobile_app_pixel_9a"])
        body = self.ha.sent[0][1]
        self.assertEqual(body["data"]["url"], "/hassio/ingress/a1b2c3d4_callsheet")
        self.assertEqual(body["data"]["clickAction"], "/hassio/ingress/a1b2c3d4_callsheet")
        self.assertEqual(r["last_sent"]["kind"], "test")
        self.assertIsNone(r["last_error"])
        self.assertEqual(self.call("GET", "/api/push/test")[0], 405)

    def test_test_button_service_gone(self):
        self.enable(targets=["mobile_app_iphone", "mobile_app_old_phone"])
        r = self.ok("POST", "/api/push/test", {})
        self.assertFalse(r["ok"])
        self.assertEqual(r["message"], "Sent to 1 of 2 phones. Home Assistant doesn't know Old Phone "
                                       "(notify.mobile_app_old_phone) any more. Open the Home Assistant app on that "
                                       "phone, or pick another phone.")
        self.assertIn("Old Phone", self.ok("GET", "/api/push")["last_error"]["text"])
        self.enable(targets=["mobile_app_old_phone"])
        r = self.ok("POST", "/api/push/test", {})
        self.assertFalse(r["ok"])
        self.assertTrue(r["message"].startswith("Home Assistant doesn't know Old Phone"))

    def test_test_button_outside_home_assistant(self):
        self.enable()
        with mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": ""}):
            status, data, _ = self.call("POST", "/api/push/test", {})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], ha.NOT_IN_HA)
        self.assertEqual(self.ha.requests, [])


class CheckTest(PushBase):
    def setUp(self):
        super().setUp()
        self.user = self.mail()
        self.ok("POST", "/api/vips", {"name": "Dana", "email": "dana@example.com", "area_id": "personal"})

    def test_alerts_once_and_query_is_metadata_since_last_check(self):
        self.enable()
        self.user["threads"] = [self.thread("t1", "Dana Lee <dana@example.com>", "Contract", BODY),
                                self.thread("t2", "Bo <bo@x.com>", "Lunch?", "free?"),
                                self.thread("t3", "Bo <bo@x.com>", "Old urgent: today, due", ago=3600)]
        self.fake.requests.clear()
        r = self.app.notifier.check()
        self.assertEqual(r, {"found": 1, "sent": 1, "held": 0})
        self.assertEqual(len(self.ha.sent), 1)
        name, body = self.ha.sent[0]
        self.assertEqual((name, body["title"], body["message"]), ("mobile_app_iphone", "VIP message", "Dana Lee: Contract"))
        self.assertNotIn("SECRET", json.dumps(self.ha.sent))
        api = self.fake.api_requests()
        listing = [q for m, p, q in api if p == "/gmail/v1/users/me/threads"]
        self.assertEqual(len(listing), 1)
        self.assertRegex(listing[0]["q"][0], r"^in:inbox .* after:\d+$")
        for m, p, q in api:
            self.assertEqual(m, "GET")
            if p.startswith("/gmail/v1/users/me/threads/"):
                self.assertEqual(q["format"], ["metadata"])
        # the next check: nothing new
        self.app.notifier.check()
        self.assertEqual(len(self.ha.sent), 1)
        st = self.app.store.push_state()
        self.assertEqual(len(st["seen"]), 1)
        self.assertNotIn("Contract", json.dumps(st))

    def test_rate_limit_and_batch(self):
        self.enable(max_per_hour=3)
        self.user["threads"] = [self.thread(f"t{i}", f"Bo{i} <bo{i}@x.com>", f"Urgent {i}: today, due") for i in range(5)]
        self.app.notifier.check()
        msgs = [b["message"] for _, b in self.ha.sent]
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[-1], "3 more urgent messages. Open Callsheet to see them.")
        # two more within the hour: held
        self.user["threads"] = [self.thread(f"u{i}", f"Cy{i} <cy{i}@x.com>", f"Urgent {i}: today, due") for i in range(2)]
        st = self.app.store.push_state()
        st["last_check"] -= 600
        self.app.store.set_push_state(st)
        r = self.app.notifier.check()
        self.assertEqual(r, {"found": 2, "sent": 0, "held": 2})
        self.assertEqual(len(self.ha.sent), 3)
        # an hour later: one batch for the held two
        st = self.app.store.push_state()
        st["sent"] = [t - 3601 for t in st["sent"]]
        self.app.store.set_push_state(st)
        self.app.notifier.check()
        self.assertEqual(self.ha.sent[-1][1]["message"], "2 more urgent messages. Open Callsheet to see them.")

    def test_seen_ids_bounded(self):
        self.app.store.set_push_state({"seen": [f"x{i}" for i in range(push.SEEN_KEEP)], "last_check": time.time() - 600})
        self.enable()
        self.user["threads"] = [self.thread("t1", "Dana <dana@example.com>", "Hi")]
        self.app.notifier.check()
        seen = self.app.store.push_state()["seen"]
        self.assertEqual(len(seen), push.SEEN_KEEP)
        self.assertTrue(seen[-1].startswith("m:"))

    def test_no_writer_no_model_no_sync(self):
        self.app.store.update_writer({"provider": "local", "local": {"base_url": "http://127.0.0.1:9/v1", "model": "m"}})
        self.enable()
        self.user["threads"] = [self.thread("t1", "Dana <dana@example.com>", "Hi")]
        boom = AssertionError("writer or model used")
        with mock.patch.object(sync.Syncer, "run_now", side_effect=boom), \
                mock.patch.object(sync.Syncer, "_write", side_effect=boom), \
                mock.patch.object(writer._opener, "open", side_effect=boom), \
                mock.patch.object(writer, "_sdk", side_effect=boom), \
                mock.patch.object(writer, "compose_system", side_effect=boom):
            r = self.app.notifier.check()
        self.assertEqual(r["sent"], 1)
        self.assertIsNone(self.app.store.cached_brief())
        self.assertEqual(self.app.store.last_sync()["at"], None)
        src = (APP / "push.py").read_text()
        self.assertEqual(set(re.findall(r"writer\.(\w+)", src)), {"states_decision"})

    def test_skipped_while_a_sync_runs(self):
        self.enable()
        self.user["threads"] = [self.thread("t1", "Dana <dana@example.com>", "Hi")]
        with self.app.syncer.lock:
            self.assertIsNone(self.app.notifier.check())
        self.assertEqual(self.ha.sent, [])

    def test_after_brief_summary_and_marker(self):
        self.enable(at_call_times=True)
        self.user["threads"] = [self.thread("t1", "Dana Lee <dana@example.com>", "Budget sign-off", BODY)]
        self.ok("POST", "/api/sync")
        self.app.store.set_push_state({"held": 4, "last_check": 1})
        self.app.notifier.after_brief()
        st = self.app.store.push_state()
        self.assertEqual(st["held"], 0)
        self.assertGreater(st["last_check"], time.time() - 60)
        self.assertEqual(len(self.ha.sent), 1)
        body = self.ha.sent[0][1]
        self.assertTrue(body["message"].startswith("1 to reply"), body["message"])
        self.assertIn("Top: Dana Lee, Budget sign-off", body["message"])
        self.assertNotIn("SECRET", json.dumps(body))
        self.assertEqual(body["data"]["tag"], "callsheet-brief")
        # off: no summary
        self.enable(at_call_times=False)
        self.app.notifier.after_brief()
        self.assertEqual(len(self.ha.sent), 1)

    def test_token_never_in_logs_or_responses(self):
        self.enable(targets=["mobile_app_iphone", "mobile_app_gone"], at_call_times=True)
        self.user["threads"] = [self.thread("t1", "Dana <dana@example.com>", "Hi")]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            self.app.notifier.check()
            self.ok("POST", "/api/push/test", {})
            self.ok("GET", "/api/push/targets")
            texts = [self.call("GET", p)[2] for p in ("/api/push", "/api/state")]
            self.ha.fail = 401
            texts.append(self.call("GET", "/api/push/targets")[2])
            texts.append(self.call("POST", "/api/push/test", {})[2])
        blob = "\n".join(self.logs + texts + [err.getvalue(), json.dumps(self.app.store.push_state())])
        self.assertNotIn(fake_ha.TOKEN, blob)
        self.assertTrue(any("Callsheet push" in x for x in self.logs))
        self.assertNotIn("Dana", "\n".join(self.logs))   # counts only
        db = (pathlib.Path(self.tmp.name) / "callsheet.db").read_bytes()
        self.assertNotIn(fake_ha.TOKEN.encode(), db)


class SlackVipTest(PushBase):
    def setUp(self):
        super().setUp()
        import fake_slack
        self.sl = fake_slack.FakeSlack()
        env = mock.patch.dict(os.environ, self.sl.env())
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.sl.close)
        self.app.syncer.sleep = self.app.notifier.sleep = lambda s: None
        self.ME = fake_slack.ME
        self.ok("POST", "/api/slack/connect", {"area_id": "personal", "token": fake_slack.TOKEN})

    def test_one_history_per_vip_dm(self):
        now = time.time()
        self.sl.user("UTOM", "Tomás Berg", "tomas@northwind.example")
        self.sl.user("UBOB", "Bob", "bob@northwind.example")
        self.sl.dm("D1", "UTOM")
        self.sl.dm("D2", "UBOB")
        self.sl.post("D1", "UTOM", "earlier", now - 7200)
        self.sl.post("D2", "UBOB", "earlier", now - 7200)
        self.ok("POST", "/api/vips", {"name": "Tomás Berg", "email": "tomas@northwind.example", "area_id": "personal"})
        self.ok("POST", "/api/sync")
        ws = self.app.store.slack_secret(self.app.store.slack_workspaces()[0]["id"])
        self.assertEqual(ws["state"]["dms"], {"UTOM": "D1", "UBOB": "D2"})
        self.enable()
        self.sl.post("D1", "UTOM", BODY, now - 30)
        self.sl.post("D1", "UTOM", "and another " + BODY, now - 20)
        self.sl.post("D2", "UBOB", "not a VIP", now - 20)
        self.sl.requests.clear()
        self.app.notifier.check()
        self.assertEqual([(r[1], r[2]["channel"]) for r in self.sl.requests], [("conversations.history", "D1")])
        self.assertEqual(len(self.ha.sent), 1)
        self.assertEqual(self.ha.sent[0][1]["message"], "Tomás Berg sent you a Slack message")
        self.assertNotIn("SECRET", json.dumps(self.ha.sent))
        self.app.notifier.check()
        self.assertEqual(len(self.ha.sent), 1)


if __name__ == "__main__":
    unittest.main()
