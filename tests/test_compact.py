import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys, unittest
from pathlib import Path
from unittest import mock
# tests/test_modules.py fails a test file without the next line
os.environ["HERDR_ENV"] = ""
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_config


class Policy(unittest.TestCase):
    def test_questions_match_compact_adviser_byte_for_byte(self):
        self.assertEqual(fc.fc_compact.QUESTIONS["done"]["criteria"]["finished"],
                         "Finished and reported, including a question, choice, or blocker fully stated and handed to whoever must act next.")
        self.assertEqual(sorted(fc.fc_compact.QUESTIONS["shape"]["criteria"]), ["coordinating", "hands_on", "unclear"])
        self.assertTrue(fc.fc_compact.QUESTIONS["done"]["instructions"].startswith("Decide whether the assistant's latest unit of work"))

    def test_score_and_floor_follow_the_curve(self):
        self.assertAlmostEqual(fc.fc_compact.score(0.8, 0.5, 0.5), 0.6)
        self.assertEqual(fc.fc_compact.score(0.8, 0.0, 0.0), 0.8)
        self.assertEqual(fc.fc_compact.floor_for(0.05), 0.9)
        self.assertEqual(fc.fc_compact.floor_for(0.5), 0.7)
        self.assertEqual(fc.fc_compact.floor_for(0.95), 0.5)
        self.assertEqual(fc.fc_compact.floor_for(None), 0.9)
        self.assertEqual(fc.fc_compact.floor_for(float("nan")), 0.9)
        self.assertTrue(fc.fc_compact.qualifies(0.7, 0.5, fc.fc_compact.PROFILES["lead"]["floors"]))
        self.assertFalse(fc.fc_compact.qualifies(0.69, 0.5, fc.fc_compact.PROFILES["lead"]["floors"]))

    def test_budget_applies_only_below_the_limit(self):
        self.assertEqual(fc.fc_compact.effective_budget(200000, 150000), 150000)
        self.assertEqual(fc.fc_compact.effective_budget(100000, 150000), 100000)
        self.assertEqual(fc.fc_compact.effective_budget(None, 150000), 150000)
        self.assertEqual(fc.fc_compact.context_pressure(75000, 200000, 150000), 0.5)
        self.assertIsNone(fc.fc_compact.context_pressure(75000, None, None))

    def test_parse_judgment_rejects_a_malformed_reply(self):
        good = {"model": "jev-latest", "usage": {"input_tokens": 10, "output_tokens": 2}, "answers": {
            "done": {"type": "choice", "choice": "finished", "probabilities": {"finished": 0.8, "not_finished": 0.1, "unclear": 0.1}, "confidence": 0.8},
            "shape": {"type": "choice", "choice": "coordinating", "probabilities": {"hands_on": 0.2, "coordinating": 0.7, "unclear": 0.1}, "confidence": 0.7}}}
        j = fc.fc_compact.parse_judgment(good)
        self.assertEqual((j["done"]["choice"], j["input_tokens"]), ("finished", 10))
        for bad in ({}, {**good, "model": 123}, {**good, "answers": {"done": good["answers"]["done"]}},
                    {**good, "answers": {**good["answers"], "done": {**good["answers"]["done"], "probabilities": {"finished": 0.5}}}}):
            with self.assertRaises(fc.fc_compact.JudgeError) as c: fc.fc_compact.parse_judgment(bad)
            self.assertEqual(c.exception.kind, "response")

    def test_error_kind_classifies_failures(self):
        kind = fc.fc_compact.error_kind
        self.assertEqual(kind(RuntimeError("HTTP 401 unauthorized")), "authentication")
        self.assertEqual(kind(RuntimeError("HTTP 403 forbidden")), "authentication")
        self.assertEqual(kind(RuntimeError("HTTP 402 payment")), "billing")
        self.assertEqual(kind(RuntimeError("PaymentRequired")), "billing")
        self.assertEqual(kind(RuntimeError("HTTP 429 slow down")), "rate-limit")
        self.assertEqual(kind(RuntimeError("HTTP 503 unavailable")), "server")
        self.assertEqual(kind(RuntimeError("the read timed out")), "timeout")
        self.assertEqual(kind(TimeoutError()), "timeout")
        self.assertEqual(kind(OSError("refused")), "network")
        self.assertEqual(kind(fc.fc_compact.JudgeError("input")), "input")
        self.assertEqual(kind(ValueError("odd")), "response")

    def test_backoff_doubles_and_caps(self):
        self.assertEqual(fc.fc_compact.backoff(0, 1000.0), (1, 1010.0))
        self.assertEqual(fc.fc_compact.backoff(5, 1000.0), (6, 1300.0))
        self.assertEqual(fc.fc_compact.backoff(6, 1000.0), (6, 1300.0))

    def test_cooldown_reasons(self):
        self.assertEqual(fc.fc_compact.cooldown_reason({"retry_after": 2000.0}, 90000, 5, 1000.0), "TypeSafe backoff")
        self.assertEqual(fc.fc_compact.cooldown_reason({"baseline": 50000}, 60000, 5, 1000.0), "Settling after a compaction")
        self.assertEqual(fc.fc_compact.cooldown_reason({"baseline": 50000}, 80000, 2, 1000.0), "Settling after a compaction")
        self.assertEqual(fc.fc_compact.cooldown_reason({"last_asked_at": 950.0, "last_asked_tokens": 10000}, 90000, 5, 1000.0), "Asked recently")
        self.assertEqual(fc.fc_compact.cooldown_reason({"last_asked_at": 0.0, "last_asked_tokens": 88000}, 90000, 5, 1000.0), "Context barely grew")
        self.assertIsNone(fc.fc_compact.cooldown_reason({"baseline": 50000, "last_asked_at": 0.0, "last_asked_tokens": 0}, 80000, 3, 1000.0))
        self.assertIsNone(fc.fc_compact.cooldown_reason({}, 90000, 5, 1000.0))


import json, tempfile

ROWS = []


def rec(kind, **k):
    base = {"type": kind, "timestamp": k.pop("at", "2026-11-01T10:00:00.000Z"), "uuid": str(len(ROWS))}
    base.update(k); ROWS.append(base); return base


class Transcript(unittest.TestCase):
    def setUp(self):
        ROWS.clear()

    def write(self):
        path = Path(tempfile.mkdtemp()) / "s.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
        return path

    def test_context_tokens_is_the_last_main_assistant_usage(self):
        rec("assistant", message={"id": "m1", "usage": {"input_tokens": 100, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30, "output_tokens": 5}, "content": []})
        rec("assistant", isSidechain=True, message={"id": "s1", "usage": {"input_tokens": 9000, "output_tokens": 1}, "content": []})
        self.assertEqual(fc.fc_transcript.context_tokens(fc.fc_transcript.records(self.write())), 150)

    def test_a_transcript_without_usage_has_no_token_count(self):
        rec("user", message={"role": "user", "content": "hi"}, origin={"kind": "human"})
        self.assertIsNone(fc.fc_transcript.context_tokens(fc.fc_transcript.records(self.write())))
        self.assertEqual(fc.fc_transcript.records(Path("/nonexistent.jsonl")), [])

    def test_records_skip_lines_that_do_not_parse(self):
        path = Path(tempfile.mkdtemp()) / "s.jsonl"
        path.write_text('{"type": "user"}\nnot json\n\n[1, 2]\n{"type": "assistant"}\n')
        self.assertEqual([r["type"] for r in fc.fc_transcript.records(path)], ["user", "assistant"])

    def test_compactions_carry_the_boundary_metadata(self):
        rec("user", message={"role": "user", "content": "hi"}, origin={"kind": "human"})
        rec("system", subtype="compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 167000, "postTokens": 30000, "durationMs": 61000})
        c, = fc.fc_transcript.compactions(fc.fc_transcript.records(self.write()))
        self.assertEqual((c["trigger"], c["pre"], c["post"], c["duration_ms"], c["index"]), ("auto", 167000, 30000, 61000, 1))
        self.assertEqual(c["at"], "2026-11-01T10:00:00.000Z")

    def test_owner_prompts_exclude_hooks_summaries_and_notifications(self):
        rec("user", at="2026-11-01T10:00:01Z", message={"role": "user", "content": "focus, what next?"}, origin={"kind": "human"})
        rec("user", at="2026-11-01T10:00:02Z", message={"role": "user", "content": [{"type": "text", "text": "hook"}]}, isMeta=True)
        rec("user", at="2026-11-01T10:00:03Z", message={"role": "user", "content": "This session is being continued from a previous conversation"}, isCompactSummary=True, turnOrigin="human")
        rec("user", at="2026-11-01T10:00:04Z", message={"role": "user", "content": "<task-notification>x</task-notification>"})
        rec("user", at="2026-11-01T10:00:05Z", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]})
        rec("user", at="2026-11-01T10:00:06Z", message={"role": "user", "content": "<command-message>compose</command-message>"}, origin={"kind": "human"})
        rows = fc.fc_transcript.records(self.write())
        self.assertEqual([p["text"] for p in fc.fc_transcript.owner_prompts(rows)], ["focus, what next?", "<command-message>compose</command-message>"])
        self.assertEqual([p["text"] for p in fc.fc_transcript.owner_prompts(rows, after="2026-11-01T10:00:01Z")], ["<command-message>compose</command-message>"])

    def test_a_prompt_without_origin_counts_unless_it_is_machine_text(self):
        rec("user", message={"role": "user", "content": [{"type": "text", "text": "plain words"}]})
        rec("user", message={"role": "user", "content": "<local-command-stdout>x</local-command-stdout>"})
        rec("user", message={"role": "user", "content": "<system-reminder>x</system-reminder>"})
        rec("user", message={"role": "user", "content": "task done"}, origin={"kind": "task-notification"})
        rows = fc.fc_transcript.records(self.write())
        self.assertEqual([p["text"] for p in fc.fc_transcript.owner_prompts(rows)], ["plain words"])

    def test_exchanges_count_from_the_last_compaction(self):
        for i in range(3):
            rec("user", message={"role": "user", "content": f"q{i}"}, origin={"kind": "human"}); rec("assistant", message={"id": f"a{i}", "content": [], "usage": {"input_tokens": 1}})
        rec("system", subtype="compact_boundary", compactMetadata={"trigger": "manual", "preTokens": 1, "postTokens": 1, "durationMs": 1})
        rec("user", message={"role": "user", "content": "q"}, origin={"kind": "human"}); rec("assistant", message={"id": "b", "content": [], "usage": {"input_tokens": 1}})
        rec("user", message={"role": "user", "content": "unanswered"}, origin={"kind": "human"})
        self.assertEqual(fc.fc_transcript.exchanges_since_compaction(fc.fc_transcript.records(self.write())), 1)

    def test_exchanges_ignore_tool_results_and_meta_records(self):
        rec("user", message={"role": "user", "content": "q"}, origin={"kind": "human"})
        rec("assistant", message={"id": "a", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]})
        rec("user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]})
        rec("assistant", message={"id": "b", "content": []})
        rec("user", message={"role": "user", "content": "hook"}, isMeta=True)
        rec("assistant", isSidechain=True, message={"id": "c", "content": []})
        self.assertEqual(fc.fc_transcript.exchanges_since_compaction(fc.fc_transcript.records(self.write())), 1)

    def test_messages_merge_blocks_and_attach_tool_results(self):
        rec("user", message={"role": "user", "content": "edit it"}, origin={"kind": "human"})
        rec("assistant", message={"id": "m1", "content": [{"type": "text", "text": "Editing."}], "usage": {"input_tokens": 1}})
        rec("assistant", message={"id": "m1", "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/a.py"}}], "usage": {"input_tokens": 1}})
        rec("user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done", "is_error": False}]})
        msgs = fc.fc_transcript.messages(fc.fc_transcript.records(self.write()))
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[1]["text"], "Editing.")
        self.assertEqual(msgs[1]["toolUses"], [{"tool": "Edit", "input": {"file_path": "/a.py"}, "text": "done", "isError": False}])

    def test_messages_handle_block_results_errors_missing_results_and_sidechains(self):
        rec("user", message={"role": "user", "content": "go"}, origin={"kind": "human"})
        rec("assistant", message={"id": "m1", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}, {"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]})
        rec("assistant", isSidechain=True, message={"id": "s1", "content": [{"type": "text", "text": "inner"}]})
        rec("user", isSidechain=True, message={"role": "user", "content": "inner prompt"})
        rec("user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "boom"}, {"type": "text", "text": "!"}], "is_error": True}]})
        msgs = fc.fc_transcript.messages(fc.fc_transcript.records(self.write()))
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[0], {"role": "user", "text": "go", "toolUses": []})
        self.assertEqual(msgs[1]["toolUses"], [
            {"tool": "Bash", "input": {"command": "x"}, "text": "boom\n!", "isError": True},
            {"tool": "Read", "input": {}, "text": None, "isError": False}])

    def test_daily_input_sums_per_day(self):
        rec("assistant", at="2026-11-01T10:00:00Z", message={"id": "m1", "content": [], "usage": {"input_tokens": 10, "cache_read_input_tokens": 90}})
        rec("assistant", at="2026-11-02T10:00:00Z", message={"id": "m2", "content": [], "usage": {"input_tokens": 5}})
        rec("assistant", at="2026-11-02T11:00:00Z", isSidechain=True, message={"id": "s", "content": [], "usage": {"input_tokens": 500}})
        self.assertEqual(fc.fc_transcript.daily_input(fc.fc_transcript.records(self.write())), {"2026-11-01": 100, "2026-11-02": 5})

    def test_daily_input_counts_a_split_response_once(self):
        usage = {"input_tokens": 10, "cache_read_input_tokens": 90}
        rec("assistant", message={"id": "m1", "content": [{"type": "text", "text": "a"}], "usage": usage})
        rec("assistant", message={"id": "m1", "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}], "usage": usage})
        rec("assistant", message={"id": "m2", "content": []})
        rec("assistant", message={"id": "m2", "content": [], "usage": {"input_tokens": 7}})
        rec("assistant", message={"content": [], "usage": {"input_tokens": 3}})
        rec("assistant", message={"content": [], "usage": {"input_tokens": 3}})
        self.assertEqual(fc.fc_transcript.daily_input(fc.fc_transcript.records(self.write())), {"2026-11-01": 113})

    def test_a_subagent_prompt_is_not_an_owner_prompt(self):
        rec("user", isSidechain=True, message={"role": "user", "content": "inner prompt"})
        rec("user", message={"role": "user", "content": ""}, origin={"kind": "human"})
        self.assertEqual(fc.fc_transcript.owner_prompts(fc.fc_transcript.records(self.write())), [])

    def test_a_unicode_line_separator_inside_a_record_does_not_split_it(self):
        rec("user", message={"role": "user", "content": "before after\u0085end"}, origin={"kind": "human"})
        rec("user", message={"role": "user", "content": "next"}, origin={"kind": "human"})
        path = Path(tempfile.mkdtemp()) / "s.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ROWS), encoding="utf-8")
        self.assertEqual([p["text"] for p in fc.fc_transcript.owner_prompts(fc.fc_transcript.records(path))], ["before after\u0085end", "next"])

    def test_transcript_path_slugs_the_folder(self):
        self.assertEqual(fc.fc_transcript.transcript_path("/Users/r/apps/x.y", "abc").name, "abc.jsonl")
        self.assertEqual(fc.fc_transcript.transcript_path("/Users/r/apps/x.y", "abc").parent.name, "-Users-r-apps-x-y")


if __name__ == "__main__":
    unittest.main()
