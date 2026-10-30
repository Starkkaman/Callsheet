"""Writer fixes from the local-model evaluation (0.3.2), against fakes only: the per-request JSON schema with
replies keyed by row id (both writers), thinking off by default, fixed max_tokens, quoted history and
signatures stripped, the decision/payment post-check and its warning, the retry while the model loads, the
answer rescued from the thinking channel, counts-only logs and the new local settings."""
import datetime as dt
import json
import sys
import unittest
from unittest import mock

import test_writer  # noqa: F401  (sets sys.path for the app and the fakes)
import writer  # noqa: E402
from fake_google import thread  # noqa: E402
from test_writer import HAVE_SDK, KEY, LOCAL_KEY, WriterBase  # noqa: E402

def schema_for(sent, ids):
    """The schema a sync sends since 0.4.0: build_schema with the ask (area ids and the rules' top refs) it was
    sent with."""
    ask = writer.make_ask(sent["properties"]["areas"]["required"], sent["properties"]["top"]["required"])
    return writer.build_schema(ids, ask)


UTC = dt.timezone.utc
FLAGGED = ["I've already approved the budget.", "The money has been released.", "Signed and sent.",
           "Good news: the budget is already approved.", "We paid the invoice yesterday.",
           "Thanks Priya, I approve the budget.", "It's booked for Friday at 10."]
NOT_FLAGGED = ["[approve or decline]", "Once you approve, I'll book it.", "Can you confirm the budget?",
               "I'll book it once it's approved.", "If the money has been released, let me know.",
               "Thanks, I'll look at the budget by [day].", "It isn't approved yet.",
               "I have [approved/declined] the budget.", "Has the contract been signed?", ""]


class SchemaTest(unittest.TestCase):
    def test_no_rows(self):
        s = writer.build_schema([])
        self.assertEqual(s, {"type": "object",
                             "properties": {"summary": {"type": "string"},
                                            "replies": {"type": "object", "properties": {}, "required": [],
                                                        "additionalProperties": False}},
                             "required": ["summary", "replies"], "additionalProperties": False})

    def test_eight_rows_in_order(self):
        ids = [f"r{i}" for i in (3, 1, 8, 2, 5, 4, 7, 6)]
        rep = writer.build_schema(ids)["properties"]["replies"]
        self.assertEqual(rep["required"], ids)
        self.assertEqual(list(rep["properties"]), ids)
        self.assertTrue(all(v == {"type": "string", "minLength": 1} for v in rep["properties"].values()))
        self.assertIs(rep["additionalProperties"], False)
        self.assertEqual(writer.build_schema(set(ids))["properties"]["replies"]["required"], sorted(ids))

    def test_system_message_carries_the_schema(self):
        text = writer.local_system(["r1", "r2"])
        self.assertIn(json.dumps(writer.build_schema(["r1", "r2"])), text)
        self.assertIn("an object keyed by row id", text)
        for prompt in (writer.SYSTEM_PROMPT, text):
            self.assertNotIn("empty string", prompt)  # every flagged row must get a suggestion
            self.assertIn("Never write that anything has been approved, paid, sent, signed, booked, agreed or "
                          "decided, and never commit the owner to an amount, date or decision. Where a reply needs "
                          "any of these, write a [placeholder] for the owner to fill in.", " ".join(prompt.split()))

    def test_local_prompt_is_the_evaluated_wording(self):
        """0.3.5 on 0.4.0: the Default profile carries the evaluated (P2F) guidance and examples; the local system
        message is the composed prompt (safety block last), the request's schema, then the safety sentence again."""
        ids = ["r1", "r2"]
        ask = writer.make_ask(["personal"], ["r1", "s1"], None, "12h")
        for a in (None, ask):
            text = writer.local_system(ids, a)
            flat = " ".join(text.split())
            for part in ("Lead with what needs the owner today: the oldest wait (with its day count), then the "
                         "clashes in time order, then what's due when.",  # 0.4.1: the V3 texts
                         "Answer each question or request in the thread directly and specifically:",
                         "Acknowledge what the sender said.",
                         "keep them short and concrete.",
                         "Each reply answers only its own thread: don't bring in points from other threads.",
                         "Examples (for style only):", "Example reply. Thread:", "Example summary. Good summary:",
                         "I'll send the revised floor plan over by [day]."):
                self.assertIn(part, flat)
            self.assertIn("untrusted content", flat)
            schema = "\n\nJSON schema of your answer:\n" + json.dumps(writer.build_schema(ids, a))
            # evaluated shape: the per-request schema, then the safety sentence once more (last)
            self.assertTrue(text.endswith(schema + "\n\n" + writer.LOCAL_RULE))
            self.assertEqual(text, writer.compose_system(None, ids, a) + schema + "\n\n" + writer.LOCAL_RULE)
            # order: reply guidance at once followed by the never-claim sentence (as in P2F), examples, the safety
            # block, the schema, the safety sentence again
            self.assertIn(writer.DEFAULT_PROMPTS["drafts"] + "\n\n" + writer.AFTER_DRAFTS + "\n\n" + writer.EXAMPLES,
                          text)
            self.assertLess(text.index(writer.EXAMPLES), text.index(writer.SAFETY))
            self.assertLess(text.index(writer.SAFETY), text.index("JSON schema of your answer:"))
            # after the reply guidance, in the safety block and once more at the end
            self.assertEqual(flat.count(writer.LOCAL_RULE), 3)

    def test_claude_prompt_has_no_local_tail(self):
        """The Claude writer gets the composed prompt (safety block last), not the local schema + LOCAL_RULE tail."""
        self.assertEqual(writer.SYSTEM_PROMPT, writer.compose_system())
        self.assertNotIn("JSON schema of your answer", writer.SYSTEM_PROMPT)
        self.assertFalse(writer.SYSTEM_PROMPT.endswith(writer.LOCAL_RULE))

    def test_parse_object_and_legacy_array(self):
        ids = ["r1", "r2"]
        obj = writer.parse_output({"summary": " A  day. ", "replies": {"r1": "Yes.", "r2": "", "r9": "x"}}, ids)
        arr = writer.parse_output({"summary": "A day.", "replies": [{"id": "r1", "suggestion": "Yes."},
                                                                     {"id": "r1", "suggestion": "Dup."},
                                                                     {"id": "r9", "suggestion": "x"}, "junk"]}, ids)
        self.assertEqual(obj, {"summary": "A day.", "replies": {"r1": "Yes."}, "flagged": [], "empty": 1})
        self.assertEqual(arr, obj)
        out = writer.parse_output({"summary": "A day.", "replies": {"r1": "I've paid it.", "r2": "Sure."}}, ids)
        self.assertEqual(out["flagged"], ["r1"])
        with self.assertRaises(writer.WriterError):
            writer.parse_output({"replies": {}}, ids)


class StripTest(unittest.TestCase):
    def s(self, text, subject=None):
        return writer.strip_quoted(text, subject)

    def test_on_wrote_and_below(self):
        self.assertEqual(self.s("Yes, Friday works.\n\nOn Mon, 3 Nov 2026 at 10:00, Priya N <priya@acme.example> "
                                "wrote:\n> Can you do Friday?\n> Thanks"), "Yes, Friday works.")
        # Gmail wraps a long attribution line
        self.assertEqual(self.s("Sounds good.\n\nOn Mon, 3 Nov 2026 at 10:00, Priya Narayanan-Smith\n"
                                "<priya.narayanan-smith@acme.example> wrote:\n\nold text"), "Sounds good.")

    def test_quoted_lines(self):
        self.assertEqual(self.s("> Can you do Friday?\nYes.\n>> older\nAnd Monday too."), "Yes.\nAnd Monday too.")

    def test_original_message_header(self):
        self.assertEqual(self.s("Agreed in principle.\n\n-----Original Message-----\nFrom: Bob\nold"),
                         "Agreed in principle.")

    HEADER = ("________________________________\nFrom: Bob Jones <bob@x.example>\n"
              "Sent: Monday, November 3, 2026 10:00\nTo: Sam\nSubject: {subject}\n\nCan you confirm the budget?")

    def test_outlook_header_cut_below_a_real_reply(self):
        reply = "Yes, Friday at 10 works for me."
        self.assertEqual(self.s(reply + "\n\n" + self.HEADER.format(subject="RE: Lunch"), "RE: Lunch"), reply)
        self.assertEqual(self.s("Sounds good to me, see you then.\nFrom: Bob\nDate: today\nSent: Monday\nold"),
                         "Sounds good to me, see you then.")

    def test_outlook_header_kept_with_short_text_above(self):
        msg = "Thanks, see below\n\n" + self.HEADER.format(subject="Budget")
        self.assertEqual(self.s(msg, "RE: Budget"), msg)
        self.assertIn("Can you confirm the budget?", self.s(self.HEADER.format(subject="Budget")))

    def test_outlook_forward_kept(self):
        fwd = self.HEADER.format(subject="Budget")
        self.assertEqual(self.s(fwd, "FW: Budget"), fwd)  # header at the top, FW: subject
        long_note = "Please have a look at this one before Thursday.\n\n"
        for subject in ("FW: Budget", "Fwd: Budget", "fw:Budget", "FWD: Budget"):
            self.assertEqual(self.s(long_note + fwd, subject), (long_note + fwd).strip(), subject)
        # the header block's own Subject says FW: (message subject unknown)
        inner = long_note + self.HEADER.format(subject="Fw: Budget")
        self.assertEqual(self.s(inner), inner.strip())

    def test_gmail_forward_kept(self):
        fwd = ("FYI\n\n---------- Forwarded message ---------\nFrom: Priya N <priya@acme.example>\n"
               "Date: Mon, 3 Nov 2026 at 10:00\nSubject: Budget\nTo: Sam <me@example.com>\n\n"
               "Can you sign off the budget by Thursday?")
        self.assertEqual(self.s(fwd, "Fwd: Budget"), fwd)
        self.assertEqual(self.s(fwd), fwd)

    def test_message_text_reads_the_subject(self):
        import base64
        fwd = self.HEADER.format(subject="Budget")
        body = "Please have a look at this one before Thursday.\n\n" + fwd
        msg = {"payload": {"mimeType": "text/plain", "headers": [{"name": "Subject", "value": "FW: Budget"}],
                           "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}
        self.assertIn("Can you confirm the budget?", writer.message_text(msg))
        msg["payload"]["headers"][0]["value"] = "RE: Budget"
        self.assertEqual(writer.message_text(msg), "Please have a look at this one before Thursday.")

    def test_signature_delimiter(self):
        self.assertEqual(self.s("Can we move it to 3pm?\n\n-- \nBob Jones\nHead of Things\n+1 555 0100"),
                         "Can we move it to 3pm?")
        self.assertEqual(self.s("Ok\n--\nBob"), "Ok")

    def test_mobile_footers(self):
        self.assertEqual(self.s("On my way.\n\nSent from my iPhone"), "On my way.")
        self.assertEqual(self.s("Fine by me.\nGet Outlook for Android"), "Fine by me.")
        keep = "Sent from the office printer, see attached."
        self.assertEqual(self.s(keep), keep)

    def test_legal_disclaimer(self):
        disc = ("CONFIDENTIALITY NOTICE: This email and any attachments are confidential and intended solely for the "
                "use of the intended recipient. If you are not the intended recipient, please delete it.")
        self.assertEqual(self.s("Invoice attached.\n\n" + disc), "Invoice attached.")
        short = "Please keep this confidential, you're the intended recipient."
        self.assertEqual(self.s(short), short)

    def test_never_empty_and_conservative(self):
        only_quote = "On Mon, 3 Nov 2026, Bob <bob@x.example> wrote:\n> Lunch?"
        self.assertEqual(self.s(only_quote), only_quote)
        self.assertEqual(self.s("> just a quote"), "> just a quote")
        self.assertEqual(self.s("-- \nBob"), "-- \nBob")
        plain = "On Monday we meet at 10.\nFrom: the plan, see below.\nWe wrote: nothing yet"
        self.assertEqual(self.s(plain), plain)
        self.assertEqual(self.s(""), "")

    def test_stripped_before_the_cap(self):
        import base64
        body = "Short answer.\n\nOn Mon, Bob <b@x.example> wrote:\n" + "> " + "x" * 9000
        msg = {"payload": {"mimeType": "text/plain",
                           "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}
        self.assertEqual(writer.message_text(msg), "Short answer.")


class PostCheckTest(unittest.TestCase):
    def test_flagged(self):
        for text in FLAGGED:
            self.assertTrue(writer.states_decision(text), text)

    def test_not_flagged(self):
        for text in NOT_FLAGGED:
            self.assertFalse(writer.states_decision(text), text)
        self.assertFalse(writer.states_decision(None))


class LocalOptionsTest(unittest.TestCase):
    def test_body_thinking_off_and_on(self):
        off = writer.local_body("m", "payload", ["r1"], writer.local_options({}))
        self.assertEqual(off["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(off["max_tokens"], 4096)
        self.assertNotIn("reasoning_budget_tokens", off)
        self.assertEqual(off["response_format"]["json_schema"]["schema"], writer.build_schema(["r1"]))
        on = writer.local_body("m", "payload", ["r1"], writer.local_options({"thinking": True}))
        self.assertEqual(on["chat_template_kwargs"], {"enable_thinking": True})
        self.assertEqual((on["reasoning_budget_tokens"], on["max_tokens"]), (1536, 1536 + 4096))
        on = writer.local_body("m", "p", [], writer.local_options({"thinking": True, "reasoning_budget": 2000,
                                                                     "max_tokens": 3000}))
        self.assertEqual((on["reasoning_budget_tokens"], on["max_tokens"]), (2000, 5000))

    def test_bad_stored_values_fall_back(self):
        self.assertEqual(writer.local_options({"thinking": "yes", "max_tokens": 5, "context": True, "timeout": "x"}),
                         writer.LOCAL_DEFAULTS)

    def test_fit_strips_harder_and_logs(self):
        msgs = [{"from": "A", "date": "d", "text": "word " * 700} for _ in range(3)]
        data = {"brief": "morning", "replies": [{"id": f"r{i}", "suggest": True, "messages": [dict(m) for m in msgs]}
                                                for i in range(1, 9)]}
        ids = [f"r{i}" for i in range(1, 9)]
        logs = []
        small = writer.fit_local({"replies": []}, [], writer.local_options({}), logs.append)
        self.assertEqual((small, logs), (writer.build_payload({"replies": []}), []))
        opts = writer.local_options({"context": 8192})
        before = json.dumps(data)
        out = writer.fit_local(data, ids, opts, logs.append)
        self.assertEqual(json.dumps(data), before)  # the caller's data is unchanged
        inner = json.loads(out.split("<data>\n", 1)[1].rsplit("\n</data>", 1)[0])
        self.assertTrue(all(len(r["messages"]) == 1 for r in inner["replies"]))
        self.assertLessEqual(writer._estimate(ids, out) + writer.local_max_tokens(opts), 8192)
        self.assertEqual(len(logs), 1)
        self.assertIn("dropped 16 older messages", logs[0])
        self.assertNotIn("word", logs[0])


class LocalBase(WriterBase):
    def setUp(self):
        super().setUp()
        self.sleeps = []
        p = mock.patch.object(writer, "_sleep", self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def local_on(self, **local):
        body = {"local": {"base_url": self.loc.base, "model": "gemma-4", "api_key": LOCAL_KEY}}
        body["local"].update(local)
        body["provider"] = "local"
        return self.ok("PATCH", "/api/writer", body)


class LocalRunTest(LocalBase):
    def test_retry_once_while_the_model_loads(self):
        self.seed()
        self.local_on()
        self.loc.loading = 1
        b = self.sync_brief()
        self.assertEqual(b["summary"]["text"], "Local summary.")
        self.assertEqual(len(self.loc.chats()), 2)
        self.assertEqual(self.sleeps, [10])
        self.assertTrue(any("was loading the model; trying again in 10 s" in line for line in self.logs))

    def test_loading_twice_fails_plainly_and_other_500s_are_not_retried(self):
        self.seed()
        self.local_on()
        self.loc.loading = 2
        self.assertIn("The AI writer didn't run: The local model server was still loading the model.",
                      self.sync_brief()["heads_up"])
        self.assertEqual((len(self.loc.chats()), self.sleeps), (2, [10]))
        self.loc.status = 500  # body "nope": not a load
        self.assertIn("The AI writer didn't run: The local model server said no (HTTP 500).",
                      self.sync_brief()["heads_up"])
        self.assertEqual((len(self.loc.chats()), self.sleeps), (3, [10]))

    def test_retry_after_a_reset_connection(self):
        self.seed()
        self.local_on()
        self.loc.resets = 1
        self.assertEqual(self.sync_brief()["summary"]["text"], "Local summary.")
        self.assertEqual((len(self.loc.chats()), self.sleeps), (2, [10]))

    def test_answer_rescued_from_the_thinking_channel(self):
        self.seed()
        self.local_on()
        self.loc.contents, self.loc.finish = [""], "length"
        self.loc.reasoning = ('Let me think. {"draft": 1} The answer: {"summary": "From thinking.", '
                              '"replies": {"r1": "Thanks, [answer].", "r2": "Friday works."}}')
        b = self.sync_brief()
        self.assertEqual(b["summary"]["text"], "From thinking.")
        self.assertEqual(b["replies"][1]["suggestion"], "Friday works.")
        self.assertEqual(len(self.loc.chats()), 1)
        self.assertIn("Writer: answer was in the thinking channel", self.logs)
        # a thinking-channel object without every reply id doesn't count
        self.loc.reasoning = '{"summary": "Half.", "replies": {"r1": "x"}}'
        self.assertIn("The AI writer didn't run: The local model ran out of room; try again.",
                      self.sync_brief()["heads_up"])

    def test_logs_are_numbers_only(self):
        self.seed()
        self.local_on()
        self.loc.contents = ['{"summary": "Priya waits.", "replies": {"r1": "Budget by [day].", "r2": "Lunch ok."}}']
        self.sync_brief()
        answer = [line for line in self.logs if "local answer" in line]
        self.assertEqual(len(answer), 1)
        self.assertRegex(answer[0], r"^Callsheet writer: local answer: finish stop, 900 prompt / 120 completion "
                                    r"tokens, prompt_n 880, cache_n 20, \d+\.\ds$")
        for line in self.logs:
            for word in ("Priya", "Budget", "budget", "Lunch", "me@example.com", LOCAL_KEY, self.loc.base):
                self.assertNotIn(word, line)

    def test_empty_suggestions_are_counted(self):
        self.seed()
        self.local_on()
        # a server that ignores minLength: the blank one is dropped and counted
        self.loc.contents = ['{"summary": "Two wait.", "replies": {"r1": "  ", "r2": "Sure."}}']
        b = self.sync_brief()
        self.assertEqual([r.get("suggestion") for r in b["replies"]], [None, "Sure."])
        lw = self.ok("GET", "/api/state")["last_writer"]
        self.assertEqual((lw["ok"], lw["empty_suggestions"]), (True, 1))
        self.assertIn("1 of 2 suggestions, 1 empty", self.logs[-1])
        self.loc.contents = ['{"summary": "Two wait.", "replies": {"r1": "Yes.", "r2": "Sure."}}']
        self.sync_brief()
        self.assertEqual(self.ok("GET", "/api/state")["last_writer"]["empty_suggestions"], 0)
        self.assertIn("2 of 2 suggestions, 0 empty", self.logs[-1])
        self.loc.status = 500
        self.sync_brief()
        self.assertEqual(self.ok("GET", "/api/state")["last_writer"]["empty_suggestions"], 0)

    def test_warning_reaches_the_brief_and_is_counted(self):
        self.seed()
        self.local_on()
        self.loc.contents = ['{"summary": "Priya waits.", "replies": {"r1": "I\'ve already approved the budget.",'
                             ' "r2": "Friday works. [approve or decline]"}}']
        b = self.sync_brief()
        r1, r2 = b["replies"]
        self.assertEqual(r1["suggestion"], "I've already approved the budget.")
        self.assertEqual(r1["suggestion_warning"], writer.WARNING)
        self.assertEqual(writer.WARNING,
                         "Check this draft: it states an approval, payment or agreement you may not have made.")
        self.assertNotIn("suggestion_warning", r2)
        self.assertIn("1 suggestion flagged", self.logs[-1])
        self.assertNotIn("approved", self.logs[-1])
        self.ok("PATCH", "/api/writer", {"provider": "off"})
        b = self.ok("GET", "/api/brief")
        self.assertTrue(all("suggestion_warning" not in r and "suggestion" not in r for r in b["replies"]))

    def test_thinking_mode_request(self):
        self.seed()
        self.local_on(thinking=True, reasoning_budget=2048, max_tokens=3000, timeout=120)
        self.sync_brief()
        body = self.loc.chats()[-1]["body"]
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": True})
        self.assertEqual((body["reasoning_budget_tokens"], body["max_tokens"]), (2048, 5048))

    def test_quotes_and_signatures_not_sent(self):
        now = dt.datetime.now(UTC)
        body = ("Can you confirm Friday?\n\n-- \nPriya N\nAcme\n\nOn Mon, 3 Nov 2026, Sam <me@example.com> wrote:\n"
                "> QUOTEDHISTORY\n")
        self.seed(threads=[thread("t1", "Priya N <priya@acme.example>", "Friday", "Friday?",
                                  now - dt.timedelta(days=3), body=body)])
        self.local_on()
        self.sync_brief()
        text = self.loc.chats()[-1]["body"]["messages"][1]["content"]
        self.assertIn("Can you confirm Friday?", text)
        self.assertNotIn("QUOTEDHISTORY", text)
        self.assertNotIn("Acme", text)

    def test_settings_patch_and_validation(self):
        w = self.local_on(thinking=True, reasoning_budget=1024, max_tokens=8192, context=65536, timeout=600)
        keys = ("thinking", "reasoning_budget", "max_tokens", "context", "timeout")
        self.assertEqual({k: w["local"][k] for k in keys},
                         {"thinking": True, "reasoning_budget": 1024, "max_tokens": 8192, "context": 65536,
                          "timeout": 600})
        for bad in ({"thinking": "yes"}, {"thinking": 1}, {"max_tokens": "4096"}, {"max_tokens": 100},
                    {"max_tokens": True}, {"max_tokens": 4096.5}, {"context": 999}, {"timeout": 5},
                    {"timeout": 99999}, {"reasoning_budget": 0}, {"temperature": 1}):
            status, _, raw = self.call("PATCH", "/api/writer", {"local": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("error", json.loads(raw), bad)
        st = self.ok("GET", "/api/state")["writer"]["local"]
        self.assertEqual((st["max_tokens"], st["timeout"]), (8192, 600))  # bad patches changed nothing

    def test_server_slot(self):
        """0.3.5: the optional llama.cpp id_slot, 0..63; null (the default) means the key isn't sent."""
        self.seed()
        w = self.local_on()
        self.assertIsNone(w["local"]["id_slot"])
        self.sync_brief()
        self.assertNotIn("id_slot", self.loc.chats()[-1]["body"])
        for bad in (-1, 64, 1.5, "3", True):
            status, _, raw = self.call("PATCH", "/api/writer", {"local": {"id_slot": bad}})
            self.assertEqual(status, 400, bad)
            self.assertIn("server slot", json.loads(raw)["error"], bad)
        for good in (0, 63, 5):
            self.assertEqual(self.ok("PATCH", "/api/writer", {"local": {"id_slot": good}})["local"]["id_slot"], good)
        self.sync_brief()
        self.assertEqual(self.loc.chats()[-1]["body"]["id_slot"], 5)
        self.assertIsNone(self.ok("PATCH", "/api/writer", {"local": {"id_slot": None}})["local"]["id_slot"])
        self.sync_brief()
        self.assertNotIn("id_slot", self.loc.chats()[-1]["body"])
        self.assertIsNone(writer.local_options({"id_slot": 64})["id_slot"])  # a bad stored value isn't sent


@unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
class ClaudeObjectTest(WriterBase):
    def test_object_and_legacy_array_and_warning(self):
        self.seed()
        self.ok("PATCH", "/api/writer", {"provider": "claude", "claude": {"api_key": KEY}})
        self.ant.output = {"summary": "Busy.", "replies": {"r1": "The money has been released.", "r2": "Sure."}}
        b = self.sync_brief()
        self.assertEqual([r.get("suggestion") for r in b["replies"]], ["The money has been released.", "Sure."])
        self.assertEqual(b["replies"][0]["suggestion_warning"], writer.WARNING)
        self.assertNotIn("suggestion_warning", b["replies"][1])
        body = self.ant.messages()[-1]["body"]
        self.assertEqual(body["output_config"]["format"]["schema"],
                         schema_for(body["output_config"]["format"]["schema"], ["r1", "r2"]))
        self.assertEqual((body["max_tokens"], body["fallbacks"], body["output_config"]["effort"]),
                         (16000, "default", "medium"))
        self.ant.output = {"summary": "Busy.", "replies": [{"id": "r2", "suggestion": "Legacy."}]}
        b = self.sync_brief()
        self.assertEqual([r.get("suggestion") for r in b["replies"]], [None, "Legacy."])
        self.assertEqual(self.ok("GET", "/api/state")["last_writer"]["empty_suggestions"], 1)  # r1 missing
        self.assertIn("1 of 2 suggestions, 1 empty", self.logs[-1])


if __name__ == "__main__":
    unittest.main()
