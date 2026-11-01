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


class SnapshotPort(unittest.TestCase):
    def msgs(self):
        return [{"role": "user", "text": "Always keep tests green.", "toolUses": []},
                {"role": "user", "text": fc.fc_snapshot.SUMMARY_PREFIX + " ... older summary", "toolUses": []},
                {"role": "assistant", "text": "Wrote the file.", "toolUses": [
                    {"tool": "Write", "input": {"file_path": "/tmp/a.md"}, "text": "x" * 2000, "isError": False},
                    {"tool": "Bash", "input": {"command": "echo hi > /tmp/b.txt && cat ~/.ssh/id_rsa"}, "text": "hi", "isError": False}]}]

    def test_state_shape_and_artifacts(self):
        s = fc.fc_snapshot.snapshot(self.msgs())
        self.assertEqual(sorted(s), ["autoCoverage", "checkpointText", "conversationTokens", "state"])
        self.assertEqual(sorted(s["state"]), ["compaction", "coverage", "previousSummary", "recent", "savedArtifacts", "userConstraints"])
        self.assertEqual(s["state"]["userConstraints"], [{"role": "user", "text": "Always keep tests green."}])
        self.assertEqual(s["state"]["savedArtifacts"], ["/tmp/a.md", "/tmp/b.txt"])
        self.assertTrue(s["state"]["previousSummary"].startswith(fc.fc_snapshot.SUMMARY_PREFIX))
        self.assertEqual(s["checkpointText"], '["Always keep tests green.","Wrote the file."]')

    def test_tool_results_are_middle_clipped_to_the_budget(self):
        tools = fc.fc_snapshot.snapshot(self.msgs())["state"]["recent"][0]["tools"]
        self.assertLessEqual(len(tools[0]["excerpt"].encode()), fc.fc_snapshot.TOOL_RESULT_BUDGET)
        self.assertIn("...[truncated ", tools[0]["excerpt"])
        self.assertTrue(fc.fc_snapshot.snapshot(self.msgs())["state"]["coverage"]["recentTextTruncated"])

    def test_secrets_and_sensitive_paths_are_redacted(self):
        s = fc.fc_snapshot.snapshot([{"role": "user", "text": "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", "toolUses": []},
                                     {"role": "assistant", "text": "ok", "toolUses": [{"tool": "Read", "input": {"file_path": "/home/u/.env"}, "text": "SECRET=1", "isError": False}]}], secrets=("hunter2",))
        self.assertNotIn("sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", json.dumps(s))
        self.assertEqual(s["state"]["recent"][0]["tools"][0]["excerpt"], "[Sensitive file content excluded]")
        self.assertTrue(s["state"]["coverage"]["redacted"])
        self.assertFalse(s["autoCoverage"])

    def test_token_estimate_is_chars_over_four(self):
        self.assertEqual(fc.fc_snapshot.estimate_conversation_tokens([{"role": "user", "text": "abcdefgh", "toolUses": []}]), 2)

    def test_token_estimate_counts_tool_input_and_result_text(self):
        m = {"role": "assistant", "text": "", "toolUses": [{"tool": "Bash", "input": {"command": "ls"}, "text": "abcd", "isError": False}]}
        self.assertEqual(fc.fc_snapshot.estimate_conversation_tokens([m]), -(-(len(json.dumps({"command": "ls"}, separators=(",", ":"))) + 4) // 4))
        self.assertEqual(fc.fc_snapshot.estimate_conversation_tokens([]), 0)

    def test_clip_cuts_on_bytes_and_drops_a_split_character(self):
        self.assertEqual(fc.fc_snapshot.clip("abc", 3), ("abc", False))
        self.assertEqual(fc.fc_snapshot.clip("abcdefgh", 6), ("abc", True))
        self.assertEqual(fc.fc_snapshot.clip("é" * 10, 8), ("éé", True))
        self.assertEqual(fc.fc_snapshot.clip("abc", 0), ("", True))

    def test_clip_middle_keeps_the_head_and_tail_around_a_marker(self):
        text, truncated = fc.fc_snapshot.clip_middle("A" * 100 + "B" * 100, 60)
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode()), 60)
        self.assertTrue(text.startswith("AAA") and text.endswith("BBB"))
        self.assertRegex(text, r"\.\.\.\[truncated 1\d\d bytes\]\.\.\.")
        self.assertEqual(fc.fc_snapshot.clip_middle("short", 60), ("short", False))
        self.assertEqual(fc.fc_snapshot.clip_middle("long text here", 0), ("", True))
        self.assertEqual(fc.fc_snapshot.clip_middle("x" * 100, 10), ("x" * 7, True))

    def test_redact_removes_keys_tokens_private_keys_and_assignments(self):
        text, hit = fc.fc_snapshot.redact("a Bearer abc.def ghp_" + "a" * 20 + " API_KEY=hunter2 -----BEGIN RSA PRIVATE KEY-----\nMII\n-----END RSA PRIVATE KEY----- z")
        self.assertTrue(hit)
        self.assertEqual(text, "a [REDACTED] [REDACTED] API_KEY=[REDACTED] [REDACTED PRIVATE KEY] z")
        self.assertEqual(fc.fc_snapshot.redact("nothing here"), ("nothing here", False))

    def test_owned_settings_lose_the_saved_key_but_keep_the_rest(self):
        text, hit = fc.fc_snapshot.redact_owned_settings(json.dumps({"mods": {"a.typesafeApiKey": "k1", "typesafeApiKey": "", "keep": 1}, "typesafeApiKey": "k2"}))
        self.assertTrue(hit)
        self.assertEqual(json.loads(text), {"mods": {"a.typesafeApiKey": "[REDACTED]", "typesafeApiKey": "", "keep": 1}, "typesafeApiKey": "[REDACTED]"})
        text, hit = fc.fc_snapshot.redact_owned_settings('{"typesafeApiKey": "abc", "x"')
        self.assertEqual((text, hit), ('{"typesafeApiKey":"[REDACTED]", "x"', True))
        self.assertEqual(fc.fc_snapshot.redact_owned_settings("typesafeApiKey=abc rest"), ("typesafeApiKey=[REDACTED] rest", True))
        self.assertEqual(fc.fc_snapshot.redact_owned_settings('{"a": 1}'), ('{"a": 1}', False))

    def test_known_secrets_are_trimmed_and_blank_ones_ignored(self):
        self.assertEqual(fc.fc_snapshot.scrub_known_secrets("pw=hunter2 hunter2", [" hunter2 ", "", None, "  "]), ("pw=[REDACTED] [REDACTED]", True))
        self.assertEqual(fc.fc_snapshot.scrub_known_secrets("clean", ["zzz"]), ("clean", False))

    def test_shell_written_paths(self):
        paths = fc.fc_snapshot.shell_written_paths
        self.assertEqual(paths("echo a > /tmp/x.txt; echo b >> y.log; echo c >| z.out"), ["/tmp/x.txt", "y.log", "z.out"])
        self.assertEqual(paths("make 2> err.log"), [])
        self.assertEqual(paths("make 1> out.log > /dev/null"), ["out.log"])
        self.assertEqual(paths("ls | tee a.txt -a b.txt"), ["a.txt", "b.txt"])
        self.assertEqual(paths("sudo env X=1 tee out.txt"), ["out.txt"])
        self.assertEqual(paths("sed -i '' -e s/a/b/ f1 f2"), ["f1", "f2"])
        self.assertEqual(paths("sed -i s/a/b/ f1"), [])  # a word after a bare -i could be a BSD suffix or a GNU script
        self.assertEqual(paths("sed -i -e s/a/b/ f1"), ["f1"])
        self.assertEqual(paths("sed -n p f1"), [])
        self.assertEqual(paths("echo $HOME > $OUT"), [])
        self.assertEqual(paths("echo > 'my file.txt'"), ["my file.txt"])
        self.assertEqual(paths("echo > my\\ file.txt"), ["my file.txt"])
        self.assertEqual(paths("[[ a > b ]] && echo hi > real.txt"), ["real.txt"])
        self.assertEqual(paths("cat <<EOF > doc.md\nbody > not.txt\nEOF\necho > after.txt"), ["doc.md", "after.txt"])
        self.assertEqual(paths("cat <<-EOF\n\tbody > no.txt\n\tEOF\necho > after.txt"), ["after.txt"])
        self.assertEqual(paths("echo 'multi\nline > no.txt' > yes.txt"), ["yes.txt"])
        self.assertEqual(paths("echo a # > no.txt"), [])

    def test_error_tool_uses_and_sensitive_paths_are_not_artifacts(self):
        m = [{"role": "assistant", "text": "x", "toolUses": [
            {"tool": "Write", "input": {"file_path": "/a/failed.md"}, "text": "no", "isError": True},
            {"tool": "Write", "input": {"file_path": "/a/.env.local"}, "text": "ok", "isError": False},
            {"tool": "Write", "input": {"file_path": "/a/server.pem"}, "text": "ok", "isError": False},
            {"tool": "Edit", "input": {"file_path": "/a/one.py"}, "text": "ok", "isError": False},
            {"tool": "Edit", "input": {"file_path": "/a/two.py"}, "text": "ok", "isError": False},
            {"tool": "Edit", "input": {"file_path": "/a/one.py"}, "text": "ok", "isError": False},
            {"tool": "Read", "input": {"file_path": "/a/read.py"}, "text": "ok", "isError": False}]}]
        self.assertEqual(fc.fc_snapshot.snapshot(m)["state"]["savedArtifacts"], ["/a/two.py", "/a/one.py"])

    def test_only_the_last_eight_artifacts_survive(self):
        uses = [{"tool": "Write", "input": {"file_path": f"/f{i}"}, "text": "", "isError": False} for i in range(12)]
        got = fc.fc_snapshot.snapshot([{"role": "assistant", "text": "", "toolUses": uses}])["state"]["savedArtifacts"]
        self.assertEqual(got, [f"/f{i}" for i in range(4, 12)])

    def test_only_the_recent_tail_is_kept_but_every_owner_ask_is(self):
        m = []
        for i in range(40):
            m += [{"role": "user", "text": f"ask {i}", "toolUses": []}, {"role": "assistant", "text": f"reply {i}", "toolUses": []}]
        s = fc.fc_snapshot.snapshot(m)["state"]
        self.assertEqual(len(s["userConstraints"]), 40)
        self.assertEqual(len(s["recent"]), 32)
        self.assertEqual(s["recent"][0]["text"], "reply 8")
        self.assertEqual(s["coverage"]["olderMessagesOmitted"], 16)
        self.assertNotIn("tools", s["recent"][0])
        self.assertFalse(s["coverage"]["transcriptLimitReached"])
        self.assertFalse(s["coverage"]["hasImages"])

    def test_the_newest_summary_wins_and_is_clipped(self):
        p = fc.fc_snapshot.SUMMARY_PREFIX
        m = [{"role": "user", "text": p + " old", "toolUses": []}, {"role": "user", "text": p + " new " + "y" * 3000, "toolUses": []}]
        s = fc.fc_snapshot.snapshot(m)["state"]
        self.assertTrue(s["previousSummary"].startswith(p + " new "))
        self.assertEqual(len(s["previousSummary"].encode()), 1500 - 3)
        self.assertEqual(s["userConstraints"], [])

    def test_user_budget_drops_the_oldest_asks_first(self):
        m = [{"role": "user", "text": "old " * 1500, "toolUses": []}, {"role": "user", "text": "new " * 1500, "toolUses": []}, {"role": "user", "text": "last", "toolUses": []}]
        s = fc.fc_snapshot.snapshot(m)
        self.assertEqual([u["text"][:3] for u in s["state"]["userConstraints"]], ["old", "new", "las"])
        self.assertEqual(s["state"]["coverage"]["omittedUserMessages"], 1)
        self.assertFalse(s["autoCoverage"])

    def test_the_transcript_limit_flag_follows_the_message_count(self):
        m = [{"role": "user", "text": "q", "toolUses": []}] * fc.fc_snapshot.MESSAGE_LIMIT
        s = fc.fc_snapshot.snapshot(m)
        self.assertTrue(s["state"]["coverage"]["transcriptLimitReached"])
        self.assertFalse(s["autoCoverage"])

    def test_the_compaction_note_is_the_mods(self):
        self.assertEqual(fc.fc_snapshot.snapshot([])["state"]["compaction"]["description"],
                         "Lossy structured summary of older context (request, files, errors, pending tasks, current work, next step) plus a few recent messages. Exact tool output and long file contents are compressed away. Other compaction hooks may differ.")
        self.assertEqual(fc.fc_snapshot.snapshot([])["checkpointText"], '["",""]')

    def test_checkpoint_text_is_javascripts_compact_json(self):
        s = fc.fc_snapshot.snapshot([{"role": "user", "text": "café \U0001F600", "toolUses": []}, {"role": "assistant", "text": "ok", "toolUses": []}])
        self.assertEqual(s["checkpointText"], '["café \U0001F600","ok"]')

    def test_deeply_nested_text_that_mentions_the_key_does_not_raise(self):
        text = "[" * 5000 + '"typesafeApiKey"' + "]" * 5000
        self.assertEqual(fc.fc_snapshot.redact_owned_settings(text), (text, False))
        fc.fc_snapshot.snapshot([{"role": "user", "text": text, "toolUses": []}])

    def test_known_secrets_are_scrubbed_across_the_snapshot(self):
        s = fc.fc_snapshot.snapshot([{"role": "user", "text": "the password is hunter2", "toolUses": []},
                                     {"role": "assistant", "text": "noted hunter2", "toolUses": [{"tool": "Bash", "input": {"command": "echo hunter2 > /tmp/hunter2.txt"}, "text": "out hunter2", "isError": False}]}], secrets=("hunter2",))
        self.assertNotIn("hunter2", json.dumps(s))
        self.assertIn("[REDACTED]", s["state"]["userConstraints"][0]["text"])
        self.assertEqual(s["state"]["savedArtifacts"], ["/tmp/[REDACTED].txt"])
        self.assertTrue(s["state"]["coverage"]["redacted"])
        self.assertFalse(s["autoCoverage"])


if __name__ == "__main__":
    unittest.main()
