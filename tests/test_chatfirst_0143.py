"""0.14.3: Chat first, the whole background request's time, and a follow-up prompt that starts with the last one;
fakes only.

- writer learns the output pace (timings.predicted_n / predicted_ms) and the output tokens per card or item; a
  background request's cap is its expected total time (prefill + output): about 8 s by day, 20 s at night.
- While Chat is active on the server (a question waiting or being answered, or answered less than 90 s ago),
  background requests start nothing: writer._post refuses them (Busy "chat") before and after the lock, and the
  sorter's, the reader's and the picker's passes stop ("chat"). Chat waits for at most the one already in flight.
- Every earlier question goes with the time note it was sent with, and the history's whole/short line moves only
  when over its turns or tokens, so a follow-up's prompt is the last prompt plus the new turn.
"""
import pathlib
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "callsheet" / "app"))

import chat  # noqa: E402
import test_chat_060 as t60  # noqa: E402
import test_timecap_0142 as t142  # noqa: E402
import writer  # noqa: E402

BASE_URL = "http://127.0.0.1:9"   # never contacted


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def clean(tc):
    writer.reset_prefill()
    writer.chat_forget()
    tc.addCleanup(writer.reset_prefill)
    tc.addCleanup(writer.chat_forget)


# --- 1. the output pace and the whole request's time ------------------------------------------------------------------------
class GenPace(unittest.TestCase):
    def setUp(self):
        clean(self)

    def test_learns_the_output_pace(self):
        self.assertIsNone(writer.gen_rate(BASE_URL, "m"))
        res = {"timings": {"prompt_n": 10, "prompt_ms": 5, "predicted_n": 300, "predicted_ms": 12000.0}}
        smp = writer.learn_prefill(BASE_URL + "/", "m", {}, res)
        self.assertAlmostEqual(smp["gen"], 25.0)
        self.assertAlmostEqual(writer.gen_rate(BASE_URL, "m"), 25.0)
        writer.learn_prefill(BASE_URL, "m", {}, {"timings": {"predicted_n": 5, "predicted_ms": 1000.0}})
        self.assertAlmostEqual(writer.gen_rate(BASE_URL, "m"), 25.0)          # a few tokens teach nothing
        self.assertEqual(writer.prefill_view()["m"]["gen_tps"], 25.0)

    def test_output_tokens_per_unit(self):
        self.assertEqual(writer.learn_output(BASE_URL, "m", "sorter", 3, {"usage": {"completion_tokens": 450}}), 150)
        self.assertEqual(writer.out_rate(BASE_URL, "m", "sorter"), 150)
        self.assertIsNone(writer.out_rate(BASE_URL, "m", "reader"))
        self.assertIsNone(writer.learn_output(BASE_URL, "m", "reader", 1, {"usage": {"completion_tokens": 3}}))
        self.assertIsNone(writer.learn_output(BASE_URL, "m", "reader", 0, {"usage": {"completion_tokens": 300}}))

    def test_the_cap_counts_the_output(self):
        self.assertEqual((writer.BG_DAY_S, writer.BG_NIGHT_S), (8.0, 20.0))
        self.assertEqual(writer.cap_tokens(200, 1.0, 8.0, out_tokens=100, gen_tps=25), 800)   # 8 s - 4 s output
        self.assertEqual(writer.cap_tokens(200, 1.0, 20.0, out_tokens=100, gen_tps=25), 3200)
        self.assertEqual(writer.cap_tokens(200, 1.0, 8.0, out_tokens=900, gen_tps=25),        # output over 8 s:
                         int(writer.PREFILL_FLOOR_S * 200))                                     # the floor
        body = {"messages": [{"role": "user", "content": "x" * int(400 * writer.CHARS_PER_TOKEN)}]}
        self.assertAlmostEqual(writer.request_secs(body, 200, 1.0, 100, 25), 6.0, delta=0.1)


# --- 2. Chat first ----------------------------------------------------------------------------------------------------------
def answer():
    return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


class WriterGate(unittest.TestCase):
    BODY = {"model": "m", "messages": [{"role": "user", "content": "x"}], "max_tokens": 10}

    def setUp(self):
        clean(self)
        self.t = FakeClock()
        p = mock.patch.object(writer, "_chat_clock", self.t)
        p.start()
        self.addCleanup(p.stop)

    def post(self, **kw):
        return writer._post(BASE_URL, None, dict(self.BODY), {"timeout": 5}, None, time.monotonic,  # noqa: SLF001
                            **kw)

    def test_active_until_90_s_after_the_last_turn(self):
        self.assertFalse(writer.chat_active(BASE_URL))
        writer.chat_touch(BASE_URL + "/")                                   # asked
        self.assertTrue(writer.chat_active(BASE_URL))
        writer.chat_begin(BASE_URL)                                         # answered for a long time
        self.t.t += 600
        self.assertTrue(writer.chat_active(BASE_URL))
        writer.chat_end(BASE_URL)
        self.t.t += writer.CHAT_IDLE_S - 1
        self.assertTrue(writer.chat_active(BASE_URL))
        self.assertAlmostEqual(writer.chat_idle_in(BASE_URL), 1.0)
        self.t.t += 2
        self.assertFalse(writer.chat_active(BASE_URL))
        self.assertFalse(writer.chat_active("http://other:8080/v1"))         # per server
        self.assertFalse(writer.chat_active(""))

    def test_background_is_refused_before_the_lock(self):
        writer.chat_touch(BASE_URL)
        with mock.patch.object(writer, "_local", side_effect=AssertionError("sent")) as loc:
            with self.assertRaises(writer.Busy) as e:
                self.post(budget=10 ** 6)                                   # a budget: background work
            self.assertEqual(str(e.exception), "chat")
            self.assertFalse(writer.LOCAL_LOCK.locked())
            loc.assert_not_called()
        with mock.patch.object(writer, "_local", return_value=answer()) as loc:
            self.post()                                                     # Chat and the brief writer: no budget
            self.post(budget=10 ** 6, background=False)
            self.assertEqual(loc.call_count, 2)
            self.t.t += writer.CHAT_IDLE_S + 1
            self.post(budget=10 ** 6)                                       # idle 90 s: background again
            self.assertEqual(loc.call_count, 3)

    def test_a_waiting_background_request_lets_chat_go_first(self):
        out = {}
        writer.LOCAL_LOCK.acquire()                                         # one request in flight
        try:
            with mock.patch.object(writer, "_local", return_value=answer()) as loc:
                def bg():
                    try:
                        self.post(budget=10 ** 6, notify=lambda e: None)
                        out["r"] = "sent"
                    except writer.Busy as e:
                        out["r"] = str(e)
                th = threading.Thread(target=bg)
                th.start()
                time.sleep(0.3)                                             # it waits for the lock
                writer.chat_begin(BASE_URL)                                 # then a chat question comes
                writer.LOCAL_LOCK.release()
                th.join(5)
                self.assertEqual(out.get("r"), "chat")
                loc.assert_not_called()
                self.assertTrue(writer.LOCAL_LOCK.acquire(timeout=1))       # and it let the lock go
        finally:
            writer.LOCAL_LOCK.release()


class ChatMarks(t60.Base):
    def setUp(self):
        super().setUp()
        clean(self)

    def test_a_question_marks_its_server_and_close_forgets(self):
        self.assertFalse(writer.chat_active(self.local.base))
        self.local.messages = [t60.say("One.")]
        self.ask("First?")
        self.assertTrue(writer.chat_active(self.local.base))               # answered just now: idle < 90 s
        self.assertGreater(writer.chat_idle_in(self.local.base), 80)
        self.chat.close()
        self.assertFalse(writer.chat_active(self.local.base))

    def test_marked_while_answering(self):
        seen = []
        real = writer._post  # noqa: SLF001

        def spy(base, *a, **k):
            seen.append(writer.chat_active(base) and writer.chat_idle_in(base) == writer.CHAT_IDLE_S)
            return real(base, *a, **k)
        self.local.messages = [t60.say("One.")]
        with mock.patch.object(writer, "_post", spy):
            self.ask("First?")
        self.assertEqual(seen, [True])                                      # a turn in progress

    def test_a_follow_up_starts_with_the_last_prompt(self):
        self.local.messages = [t60.say("One.")]
        conv, _ = self.ask("First?")
        self.now[0] += 70
        self.local.messages = [t60.tool_msg(t60.call("c1", "search", {"q": "invoice"})), t60.say("Two.")]
        self.ask("Second?", cid=conv["id"])
        self.now[0] += 70
        self.local.messages = [t60.say("Three.")]
        self.ask("Third?", cid=conv["id"])
        b = [x["messages"] for x in self.bodies()]
        self.assertEqual(len(b), 4)
        self.assertEqual(b[1][:len(b[0])], b[0])                            # turn 2 = turn 1's prompt + more
        self.assertEqual(b[2][:len(b[1])], b[1])                            # its tool round
        self.assertEqual(b[3][:len(b[1])], b[1])                            # turn 3 = turn 2's first prompt + more
        self.assertEqual(b[3][len(b[1])]["content"], "Two.")
        self.assertIn("[Callsheet: the time now is ", b[3][1]["content"])  # earlier questions keep their time


class CutStepsInARealChat(t60.Base):
    def test_the_line_moves_at_the_oldest_turns(self):
        with mock.patch.object(chat, "HISTORY_WHOLE_MAX", 3), mock.patch.object(chat, "HISTORY_STEP", 2):
            cid, prev, steps = None, None, 0
            for i in range(7):
                self.local.messages = [t60.say(f"Answer {i}. " + " ".join(f"Point {j} of {i}." for j in range(60)))]   # > OLD_ANSWER
                conv, _ = self.ask(f"Question {i}?", cid=cid)
                cid = conv["id"]
                m = [b for b in self.bodies() if b["messages"][0]["role"] == "system"][-1]["messages"]   # the turn's
                if prev is not None and m[:len(prev)] != prev:
                    steps += 1
                    self.assertEqual(m[0], prev[0])                         # the system prompt stays
                prev = m
            self.assertEqual(steps, 2)                                      # 7 turns: two steps of 2


if __name__ == "__main__":
    unittest.main()
