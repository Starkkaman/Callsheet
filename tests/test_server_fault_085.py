"""0.8.5 server faults: garbage from the local model server (<unusedNN> tokens, <|...|> special tokens, runs of control
characters, one token over and over, an answer cut at the length limit with only junk) and llama.cpp internal errors
("Unexpected empty grammar stack after accepting piece") are a server fault: never retried, the server and model are
marked unhealthy, nothing goes to them for 15 minutes, then a real health probe (a short generation, not a one-token
canary) runs first. The writer makes the rules brief (never Claude), the sorter stops for the run, Chat answers plainly
and stores nothing of the garbage, Setup -> AI sorting shows the line with Check again. Fake servers only."""
import datetime as dt
import json
import pathlib
import sys
import unittest
from unittest import mock

import test_writer  # noqa: F401  (sets sys.path for the app and the fakes)
import fake_ai  # noqa: E402
import writer  # noqa: E402
import sync  # noqa: E402
import chat  # noqa: E402
from test_writer import KEY, LOCAL_KEY  # noqa: E402
from fake_google import thread  # noqa: E402
from test_writer_local import LocalBase  # noqa: E402
import test_chat_060 as t60  # noqa: E402

UNUSED = "<unused32>" * 400
GRAMMAR = (500, {"error": {"code": 500, "message": "Unexpected empty grammar stack after accepting piece: <unused32>",
                           "type": "server_error"}}, None)
SENTENCE = "The quick brown fox jumps over the lazy dog."
HEADS_UP = "The local model isn't healthy right now, so this brief was made by the rules."


def answer(content, finish="stop", reasoning=None, tokens=50):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return 200, {"id": "c1", "object": "chat.completion", "choices": [{"index": 0, "message": msg,
                                                                      "finish_reason": finish}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": tokens}}, None


def is_probe(req):
    return writer.PROBE_PROMPT in json.dumps((req["body"] or {}).get("messages"))


class FaultLocal(fake_ai.FakeLocal):
    """FakeLocal with .script(req) -> (status, body, headers) or None (the usual answer). .probe: the probe's answer
    (None: the usual script)."""

    def __init__(self):
        self.script = None
        self.probe = None
        super().__init__()

    def answer(self, req):
        if req["path"] == "/v1/chat/completions":
            if self.probe is not None and is_probe(req):
                return self.probe(req)
            if self.script is not None:
                r = self.script(req)
                if r is not None:
                    return r
        return super().answer(req)

    def probes(self):
        return [r for r in self.chats() if is_probe(r)]

    def work(self):
        return [r for r in self.chats() if not is_probe(r)]


def healthy_probe(_req):
    return answer(SENTENCE)


class Wall:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def wall_patch(case):
    case.wall = Wall()
    p = mock.patch.object(writer, "_wall", case.wall)
    p.start()
    case.addCleanup(p.stop)
    writer.reset_health()
    case.addCleanup(writer.reset_health)


class DetectorTest(unittest.TestCase):
    def test_rules(self):
        cases = {
            "unused": [UNUSED, "<unused32>", "  <unused7>\n", "ok <unused1> <unused2> <unused3>"],
            "special": ["<|im_end|><|im_start|><|im_end|>", "<|eot_id|>", "<|channel|>analysis<|message|>x<|end|>"],
            "control": ["Hi \x00\x01\x02\x03\x04\x05\x06\x07 there", "�" * 8, "​" * 12],
            "repeat": ["the " * 25, "Sure! " + "ok " * 30, "abc" * 25, '{"summary": "' + "na" * 40 + '"}'],
        }
        for rule, texts in cases.items():
            for t in texts:
                self.assertEqual(writer.junk_rule(t), rule, repr(t[:40]))
        fine = ["", "A calm day: stand-up at 9:30, then Priya's budget.", "Hello <unused32> world",
                '{"summary": "Local summary.", "replies": {"r1": "Thanks, [answer]."}}', "-" * 60, "=" * 40,
                "| a | b |\n|---|---|\n| 1 | 2 |", "the " * 24, "Tabs\tand\nnewlines\r\n" * 5,
                "Grüße aus München, 東京 👋🏽 👨‍👩‍👧", "ok <|im_end|>", "x \x00 y"]
        for t in fine:
            self.assertIsNone(writer.junk_rule(t), repr(t[:40]))

    def test_answers(self):
        res = lambda content, finish="stop", reasoning=None: answer(content, finish, reasoning)[1]  # noqa: E731
        self.assertEqual(writer.fault_of(res("", "length", "<unused4>" * 3)), "unused")
        self.assertEqual(writer.fault_of(res("​​", "length")), "empty_length")
        self.assertEqual(writer.fault_of(res("<unused1> \x01", "length", "")), "unused")
        self.assertIsNone(writer.fault_of(res("", "length", "")))            # a plain cut stays "ran out of room"
        self.assertIsNone(writer.fault_of(res("", "length", "Let me think about the brief.")))
        self.assertIsNone(writer.fault_of(res("​​", "stop")))
        self.assertIsNone(writer.fault_of(res('{"summary": "Fine."}')))
        call = {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
            "name": "search", "arguments": '{"q": "' + "<unused9>" * 5 + '"}'}}]}
        self.assertEqual(writer.fault_of({"choices": [{"message": call, "finish_reason": "tool_calls"}]}), "unused")
        self.assertIsNone(writer.fault_of({"choices": []}))
        self.assertIsNone(writer.fault_of("nope"))

    def test_probe_needs_real_words_and_stop(self):
        self.assertIsNone(writer.probe_result(answer(SENTENCE)[1]))
        self.assertIsNone(writer.probe_result(answer("the quick brown fox jumps over the lazy dog")[1]))
        self.assertEqual(writer.probe_result(answer("OK")[1]), "probe_words")           # a one-token canary
        self.assertEqual(writer.probe_result(answer(SENTENCE, "length")[1]), "probe_finish")
        self.assertEqual(writer.probe_result(answer("")[1]), "probe_empty")
        self.assertEqual(writer.probe_result(answer(UNUSED)[1]), "unused")
        self.assertEqual(writer.probe_result(answer("quick brown fox lazy dog " + "#" * 200)[1]), "probe_words")
        body = writer.probe_body("example-model", 1)
        self.assertGreaterEqual(body["max_tokens"], 64)
        self.assertEqual((body["id_slot"], body["chat_template_kwargs"], body["temperature"]),
                         (1, {"enable_thinking": False}, 0))
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(writer.probe_body("gpt-oss-20b")["reasoning_effort"], "low")


class WriterFaultTest(LocalBase):
    def setUp(self):
        super().setUp()
        self.loc.close()
        self.loc = FaultLocal()
        wall_patch(self)


    def test_prompt_shape_unchanged(self):
        # the writer's request body is built as before (test_local_prompt_shape pins the hashes); the probe is separate
        opts = dict(writer.LOCAL_DEFAULTS)
        body = writer.local_body("example-model", "{}", ["r1"], opts)
        self.assertNotIn(writer.PROBE_PROMPT, json.dumps(body))
        self.assertEqual(set(body), {"model", "stream", "max_tokens", "response_format", "chat_template_kwargs",
                                     "messages"})


class ChatFaultTest(t60.Base):
    def setUp(self):
        super().setUp()
        self.local.close()
        self.local = FaultLocal()
        self.addCleanup(self.local.close)
        self.store.update_writer({"local": {"base_url": self.local.base, "model": "qwen3-coder"}})
        wall_patch(self)

    def stored(self, cid):
        return "\n".join(p.read_text() for p in pathlib.Path(self.data, "chat").glob("*.json"))

    def test_unused_tokens_plain_answer_nothing_stored(self):
        self.local.messages = [t60.say("<unused32>" * 300)]
        conv, last = self.ask("When is the budget review?")
        self.assertEqual(last["error"], "The local model isn't working right now. Try again in a few minutes.")
        self.assertTrue(last.get("retry"))
        self.assertEqual(last["text"], "")
        self.assertEqual(len(self.local.chats()), 1)
        self.assertNotIn("unused32", self.stored(conv["id"]))
        self.assertTrue(any("isn't healthy (unused)" in x for x in self.logs), self.logs)
        # while unhealthy nothing is sent; the next question answers at once
        conv, last = self.ask("And the dentist?")
        self.assertEqual(last["error"], chat.UNHEALTHY)
        self.assertEqual(len(self.local.chats()), 1)

    def test_grammar_stack_500_does_not_switch_to_json(self):
        self.local.script = lambda req: GRAMMAR
        conv, last = self.ask("Q")
        self.assertEqual(last["error"], chat.UNHEALTHY)
        self.assertEqual(len(self.local.chats()), 1)        # no JSON-protocol retry, no correction retry
        self.assertFalse(any("refused tools" in x for x in self.logs))

    def test_repeated_tokens_with_a_healthy_probe_is_one_failed_answer(self):
        self.local.probe = healthy_probe
        self.local.messages = [t60.say("SPAM " * 40), t60.say("The review is tomorrow.")]
        conv, last = self.ask("Q")
        self.assertEqual(last["error"], chat.GARBAGE)
        self.assertNotIn("SPAM", self.stored(conv["id"]))
        self.assertTrue(writer.health_view()["healthy"])
        conv, last = self.ask("Q again", conv["id"])
        self.assertEqual(last["text"], "The review is tomorrow.")
        self.assertEqual(len(self.local.probes()), 1)

    def test_repeated_tokens_then_recovery(self):
        self.local.probe = lambda req: answer("Sure " * 40)
        self.local.messages = [t60.say("Sure " * 40)]
        conv, last = self.ask("Q")
        self.assertEqual(last["error"], chat.UNHEALTHY)
        self.assertNotIn("Sure Sure", self.stored(conv["id"]))
        self.wall.advance(16 * 60)
        self.local.probe = healthy_probe
        self.local.messages = [t60.say("The review is tomorrow.")]
        conv, last = self.ask("Q again", conv["id"])
        self.assertEqual(last["text"], "The review is tomorrow.")
        self.assertEqual(len(self.local.probes()), 2)   # the immediate one (failed), then the one after the backoff
        for line in self.logs:
            self.assertNotIn("Sure", line)
            self.assertNotIn("review", line)


if __name__ == "__main__":
    unittest.main()
