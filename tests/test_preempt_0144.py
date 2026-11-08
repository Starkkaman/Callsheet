"""0.14.4: Chat preempts background work; fakes only.

- A Chat request that needs the model while a background request (sorter, reader, picker) holds LOCAL_LOCK cuts it:
  writer.preempt_background shuts its connection (mid-prefill, before any answer byte, and mid-generation), the fake
  llama-server sees the client go and stops within about a second, and Chat takes the lock within about a second.
- The background caller gets Busy("preempted"): its items stay queued, are retried later and stored once, and count
  as "preempted" (per kind and day) in counts-only logs and "Copy the numbers". An item cut PREEMPT_NIGHT times waits
  for the night windows; a busy-chat day still clears its backlog by the end of the night window.
- Background requests stream (stream: true): llama-server stops a streamed request when its client goes.
- The brief writer is never cut: Chat waits for it and shows the "Writing the brief…" note.
- The output estimate is the 80th percentile of the measured output per card, so the 8 s cap holds for most batches.
"""
import datetime as dt
import json
import pathlib
import select
import socket
import sys
import threading
import time
import types
import unittest
import http.server
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"))

import chat  # noqa: E402
import test_chat_060 as t60  # noqa: E402
import test_picker_0130 as t130  # noqa: E402
import test_reader_0120 as t120  # noqa: E402
import test_resident_0141 as t141  # noqa: E402
import test_sorting_084 as t84  # noqa: E402
import triage  # noqa: E402
import writer  # noqa: E402
from fake_ai import FakeLocal  # noqa: E402


def clean(tc):
    writer.reset_prefill()
    writer.chat_forget()
    writer.reset_preempt()
    tc.addCleanup(writer.reset_prefill)
    tc.addCleanup(writer.chat_forget)
    tc.addCleanup(writer.reset_preempt)


def _gone(conn):
    """True when the client closed its side (what llama-server checks while it works on a request)."""
    try:
        r, _, _ = select.select([conn], [], [], 0)
        return bool(r) and conn.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


class SlowServer:
    """A llama-server stand-in: `prefill` seconds with no answer bytes, then `tokens` pieces `gap` seconds apart,
    streamed (SSE) when the body asks for it, else one JSON answer at the end. Like llama-server it notices a client
    that went away while it works and stops; .reqs records each request: phase ("done", "prefill", "generation"),
    pieces sent, and when it stopped (time.monotonic)."""

    def __init__(self, prefill=0.0, tokens=20, gap=0.02, content=None):
        self.prefill, self.tokens, self.gap = prefill, tokens, gap
        self.content = content or (lambda body: LONG)
        self.reqs = []
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                rec = {"stream": bool(body.get("stream")), "phase": None, "sent": 0, "body": body,
                       "start": time.monotonic(), "end": None}
                with fake.lock:
                    fake.reqs.append(rec)
                self.close_connection = True
                try:
                    fake.serve(self, rec, body)
                finally:
                    rec["end"] = time.monotonic()

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def serve(self, h, rec, body):
        end = time.monotonic() + self.prefill
        while time.monotonic() < end:                         # prefill: nothing sent yet
            if _gone(h.connection):
                rec["phase"] = "prefill"
                return
            time.sleep(0.01)
        text = self.content(body)
        k = max(1, self.tokens)
        size = -(-len(text) // k)
        pieces = [text[i:i + size] for i in range(0, len(text), size)] or [""]
        usage = {"prompt_tokens": 1000, "completion_tokens": 40 * len(pieces)}
        timings = {"prompt_n": 1000, "prompt_ms": self.prefill * 1000 or 1.0, "cache_n": 0,
                   "predicted_n": 40 * len(pieces), "predicted_ms": max(1.0, self.gap * 1000 * len(pieces))}
        if not rec["stream"]:
            for _ in pieces:
                if _gone(h.connection):
                    rec["phase"] = "generation"
                    return
                time.sleep(self.gap)
            out = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                           "finish_reason": "stop"}], "usage": usage, "timings": timings}).encode()
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(out)))
            h.end_headers()
            h.wfile.write(out)
            rec["phase"] = "done"
            return
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.end_headers()
        try:
            for p in pieces:
                if _gone(h.connection):
                    rec["phase"] = "generation"
                    return
                h.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": p}}]}).encode()
                              + b"\n\n")
                h.wfile.flush()
                rec["sent"] += 1
                time.sleep(self.gap)
            h.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                                  "usage": usage, "timings": timings}).encode() + b"\n\ndata: [DONE]\n\n")
            h.wfile.flush()
            rec["phase"] = "done"
        except (BrokenPipeError, ConnectionResetError, OSError):
            rec["phase"] = "generation"

    def wait_phase(self, i=0, until=5.0):
        t = time.monotonic() + until
        while time.monotonic() < t:
            if len(self.reqs) > i and self.reqs[i]["end"] is not None:
                return self.reqs[i]
            time.sleep(0.01)
        return self.reqs[i] if len(self.reqs) > i else None

    def wait_request(self, n=1, until=5.0):
        t = time.monotonic() + until
        while time.monotonic() < t and len(self.reqs) < n:
            time.sleep(0.01)
        return len(self.reqs) >= n

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


LONG = json.dumps({"ok": " ".join(f"word{i}" for i in range(400))})
OK = '{"ok": true}'
BG_BODY = {"model": "m", "id_slot": 1, "messages": [{"role": "user", "content": "x" * 400}], "max_tokens": 200,
           "stream": True, "stream_options": {"include_usage": True}}
CHAT_BODY = {"model": "m", "id_slot": 0, "messages": [{"role": "user", "content": "Hi?"}], "max_tokens": 50}


def bg_post(base, out, body=None, timeout=30):
    """A background request through writer._post (a budget makes it background), in its own thread."""
    def run():
        t0 = time.monotonic()
        try:
            out["res"] = writer._post(base, None, dict(body or BG_BODY), {"timeout": timeout}, None,  # noqa: SLF001
                                      time.monotonic, notify=lambda e: None, budget=10 ** 6)
        except Exception as e:  # noqa: BLE001
            out["err"] = e
        out["secs"] = time.monotonic() - t0
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def chat_post(base, events=None):
    """Chat's request (preempt) to its own server; returns (seconds until it had the lock, the answer)."""
    timing = {}
    t0 = time.monotonic()
    res = writer._post(base, None, dict(CHAT_BODY), {"timeout": 10}, None, time.monotonic,  # noqa: SLF001
                       notify=(events.append if events is not None else (lambda e: None)), timing=timing,
                       preempt=True)
    return time.monotonic() - t0, res, timing


# --- 1. writer: the cut, mid-prefill and mid-generation ----------------------------------------------------------------
class WriterPreempt(unittest.TestCase):
    def setUp(self):
        clean(self)
        self.local = FakeLocal()
        self.addCleanup(self.local.close)

    def server(self, **kw):
        s = SlowServer(**kw)
        self.addCleanup(s.close)
        return s

    def test_mid_prefill_the_server_stops_and_chat_goes_within_1s(self):
        srv = self.server(prefill=8.0)
        out = {}
        th = bg_post(srv.base, out)
        self.assertTrue(srv.wait_request())
        time.sleep(0.3)                                                   # it is prefilling: no answer byte yet
        self.assertEqual(writer.background_in_flight(), 1)
        t_cut = time.monotonic()
        secs, res, timing = chat_post(self.local.base)
        th.join(5)
        self.assertLess(secs, 1.0)                                        # Chat had the model within a second
        self.assertEqual(res["choices"][0]["message"]["content"], self.local.contents[0])
        self.assertIsInstance(out.get("err"), writer.Busy)
        self.assertEqual(str(out["err"]), writer.PREEMPTED)
        rec = srv.wait_phase(0)
        self.assertEqual(rec["phase"], "prefill")                         # the server stopped in its prefill
        self.assertLess(rec["end"] - t_cut, 1.0)                          # ... within a second (slot free)
        self.assertEqual(writer.background_in_flight(), 0)
        v = writer.preempt_view()
        self.assertEqual(v["n"], 1)
        self.assertLess(v["acquire_s_max"], 1.0)
        self.assertFalse(writer.LOCAL_LOCK.locked())

    def test_mid_generation_streamed(self):
        srv = self.server(prefill=0.05, tokens=200, gap=0.03)             # about 6 s of output
        out = {}
        th = bg_post(srv.base, out)
        t = time.monotonic() + 5
        while time.monotonic() < t and not (srv.reqs and srv.reqs[0]["sent"] >= 5):
            time.sleep(0.01)
        self.assertGreaterEqual(srv.reqs[0]["sent"], 5)                   # it is writing its answer
        t_cut = time.monotonic()
        secs, _, _ = chat_post(self.local.base)
        th.join(5)
        self.assertLess(secs, 1.0)
        self.assertEqual(str(out.get("err")), writer.PREEMPTED)
        rec = srv.wait_phase(0)
        self.assertEqual(rec["phase"], "generation")
        self.assertLess(rec["sent"], 200)                                 # generation ended early
        self.assertLess(rec["end"] - t_cut, 1.0)
        self.assertNotIn("res", out)                                      # nothing half returned
        self.assertIsNone(writer.gen_rate(srv.base, "m"))                 # nothing learned from a cut request

    def test_non_stream_request_is_cut_too(self):
        srv = self.server(prefill=0.05, tokens=200, gap=0.03)
        out = {}
        body = {k: v for k, v in BG_BODY.items() if k not in ("stream", "stream_options")}
        th = bg_post(srv.base, out, body)
        self.assertTrue(srv.wait_request())
        time.sleep(0.5)
        secs, _, _ = chat_post(self.local.base)
        th.join(5)
        self.assertLess(secs, 1.0)
        self.assertEqual(str(out.get("err")), writer.PREEMPTED)
        self.assertEqual(srv.wait_phase(0)["phase"], "generation")        # the fake saw the client go

    def test_a_finished_answer_is_returned_not_cut(self):
        srv = self.server(prefill=0.0, tokens=3, gap=0.0, content=lambda b: OK)
        out = {}
        bg_post(srv.base, out).join(5)
        self.assertEqual(out["res"]["choices"][0]["message"]["content"], OK)
        self.assertEqual(writer.preempt_background(), 0)                  # nothing in flight any more
        real = writer._local

        def late(*a, **k):                                                # Chat comes as the answer completes
            res = real(*a, **k)
            self.assertEqual(writer.preempt_background(), 1)
            return res
        out = {}
        with mock.patch.object(writer, "_local", late):
            bg_post(srv.base, out).join(5)
        self.assertNotIn("err", out)                                      # complete: returned, stored once
        self.assertEqual(out["res"]["choices"][0]["finish_reason"], "stop")

    def test_background_requests_stream_and_the_answer_is_whole(self):
        srv = self.server(prefill=0.0, tokens=7, gap=0.0, content=lambda b: LONG)
        out = {}
        bg_post(srv.base, out).join(5)
        self.assertTrue(srv.reqs[0]["stream"])
        self.assertEqual(out["res"]["choices"][0]["message"]["content"], LONG)
        self.assertEqual(out["res"]["usage"]["completion_tokens"], 280)
        self.assertIn("predicted_ms", out["res"]["timings"])
        self.assertIsNotNone(writer.gen_rate(srv.base, "m"))             # the output pace is learned from it

    def test_the_brief_is_never_cut(self):
        srv = self.server(prefill=1.2, tokens=5, gap=0.01, content=lambda b: OK)
        out = {}

        def brief():
            out["res"] = writer._post(srv.base, None, dict(CHAT_BODY), {"timeout": 10}, None,  # noqa: SLF001
                                      time.monotonic)                     # the brief writer: no budget, no notify
        th = threading.Thread(target=brief, daemon=True)
        th.start()
        self.assertTrue(srv.wait_request())
        self.assertEqual(writer.background_in_flight(), 0)                # never registered
        events = []
        secs, _, timing = chat_post(self.local.base, events)
        th.join(5)
        self.assertEqual(srv.wait_phase(0)["phase"], "done")              # the brief finished
        self.assertIn("res", out)
        self.assertIn("lock", events)                                     # Chat waited (its note)
        self.assertGreater(secs, 0.8)
        self.assertEqual(writer.preempt_view()["n"], 0)

    def test_a_waiting_chat_cuts_a_request_that_takes_the_lock_late(self):
        writer.LOCAL_LOCK.acquire()                                       # someone else first (not background)
        srv = self.server(prefill=8.0)
        out = {}
        try:
            th = bg_post(srv.base, out)
            time.sleep(0.2)
        finally:
            writer.LOCAL_LOCK.release()
        self.assertTrue(srv.wait_request())
        secs, _, _ = chat_post(self.local.base)
        th.join(5)
        self.assertLess(secs, 1.0)
        self.assertEqual(str(out.get("err")), writer.PREEMPTED)


# --- 2. the sorter through the real writer: retried, stored once ------------------------------------------------------
def sorter_content(body):
    ids = body["response_format"]["json_schema"]["schema"]["required"]
    return json.dumps({c: t84.answer() for c in ids})


class SorterPreempt(t141.Base):
    def setUp(self):
        super().setUp()
        clean(self)
        self.t = types.SimpleNamespace(v=1000.0)
        p = mock.patch.object(writer, "_chat_clock", lambda: self.t.v)
        p.start()
        self.addCleanup(p.stop)
        self.srv = SlowServer(prefill=6.0, tokens=10, gap=0.01, content=sorter_content)
        self.addCleanup(self.srv.close)
        self.local = FakeLocal()
        self.addCleanup(self.local.close)
        self.store.update_writer({"provider": "local", "local": {"base_url": self.srv.base, "model": triage.GEMMA}})
        self.sorter._post_fn = None  # noqa: SLF001
        self.sorter._models_fn = lambda base, key: t141.router(triage.GEMMA, ["llama-server", "--ctx-size", "32768"])
        t84.paced(self.store, 1.0)

    def test_cut_then_retried_and_stored_once(self):
        self.sorter.collect_sync(self.threads(3))
        out = {}

        def run():
            out["r"] = self.sorter.run_pass()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        self.assertTrue(self.srv.wait_request())
        time.sleep(0.2)
        writer.chat_begin(self.srv.base)                                  # a chat turn on the same server
        secs, _, _ = chat_post(self.local.base)
        writer.chat_end(self.srv.base)
        th.join(10)
        self.assertLess(secs, 1.0)
        self.assertEqual(out["r"], writer.PREEMPTED)
        self.assertEqual(len(self.sorter.pending), 3)                    # every card still queued
        self.assertEqual(self.sorter.labels.count(status="ai"), 0)       # nothing half stored (rules labels only)
        sent = self.srv.reqs[0]["body"]["response_format"]["json_schema"]["schema"]["required"]
        with self.store.lock:
            cut = dict(self.store.db.execute("SELECT key, preempted FROM triage_queue").fetchall())
        self.assertEqual(sorted(cut.values()), sorted([1] * len(sent) + [0] * (3 - len(sent))))
        today = self.sorter.bg_numbers()["today"]["sorter"]
        self.assertEqual((today["preempted"], today["night_only"]), (1, 0))
        self.assertEqual(self.sorter.bg_numbers()["preempt"]["n"], 1)
        self.assertTrue(any("gave way to Chat" in x and "1 preempted today" in x for x in self.logs), self.logs)
        self.assertFalse(any("Subject" in x or "@" in x for x in self.logs if "gave way" in x))  # counts only
        # Chat idle again: the same cards go, once each
        self.t.v += writer.CHAT_IDLE_S + 1
        self.srv.prefill = 0.0
        for _ in range(5):
            if not self.sorter.pending:
                break
            self.sorter.run_pass()
            self.clock.advance(30)
        self.assertEqual(len(self.sorter.pending), 0)
        self.assertEqual(self.sorter.labels.count(), 3)
        self.assertEqual(self.sorter.labels.count(status="ai"), 3)
        done = [r for r in self.srv.reqs if r["phase"] == "done"]
        answered = [c for r in done for c in r["body"]["response_format"]["json_schema"]["schema"]["required"]]
        self.assertEqual(len(answered), 3)                                # no double results
        self.assertTrue(all(r["stream"] for r in self.srv.reqs))


# --- 3. the reader and the picker keep their items -------------------------------------------------------------------------
class ReaderPreempt(t120.Base):
    def setUp(self):
        super().setUp()
        clean(self)

    def test_item_stays_queued_and_is_read_once(self):
        s = t120.item("1")
        self.assertTrue(self.reader.enqueue(s, "needs_you"))
        self.rpost.raises = [writer.Busy(writer.PREEMPTED)]
        self.assertEqual(self.reader.run_pass(), writer.PREEMPTED)
        self.assertEqual(self.reader.pending(), 1)
        self.assertIsNone(self.reader.get(s["key"]))
        self.assertEqual(self.sorter.bg_numbers()["today"]["reader"]["preempted"], 1)
        self.assertEqual(self.reader.run_pass(), "done")
        self.assertEqual(self.reader.pending(), 0)
        self.assertEqual(len(self.rpost.calls), 2)
        self.assertIsNotNone(self.reader.get(s["key"]))

    def test_cut_three_times_waits_for_the_night(self):
        s = t120.item("1")
        self.reader.enqueue(s, "needs_you")
        self.rpost.raises = [writer.Busy(writer.PREEMPTED)] * 3
        for _ in range(3):
            self.assertEqual(self.reader.run_pass(), writer.PREEMPTED)
        self.assertEqual(self.sorter.bg_numbers()["today"]["reader"]["night_only"], 1)
        self.assertEqual(self.sorter.night_waiting()["reader"], 1)
        self.assertEqual(self.reader.run_pass(), "waiting")              # by day: not taken
        self.assertEqual(len(self.rpost.calls), 3)
        self.clock.at(t84.TUE.replace(hour=23))                          # the night window
        self.assertEqual(self.reader.run_pass(), "done")
        self.assertEqual(self.reader.pending(), 0)


class PickerPreempt(t130.Base):
    def setUp(self):
        super().setUp()
        clean(self)

    def test_cards_stay_queued_and_are_picked_once(self):
        self.feed("Hacker News", "Home Assistant item 1 " + "word " * 30, "Home Assistant news. " * 40)
        self.feed("Hacker News", "Home Assistant item 2 " + "word " * 30, "Home Assistant news. " * 40)
        self.pp.raises = [writer.Busy(writer.PREEMPTED)]
        self.assertEqual(self.picker.run_pass(), writer.PREEMPTED)
        n = self.picker.pending()
        self.assertGreaterEqual(n, 2)
        self.assertEqual(self.sorter.bg_numbers()["today"]["picker"]["preempted"], 1)
        with self.store.lock:
            cut = [r[0] for r in self.store.db.execute("SELECT preempted FROM picker_queue")]
        self.assertIn(1, cut)
        self.assertEqual(self.run_all(), "done")
        with self.store.lock:
            picked = self.store.db.execute("SELECT COUNT(*) FROM picker_items").fetchone()[0]
        self.assertEqual(picked, n)
        sent = [c for b in self.pp.calls[1:] for c in t130.blocks(b)[0]]
        self.assertEqual(len(sent), n)                                    # each card answered once


# --- 4. a busy-chat day: the backlog clears in the night windows ----------------------------------------------------------
class BusyChatDay(t141.Base):
    CARD_S = 4.0

    def setUp(self):
        super().setUp()
        clean(self)
        p = mock.patch.object(writer, "_chat_clock", self.clock.mono)
        p.start()
        self.addCleanup(p.stop)
        t84.paced(self.store, 1.0, self.sorter.settings()["model"])
        self.sorter.update_settings({"ctx_size": 40960, "backfill": {"on": False}})
        self.answered = []
        self.cut = 0

    def busy_day(self, gaps, items=80):
        """12:00 to 22:00 a chat question every `gaps` seconds (cycled), then the 22:00-01:00 night window without
        Chat; the sorter's pass every 30 s. Returns (stored by 22:00, the numbers at 22:00, waiting for the night)."""
        day = t84.TUE.replace(hour=12, minute=0)
        self.clock.at(day)
        night = t84.TUE.replace(hour=22)
        end = night + dt.timedelta(hours=3)
        chats, t, i = [], day.timestamp(), 0
        while t < night.timestamp():
            chats.append(t)
            t += gaps[i % len(gaps)]
            i += 1
        base = self.sorter._server()[0]  # noqa: SLF001

        def asked_until(now):
            while chats and chats[0] <= now:
                chats.pop(0)
                writer.chat_touch(base)

        def post(body, timeout):
            ids = body["response_format"]["json_schema"]["schema"]["required"]
            start, dur = self.clock.t, self.CARD_S * len(ids)
            nxt = next((c for c in chats if start < c), None)
            if nxt is not None and nxt < start + dur:                     # a question comes mid-request: cut
                self.clock.advance(nxt - start)
                asked_until(self.clock.t)
                self.cut += 1
                raise writer.Busy(writer.PREEMPTED)
            self.clock.advance(dur)
            self.answered.extend(ids)
            return {"choices": [{"message": {"content": json.dumps({c: t84.answer() for c in ids})},
                                 "finish_reason": "stop"}], "usage": {"prompt_tokens": 500, "completion_tokens": 0}}
        self.sorter._post_fn = post  # noqa: SLF001
        self.sorter.collect_sync(self.threads(items))
        by_day = None
        while self.clock.t < end.timestamp():
            asked_until(self.clock.t)
            if by_day is None and self.clock.t >= night.timestamp():
                by_day = (self.sorter.labels.count(status="ai"), self.sorter.bg_numbers(),
                          self.sorter.night_waiting()["sorter"])
            self.sorter.run_pass()
            self.clock.advance(30)
        self.assertEqual(len(self.sorter.pending), 0)                     # all done within the night window
        self.assertEqual(self.sorter.labels.count(status="ai"), items)
        self.assertEqual(len(self.answered), items)                       # as many answered as labels: none twice
        self.assertEqual(self.sorter.night_waiting()["sorter"], 0)
        return by_day

    def test_a_question_every_minute_for_ten_hours(self):
        stored, numbers, _ = self.busy_day([60])                          # never 90 s idle: nothing by day
        self.assertEqual(stored, 0)
        self.assertEqual(numbers["today"].get("sorter", {}).get("preempted", 0), 0)

    def test_questions_with_pauses_cut_requests_and_the_night_clears_them(self):
        stored, numbers, waiting = self.busy_day([60, 100, 60, 100, 60, 100, 220])
        self.assertGreater(self.cut, 0)
        self.assertGreater(numbers["today"]["sorter"]["preempted"], 0)
        self.assertGreater(numbers["today"]["sorter"]["night_only"], 0)
        self.assertGreater(waiting, 0)                                    # some waited for the night
        self.assertGreater(stored, 0)                                     # some went by day, between questions
        self.assertLess(stored, 80)


# --- 5. Chat itself: cut a background request, wait for a brief --------------------------------------------------------
class ChatPreempts(t60.Base):
    def setUp(self):
        super().setUp()
        clean(self)

    def test_a_question_cuts_the_background_request(self):
        srv = SlowServer(prefill=8.0)
        self.addCleanup(srv.close)
        out = {}
        th = bg_post(srv.base, out)
        self.assertTrue(srv.wait_request())
        time.sleep(0.2)
        self.local.messages = [t60.say("Yes.")]
        t0 = time.monotonic()
        conv, _ = self.ask("Anything new?")
        th.join(5)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(str(out.get("err")), writer.PREEMPTED)
        self.assertEqual(srv.wait_phase(0)["phase"], "prefill")
        self.assertLess(self.chat.numbers()["turns"][-1].get("lock_s") or 0, 1.0)
        self.assertFalse(conv.get("brief_note"))

    def test_a_question_during_a_brief_waits_with_the_note(self):
        srv = SlowServer(prefill=1.5, tokens=3, gap=0.01, content=lambda b: OK)
        self.addCleanup(srv.close)
        syn = types.SimpleNamespace(writing={"started": 1}, lock=threading.Lock())
        c = self.make_chat(syncer=syn)
        out = {}

        def brief():
            out["res"] = writer._post(srv.base, None, dict(CHAT_BODY), {"timeout": 10}, None,  # noqa: SLF001
                                      time.monotonic)
        th = threading.Thread(target=brief, daemon=True)
        th.start()
        self.assertTrue(srv.wait_request())
        self.local.messages = [t60.say("Yes.")]
        conv, _ = self.ask("Anything new?", c=c)
        th.join(5)
        self.assertIn("res", out)                                         # the brief was never cut
        self.assertEqual(srv.wait_phase(0)["phase"], "done")
        self.assertTrue(conv["brief_note"])                               # "Writing the brief…"
        self.assertEqual(writer.preempt_view()["n"], 0)
        self.assertEqual(chat.BRIEF_NOTE, "Writing the brief — answers may be slow for a minute.")


# --- 6. the output estimate -------------------------------------------------------------------------------------------------
class HonestOutput(unittest.TestCase):
    def setUp(self):
        clean(self)

    def test_the_80th_percentile_of_the_output_per_card(self):
        base = "http://127.0.0.1:9/v1"
        for n in (100, 120, 140, 160, 300):                               # answers vary: one long one
            writer.learn_output(base, "m", "sorter", 1, {"usage": {"completion_tokens": n}})
        self.assertEqual(writer.out_rate(base, "m", "sorter"), 160)       # not the median (140)
        self.assertEqual(writer.upper([]), None)
        self.assertEqual(writer.upper([5.0]), 5.0)

    def test_the_cap_holds_for_most_batches(self):
        """Batches sized by the learned numbers stay under 8 s in at least 80% of cases on a varied output."""
        import random
        rnd = random.Random(7)
        base = "http://127.0.0.1:9/v1"
        outs = [rnd.choice((90, 110, 130, 150, 170, 200)) for _ in range(writer.PREFILL_KEEP)]
        for n in outs:
            writer.learn_output(base, "m", "sorter", 1, {"usage": {"completion_tokens": n}})
        writer.learn_prefill(base, "m", {}, {"timings": {"predicted_n": 300, "predicted_ms": 10000.0}})  # 30/s
        per, gen = writer.out_rate(base, "m", "sorter"), writer.gen_rate(base, "m")
        ok = 0
        for _ in range(200):
            cards = 1
            while (cards + 1) * per / gen + 1.0 <= writer.BG_DAY_S:      # 1 s of prefill
                cards += 1
            real = sum(rnd.choice((90, 110, 130, 150, 170, 200)) for _ in range(cards)) / gen + 1.0
            ok += real <= writer.BG_DAY_S
        self.assertGreaterEqual(ok / 200, 0.8)


if __name__ == "__main__":
    unittest.main()
