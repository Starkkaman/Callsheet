"""0.6.2 chat on a busy model server: waiting for Callsheet's own writer (LOCAL_LOCK) isn't answer time, a slow
first byte shows "Waiting for the model server", a model load is noted once, a timeout ends with a plain message
and a retry flag ("Try again" in the page), the 300 s default and its migration, and the page's polling.
Fake model server only (fake_ai.py)."""
import pathlib
import re
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "callsheet" / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import chat  # noqa: E402
import writer  # noqa: E402
from test_chat_060 import Base, call, say, tool_msg  # noqa: E402


def until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.01)
    return pred()


class BusyServerTest(Base):
    def pending(self, cid):
        return self.chat.view(cid)["pending"]

    def test_slow_first_byte_shows_the_busy_server_text(self):
        self.local.delay = 0.6
        self.local.messages = [say("Late but fine.")]
        with mock.patch.object(chat, "SLOW_AFTER", 0.2):
            conv = self.chat.ask(None, "Q")
            seen = until(lambda: (self.pending(conv["id"]) or {}).get("text") == chat.SLOW)
            self.assertTrue(seen, "no busy-server text while the server was slow")
            self.assertEqual(chat.SLOW, "Waiting for the model server (it's busy)…")
            self.assertTrue(self.chat.wait(conv["id"], 10))
        last = self.chat.view(conv["id"])["messages"][-1]
        self.assertEqual(last["text"], "Late but fine.")
        line = [x for x in self.logs if x.startswith("Callsheet chat: local (native)")][0]
        self.assertRegex(line, r"server \d+\.\ds to start answering \+ \d+\.\ds$")
        first = float(re.search(r"server (\d+\.\d)s to start", line).group(1))
        self.assertGreaterEqual(first, 0.5)

    def test_waiting_for_the_writer_is_shown_and_not_counted(self):
        self.local.messages = [tool_msg(call("c1", "stats", {})), say("Done.")]
        with mock.patch.object(chat, "TURN_DEADLINE", 0.4):
            with writer.LOCAL_LOCK:  # the brief writer's request in flight
                conv = self.chat.ask(None, "Q")
                p = until(lambda: (self.pending(conv["id"]) or {}).get("state") == "waiting" and self.pending(conv["id"]))
                self.assertEqual((p["state"], p["text"]), ("waiting", chat.BUSY_BRIEF))
                self.assertEqual(p["text"], "Callsheet is writing your brief, one moment.")
                time.sleep(0.7)  # longer than the whole turn deadline
                self.assertEqual(self.local.chats(), [])
            self.assertTrue(self.chat.wait(conv["id"], 10))
        last = self.chat.view(conv["id"])["messages"][-1]
        self.assertEqual(last.get("text"), "Done.", last)  # the wait didn't eat the answer time
        self.assertTrue(any("waited" in x and "for Callsheet's own requests" in x for x in self.logs))

    def test_stop_while_waiting_for_the_writer(self):
        with writer.LOCAL_LOCK:
            conv = self.chat.ask(None, "Q")
            until(lambda: (self.pending(conv["id"]) or {}).get("state") == "waiting")
            self.chat.stop(conv["id"])
            self.assertTrue(self.chat.wait(conv["id"], 5))
        self.assertTrue(self.chat.view(conv["id"])["messages"][-1]["stopped"])
        self.assertEqual(self.local.chats(), [])

    def test_model_loading_is_noted_once(self):
        self.local.loading = 1
        self.local.messages = [say("Loaded.")]
        seen = []

        def nap(s):
            cid = next(iter(self.chat.jobs))
            seen.append(self.pending(cid)["text"])
        with mock.patch.object(writer, "_sleep", nap):
            conv, msg = self.ask("Q")
        self.assertEqual(seen, [chat.LOADING])
        self.assertEqual(chat.LOADING, "Loading the chat model…")
        self.assertEqual(msg["text"], "Loaded.")
        self.assertEqual(sum("loading the chat model" in x for x in self.logs), 1)

    def test_timeout_is_a_plain_message_with_retry_and_retry_works(self):
        self.store._set_meta("chat", {"local": {"timeout": 0.3}, "v062": True})  # noqa: SLF001 (shorter than any setting)
        self.local.delay = 0.8
        conv, msg = self.ask("Where is the invoice?")
        self.assertTrue(msg["retry"])
        self.assertIn("busy and didn't answer within", msg["error"])
        self.assertIn("try again", msg["error"])
        self.assertIsNone(self.chat.view(conv["id"])["pending"])  # nothing left pending: the page stops polling
        line = [x for x in self.logs if x.startswith("Callsheet chat: local failed:")][0]
        self.assertIn("timed out after 0.3 s without an answer", line)
        self.assertNotIn("invoice", line)
        # one click re-asks the same question in the same chat
        self.local.delay = 0
        self.local.messages = [say("Here it is.")]
        conv, msg = self.ask("Where is the invoice?", cid=conv["id"])
        self.assertEqual(msg["text"], "Here it is.")
        self.assertNotIn("retry", msg)

    def test_other_errors_have_no_retry(self):
        self.local.status = 401
        _, msg = self.ask("Q")
        self.assertIn("API key", msg["error"])
        self.assertNotIn("retry", msg)

    def test_secs(self):
        self.assertEqual((chat._secs(300), chat._secs(60), chat._secs(90)),  # noqa: SLF001
                         ("5 minutes", "1 minute", "90 seconds"))
        self.assertEqual(chat.TIMED_OUT.format(n="5 minutes"), "The model server was busy and didn't answer within "
                         "5 minutes. Nothing went wrong on your side: try again in a moment.")


class TimeoutSettingTest(Base):
    def test_default_and_migration(self):
        self.assertEqual(chat.LOCAL_DEFAULTS["timeout"], 300)
        self.assertEqual(chat.LOCAL_LIMITS["timeout"], (30, 600))
        self.assertEqual(chat.load_settings(self.store)["local"]["timeout"], 300)
        # saved before 0.6.2 with the old default: becomes 300; another saved value stays
        self.store._set_meta("chat", {"provider": "local", "local": {"timeout": 120}})  # noqa: SLF001
        self.assertEqual(chat.load_settings(self.store)["local"]["timeout"], 300)
        self.store._set_meta("chat", {"provider": "local", "local": {"timeout": 200}})  # noqa: SLF001
        self.assertEqual(chat.load_settings(self.store)["local"]["timeout"], 200)
        # from 0.6.2 on a 120 the owner picks stays
        chat.update_settings(self.store, {"local": {"timeout": 120}})
        self.assertEqual(chat.load_settings(self.store)["local"]["timeout"], 120)
        chat.update_settings(self.store, {"local": {"max_tokens": 4096}})
        self.assertEqual(chat.load_settings(self.store)["local"]["timeout"], 120)

    def test_writer_post_without_notify_is_unchanged(self):
        timing = {}
        res = writer._post(self.local.base, None, {"model": "qwen3-coder", "messages": []},  # noqa: SLF001
                           {"timeout": 5}, None, time.monotonic, timing=timing)
        self.assertIn("choices", res)
        self.assertEqual(set(timing), {"lock_s", "first_byte_s", "read_s"})
        self.assertFalse(writer.LOCAL_LOCK.locked())


class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        js = (ROOT / "callsheet" / "app" / "static" / "app.js").read_text()
        cls.chat = js[js.index("/* ---------- 0.6.0 chat:"):js.index("/* ---------- end 0.6.0 chat")]

    def test_try_again_button(self):
        self.assertIn("if (m.retry && i === msgs.length - 1 && q && q.role === 'user' && !chatPending())", self.chat)
        self.assertIn("'data-cretry': String(i - 1) }, 'Try again')", self.chat)
        self.assertIn("else if (d.cretry) { var rq = CH.conv && CH.conv.messages && CH.conv.messages[Number(d.cretry)]; "
                      "if (rq && rq.role === 'user') { t.disabled = true; chatAsk(rq.text); } }", self.chat)

    def test_polling_stops_when_done_or_failing(self):
        poll = self.chat[self.chat.index("function chatPoll()"):self.chat.index("function chatAsk(")]
        self.assertIn("if (!CH.conv || !CH.conv.pending) return;", poll)          # done (or an error answer): no timer
        self.assertIn("if (c.pending) { chatRefreshMsgs(); chatPoll(); }", poll)   # polls again only while pending
        self.assertIn("if (++CH.fails < 5) { chatPoll(); return; }", poll)         # then stops with the error
        self.assertIn("CH.conv.pending = null; CH.err = e.message;", poll)
        self.assertIn("p.text || 'Thinking…'", self.chat)  # the server's text (busy, loading) is what the page shows


if __name__ == "__main__":
    unittest.main()
