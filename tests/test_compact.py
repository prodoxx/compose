import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, shutil, sys, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
# tests/test_modules.py fails a test file without the next line
os.environ["HERDR_ENV"] = ""
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


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
        rec("user", at="2026-11-01T10:00:07Z", message={"role": "user", "content": " \n\t "}, origin={"kind": "human"})  # only whitespace: nothing typed
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


class Log(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d)
        p = mock.patch.object(fc.fc_config, "CONFIG_PATH", d / "config.json"); p.start(); self.addCleanup(p.stop)

    def test_append_and_read_round_trip_in_order(self):
        fc.fc_compactlog.append("turn", pane="w1:p2", tokens=1000)
        fc.fc_compactlog.append("verdict", pane="w1:p2", decision="qualifies")
        self.assertEqual([r["kind"] for r in fc.fc_compactlog.read()], ["turn", "verdict"])
        self.assertEqual(fc.fc_compactlog.read(since=time.time() + 60), [])

    def test_typed_prompts_are_found_by_hash_after_a_time(self):
        fc.fc_compactlog.typed("w1:p2", "/compact keep it")
        self.assertTrue(fc.fc_compactlog.was_typed("w1:p2", "/compact keep it", "2000-01-01T00:00:00Z"))
        self.assertFalse(fc.fc_compactlog.was_typed("w1:p2", "/compact keep it", "2999-01-01T00:00:00Z"))
        self.assertFalse(fc.fc_compactlog.was_typed("w1:p3", "/compact keep it", "2000-01-01T00:00:00Z"))

    def test_a_typed_check_reads_only_the_records_since_its_time(self):
        fc.fc_compactlog.typed("w1:p2", "/compact keep it")
        with mock.patch.object(fc.fc_compactlog, "read", wraps=fc.fc_compactlog.read) as read:
            self.assertTrue(fc.fc_compactlog.was_typed("w1:p2", "/compact keep it", "2000-01-01T00:00:00Z"))
        self.assertEqual(read.call_args.kwargs, {"since": fc.fc_compactlog.epoch("2000-01-01T00:00:00Z")})

    def test_a_record_holding_unicode_line_separators_stays_one_record(self):
        text = "line one line two line three\u0085end é"
        fc.fc_compactlog.typed("w1:p2", text)
        self.assertTrue(fc.fc_compactlog.was_typed("w1:p2", text, "2000-01-01T00:00:00Z"))
        self.assertEqual(len(fc.fc_compactlog.read()), 1)

    def test_a_short_write_is_carried_on_until_the_line_is_whole(self):
        real, pieces = os.write, []
        def short(fd, data):  # the first write takes half the line, as a short write may
            n = real(fd, bytes(data[:len(data) // 2] if not pieces else data))
            pieces.append(n)
            return n
        with mock.patch.object(os, "write", side_effect=short):
            fc.fc_compactlog.append("verdict", pane="w1:p2", reason="because " * 50)
        fc.fc_compactlog.append("turn", pane="w1:p2", tokens=1)
        self.assertEqual(len(pieces), 2)
        self.assertEqual([r["kind"] for r in fc.fc_compactlog.read()], ["verdict", "turn"])

    def test_a_long_record_is_written_whole(self):
        reason = "because " * 5000
        fc.fc_compactlog.append("verdict", pane="w1:p2", reason=reason)
        fc.fc_compactlog.append("turn", pane="w1:p2", tokens=1)
        self.assertEqual([r["kind"] for r in fc.fc_compactlog.read()], ["verdict", "turn"])
        self.assertEqual(fc.fc_compactlog.read()[0]["reason"], reason)

    def test_one_marker_per_pane_that_clears(self):
        fc.fc_compactlog.set_inflight("w1:p2", "handoff", 42, "judged")
        self.assertEqual(fc.fc_compactlog.inflight("w1:p2")["step"], "handoff")
        fc.fc_compactlog.clear_inflight("w1:p2")
        self.assertIsNone(fc.fc_compactlog.inflight("w1:p2"))

    def test_an_expired_marker_counts_as_gone(self):
        fc.fc_compactlog.set_inflight("w1:p2", "compact", 42, "ceiling")
        with mock.patch.object(time, "time", return_value=time.time() + fc.fc_compactlog.INFLIGHT_TTL + 1):
            self.assertIsNone(fc.fc_compactlog.inflight("w1:p2"))

    def test_pane_state_round_trips_and_defaults_empty(self):
        self.assertEqual(fc.fc_compactlog.pane_state("w1:p2"), {})
        fc.fc_compactlog.save_pane_state("w1:p2", {"failures": 2})
        self.assertEqual(fc.fc_compactlog.pane_state("w1:p2"), {"failures": 2})


def write():
    path = Path(tempfile.mkdtemp()) / "s.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
    return path


class Consider(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d)
        for target, name, val in ((fc.fc_config, "CONFIG_PATH", d / "config.json"), (fc.fc_config, "COMPACT", dict(fc.fc_config.COMPACT_DEFAULTS)),
                                  (fc.fc_config, "apply_config", lambda **k: None), (fc.fc_config, "typesafe_key", lambda env, what: "k")):
            p = mock.patch.object(target, name, val); p.start(); self.addCleanup(p.stop)
        self.reply = {"model": "jev-latest", "usage": {"input_tokens": 1, "output_tokens": 1}, "answers": {
            "done": {"type": "choice", "choice": "finished", "probabilities": {"finished": 0.9, "not_finished": 0.05, "unclear": 0.05}, "confidence": 0.9},
            "shape": {"type": "choice", "choice": "coordinating", "probabilities": {"hands_on": 0.1, "coordinating": 0.8, "unclear": 0.1}, "confidence": 0.8}}}
        ROWS.clear()
        rec("user", message={"role": "user", "content": "go"}, origin={"kind": "human"})

    def turn(self, tokens):
        rec("assistant", message={"id": "a", "content": [{"type": "text", "text": "done"}], "usage": {"input_tokens": tokens}})
        return write()

    def consider(self, tokens, role="manager", ask=None, now=1000.0):
        with mock.patch.object(fc.fc_models, "jev_ask", ask or mock.Mock(return_value=self.reply)) as j:
            return fc.fc_compact.consider("w1:p2", role, self.turn(tokens), now=now), j

    def test_below_the_minimum_nothing_is_asked(self):
        v, j = self.consider(39999)
        self.assertEqual((v["decision"], j.call_count), ("below-minimum", 0))

    def test_a_qualifying_verdict_for_the_manager_ignores_hands_on(self):
        v, j = self.consider(90000)
        self.assertEqual(v["decision"], "qualifies")
        self.assertEqual((v["score"], v["floor"], v["usage"]), (0.9, 0.65, 0.6))
        self.assertEqual(j.call_args.kwargs["timeout"], 2)
        self.assertEqual(fc.fc_compactlog.read()[-1]["decision"], "qualifies")

    def test_the_lead_profile_weighs_hands_on(self):
        v, _ = self.consider(90000, role="lead")
        self.assertEqual((v["decision"], v["score"]), ("below-floor", 0.495))

    def test_the_ceiling_wins_over_everything_but_the_env_switch(self):
        v, j = self.consider(150000)
        self.assertEqual((v["decision"], j.call_count), ("ceiling", 0))
        with mock.patch.dict(os.environ, {"COMPOSE_NO_COMPACT": "1"}):
            v, _ = self.consider(150000)
        self.assertEqual(v["decision"], "off")

    def test_the_ceiling_waits_out_the_settling_after_a_compaction_but_not_the_ask_throttle(self):
        ROWS.clear()
        rec("system", subtype="compact_boundary", compactMetadata={"trigger": "manual", "preTokens": 170000, "postTokens": 152000})
        rec("user", message={"role": "user", "content": "go on"}, origin={"kind": "human"})
        fc.fc_compactlog.save_pane_state("w1:p2", {"baseline": 152000, "last_asked_at": 999.0})
        v, j = self.consider(155000)  # just compacted and still past the ceiling: compacting again would land at the same size
        self.assertEqual((v["decision"], v["reason"], j.call_count), ("cooldown", "Settling after a compaction", 0))
        for n in range(2):  # two more answered exchanges make three since the boundary
            rec("user", message={"role": "user", "content": f"next {n}"}, origin={"kind": "human"})
            rec("assistant", message={"id": f"r{n}", "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 160000}})
        rec("user", message={"role": "user", "content": "and now"}, origin={"kind": "human"})
        v, _ = self.consider(175000, now=1001.0)  # settled; asked a second ago, which only throttles asking the judge
        self.assertEqual(v["decision"], "ceiling")

    def test_the_request_is_measured_as_it_is_sent(self):
        state = {"text": "café, naïve, 日本語 " * 50}
        sent = []
        with mock.patch.object(fc.fc_models, "http_json", lambda req, *a, **k: sent.append(req.data) or {"usage": {}}):
            fc.fc_models.jev_ask({}, {}, state, fc.fc_compact.QUESTIONS, "compact judge")
        self.assertEqual(fc.fc_compact._request_bytes(state), len(sent[0]))

    def test_an_error_backs_off_and_a_second_turn_waits(self):
        v, _ = self.consider(90000, ask=mock.Mock(side_effect=RuntimeError("typesafe: HTTP 500 boom")))
        self.assertEqual((v["decision"], v["reason"]), ("error", "server"))
        self.assertEqual(fc.fc_compactlog.pane_state("w1:p2")["failures"], 1)
        v, j = self.consider(96000, now=1003.0)
        self.assertEqual((v["decision"], v["reason"], j.call_count), ("cooldown", "TypeSafe backoff", 0))

    def test_an_oversized_request_is_never_sent(self):
        ROWS.clear()
        rec("user", message={"role": "user", "content": "x" * 40000}, origin={"kind": "human"})
        with mock.patch.object(fc.fc_compact, "MAX_REQUEST_BYTES", 2000):
            v, j = self.consider(90000)
        self.assertEqual((v["decision"], v["reason"], j.call_count), ("error", "input", 0))

    def test_a_second_ask_within_two_minutes_waits(self):
        self.consider(90000)
        v, j = self.consider(96000, now=1060.0)
        self.assertEqual((v["decision"], v["reason"], j.call_count), ("cooldown", "Asked recently", 0))

    def test_failures_are_classified_and_a_success_clears_the_backoff(self):
        v, _ = self.consider(90000, ask=mock.Mock(side_effect=fc.fc_models.PaymentRequired("typesafe", "no credit")))
        self.assertEqual(v["reason"], "billing")
        v, _ = self.consider(120000, now=2000.0, ask=mock.Mock(return_value={"nope": 1}))
        self.assertEqual((v["decision"], v["reason"]), ("error", "response"))
        self.assertEqual(fc.fc_compactlog.pane_state("w1:p2")["failures"], 2)
        v, _ = self.consider(130000, now=3000.0)
        state = fc.fc_compactlog.pane_state("w1:p2")
        self.assertEqual((v["decision"], state["failures"], "retry_after" in state), ("qualifies", 0, False))

    def test_a_missing_key_or_a_bad_config_is_a_configuration_error(self):
        def broken(**k): raise SystemExit("bad config")
        with mock.patch.object(fc.fc_config, "apply_config", broken):
            v, j = self.consider(90000)
        self.assertEqual((v["decision"], v["reason"], j.call_count), ("error", "configuration", 0))
        def nokey(env, what): raise RuntimeError("needs a key")
        with mock.patch.object(fc.fc_config, "typesafe_key", nokey):
            v, j = self.consider(90000, now=5000.0)
        self.assertEqual((v["decision"], v["reason"], j.call_count), ("error", "configuration", 0))

    def test_the_judge_sees_only_the_context_since_the_last_compaction(self):
        ROWS.clear()
        rec("user", message={"role": "user", "content": "old topic about zebras"}, origin={"kind": "human"})
        rec("system", subtype="compact_boundary", compactMetadata={"trigger": "manual", "preTokens": 100000, "postTokens": 5000})
        rec("user", message={"role": "user", "content": "new topic"}, origin={"kind": "human"})
        seen = []
        v, _ = self.consider(90000, ask=lambda env, cfg, state, *a, **k: seen.append(json.dumps(state)) or self.reply)
        self.assertEqual(v["decision"], "qualifies")
        self.assertIn("new topic", seen[0])
        self.assertNotIn("zebras", seen[0])

    def test_rows_the_hook_already_read_are_not_read_again(self):
        path = self.turn(90000)
        rows = fc.fc_transcript.records(path)
        with mock.patch.object(fc.fc_transcript, "records", side_effect=AssertionError("read again")), \
                mock.patch.object(fc.fc_models, "jev_ask", return_value=self.reply):
            v = fc.fc_compact.consider("w1:p2", "manager", path, now=1000.0, rows=rows)
        self.assertEqual((v["decision"], v["tokens"]), ("qualifies", 90000))

    def test_the_judged_switch_off_asks_nothing(self):
        fc.fc_config.COMPACT["judged"] = False
        v, j = self.consider(90000)
        self.assertEqual((v["decision"], j.call_count), ("off", 0))

    def test_a_bad_config_still_lets_the_ceiling_stand_but_blocks_the_ask(self):
        broken = mock.Mock(side_effect=SystemExit("bad"))
        with mock.patch.object(fc.fc_config, "apply_config", broken):
            v, j = self.consider(150000)
            self.assertEqual((v["decision"], j.call_count), ("ceiling", 0))
            v, j = self.consider(90000, now=2000.0)
        self.assertEqual((v["decision"], v["reason"], j.call_count), ("error", "configuration", 0))

    def test_a_broken_compact_section_is_a_configuration_error_not_a_crash(self):
        broken = mock.Mock(side_effect=SystemExit(2))
        with mock.patch.object(fc.fc_config, "apply_config", broken), mock.patch.object(fc.fc_config, "COMPACT", {**fc.fc_config.COMPACT_DEFAULTS, "budget": 0}):
            v, j = self.consider(90000)
        self.assertEqual((v["decision"], v["reason"], j.call_count), ("error", "configuration", 0))
        self.assertEqual(fc.fc_compactlog.pane_state("w1:p2")["failures"], 1)

    def test_the_config_is_applied_before_any_gate(self):
        applied = mock.Mock()
        with mock.patch.object(fc.fc_config, "apply_config", applied):
            v, _ = self.consider(1000)
        self.assertEqual(v["decision"], "below-minimum")
        applied.assert_called_once_with(move=False)

    def test_known_secrets_never_reach_the_judge(self):
        secret = "sk-test-secret-value-1234"
        ROWS.clear()
        rec("user", message={"role": "user", "content": f"deploy with key {secret} please"}, origin={"kind": "human"})
        seen = []
        with mock.patch.object(fc.fc_core, "SECRET_VALUES", {secret}):
            v, _ = self.consider(90000, ask=lambda env, cfg, state, *a, **k: seen.append(json.dumps(state)) or self.reply)
        self.assertEqual(v["decision"], "qualifies")
        self.assertIn("deploy with key", seen[0])
        self.assertNotIn(secret, seen[0])


class Weekly(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d)
        p = mock.patch.object(fc.fc_config, "CONFIG_PATH", d / "config.json"); p.start(); self.addCleanup(p.stop)
        self.root = d / "projects"
        (self.root / "proj").mkdir(parents=True)
        self.path = self.root / "proj" / "sess-abcdef12.jsonl"
        ROWS.clear()
        self.addCleanup(ROWS.clear)  # the rows are module-level: none outlive this test
        usage = lambda n: {"input_tokens": n - 20, "cache_read_input_tokens": 20, "output_tokens": 1}
        rec("assistant", at="2026-11-01T09:00:00.000Z", message={"id": "m1", "usage": usage(100), "content": []})
        rec("system", at="2026-11-01T10:00:00.000Z", subtype="compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 160000, "postTokens": 20000})
        rec("user", at="2026-11-01T10:05:00.000Z", message={"role": "user", "content": "focus, what next?"}, origin={"kind": "human"})
        rec("assistant", at="2026-11-01T11:00:00.000Z", message={"id": "m2", "usage": usage(100), "content": []})
        rec("system", at="2026-11-01T12:00:00.000Z", subtype="compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 174000, "postTokens": 20000})
        rec("user", at="2026-11-01T12:05:00.000Z", message={"role": "user", "content": "thanks"}, origin={"kind": "human"})
        rec("assistant", at="2026-11-02T08:00:00.000Z", message={"id": "m3", "usage": usage(100), "content": []})
        rec("system", at="2026-11-02T09:00:00.000Z", subtype="compact_boundary", compactMetadata={"trigger": "manual", "preTokens": 90000, "postTokens": 30000})
        self.path.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
        self.log = [{"kind": "compaction", "at": "2026-11-02T09:00:30Z", "pane": "w1:p2", "role": "manager", "trigger": "compose",
                     "pre": 90000, "post": 30000, "duration_ms": 9000, "outcome": "done", "transcript": str(self.path)}]

    def test_the_week_counts_triggers_medians_input_and_the_phrase_share(self):
        w = fc.fc_compactlog.week(self.root, since=0, log=self.log)
        self.assertEqual(w["compactions"], {"native": 2, "compose": 1})
        self.assertEqual(w["median_pre"]["native"], 167000)
        self.assertEqual(w["median_pre"]["compose"], 90000)
        self.assertEqual(w["where_were_we"], (1, 3))
        self.assertEqual(sum(w["input_per_session_day"].values()), 300)
        self.assertEqual(w["input_per_session_day"][("sess-abcdef12", "2026-11-01")], 200)

    def test_a_boundary_is_matched_by_its_own_transcript_and_the_time_window_once(self):
        other = [{**self.log[0], "transcript": str(self.root / "proj" / "other.jsonl")}]
        self.assertEqual(fc.fc_compactlog.week(self.root, 0, other)["compactions"], {"native": 2, "manual": 1})
        early = [{**self.log[0], "at": "2026-11-02T08:49:00Z"}]  # 660 seconds before: out of the window
        self.assertEqual(fc.fc_compactlog.week(self.root, 0, early)["compactions"], {"native": 2, "manual": 1})
        bare = [{k: v for k, v in dict(self.log[0], trigger=t, at=at).items() if k != "transcript"} for t, at in (("compose", "2026-11-02T09:00:30Z"), ("ceiling", "2026-11-02T09:00:40Z"))]
        self.assertEqual(fc.fc_compactlog.week(self.root, 0, bare)["compactions"], {"native": 2, "compose": 1})  # one record, one boundary
        timeout = [{**self.log[0], "outcome": "timeout"}]
        self.assertEqual(fc.fc_compactlog.week(self.root, 0, timeout)["compactions"], {"native": 2, "manual": 1})

    def test_two_records_in_the_same_second_are_claimed_in_file_order(self):
        twin = lambda trigger: {**self.log[0], "trigger": trigger}
        self.assertEqual(fc.fc_compactlog.week(self.root, 0, [twin("ceiling"), twin("compose")])["compactions"], {"native": 2, "ceiling": 1})
        self.assertEqual(fc.fc_compactlog.week(self.root, 0, [twin("compose"), twin("ceiling")])["compactions"], {"native": 2, "compose": 1})

    def stops(self):
        """The log of a week with runs that ended before compacting, one of them before the window and one that never started."""
        ended = lambda kind, outcome, at="2026-11-02T10:00:00Z": {"kind": kind, "at": at, "pane": "w1:p2", "outcome": outcome}
        return self.log + [ended("step", "gave-up"), ended("step", "gave-up"), ended("step", "not-settled"), ended("step", "refused"),
                           ended("compaction", "disarmed-owner-typing"), ended("compaction", "timeout"), ended("step", "unknown-role"),
                           ended("step", "not-quiet", at="2026-10-01T10:00:00Z"), {"kind": "step", "at": "2026-11-02T10:00:00Z", "step": "handoff"}]

    def test_the_week_counts_runs_that_ended_before_compacting(self):
        since = fc.fc_compactlog.epoch("2026-11-01T00:00:00Z")
        self.assertEqual(fc.fc_compactlog.week(self.root, since, self.stops())["stopped"],
                         {"gave-up": 2, "not-settled": 1, "refused": 1, "disarmed-owner-typing": 1, "timeout": 1})
        self.assertEqual(fc.fc_compactlog.week(self.root, since, self.log)["stopped"], {})

    def test_the_window_drops_older_boundaries_and_days(self):
        since = fc.fc_compactlog.epoch("2026-11-02T00:00:00Z")
        w = fc.fc_compactlog.week(self.root, since, self.log)
        self.assertEqual(w["compactions"], {"compose": 1})
        self.assertEqual(w["input_per_session_day"], {("sess-abcdef12", "2026-11-02"): 100})
        self.assertEqual(w["where_were_we"], (0, 1))

    def test_where_were_we_is_a_case_insensitive_phrase_list(self):
        self.assertTrue(fc.fc_compactlog.where_were_we("Focus, what do we need to do next?"))
        self.assertFalse(fc.fc_compactlog.where_were_we("thanks, go ahead"))
        self.assertFalse(fc.fc_compactlog.where_were_we("x" * 200 + "status"))

    def test_the_command_prints_the_summary(self):
        out = []
        now = fc.fc_compactlog.epoch("2026-11-03T00:00:00Z")
        with mock.patch.object(fc.fc_compactlog, "transcripts", return_value=list(self.root.glob("*/*.jsonl"))), \
                mock.patch.object(fc.fc_compactlog, "read", side_effect=lambda since=None: self.log), \
                mock.patch.object(time, "time", return_value=now), mock.patch("builtins.print", out.append):
            fc.fc_compactlog.cmd_compactions(SimpleNamespace(repo=".", project=None, days=7, since=None))
        self.assertEqual(out[0], "compactions in the last 7 days (where-were-we is a phrase heuristic)")
        self.assertIn("  native: 2, median before 167000", out)
        self.assertIn("  compose: 1, median before 90000", out)
        self.assertIn("  2026-11-01 sess-abc: 200 input tokens", out)
        self.assertIn("  where were we: 1/3 first prompts after a compaction", out)
        self.assertIn("  ended before compacting: none", out)

    def test_the_command_counts_runs_that_ended_before_compacting(self):
        out = []
        with mock.patch.object(fc.fc_compactlog, "transcripts", return_value=[self.path]), mock.patch.object(fc.fc_compactlog, "read", return_value=self.stops()), \
                mock.patch("builtins.print", out.append):
            fc.fc_compactlog.cmd_compactions(SimpleNamespace(repo=".", project=None, days=7, since="2026-11-01"))
        self.assertEqual(out[-1], "  ended before compacting: 2 gave-up, 1 not-settled, 1 refused, 1 disarmed-owner-typing, 1 timeout")

    def test_a_since_that_is_not_a_date_is_one_line_and_exit_2(self):
        out = []
        with mock.patch.object(fc.fc_compactlog, "read", return_value=self.log), mock.patch("builtins.print", lambda *a, **k: out.append(a[0])):
            with self.assertRaises(SystemExit) as c:
                fc.fc_compactlog.cmd_compactions(SimpleNamespace(repo=".", project=None, days=7, since="11/02/2026"))
        self.assertEqual((c.exception.code, out), (2, ["compose compactions: --since takes a date as YYYY-MM-DD, not '11/02/2026'"]))

    def test_since_picks_the_window_by_date(self):
        out = []
        with mock.patch.object(fc.fc_compactlog, "transcripts", return_value=[self.path]), mock.patch.object(fc.fc_compactlog, "read", return_value=self.log), \
                mock.patch("builtins.print", out.append):
            fc.fc_compactlog.cmd_compactions(SimpleNamespace(repo=".", project=None, days=7, since="2026-11-02"))
        self.assertTrue(out[0].startswith("compactions since 2026-11-02"))
        self.assertIn("  compose: 1, median before 90000", out)
        self.assertFalse(any(l.strip().startswith("native") for l in out))


if __name__ == "__main__":
    unittest.main()
