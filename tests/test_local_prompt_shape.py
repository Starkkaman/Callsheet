"""The local writer's system message matches the shape the LLM eval measured (P2F + schema + the rule last)."""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "callsheet" / "app"))
import writer  # noqa: E402


class LocalPromptShape(unittest.TestCase):
    def test_rule_is_last_after_the_schema(self):
        text = writer.local_system({"r1", "r2"})
        schema = "\n\nJSON schema of your answer:\n" + json.dumps(writer.build_schema({"r1", "r2"}))
        self.assertTrue(text.endswith(schema + "\n\n" + writer.LOCAL_RULE))
        self.assertTrue(text.startswith(writer.LOCAL_SYSTEM_PROMPT))

    def test_rule_matches_the_evaluated_wording(self):
        self.assertEqual(writer.LOCAL_RULE,
                         "Never write that anything has been approved, paid, sent, signed, booked, agreed or decided, "
                         "and never commit the owner to an amount, date or decision. Where a reply needs any of these, "
                         "write a [placeholder] for the owner to fill in.")
        self.assertIn(writer.LOCAL_RULE, writer.LOCAL_SYSTEM_PROMPT)  # also in the prompt itself, as evaluated


if __name__ == "__main__":
    unittest.main()
