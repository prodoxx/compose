import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class FigmaCheck(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1"}))

    def check(self, figma=None):
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("sys.stdout", out):
            fc.fc_figma.cmd_figma(SimpleNamespace(repo=".", project="p", action="check", figma=figma, only=None))
        return out.getvalue()

    def stored(self):
        return json.loads((self.proj / "linear.json").read_text())

    def test_figma_key_parses_urls_and_bare_keys(self):
        for text, key in (("https://www.figma.com/design/AbCdEf123456/Shop?node-id=1-2", "AbCdEf123456"),
                          ("https://www.figma.com/design/AbCdEf123456/branch/BrAnCh789012/Shop", "BrAnCh789012"),
                          ("https://figma.com/board/BoArD1234567/Flow", "BoArD1234567"),
                          ("https://www.figma.com/slides/SlIdEs123456", "SlIdEs123456"),
                          ("AbCdEf123456", "AbCdEf123456"),
                          ("junk", None), ("abc123456", None), ("a" * 41, None), ("AbCdEf-123456", None),
                          ("https://example.com/design/AbCdEf123456/Shop", None)):
            self.assertEqual(fc.fc_core.figma_key(text), key, text)

    def test_store_figma_keeps_other_keys_and_creates_missing_file(self):
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "figma": "OldKey123456", "x": 1}))
        fc.fc_figma.store_figma(self.proj, "NewKey123456")
        self.assertEqual(self.stored(), {"feature": "ENG-1", "figma": "NewKey123456", "x": 1})
        (self.proj / "linear.json").unlink()
        fc.fc_figma.store_figma(self.proj, "NewKey123456")
        self.assertEqual(self.stored(), {"figma": "NewKey123456"})

    def test_check_stores_a_url_then_shows_the_stored_key(self):
        with mock.patch.object(fc.fc_figma, "framelink_fetch", return_value="{}") as fetch:
            out = self.check("https://www.figma.com/design/AbCdEf123456/Shop?node-id=1-2")
            self.assertIn("figma file AbCdEf123456 stored in ", self.check())
        self.assertEqual(self.stored(), {"feature": "ENG-1", "figma": "AbCdEf123456"})
        self.assertIn("figma file AbCdEf123456 stored in ", out)
        self.assertIn("confirm the write connector with whoami", out)
        self.assertIn("framelink reads figma file AbCdEf123456", out)
        fetch.assert_called_with("AbCdEf123456", depth=1)

    def test_check_passes_an_empty_file_framelink_cannot_parse(self):
        crash = RuntimeError("framelink fetch failed (exit 1): TypeError: Cannot read properties of undefined (reading 'children')")
        empty = {"document": {"children": [{"type": "CANVAS", "id": "0:1", "children": []}]}}
        with mock.patch.object(fc.fc_figma, "framelink_fetch", side_effect=crash), \
             mock.patch.object(fc.fc_figma.fc_figmaplan, "figma_api", return_value=empty) as api:
            out = self.check("AbCdEf123456")
        self.assertIn("framelink reads figma file AbCdEf123456 (empty: no frame on any page yet)", out)
        self.assertEqual(api.call_args.args[0], "/files/AbCdEf123456?depth=2")

    def test_check_keeps_the_crash_for_a_file_that_has_content_or_cannot_be_read(self):
        crash = RuntimeError("framelink fetch failed (exit 1): TypeError: Cannot read properties of undefined (reading 'children')")
        filled = {"document": {"children": [{"type": "CANVAS", "id": "0:1", "children": [{"type": "FRAME", "id": "1:2"}]}]}}
        for rest in (filled, RuntimeError("figma api /files/AbCdEf123456 failed: HTTP 404")):
            with self.subTest(rest=rest), mock.patch.object(fc.fc_figma, "framelink_fetch", side_effect=crash), \
                 mock.patch.object(fc.fc_figma.fc_figmaplan, "figma_api", **{"side_effect" if isinstance(rest, Exception) else "return_value": rest}):
                with self.assertRaises(SystemExit) as e:
                    self.check("AbCdEf123456")
                self.assertIn("framelink cannot read figma file AbCdEf123456: framelink fetch failed", str(e.exception))

    def test_check_says_how_to_set_up_framelink_when_it_cannot_read(self):
        with mock.patch.object(fc.fc_figma, "framelink_fetch", side_effect=RuntimeError(fc.fc_figma.FRAMELINK_SETUP)):
            with self.assertRaises(SystemExit) as e:
                self.check("AbCdEf123456")
        msg = str(e.exception)
        for text in ("framelink cannot read figma file AbCdEf123456", ".config/figma-context-mcp/.env", "claude mcp add framelink-figma", "--env", "FIGMA_API_KEY=<token>"):
            self.assertIn(text, msg)

    def test_framelink_fetch_uses_env_file_depth_and_never_leaks_the_token(self):
        env = self.proj / "fl.env"
        with self.assertRaises(RuntimeError) as e:
            fc.fc_figma.framelink_fetch("K", env_file=env)
        self.assertIn("claude mcp add framelink-figma", str(e.exception))
        env.write_text("FIGMA_API_KEY=figd_secret_token\n")
        ok = SimpleNamespace(returncode=0, stdout='{"nodes": []}', stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok) as run:
            self.assertEqual(fc.fc_figma.framelink_fetch("K", node="1:2", depth=1, env_file=env), '{"nodes": []}')
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], ["npx", "-y", "figma-developer-mcp", "fetch"])
        self.assertEqual(cmd[cmd.index("--env") + 1], str(env))
        for flag, val in (("--file-key", "K"), ("--node-id", "1:2"), ("--depth", "1")):
            self.assertEqual(cmd[cmd.index(flag) + 1], val)
        self.assertNotIn("figd_secret_token", " ".join(cmd))
        bad = SimpleNamespace(returncode=1, stdout="", stderr="403 token figd_secret_token rejected")
        with mock.patch.object(subprocess, "run", return_value=bad), self.assertRaises(RuntimeError) as e:
            fc.fc_figma.framelink_fetch("K", env_file=env)
        self.assertIn("403 token *** rejected", str(e.exception))
        self.assertNotIn("figd_secret_token", str(e.exception))

    def test_check_refuses_junk_and_a_missing_key(self):
        with self.assertRaises(SystemExit) as e:
            self.check("not a key")
        self.assertEqual(str(e.exception), "not a Figma file url or key: not a key")
        self.assertEqual(self.stored(), {"feature": "ENG-1"})
        with self.assertRaises(SystemExit) as e:
            self.check()
        self.assertEqual(str(e.exception), "no Figma file: compose figma check --figma <url or key>")


class FigmaTree(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "figma": "AbCdEf123456"}))
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            judge_task("ENG-1", "001", "Button", ["Has a label"]), judge_task("ENG-2", "002", "Code", ["x"], kind="code")]})

    def tree(self, **kw):
        ns = dict(repo=".", project="p", action="tree", figma=None, only="001", node="1:2,3-4", depth=None, bindings=None)
        out = io.StringIO()
        shots = getattr(self, "shots", mock.Mock(return_value=([], [])))
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("sys.stdout", out), \
                mock.patch.object(fc.fc_figma.fc_figmashots, "save_screens", shots):
            fc.fc_figma.cmd_figma(SimpleNamespace(**{**ns, **kw}))
        return out.getvalue()

    def test_renders_a_screen_of_each_node_with_the_tree(self):
        self.shots = mock.Mock(return_value=([("1:2", self.proj / "a.png"), ("3:4", self.proj / "b.png")], ["no screen of 5:6: Figma rendered nothing for it"]))
        with mock.patch.object(fc.fc_figma, "framelink_fetch", side_effect=lambda key, node=None, depth=None: f"TREE {node}"):
            out = self.tree()
        self.assertEqual(self.shots.call_args.args, ("AbCdEf123456", ["1:2", "3:4"], self.proj / "results" / "001.screens"))
        self.assertIn("and 2 screens in", out)
        self.assertIn("no screen of 5:6", out)

    def test_a_render_that_fails_saves_no_tree(self):
        self.shots = mock.Mock(side_effect=RuntimeError("figma api /images failed: HTTP 500"))
        with mock.patch.object(fc.fc_figma, "framelink_fetch", side_effect=lambda key, node=None, depth=None: f"TREE {node}"), self.assertRaises(SystemExit) as e:
            self.tree()
        self.assertEqual(str(e.exception), "no screens of 001: figma api /images failed: HTTP 500")
        self.assertFalse((self.proj / "results" / "001.tree.txt").exists())

    def test_saves_each_nodes_framelink_tree_then_the_write_results(self):
        (self.proj / "bind.txt").write_text('{"bound": "color/bg"}\n')
        with mock.patch.object(fc.fc_figma, "framelink_fetch", side_effect=lambda key, node=None, depth=None: f"TREE {node}") as fetch:
            out = self.tree(bindings=str(self.proj / "bind.txt"), depth=3)
        text = (self.proj / "results" / "001.tree.txt").read_text()
        self.assertEqual(text, 'NODE 1:2\nTREE 1:2\n\nNODE 3-4\nTREE 3:4\n\nWRITE RESULT (facts the write script returned: node ids, variable bindings, component keys)\n{"bound": "color/bg"}\n')
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ["AbCdEf123456"] * 2)
        self.assertEqual(fetch.call_args.kwargs, {"node": "3:4", "depth": 3})
        self.assertIn("001.tree.txt", out)

    def test_every_node_inside_an_instance_is_marked_as_part_of_it(self):
        """A judge reading `built only from instances` took an instance's own text, icon, and fill for hand-drawn nodes: the
        tree must say which nodes are an instance's contents, on each node, since a chunk holds a node without its ancestors."""
        doc = {"metadata": {}, "nodes": [{"id": "1:2", "name": "Light", "type": "FRAME", "children": [
            {"id": "5:1", "name": "Card", "type": "INSTANCE", "componentId": "9:9", "children": [
                {"id": "I5:1;7:1", "type": "TEXT", "text": "Walk"},
                {"id": "I5:1;7:2", "name": "Pill", "type": "INSTANCE", "children": [{"id": "I5:1;7:2;8:1", "type": "RECTANGLE"}]}]},
            {"id": "1:3", "type": "RECTANGLE"}]}]}
        with mock.patch.object(fc.fc_figma, "framelink_fetch", side_effect=lambda key, node=None, depth=None: json.dumps(doc)):
            self.tree(node="1:2")
        saved = json.loads((self.proj / "results" / "001.tree.txt").read_text().split("\n", 1)[1])
        light = saved["nodes"][0]
        card, loose = light["children"]
        text, pill = card["children"]
        self.assertEqual([n.get("inInstance") for n in (light, card, loose)], [None, None, None])
        self.assertEqual([n.get("inInstance") for n in (text, pill, pill["children"][0])], ["5:1", "5:1", "I5:1;7:2"])

    def test_the_judge_is_told_an_instances_contents_are_not_hand_drawn(self):
        for rules in (fc.fc_figma.TREE_READING, fc.fc_figma.CONFIRM):
            self.assertIn("inInstance", rules)

    def test_refuses_a_missing_id_node_non_design_task_and_a_failed_fetch(self):
        for kw, msg in ((dict(only=None), "needs --only"), (dict(node=None), "needs --only"), (dict(only="002"), "002 is not a design task")):
            with self.assertRaises(SystemExit) as e:
                self.tree(**kw)
            self.assertIn(msg, str(e.exception))
        with mock.patch.object(fc.fc_figma, "framelink_fetch", side_effect=RuntimeError("framelink fetch failed")), self.assertRaises(SystemExit) as e:
            self.tree()
        self.assertEqual(str(e.exception), "framelink fetch failed")
        self.assertFalse((self.proj / "results" / "001.tree.txt").exists())


def judge_task(ident, tid, title, crits, kind="design"):
    desc = "## Acceptance criteria\n" + "".join(f"- {c}\n" for c in crits) + f"\n```compose\nid: {tid}\nkind: {kind}\ntest: t\n```\n"
    return {"id": ident, "title": title, "description": desc, "status": "Todo", "blockedBy": []}


def reask_reply(choice):
    return {"model": "jev-test", "answers": {"c0": {"choice": choice, "probabilities": {choice: 0.9}}}}


def big_tree(variants=15, pad=10000):
    """A Framelink-shaped tree of ~variants*pad characters: a Button set whose every variant holds a Tint child, as design 138 had."""
    kids = [{"id": f"1:{i}", "name": f"variant={i}", "type": "COMPONENT", "padding": "x" * pad,
             "children": [{"id": f"2:{i}", "name": "Tint", "type": "RECTANGLE"}]} for i in range(variants)]
    doc = {"metadata": {"name": "File"}, "nodes": [{"id": "9:536", "name": "Button", "type": "COMPONENT_SET", "children": kids}], "globalVars": {"styles": {}}}
    return "NODE 9:536\n" + json.dumps(doc, indent=2) + "\n"


class FigmaJudge(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "figma": "AbCdEf123456"}))
        (self.proj / "verify.json").write_text(json.dumps({"acceptance_threshold": 0.7}))
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            judge_task("ENG-3", "001", "Cart page", ["Shows the total", "Has a pay button"]), judge_task("ENG-4", "002", "Store", ["x"], kind="code"),
            judge_task("ENG-5", "003", "Hero", ["Has a headline"]), judge_task("ENG-6", "004", "Pay", ["Has a card field"]), judge_task("ENG-7", "005", "Bare", [])]})
        for tid in ("001", "002", "004", "005", "009"):  # a tree for every id but 003, so only the id and kind checks can refuse 002 and 009
            (self.proj / "results" / f"{tid}.tree.txt").write_text(f"FRAME {tid}\n  TEXT Total: $10\n")
        for mod, name, value in ((fc.fc_config, "ENV_FILE", self.proj / "none.env"), (fc.fc_core, "say", lambda m: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def judge(self, only, replies, confirm=None, fail=None, blocks=False, reask=None, force=False):
        replies = {t: {"model": "jev-test", "answers": {f"c{i}": {"choice": c, "probabilities": {c: p}} for i, (c, p) in enumerate(r)}} for t, r in replies.items()}
        self.out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_models, "jev_ask", side_effect=lambda env, cfg, state, q, label: reask_reply(reask) if label.endswith("re-ask") else replies[state["task"]]) as self.ask, \
                mock.patch.object(fc.fc_models, "fast_json", side_effect=fail, return_value=confirm) as self.confirm, mock.patch.object(fc.fc_linear, "linear_event") as self.ev, mock.patch("sys.stdout", self.out):
            try:
                fc.fc_figma.cmd_figma(SimpleNamespace(repo=".", project="p", action="judge", figma=None, only=only, force=force))
            except SystemExit as e:
                if not blocks:
                    raise
                self.assertEqual(e.code, 1)  # a blocked task exits non-zero
        return json.loads((self.proj / "results" / f"{only[:3]}.json").read_text())

    def test_all_met_writes_a_passed_result_and_tells_linear(self):
        (self.proj / "results" / "001.json").write_text(json.dumps({"status": "failed", "attempts": 2}))
        res = self.judge("001", {"Cart page": [("MET", 0.95), ("MET", 0.9)]})
        env, cfg, state, questions, label = self.ask.call_args.args
        self.assertEqual((state, label, cfg["acceptance_threshold"]), ({"task": "Cart page", "tree": "FRAME 001\n  TEXT Total: $10\n"}, "figma judge", 0.7))
        self.assertEqual([(q["instructions"]["criterion"], q["instructions"]["task"], sorted(q["criteria"])) for q in questions.values()],
                         [("Shows the total", "001", ["MET", "NOT_MET", "UNCLEAR"]), ("Has a pay button", "001", ["MET", "NOT_MET", "UNCLEAR"])])
        self.assertEqual({k: res[k] for k in ("id", "title", "status", "kind", "failed", "attempts", "seconds", "cost", "model", "branch")},
                         {"id": "001", "title": "Cart page", "status": "passed", "kind": "design", "failed": [], "attempts": 3, "seconds": 0.0, "cost": 0.0, "model": "jev-test", "branch": "figma:AbCdEf123456"})
        self.assertEqual((len(res["criteria"]), bool(res["finished_at"]), self.confirm.called, "TREE" in questions["c0"]["instructions"]["rules"]), (2, True, False, True))
        self.assertIn("001 MET p_not_met=0.00 Shows the total", self.out.getvalue())
        self.assertEqual(self.ev.call_args_list, [mock.call(self.proj, mock.ANY, "passed", res)])

    def test_a_hand_accepted_task_is_not_judged_again_unless_forced(self):
        hand = {"id": "001", "status": "hand_done", "note": "accepted by the designer", "attempts": 1}
        (self.proj / "results" / "001.json").write_text(json.dumps(hand))
        self.judge("001", {"Cart page": [("UNCLEAR", 0.3), ("MET", 0.9)]})
        self.assertEqual(json.loads((self.proj / "results" / "001.json").read_text()), hand)
        self.assertEqual((self.ask.called, self.ev.called), (False, False))
        self.assertIn("001 skipped: accepted by hand (hand_done); pass --force to judge it anyway", self.out.getvalue())
        res = self.judge("001", {"Cart page": [("MET", 0.95), ("MET", 0.9)]}, force=True)
        self.assertEqual((res["status"], self.ask.called, self.ev.called), ("passed", True, True))

    def test_only_a_confirmed_not_met_at_the_threshold_fails(self):
        res = self.judge("001", {"Cart page": [("NOT_MET", 0.65), ("NOT_MET", 0.7)]}, fail=RuntimeError("down"), blocks=True)
        self.assertEqual((res["status"], self.confirm.call_count), ("blocked", 1))
        self.assertIn("CRITERION: Has a pay button", self.confirm.call_args.args[2])
        replies = {"Cart page": [("MET", 0.9), ("MET", 0.9)], "Pay": [("NOT_MET", 0.9)]}
        with self.assertRaises(SystemExit) as e:
            self.judge("001,004", replies, confirm={"violated": True, "evidence": "FRAME 004"})
        self.assertNotIn(e.exception.code, (0, None))
        res = json.loads((self.proj / "results" / "004.json").read_text())
        self.assertEqual((res["status"], len(res["failed"])), ("failed", 1))
        self.assertIn("004 NOT_MET p_not_met=0.90 Has a card field", self.out.getvalue())
        self.assertEqual([c.args[1]["id"] for c in self.ev.call_args_list], ["001"])

    def test_a_refuted_not_met_or_unclear_is_re_asked_once_and_only_a_met_reply_passes(self):
        refuted = {"violated": False, "evidence": "Criterion is satisfied"}
        res = self.judge("004", {"Pay": [("NOT_MET", 0.9)]}, confirm=refuted, reask="MET")
        row = res["criteria"][0]
        self.assertEqual((res["status"], row["choice"], row["first_choice"], row["reask"]["choice"], row["evidence"]), ("passed", "MET", "NOT_MET", "MET", "Criterion is satisfied"))
        self.assertEqual(self.ask.call_count, 2)
        self.assertIn("Criterion is satisfied", self.ask.call_args.args[3]["c0"]["instructions"]["context"])
        res = self.judge("004", {"Pay": [("UNCLEAR", 0.3)]}, reask="MET")
        self.assertEqual((res["status"], self.ask.call_count, self.confirm.called), ("passed", 2, False))
        for first, again in (("NOT_MET", "NOT_MET"), ("NOT_MET", "UNCLEAR"), ("UNCLEAR", "NOT_MET"), ("UNCLEAR", "UNCLEAR")):
            res = self.judge("004", {"Pay": [(first, 0.9)]}, confirm=refuted, reask=again, blocks=True)
            self.assertEqual((res["status"], len(res["unclear"]), self.ask.call_count), ("failed", 1, 2), (first, again))

    def test_a_task_figma_line_is_the_file_the_result_names(self):
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            {**judge_task("ENG-3", "001", "Cart page", ["x"]), "description": judge_task("ENG-3", "001", "Cart page", ["x"])["description"].replace("test: t", "test: t\nfigma: LibKey123456789")}]})
        res = self.judge("001", {"Cart page": [("MET", 0.95)]})
        self.assertEqual(res["branch"], "figma:LibKey123456789")

    def test_a_violation_shown_only_by_a_node_inside_an_instance_is_no_violation_of_built_from_instances(self):
        """Design 107: the confirm step quoted a RECTANGLE inside an instance as hand-drawn although the saved tree marks it inInstance."""
        crit = "Built only from component instances plus layout frames"
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [
            judge_task("ENG-6", "004", "Pay", [crit, "Has a card field"])]})
        doc = {"nodes": [{"id": "1:2", "type": "FRAME", "children": [
            {"id": "5:1", "type": "INSTANCE", "children": [{"id": "I5:1;7:2", "type": "RECTANGLE", "inInstance": "5:1"}]},
            {"id": "1:3", "type": "RECTANGLE", "name": "Divider"}]}]}
        doc["nodes"][0]["children"][0]["children"].append({"id": "I5:1;7:3", "type": "FRAME", "name": "Card header", "inInstance": "5:1"})
        (self.proj / "results" / "004.tree.txt").write_text("NODE 1:2\n" + json.dumps(doc, indent=2) + "\n")
        inside, loose = ({"violated": True, "evidence": f"Node {i} is type RECTANGLE, not INSTANCE or FRAME"} for i in ("I5:1;7:2", "1:3"))
        res = self.judge("004", {"Pay": [("NOT_MET", 0.9), ("NOT_MET", 0.9)]}, confirm=inside, reask="MET", blocks=True)
        first, second = res["criteria"]
        self.assertEqual((first["confirmed"], first["choice"], [r["criterion"] for r in res["failed"]]), (False, "MET", ["Has a card field"]))
        self.assertIn("only nodes inside a component instance", first["evidence"])
        self.assertTrue(second["confirmed"])  # a criterion that is not about instances keeps the verdict
        res = self.judge("004", {"Pay": [("NOT_MET", 0.9), ("MET", 0.9)]}, confirm=loose, blocks=True)
        self.assertEqual((res["criteria"][0]["confirmed"], res["status"]), (True, "failed"))
        both = {"violated": True, "evidence": "Node 5:1 is an INSTANCE but I5:1;7:2 is type RECTANGLE, not INSTANCE or FRAME"}  # the instance itself is named too
        res = self.judge("004", {"Pay": [("NOT_MET", 0.9), ("NOT_MET", 0.9)]}, confirm=both, reask="MET", blocks=True)
        self.assertEqual(res["criteria"][0]["confirmed"], False)
        for evidence, confirmed in (('Node 1:2 holds a FRAME "Card header" that is not a layout frame', False),  # a named container and a node named only by its name
                                    ('Node 1:2 holds a RECTANGLE "Divider" that is not an instance', True),  # a hand-drawn node, though it also names the container
                                    ('Node 1:2 holds a "Card header" and a "Divider"', True)):
            res = self.judge("004", {"Pay": [("NOT_MET", 0.9), ("NOT_MET", 0.9)]}, confirm={"violated": True, "evidence": evidence}, reask="MET", blocks=True)
            self.assertEqual(res["criteria"][0]["confirmed"], confirmed, evidence)

    def test_a_confirm_error_blocks_with_no_re_ask(self):
        res = self.judge("004", {"Pay": [("NOT_MET", 0.9)]}, fail=RuntimeError("down"), reask="MET", blocks=True)
        self.assertEqual((res["status"], res["criteria"][0]["choice"], self.ask.call_count), ("blocked", "NOT_MET", 1))

    def test_only_all_met_passes_so_unclear_and_unconfirmed_not_met_block(self):
        res = self.judge("001", {"Cart page": [("NOT_MET", 0.0), ("UNCLEAR", 0.14)]}, confirm={"violated": False, "evidence": ""}, blocks=True)
        self.assertEqual((res["status"], res["failed"], len(res["unclear"])), ("failed", [], 2))
        self.assertFalse(self.ev.called)

    def test_refuses_with_one_line(self):
        for only, needle in ((None, "figma judge needs --only <ids>"), ("009", "009"), ("002", "002"), ("003", f"no node tree at {self.proj / 'results' / '003.tree.txt'}"), ("005", "005: no acceptance criteria to judge")):
            with self.subTest(only=only), self.assertRaises(SystemExit) as e:
                self.judge(only, {})
            self.assertIn(needle, str(e.exception))
            self.assertNotIn("\n", str(e.exception))
            self.assertEqual("no node tree" in str(e.exception), only == "003")

    def test_choices_name_the_tree_not_the_code(self):
        self.judge("001", {"Cart page": [("MET", 0.9), ("MET", 0.9)]})
        tree = {"MET": "The tree satisfies this criterion.", "NOT_MET": "The tree violates or omits this criterion.", "UNCLEAR": "Cannot be decided from the tree shown."}
        for q in self.ask.call_args.args[3].values():
            self.assertEqual(q["criteria"], tree)
            self.assertNotIn("code", " ".join(q["criteria"].values()).lower())

    def test_a_billing_halt_from_the_confirm_call_stops_the_command(self):
        with self.assertRaises(fc.fc_models.PaymentRequired):
            self.judge("001,004", {"Cart page": [("NOT_MET", 0.9), ("MET", 0.9)], "Pay": [("MET", 0.9)]}, fail=fc.fc_models.PaymentRequired("openrouter", "402"))
        self.assertEqual((self.confirm.call_count, self.ask.call_count, self.ev.called), (1, 1, False))
        self.assertFalse((self.proj / "results" / "001.json").exists())

    def test_a_confirm_reply_wrapped_in_a_list_is_unwrapped(self):
        res = self.judge("004", {"Pay": [("NOT_MET", 0.9)]}, confirm=[{"violated": True, "evidence": "FRAME 004"}], blocks=True)
        self.assertEqual((res["status"], res["criteria"][0]["evidence"], self.confirm.call_count, res["judge_errors"]), ("failed", "FRAME 004", 1, []))

    def test_only_a_violated_reply_confirms_a_not_met(self):
        """Asked for {"confirmed": bool}, the confirmer answered confirmed=true with evidence that the criterion is met (on "No
        sidebar ...": "no Sidebar exists"), reading the key as "the criterion is confirmed", and a met criterion failed. The
        reply names the violation, after its evidence, and the prompt says a met or rightly absent criterion is no violation."""
        for reply, status in (({"evidence": "no node is a Sidebar, as the criterion requires", "violated": False}, "passed"),
                              ({"evidence": "FRAME 004 is a Sidebar", "violated": True}, "failed")):
            res = self.judge("004", {"Pay": [("NOT_MET", 0.9)]}, confirm=reply, reask="MET", blocks=True)
            self.assertEqual((res["status"], res["judge_errors"]), (status, []), reply)
        system = self.confirm.call_args_list[0].args[1]
        self.assertIn('{"evidence": str, "violated": bool}', system)
        self.assertIn("absent and it is absent, is violated=false", system)

    def test_a_confirm_call_that_still_fails_is_a_judge_error_not_a_verdict(self):
        for kw in ({"confirm": [1, 2]}, {"fail": ValueError("bad json")}):
            res = self.judge("004", {"Pay": [("NOT_MET", 0.9)]}, blocks=True, **kw)
            row = res["criteria"][0]
            self.assertEqual((res["status"], row["confirmed"], row["judge_error"], res["failed"], len(res["judge_errors"])), ("blocked", False, True, [], 1))
            self.assertIn("confirm call failed", row["evidence"])
            self.assertEqual(self.confirm.call_count, 2)  # retried once with a stricter instruction
            self.assertIn("no list", self.confirm.call_args.args[1])
            self.assertIn("BLOCKED by a judge tool error", self.out.getvalue())
            self.assertFalse(self.ev.called)


    def judge_big(self, answer, confirm=None, text=None, criterion="Every variant has a Tint"):
        """Judge task 001 on a big tree; answer(part_text) -> (choice, p_not_met) per chunk, one criterion. `confirm` is the
        confirm call's reply, or a function of (system, user) giving it."""
        (self.proj / "results" / "001.tree.txt").write_text(text or big_tree())
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [judge_task("ENG-3", "001", "Cart page", [criterion])]})
        self.states, self.questions = [], []

        def jev(env, cfg, state, q, label):
            self.states.append(state)
            self.questions.append(q)
            choice, p = answer(state["tree"])
            return {"model": "jev-test", "answers": {"c0": {"choice": choice, "probabilities": {choice: 0.9, "NOT_MET": p}}}}
        reply = {"side_effect": lambda env, system, user, **kw: confirm(system, user)} if callable(confirm) else {"return_value": confirm}
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_models, "jev_ask", side_effect=jev), \
                mock.patch.object(fc.fc_models, "fast_json", **reply) as self.confirm, mock.patch.object(fc.fc_linear, "linear_event"), mock.patch("sys.stdout", io.StringIO()):
            try:
                fc.fc_figma.cmd_figma(SimpleNamespace(repo=".", project="p", action="judge", figma=None, only="001"))
            except SystemExit as e:
                self.assertEqual(e.code, 1)
        return json.loads((self.proj / "results" / "001.json").read_text())

    def test_chunks_hold_every_node_and_stay_under_the_limit(self):
        text = big_tree()
        chunks = fc.fc_figma.fc_figmanodes.tree_chunks(text)
        self.assertGreater(len(chunks), 4)
        self.assertTrue(all(len(c) <= fc.fc_figma.fc_figmanodes.TREE_CHUNK_CHARS for c in chunks))
        joined = "\n".join(chunks)
        for i in range(15):
            self.assertIn(f'"id": "1:{i}"', joined)
            self.assertIn(f'"id": "2:{i}"', joined)
        self.assertEqual(fc.fc_figma.fc_figmanodes.tree_chunks("small tree\n"), ["small tree\n"])

    def test_every_chunk_carries_the_write_results_binding_facts(self):
        """Framelink shows a variable-bound fill as its hex, so the judge proves a binding only from the WRITE RESULT; a part that
        holds the hex fills but not that section made it call bound fills raw hex."""
        facts = "WRITE RESULT (facts the write script returned: node ids, variable bindings, component keys)\n" + json.dumps({"bound": "color/bg"})
        chunks = fc.fc_figma.fc_figmanodes.tree_chunks(big_tree() + "\n" + facts + "\n")
        self.assertGreater(len(chunks), 4)
        self.assertTrue(all(len(c) <= fc.fc_figma.fc_figmanodes.TREE_CHUNK_CHARS for c in chunks))
        self.assertTrue(all(c.count(facts) == 1 for c in chunks))
        self.assertEqual("\n".join(chunks).count('"id": "2:14"'), 1)

    def test_the_judge_is_told_a_bound_fill_reads_as_its_hex(self):
        for rules in (fc.fc_figma.TREE_READING, fc.fc_figma.CONFIRM):
            self.assertIn("resolved hex", rules)
            self.assertIn("WRITE RESULT", rules)

    def test_a_node_with_no_children_or_a_plain_text_tree_is_still_covered(self):
        blob = "NODE 1:1\n" + "".join(f"line {i} " + "y" * 200 + "\n" for i in range(500))
        chunks = fc.fc_figma.fc_figmanodes.tree_chunks(blob)
        self.assertTrue(all(len(c) <= fc.fc_figma.fc_figmanodes.TREE_CHUNK_CHARS for c in chunks))
        self.assertEqual("".join(chunks).count("line "), 500)
        self.assertEqual("".join(fc.fc_figma.fc_figmanodes.tree_chunks("z" * 70000 + "\n")).count("z"), 70000)

    def test_a_big_tree_is_judged_whole_and_the_result_counts_the_chunks(self):
        res = self.judge_big(lambda t: ("MET", 0.0) if '"id": "2:7"' in t else ("UNCLEAR", 0.0))
        self.assertEqual((res["status"], res["chunks"], len(self.states)), ("passed", len(self.states), res["chunks"]))
        self.assertGreater(res["chunks"], 1)
        self.assertTrue(all(len(s["tree"]) <= fc.fc_figma.fc_figmanodes.TREE_CHUNK_CHARS for s in self.states))
        self.assertEqual([s["tree_part"] for s in self.states][:2], [f"1 of {res['chunks']}", f"2 of {res['chunks']}"])

    def test_a_confirmed_violation_in_one_chunk_fails_the_criterion_whatever_the_others_say(self):
        res = self.judge_big(lambda t: ("NOT_MET", 0.9) if '"id": "1:12"' in t else ("MET", 0.0), confirm={"violated": True, "evidence": "variant=12"})
        self.assertEqual((res["status"], res["criteria"][0]["choice"], res["criteria"][0]["evidence"]), ("failed", "NOT_MET", "variant=12"))

    def test_a_node_held_by_another_chunk_is_met_though_the_chunks_without_it_said_not_met(self):
        """Issue 87: the node a criterion names sits in one chunk; a chunk without it answered NOT_MET (it omits the node), and a
        confirmer shown only that chunk agreed. What a chunk without the node answers varies from run to run, so the verdict must
        not depend on it: the confirm call sees the whole tree, finds the node, and the chunk that holds it decides."""
        text = big_tree(5)
        has = lambda t: '"id": "2:4"' in t
        confirm = lambda system, user: {"violated": not has(user), "evidence": "2:4 is in the tree" if has(user) else "the tree has no 2:4"}
        for absent in (("NOT_MET", 0.9), ("UNCLEAR", 0.0), ("NOT_MET", 0.3)):
            with self.subTest(absent=absent):
                res = self.judge_big(lambda t: ("MET", 0.0) if has(t) else absent, confirm=confirm, text=text, criterion="The last variant has its Tint (2:4)")
                row = res["criteria"][0]
                self.assertEqual((res["status"], row["choice"], res["failed"], res["chunks"]), ("passed", "MET", [], len(fc.fc_figma.fc_figmanodes.tree_chunks(text))))
                self.assertEqual((res["chunks"] > 2, row["parts"].count("MET"), row["parts"].count(absent[0])), (True, 1, res["chunks"] - 1))
                self.assertEqual({c.args[2].split("TREE:\n", 1)[1] for c in self.confirm.call_args_list}, {text} if absent[0] == "NOT_MET" else set())

    def test_each_chunk_is_judged_as_a_part_so_a_node_it_lacks_is_unclear_not_a_violation(self):
        self.judge_big(lambda t: ("MET", 0.0), text=big_tree(5))
        for q in self.questions:
            crit, rules = q["c0"]["criteria"], q["c0"]["instructions"]["rules"]
            self.assertNotIn("omits", crit["NOT_MET"])
            self.assertIn("another part", crit["UNCLEAR"])
            self.assertIn("another part", rules)

    def test_a_tree_too_big_for_one_confirm_call_is_confirmed_chunk_by_chunk_where_absence_is_no_violation(self):
        has = lambda t: '"id": "2:12"' in t
        res = self.judge_big(lambda t: ("MET", 0.0) if has(t) else ("NOT_MET", 0.9), confirm={"violated": False, "evidence": "nothing here violates it"})
        self.assertGreater(len(big_tree()), fc.fc_figma.CONFIRM_TREE_CHARS)
        self.assertEqual((res["status"], self.confirm.call_count), ("passed", res["chunks"] - 1))
        for call in self.confirm.call_args_list:
            system, user = call.args[1:3]
            self.assertIn("never a violation", system)
            self.assertRegex(user, rf"TREE \(part \d+ of {res['chunks']}\)")
            self.assertLessEqual(len(user), fc.fc_figma.fc_figmanodes.TREE_CHUNK_CHARS + 500)

    def test_no_chunk_showing_met_leaves_the_criterion_unmet(self):
        res = self.judge_big(lambda t: ("UNCLEAR", 0.0), confirm={"confirmed": False, "evidence": "the tree does not show it"})
        self.assertEqual((res["status"], res["criteria"][0]["choice"]), ("failed", "UNCLEAR"))

    def test_a_criterion_whose_nodes_sit_in_different_chunks_is_settled_on_the_whole_tree(self):
        """Issue 343: no chunk holds both 2:0 and 2:4, so each answers UNCLEAR and none can say MET. The whole tree fits one call,
        which is asked once whether it meets every part; its yes passes the criterion, its no leaves the re-ask as before, and a
        call that fails is a judge error."""
        text, crit = big_tree(5), "The first and last variants have their Tint (2:0 and 2:4)"
        self.assertGreater(len(fc.fc_figma.fc_figmanodes.tree_chunks(text)), 2)
        both = lambda user: '"id": "2:0"' in user and '"id": "2:4"' in user
        for met, status, choice in ((True, "passed", "MET"), (False, "failed", "UNCLEAR")):
            with self.subTest(met=met):
                res = self.judge_big(lambda t: ("UNCLEAR", 0.0), text=text, criterion=crit,
                                     confirm=lambda system, user: {"confirmed": met and both(user), "evidence": "2:0 and 2:4 each hold a Tint"})
                row = res["criteria"][0]
                self.assertEqual((res["status"], row["choice"], row["whole_tree"]["met"]), (status, choice, met))
                system, user = self.confirm.call_args_list[0].args[1:3]
                self.assertEqual((self.confirm.call_count, user.split("TREE:\n", 1)[1]), (1, text))
                self.assertIn("SATISFIES", system)
                self.assertEqual("reask" in row, not met)  # a yes needs no re-ask
        res = self.judge_big(lambda t: ("UNCLEAR", 0.0), text=text, criterion=crit, confirm=lambda system, user: {"no": "verdict"})
        self.assertEqual((res["status"], res["criteria"][0]["choice"]), ("blocked", "UNCLEAR"))
        self.assertIn("confirm call failed", res["judge_errors"][0]["evidence"])
        self.assertNotIn("reask", res["criteria"][0])  # the chunks' re-ask could still turn it MET

    def test_a_tree_over_the_confirm_size_is_still_settled_on_the_whole_tree_when_it_fits_one_call(self):
        """Task 059 had 186695 characters in 8 chunks, every chunk UNCLEAR on "24 variants, each 44 or 48 high", and the whole-tree
        call that says so was never made. Only a tree beyond SETTLE_TREE_CHARS stays with the chunks."""
        text, crit = big_tree(), "The first and last variants have their Tint (2:0 and 2:14)"
        self.assertTrue(fc.fc_figma.CONFIRM_TREE_CHARS < len(text) <= fc.fc_figma.SETTLE_TREE_CHARS)
        for met, status in ((True, "passed"), (False, "failed")):
            with self.subTest(met=met):
                res = self.judge_big(lambda t: ("UNCLEAR", 0.0), text=text, criterion=crit, confirm={"confirmed": met, "evidence": "both hold a Tint"})
                self.assertEqual((res["status"], self.confirm.call_count, res["criteria"][0]["whole_tree"]["met"]), (status, 1, met))
                self.assertEqual(self.confirm.call_args.args[2].split("TREE:\n", 1)[1], text)
        huge = big_tree(40)
        self.assertGreater(len(huge), fc.fc_figma.SETTLE_TREE_CHARS)
        res = self.judge_big(lambda t: ("UNCLEAR", 0.0), text=huge, criterion=crit, confirm={"confirmed": True, "evidence": "x"})
        self.assertEqual((res["status"], self.confirm.call_count, "whole_tree" in res["criteria"][0]), ("failed", 0, False))

    def test_a_chunk_that_errors_blocks_and_is_never_a_pass(self):
        def jev(env, cfg, state, q, label):
            if state.get("tree_part", "").startswith("2 of"):
                raise RuntimeError("max_tokens_exceeded")
            return {"model": "m", "answers": {"c0": {"choice": "MET", "probabilities": {"MET": 0.9}}}}
        (self.proj / "results" / "001.tree.txt").write_text(big_tree())
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [judge_task("ENG-3", "001", "Cart page", ["Every variant has a Tint"])]})
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_models, "jev_ask", side_effect=jev), mock.patch.object(fc.fc_linear, "linear_event") as ev, mock.patch("sys.stdout", io.StringIO()), self.assertRaises(SystemExit):
            fc.fc_figma.cmd_figma(SimpleNamespace(repo=".", project="p", action="judge", figma=None, only="001"))
        res = json.loads((self.proj / "results" / "001.json").read_text())
        self.assertEqual(res["status"], "blocked")
        self.assertIn("chunk 2 of", res["judge_errors"][0]["evidence"])
        self.assertIn("max_tokens_exceeded", res["judge_errors"][0]["evidence"])
        self.assertFalse(ev.called)


class FigmaCaps(unittest.TestCase):
    """The capability preflight: plan limits recorded once, a blocking limit is one up-front card, never a failing write."""
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "figma": "AbCdEf123456"}))

    def check(self, **kw):
        ns = dict(repo=".", project="p", action="check", figma=None, only=None, plan=None, pages=None, modes=None, need_pages=None, need_modes=None)
        out = io.StringIO()
        top = '{"nodes": [{"type": "CANVAS", "id": "0:1"}, {"type": "CANVAS", "id": "0:2"}]}'
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("sys.stdout", out), \
             mock.patch.object(fc.fc_figma, "framelink_fetch", return_value=top):
            try:
                fc.fc_figma.cmd_figma(SimpleNamespace(**{**ns, **kw}))
            except SystemExit as e:
                return out.getvalue(), str(e)
        return out.getvalue(), None

    def caps(self):
        return json.loads((self.proj / "linear.json").read_text()).get("figma_caps")

    def test_plan_tier_reads_whoami_text(self):
        t = fc.fc_figma.fc_figmaplan.plan_tier
        for text, tier in (("Starter", "starter"), ("free", "starter"), ("pro", "professional"), ("Professional (full seat)", "professional"),
                           ("org", "organization"), ("Organization", "organization"), ("enterprise", "enterprise"), ("team", None), ("", None), (None, None)):
            self.assertEqual(t(text), tier, text)

    def test_a_starter_plan_blocks_the_design_system_before_any_planning(self):
        out, err = self.check(plan="starter")
        self.assertEqual(err, "the Figma plan blocks the design")
        self.assertIn("plan starter; pages per file 3; modes per collection 1; library publish no", out)
        self.assertIn("the file has 2 page(s) now", out)
        self.assertIn("BLOCKED variable modes: the design system needs 2 per collection (Light and Dark), the starter plan allows 1", out)
        self.assertIn("raise ONE needs-user card", out)
        self.assertIn("measure them with the probe", out)
        self.assertEqual(self.caps()["plan"], "starter")
        self.assertEqual(self.caps()["source"], "table")

    def test_a_design_with_one_mode_is_not_blocked_by_a_starter_plan(self):
        out, err = self.check(plan="starter", need_modes=1)
        self.assertIsNone(err)
        self.assertNotIn("BLOCKED", out)
        out, err = self.check(plan="starter", need_modes=2)
        self.assertIn("BLOCKED variable modes: the design system needs 2 per collection", out)

    def test_a_probed_value_replaces_the_predicted_limit_and_an_upgrade_unblocks(self):
        out, err = self.check(plan="starter", modes="unlimited", pages="3", need_pages=5)
        self.assertEqual(err, "the Figma plan blocks the design")
        self.assertNotIn("variable modes", out)
        self.assertIn("BLOCKED pages: the design needs 5 per file, the starter plan allows 3", out)
        self.assertNotIn("measure them with the probe", out)  # already measured
        self.assertEqual(self.caps()["source"], "probe")
        out, err = self.check(plan="pro", need_pages=5)  # after the upgrade
        self.assertIsNone(err)
        self.assertIn("plan professional; pages per file no limit; modes per collection 4; library publish yes", out)
        self.assertNotIn("BLOCKED", out)

    def test_check_without_a_plan_shows_what_is_stored_or_says_it_is_not_checked(self):
        out, err = self.check()
        self.assertIn("capabilities not checked", out)
        self.assertIn("compose figma check --project <slug> --plan", out)
        self.check(plan="org")
        out, _ = self.check()
        self.assertIn("plan organization", out)

    def test_unknown_plan_and_bad_counts_are_refused(self):
        for kw, msg in ((dict(plan="team"), "unknown Figma plan"), (dict(plan="pro", pages="lots"), "not a count or `unlimited`: lots")):
            out, err = self.check(**kw)
            self.assertIn(msg, err)
        self.assertIsNone(self.caps())

    def test_designcrews_stops_when_the_tasks_need_more_pages_than_the_plan_allows(self):
        metas = [{"id": t, "kind": "design", "nodes": [f"{p}/X"], "title": t} for t, p in (("001", "Buttons"), ("002", "Inputs"), ("003", "Cards"), ("004", "Forms"))]
        self.assertEqual(fc.fc_figma.fc_figmaplan.design_pages(metas + [{"id": "5", "kind": "code", "nodes": ["Z/Y"]}]), {"Buttons", "Inputs", "Cards", "Forms"})
        caps = fc.fc_figma.fc_figmaplan.figma_caps("starter", modes=None)
        self.assertEqual(fc.fc_figma.fc_figmaplan.caps_blockers(caps, 4, 0), ["pages: the design needs 4 per file, the starter plan allows 3"])
        self.assertEqual(fc.fc_figma.fc_figmaplan.caps_blockers(caps, 3, 0), [])
        (self.proj / "linear.json").write_text(json.dumps({"figma": "AbCdEf123456", "figma_caps": caps}))
        with mock.patch.object(fc.fc_figma.fc_tasks, "load_tasks", return_value=metas), mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj):
            with self.assertRaises(SystemExit) as e:
                fc.fc_figma.cmd_designcrews(SimpleNamespace(repo=".", project="p", only=None, print_=False, max=None, redo=False))
        self.assertIn("pages: the design needs 4 per file, the starter plan allows 3", str(e.exception))
        self.assertIn("raise ONE needs-user card", str(e.exception))

    def test_the_probe_and_the_publish_step_are_in_the_designer_brief_and_the_stored_caps_follow_it(self):
        brief = fc.fc_figma.DESIGNER_BRIEF.format(slug="p", wt="/w", feature="ENG-1", figma="AbCd")
        for text in ("Capability preflight", "compose figma check --project p --plan", "figma.createPage()", "addMode", "create_new_file", "compose-probe",
                     "--pages <pages> --modes <modes>", "one needs-user card", "compose figma publish --project p --library", "`publish: none`", "`publish: ask`"):
            self.assertIn(text, brief)
        wt = Path(tempfile.mkdtemp())
        for caps, line in ((None, "Figma capabilities: not checked yet"), (fc.fc_figma.fc_figmaplan.figma_caps("pro"), "Figma capabilities (table): plan professional")):
            (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "figma": "AbCdEf123456", **({"figma_caps": caps} if caps else {})}))
            out = io.StringIO()
            with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_herdr, "project_worktree", return_value=wt), \
                 mock.patch.object(fc.fc_figma.fc_init, "check_deps", return_value=[]), mock.patch("sys.stdout", out):
                fc.fc_figma.cmd_designer(SimpleNamespace(repo=".", project="p", extra=None, figma=None))
            self.assertIn(line, out.getvalue())


class FigmaPublish(unittest.TestCase):
    """A publish is asked for only when the library shows one pending, and at most one ask is open at a time."""
    LIB = "LibKey1234567"

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        self.proj.mkdir(parents=True)
        self.set({"feature": "ENG-1", "figma": "AbCdEf123456"})
        self.library = {"components": [{"key": "ck1", "updated_at": "2026-10-20T10:00:00Z"}, {"key": "ck2", "updated_at": "2026-10-20T10:30:00Z"}],
                        "component_sets": [], "styles": [{"key": "sk1", "updated_at": "2026-10-20T09:00:00Z"}]}
        self.modified = "2026-10-20T10:30:30Z"

    def set(self, data):
        (self.proj / "linear.json").write_text(json.dumps(data))

    def data(self):
        return json.loads((self.proj / "linear.json").read_text())

    def api(self, path, env_file=None, setup=""):
        if path.endswith("?depth=1"):
            return {"lastModified": self.modified}
        kind = path.rsplit("/", 1)[1]
        return {"meta": {kind: self.library[kind]}}

    def publish(self, **kw):
        ns = dict(repo=".", project="p", action="publish", library=self.LIB, keys=None, withdraw=False)
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("sys.stdout", out), \
             mock.patch.object(fc.fc_figma.fc_figmaplan, "figma_api", side_effect=self.api):
            try:
                fc.fc_figma.cmd_figma(SimpleNamespace(**{**ns, **kw}))
            except SystemExit as e:
                return str(e)
        return out.getvalue()

    def test_a_library_published_after_its_last_edit_has_nothing_pending(self):
        out = self.publish()
        self.assertTrue(out.startswith("publish: none\n"), out)
        self.assertIn("do not ask the product owner", out)
        self.assertEqual(self.data()["figma_library"], self.LIB)
        self.assertNotIn("figma_publish_ask", self.data())

    def test_an_unpublished_key_is_pending_and_a_second_check_finds_the_ask_open(self):
        out = self.publish(keys="ck1,ck9")
        self.assertTrue(out.startswith("publish: ask\n"), out)
        self.assertIn("1 needed key(s) are not published: ck9", out)
        self.assertEqual(self.data()["figma_publish_ask"]["library"], self.LIB)
        again = self.publish(keys="ck1,ck9,ck8")
        self.assertTrue(again.startswith("publish: open\n"), again)
        self.assertIn("do not ask again", again)

    def test_an_edit_after_the_last_publish_is_pending_but_an_edit_within_the_slack_is_not(self):
        self.modified = "2026-10-20T10:31:30Z"
        self.assertTrue(self.publish().startswith("publish: none"))
        self.modified = "2026-10-20T11:00:00Z"
        out = self.publish()
        self.assertTrue(out.startswith("publish: ask\n"), out)
        self.assertIn("edited at 2026-10-20 11:00Z, after its last publish at 2026-10-20 10:30Z", out)

    def test_the_ask_closes_when_the_publish_shows_and_a_new_edit_opens_a_new_one(self):
        self.modified = "2026-10-20T11:00:00Z"
        self.assertTrue(self.publish().startswith("publish: ask"))
        self.library["components"].append({"key": "ck3", "updated_at": "2026-10-21T00:00:00Z"})  # the product owner published
        self.modified = "2026-10-21T00:00:10Z"
        self.assertTrue(self.publish().startswith("publish: none"))
        self.assertNotIn("figma_publish_ask", self.data())
        self.modified = "2026-10-21T02:00:00Z"
        self.assertTrue(self.publish().startswith("publish: ask"))

    def test_a_withdrawn_ask_reads_none_until_the_publish_shows(self):
        self.modified = "2026-10-20T11:00:00Z"
        self.assertTrue(self.publish().startswith("publish: ask"))
        out = self.publish(withdraw=True)
        self.assertTrue(out.startswith("publish: none\n"), out)
        self.assertIn("withdrawn", out)
        self.assertNotIn("figma_publish_ask", self.data())
        self.assertTrue(self.publish().startswith("publish: none\n"))
        self.library["components"].append({"key": "ck3", "updated_at": "2026-10-21T00:00:00Z"})  # a publish shows: the withdrawal is spent
        self.modified = "2026-10-21T00:00:10Z"
        self.assertTrue(self.publish().startswith("publish: none"))
        self.assertNotIn("figma_publish_withdrawn", self.data())
        self.modified = "2026-10-21T02:00:00Z"
        self.assertTrue(self.publish().startswith("publish: ask"))

    def test_nothing_published_yet_is_pending(self):
        self.library = {"components": [], "component_sets": [], "styles": []}
        out = self.publish()
        self.assertTrue(out.startswith("publish: ask\n"), out)
        self.assertIn("nothing in the library is published yet", out)

    def test_a_plan_that_cannot_publish_and_an_unreadable_library_never_ask(self):
        self.set({"figma_caps": fc.fc_figma.fc_figmaplan.figma_caps("starter")})
        out = self.publish()
        self.assertTrue(out.startswith("publish: none\nthe starter plan cannot publish a library"), out)
        self.set({})
        with mock.patch.object(fc.fc_figma.fc_figmaplan, "figma_api", side_effect=RuntimeError("figma api /files/x/components failed: HTTP 403 (the token needs the library_content:read scope)")), \
             mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj):
            with self.assertRaises(SystemExit) as e:
                fc.fc_figma.cmd_figma(SimpleNamespace(repo=".", project="p", action="publish", library=self.LIB, keys=None, withdraw=False))
        self.assertTrue(str(e.exception).startswith("publish: unknown\n"))
        self.assertIn("library_content:read", str(e.exception))
        self.assertIn("do not ask", str(e.exception))

    def test_a_library_is_remembered_and_junk_or_none_is_refused(self):
        self.publish()
        self.assertTrue(self.publish(library=None).startswith("publish: none"))
        self.assertIn("not a Figma file url or key: junk", self.publish(library="junk"))
        self.set({})
        self.assertIn("no library file", self.publish(library=None))

    def test_figma_api_needs_the_token_and_never_leaks_it(self):
        env = self.proj / "fl.env"
        with self.assertRaises(RuntimeError) as e:
            fc.fc_figma.fc_figmaplan.figma_api("/files/K/components", env_file=env, setup="SETUP TEXT")
        self.assertEqual(str(e.exception), "SETUP TEXT")
        env.write_text("# c\nFIGMA_API_KEY=figd_secret_token\n")
        err = fc.fc_figma.fc_figmaplan.urllib.error.HTTPError("u", 403, "forbidden", {}, None)
        self.addCleanup(err.close)
        with mock.patch.object(fc.fc_figma.fc_figmaplan.urllib.request, "urlopen", side_effect=err) as op, self.assertRaises(RuntimeError) as e:
            fc.fc_figma.fc_figmaplan.figma_api("/files/K/components?x=1", env_file=env)
        self.assertEqual(op.call_args[0][0].get_header("X-figma-token"), "figd_secret_token")
        self.assertIn("HTTP 403 (the token needs the library_content:read scope)", str(e.exception))
        self.assertNotIn("figd_secret_token", str(e.exception))
        self.assertNotIn("x=1", str(e.exception))


def png(w, h, rgb=(255, 255, 255)):
    """A plain PNG of w x h pixels, stdlib only."""
    import struct, zlib
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class FigmaScreens(unittest.TestCase):
    """save_screens with jev's tiles tool faked as `tile`: two tiles for a render over 2000 pixels tall, else one."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.shots = fc.fc_figma.fc_figmashots
        self.tiled = []
        def tile(src, out_dir, stem, tiles_max):
            self.tiled.append((stem, tiles_max))
            n = min(tiles_max, 2 if src.read_bytes() == b"tall" else 1)
            names = [f"{stem}.png"] if n == 1 else [f"{stem}.{k}.png" for k in range(1, n + 1)]
            for name in names:
                (out_dir / name).write_bytes(b"png")
            return [out_dir / name for name in names]
        p = mock.patch.object(self.shots, "tile", side_effect=tile); p.start(); self.addCleanup(p.stop)

    def test_saves_labelled_screens_retries_a_big_node_at_scale_1_and_empties_old_screens(self):
        calls = []
        def api(path):
            calls.append(path)
            big = "scale=2" in path
            return {"err": None, "images": {"1:2": "u12" if big else None, "3:4": None if big else "u34", "5:6": None}}
        out = self.dir / "001.screens"
        out.mkdir()
        (out / "old.png").write_bytes(b"old")
        shots, notes = self.shots.save_screens("K", ["1:2", "3:4", "5:6"], out, api=api, fetch=lambda u: b"tall" if u == "u34" else b"x")
        self.assertEqual(calls, ["/images/K?ids=1:2,3:4,5:6&format=png&scale=2", "/images/K?ids=3:4,5:6&format=png&scale=1"])
        self.assertEqual([label for label, _ in shots], ["1:2", "3:4 (1 of 2, top to bottom)", "3:4 (2 of 2, top to bottom)"])
        self.assertEqual(notes, ["no screen of 5:6: Figma rendered nothing for it"])
        self.assertEqual(self.shots.screens(out), (shots, ["5:6"]))
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(["screens.json"] + [p.name for _, p in shots]))

    def test_the_budget_caps_the_screens_and_tall_frames_share_it(self):
        nodes = [f"1:{i}" for i in range(self.shots.SCREEN_BUDGET + 2)]
        shots, notes = self.shots.save_screens("K", nodes, self.dir / "s", api=lambda p: {"images": {n: "u" for n in nodes}}, fetch=lambda u: b"tall")
        self.assertEqual([m for _, m in self.tiled], [1] * self.shots.SCREEN_BUDGET)  # each shown node keeps one tile
        self.assertEqual(len(shots), self.shots.SCREEN_BUDGET)
        self.assertIn(", ".join(nodes[-2:]), notes[0])
        self.assertEqual(self.shots.screens(self.dir / "s")[1], nodes[-2:])
        self.tiled.clear()
        self.shots.save_screens("K", ["1:2", "3:4"], self.dir / "s", api=lambda p: {"images": {"1:2": "u", "3:4": "u"}}, fetch=lambda u: b"tall")
        self.assertEqual(self.tiled, [("1-2", self.shots.TILES_MAX), ("3-4", self.shots.TILES_MAX)])

    def test_a_figma_error_raises_and_no_manifest_means_no_screens(self):
        with self.assertRaises(RuntimeError) as e:
            self.shots.save_screens("K", ["1:2"], self.dir / "s", api=lambda p: {"err": "Invalid node"}, fetch=None)
        self.assertIn("Invalid node", str(e.exception))
        self.assertEqual(self.shots.screens(self.dir / "s"), ([], []))


class FigmaTiles(unittest.TestCase):
    def test_tile_runs_jevs_tiles_tool_in_its_uv_project_and_reads_its_names(self):
        shots = fc.fc_figma.fc_figmashots
        done = subprocess.CompletedProcess([], 0, stdout='["1-2.1.png", "1-2.2.png"]\n', stderr="")
        with mock.patch.object(shots.subprocess, "run", return_value=done) as run:
            self.assertEqual(shots.tile(Path("/r/raw.png"), Path("/o"), "1-2", 4), [Path("/o/1-2.1.png"), Path("/o/1-2.2.png")])
        self.assertEqual(run.call_args.args[0], ["uv", "run", "--quiet", "--project", str(fc.fc_config.JEV_DIR), "python", "-m", "jev_ultrafast.tiles", "/r/raw.png", "/o", "1-2", "4"])
        with mock.patch.object(shots.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="PIL.UnidentifiedImageError")), self.assertRaises(RuntimeError) as e:
            shots.tile(Path("/r/raw.png"), Path("/o"), "1-2", 4)
        self.assertIn("UnidentifiedImageError", str(e.exception))


class JevImages(unittest.TestCase):
    def ask(self, state):
        sent = {}
        def http(req, provider, timeout=90):
            sent.update(body=json.loads(req.data), headers=dict(req.header_items()), timeout=timeout)
            return {"answers": {}}
        with mock.patch.object(fc.fc_models, "http_json", side_effect=http), mock.patch.object(fc.fc_config, "typesafe_key", return_value="k"):
            fc.fc_models.jev_ask({}, {}, state, {"c0": {"type": "choice"}}, "t")
        return sent

    def test_an_image_goes_once_as_an_asset_with_the_images_header(self):
        p = Path(tempfile.mkdtemp()) / "a.png"
        p.write_bytes(png(300, 300))
        image = fc.fc_models.Image(p)
        sent = self.ask({"tree": "T", "screens": {"1:2": image, "again": fc.fc_models.Image(p)}})
        ref = sent["body"]["state"]["screens"]["1:2"]
        self.assertEqual(sent["body"]["state"]["screens"]["again"], ref)
        self.assertEqual(sent["body"]["assets"], {ref: {"type": "image", "format": "png", "data_base64": __import__("base64").b64encode(p.read_bytes()).decode()}})
        self.assertEqual(sent["headers"].get("Typesafe-danger-zone-images"), "true")
        self.assertEqual(sent["timeout"], 180)

    def test_a_text_state_sends_no_assets_and_no_images_header(self):
        sent = self.ask({"tree": "T"})
        self.assertNotIn("assets", sent["body"])
        self.assertNotIn("Typesafe-danger-zone-images", sent["headers"])


class FigmaJudgeScreens(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()).resolve() / ".compose" / "p"
        shots = self.proj / "results" / "001.screens"
        shots.mkdir(parents=True)
        (shots / "1-2.png").write_bytes(png(300, 300))
        (shots / "screens.json").write_text(json.dumps({"screens": [["1:2", "1-2.png"]], "missing": []}))
        (self.proj / "results" / "001.tree.txt").write_text("FRAME 001\n")
        (self.proj / "linear.json").write_text(json.dumps({"feature": "ENG-1", "figma": "AbCdEf123456"}))
        (self.proj / "verify.json").write_text(json.dumps({"acceptance_threshold": 0.7}))
        fc.fc_tasks.sync_tasks(self.proj, {"feature": {"id": "ENG-1", "title": "F", "description": ""}, "issues": [judge_task("ENG-3", "001", "Cart page", ["Shows the total"])]})
        for mod, name, value in ((fc.fc_config, "ENV_FILE", self.proj / "none.env"), (fc.fc_core, "say", lambda m: None)):
            p = mock.patch.object(mod, name, value); p.start(); self.addCleanup(p.stop)

    def judge(self, tree, screens, confirm=None, tree_reask="UNCLEAR", look="LOOK"):
        """tree: the tree verdict (choice, p); screens: the screen verdicts in order (first ask, then the re-ask), or an exception;
        look: whether a picture can decide the criterion (LOOK or STRUCTURE), or an exception."""
        screens = list(screens) if isinstance(screens, list) else screens
        self.screen_states, self.look_asks = [], []
        def jev(env, cfg, state, q, label):
            if label == "figma judge looks":
                self.look_asks.append((state, q))
                if isinstance(look, Exception):
                    raise look
                choice, p = look if isinstance(look, tuple) else (look, 0.95)
                return {"answers": {"c0": {"choice": choice, "probabilities": {choice: p}}}}
            if label == "figma judge screens":
                self.screen_states.append((state, q))
                if isinstance(screens, Exception):
                    raise screens
                c, p = screens.pop(0)
                return {"answers": {"c0": {"choice": c, "probabilities": {c: 0.9, "NOT_MET": p}}}}
            if label.endswith("re-ask"):
                return reask_reply(tree_reask)
            return {"model": "jev-test", "answers": {"c0": {"choice": tree[0], "probabilities": {tree[0]: 0.9, "NOT_MET": tree[1]}}}}
        self.out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_models, "jev_ask", side_effect=jev), \
                mock.patch.object(fc.fc_models, "fast_json", return_value=confirm), mock.patch.object(fc.fc_linear, "linear_event"), mock.patch("sys.stdout", self.out):
            try:
                fc.fc_figma.cmd_figma(SimpleNamespace(repo=".", project="p", action="judge", figma=None, only="001", force=False))
            except SystemExit as e:
                self.assertEqual(e.code, 1)
        return json.loads((self.proj / "results" / "001.json").read_text())

    def test_jev_sees_every_screen_by_its_node_label(self):
        res = self.judge(("MET", 0.0), [("MET", 0.0)])
        state, q = self.screen_states[0]
        self.assertEqual(list(state["screens"]), ["1:2"])
        self.assertIsInstance(state["screens"]["1:2"], fc.fc_models.Image)
        self.assertEqual(q["c0"]["criteria"], fc.fc_figma.fc_figmashots.SCREEN_CHOICES)
        self.assertEqual((res["status"], res["screens"]), ("passed", ["1:2"]))

    def test_a_violation_the_screens_show_twice_fails_a_criterion_the_tree_passed(self):
        res = self.judge(("MET", 0.0), [("NOT_MET", 0.9), ("NOT_MET", 0.8)])
        row = res["criteria"][0]
        self.assertEqual((res["status"], row["choice"], row["by"], len(res["failed"])), ("failed", "NOT_MET", "screens", 1))
        self.assertIn("001.screens", row["evidence"])

    def test_a_screen_violation_jev_does_not_repeat_or_below_the_threshold_passes(self):
        for screens in ([("NOT_MET", 0.9), ("MET", 0.1)], [("NOT_MET", 0.5)]):
            with self.subTest(screens=screens):
                res = self.judge(("MET", 0.0), screens)
                self.assertEqual((res["status"], res["criteria"][0]["choice"]), ("passed", "MET"))

    def test_the_screens_settle_a_criterion_the_tree_left_unclear_but_never_lift_a_confirmed_violation(self):
        res = self.judge(("UNCLEAR", 0.0), [("MET", 0.0)])
        self.assertEqual((res["status"], res["criteria"][0]["by"]), ("passed", "screens"))
        res = self.judge(("NOT_MET", 0.9), [("MET", 0.0)], confirm={"violated": True, "evidence": "no total"})
        self.assertEqual((res["status"], res["criteria"][0]["choice"]), ("failed", "NOT_MET"))
        res = self.judge(("UNCLEAR", 0.0), [("UNCLEAR", 0.0)])
        self.assertEqual(res["status"], "failed")

    def test_screens_that_cannot_be_judged_block_and_none_saved_judges_the_tree_alone(self):
        res = self.judge(("MET", 0.0), RuntimeError("typesafe: HTTP 400"))
        self.assertEqual(res["status"], "blocked")
        self.assertIn("HTTP 400", res["judge_errors"][0]["evidence"])
        (self.proj / "results" / "001.screens" / "screens.json").unlink()
        res = self.judge(("MET", 0.0), [])
        self.assertEqual((res["status"], res["screens"], self.screen_states), ("passed", [], []))
        self.assertIn("no screens, judged from the tree alone", self.out.getvalue())

    def test_a_criterion_only_the_tree_can_decide_is_never_shown_the_screens(self):
        res = self.judge(("MET", 0.0), [("NOT_MET", 0.9), ("NOT_MET", 0.9)], look="STRUCTURE")
        self.assertEqual((res["status"], res["criteria"][0]["choice"], self.screen_states), ("passed", "MET", []))
        state, q = self.look_asks[0]
        self.assertNotIn("screens", state)  # sorted on its text alone: shown screens, Jev judges a token criterion by the look
        self.assertEqual((q["c0"]["criteria"], q["c0"]["instructions"]["criterion"]), (fc.fc_figma.fc_figmashots.LOOK_CHOICES, "Shows the total"))
        res = self.judge(("UNCLEAR", 0.0), [("MET", 0.0)], look="STRUCTURE")
        self.assertEqual((res["status"], res["criteria"][0]["choice"]), ("failed", "UNCLEAR"))
        res = self.judge(("UNCLEAR", 0.0), [("MET", 0.0)], look=("LOOK", 0.72))  # a named style: a look, but not surely
        self.assertEqual((res["status"], self.screen_states), ("failed", []))
        res = self.judge(("MET", 0.0), [("MET", 0.0)], look="MAYBE")
        self.assertEqual(res["status"], "blocked")
        self.assertIn("which a screen can decide", res["judge_errors"][0]["evidence"])

    def test_a_node_with_no_screen_is_never_missing_from_the_screens(self):
        (self.proj / "results" / "001.screens" / "screens.json").write_text(json.dumps({"screens": [["1:2", "1-2.png"]], "missing": ["3:4"]}))
        self.judge(("MET", 0.0), [("MET", 0.0)])
        self.assertIn("(3:4)", self.screen_states[0][1]["c0"]["instructions"]["rules"])


class FigmaConnectorBriefs(unittest.TestCase):
    def test_designer_and_builder_briefs_name_both_connectors_and_ban_other_plugins(self):
        builder = fc.fc_figma.design_brief({"id": "001", "nodes": ["Page/A"]}, "AbCd", Path("r"))
        designer = fc.fc_figma.DESIGNER_BRIEF.format(slug="p", wt="/w", feature="ENG-1", figma="AbCd")
        for brief in (builder, designer):
            self.assertIn("mcp__claude_ai_Figma__*", brief)
            self.assertIn("mcp__plugin_figma_figma__*", brief)
            self.assertIn("one ToolSearch select call", brief)
            self.assertIn("Never use another `plugin:*figma*` server", brief)

    def test_designer_brief_reads_tokens_through_trail(self):
        designer = " ".join(fc.fc_figma.DESIGNER_BRIEF.format(slug="p", wt="/w", feature="ENG-1", figma="AbCd").split())
        self.assertIn("the repo's tokens through trail (`trail ask \"<question>\" --source`)", designer)
        self.assertNotRegex(designer, r"(?<![\w/.@-])graft(?![\w/-])")  # no graft command or word left anywhere in the brief

    def test_designer_searches_linear_through_compose_linear_and_no_mcp_tool(self):
        designer = " ".join(fc.fc_figma.DESIGNER_BRIEF.format(slug="p", wt="/w", feature="ENG-1", figma="AbCd").split())
        self.assertRegex(designer, r'compose linear list --search[^.;]*"design system", "tokens", "Figma"')
        tools = ("save_issue", "get_issue", "list_issues", "save_comment", "get_project", "list_issue_labels", "create_issue_label",
                 "save_document", "mcp__claude_ai_Linear")
        self.assertEqual([t for t in tools if t in designer], [])


if __name__ == "__main__":
    unittest.main()
