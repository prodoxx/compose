import hermetic  # first: no test sees the real home
import inspect, json, os, subprocess, tempfile, unittest, unittest.mock
from pathlib import Path
import importlib.machinery, importlib.util, sys
# tests/test_modules.py fails a test file without the next line
os.environ["HERDR_ENV"] = ""
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_factorystate as st


BIN = Path(__file__).resolve().parents[1] / "bin"


class PlansLanded(unittest.TestCase):
    def params(self, fn):
        return list(inspect.signature(fn).parameters)

    def test_the_interfaces_this_builds_on_exist_as_promised(self):
        import fc_change as c, fc_delivery as d, fc_linear, fc_plan as p, fc_readiness as r, fc_reconcile
        self.assertEqual([self.params(f) for f in (d.product_plan, d.delivery_order, d.last_reconcile)], [["repo"]] * 3)
        self.assertEqual(self.params(d.reconcile), ["repo", "dry_run"])
        self.assertTrue(d.NO_PLAN.startswith("no product delivery plan yet"))
        self.assertEqual((self.params(p.order_entries), self.params(p.base_built)), (["tracks", "features"], ["state", "labels"]))
        self.assertEqual(self.params(r.story_readiness), ["repo", "story", "refresh", "write"])
        self.assertTrue(issubclass(r.NotAStory, Exception) and callable(r.readiness_line))
        self.assertEqual((self.params(c.rests_on), self.params(c.hold_tasks)), (["proj", "refs"], ["proj", "ids", "reason"]))
        self.assertEqual(fc_linear.NEEDS_SHAPING, "needs-shaping")
        self.assertEqual(self.params(fc_reconcile.live_leads), ["repo"])

    def test_their_verbs_are_registered(self):
        run = lambda *a: subprocess.run([sys.executable, str(BIN / "compose.py"), *a], capture_output=True, text=True).stdout
        for verb in ("delivery", "readiness", "reconcile", "hold"):
            self.assertRegex(run("--help"), rf"(?m)^    {verb}\b")
        self.assertIn("--rests-on", run("hold", "--help"))


class State(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.repo = Path(d.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def test_state_round_trips_and_events_are_numbered(self):
        self.assertEqual(st.load_state(self.repo), st.EMPTY)
        s = st.load_state(self.repo); s["dispatched"]["NEB-10"] = {"feature": "NEB-9"}; st.save_state(self.repo, s)
        self.assertEqual(st.load_state(self.repo)["dispatched"], {"NEB-10": {"feature": "NEB-9"}})
        self.assertEqual(st.state_path(self.repo), self.repo / ".compose" / "factory.json")
        self.assertEqual([st.add_event(self.repo, f"event factory=x n={i}") for i in range(3)], [1, 2, 3])
        self.assertEqual([e["line"] for e in st.events_since(self.repo, 1)], ["event factory=x n=1", "event factory=x n=2"])

    def test_unreadable_state_reads_as_empty(self):
        st.state_path(self.repo).parent.mkdir(exist_ok=True); st.state_path(self.repo).write_text("{not json")
        self.assertEqual(st.load_state(self.repo), st.EMPTY)

    def test_saved_values_of_the_wrong_type_are_ignored(self):
        st.state_path(self.repo).parent.mkdir(exist_ok=True)
        st.state_path(self.repo).write_text('{"dispatched": null, "said": [], "mode": ["x"], "day_tickets": 3, "pid": 7}')
        s = st.load_state(self.repo)
        self.assertEqual((s["dispatched"], s["said"], s["mode"], s["day_tickets"], s["pid"]), ({}, {}, "day", 3, 7))
        st.state_path(self.repo).write_text("[1]")
        self.assertEqual(st.load_state(self.repo), st.EMPTY)

    def test_corrupt_event_lines_are_skipped_and_a_torn_line_is_not_glued_onto(self):
        st.events_path(self.repo).parent.mkdir(exist_ok=True)
        good = '{"n": 4, "at": 1.0, "line": "kept"}'
        st.events_path(self.repo).write_text("null\n[]\n{}\n" + '{"x":1}\n' + good + '\n{"n": 9, "at"')
        self.assertEqual([e["n"] for e in st.events_since(self.repo, 0)], [4])
        self.assertEqual(st.add_event(self.repo, "next"), 5)
        self.assertEqual([e["line"] for e in st.events_since(self.repo, 4)], ["next"])

    def test_day_pace_is_the_repos_setting_else_the_config(self):
        s = st.load_state(self.repo); self.assertEqual(st.pace(s), {"mode": "day", "cap": 2})
        s["day_tickets"] = 1; self.assertEqual(st.pace(s), {"mode": "day", "cap": 1})

    def test_say_once_repeats_nothing_until_the_line_changes(self):
        s, out = st.load_state(self.repo), []
        for line in ("a", "a", "b"): st.say_once(s, "k", line, out)
        self.assertEqual(out, ["a", "b"])


import fc_factoryturn as ft


def story(id, state="Todo", *labels):
    return {"id": id, "title": id, "state": state, "labels": list(labels)}

def plan_of(**features):
    return {"features": {k.replace("_", "-"): {"id": k.replace("_", "-"), "stories": v} for k, v in features.items()}}

def entry(id, feature, kind="story", state="Todo", waits_on=()):
    return {"id": id, "title": id, "state": state, "kind": kind, "feature": feature, "track": "t", "wave": 1, "waits_on": list(waits_on)}

def rd(id, ready=True, has_tasks=True, specced=True, missing=(), error=None):
    return {"id": id, "ready": ready, "has_tasks": has_tasks, "specced": specced, "missing": list(missing), "error": error}

def judge(results):
    return lambda s, **kw: results[s]


class Pick(unittest.TestCase):
    def test_build_needs_ready_tasks_and_the_label_and_cut_needs_no_needs_shaping(self):
        self.assertEqual(ft.classify(["Story", "Ready"], rd("A")), "build")
        self.assertIsNone(ft.classify(["Story"], rd("A")))  # tasks but no Ready: another gate holds it
        self.assertEqual(ft.classify(["Story"], rd("A", has_tasks=False)), "cut")
        self.assertIsNone(ft.classify(["Story", "needs-shaping"], rd("A", has_tasks=False)))
        self.assertIsNone(ft.classify(["Story", "Ready"], rd("A", ready=False)))

    def test_in_flight_counts_hand_started_stories_and_frees_in_review(self):
        plan = plan_of(NEB_9=[story("NEB-10", "In Progress"), story("NEB-11"), story("NEB-13", "In Review")])
        self.assertEqual(ft.in_flight(plan, {"dispatched": {"NEB-11": {}, "NEB-13": {}}}), ["NEB-10", "NEB-11"])

    def test_pick_fills_free_slots_in_order_and_starts_nothing_without_one(self):
        plan = plan_of(NEB_9=[story("NEB-10", "Todo", "Ready"), story("NEB-11", "Todo", "Ready"), story("NEB-14", "In Progress")])
        entries = [entry("NEB-14", "NEB-9", state="In Progress"), entry("NEB-10", "NEB-9"), entry("NEB-11", "NEB-9"),
                   entry("NEB-20", "NEB-12", waits_on=("NEB-9",))]
        j = judge({"NEB-10": rd("NEB-10"), "NEB-11": rd("NEB-11")})
        out = ft.pick(entries, plan, {"dispatched": {}}, 1, j)
        self.assertEqual(([(e["id"], k) for e, k in out["picks"]], out["ready_waiting"], out["offer"]), ([("NEB-10", "build")], True, None))
        self.assertEqual(ft.pick(entries, plan, {"dispatched": {}}, 0, j)["picks"], [])

    def test_nothing_dispatchable_offers_the_first_entry_that_needs_planning(self):
        missing = [{"item": "scope", "issue": "NEB-10", "question": "What is out?"}]
        out = ft.pick([entry("NEB-10", "NEB-9"), entry("NEB-12", "NEB-12", kind="feature")], plan_of(NEB_9=[story("NEB-10")]),
                      {"dispatched": {}}, 2, judge({"NEB-10": rd("NEB-10", ready=False, specced=False, missing=missing)}))
        self.assertEqual(out["offer"], {"id": "NEB-10", "layer": "story", "why": "scope: What is out?"})
        out = ft.pick([entry("NEB-12", "NEB-12", kind="feature")], plan_of(), {"dispatched": {}}, 2, judge({}))
        self.assertEqual(out["offer"], {"id": "NEB-12", "layer": "stories", "why": "no stories yet"})

    def test_pick_calls_the_judge_with_the_id_alone_so_the_caller_binds_the_repo(self):
        seen = []
        def j(s, **kw):
            seen.append((s, kw)); return rd(s)
        out = ft.pick([entry("NEB-10", "NEB-9")], plan_of(NEB_9=[story("NEB-10", "Todo", "Ready")]), {"dispatched": {}}, 1, j)
        self.assertEqual((seen, len(out["picks"])), ([("NEB-10", {"write": True})], 1))

    def test_a_failed_judge_is_reported_never_offered_and_judging_stops_at_the_limit(self):
        out = ft.pick([entry("NEB-10", "NEB-9")], plan_of(NEB_9=[story("NEB-10", "Todo", "Ready")]), {"dispatched": {}}, 2,
                      judge({"NEB-10": rd("NEB-10", ready=False, specced=None, error="no TypeSafe key")}))
        self.assertEqual((out["picks"], out["offer"], out["failed"]), ([], None, [("NEB-10", "no TypeSafe key")]))
        calls, entries = [], [entry(f"NEB-{n}", "NEB-9") for n in range(30, 40)]
        ft.pick(entries, plan_of(NEB_9=[story(e["id"]) for e in entries]), {"dispatched": {}}, 2,
                lambda s, **kw: calls.append(s) or rd(s, ready=False, specced=False))
        self.assertLessEqual(len(calls), ft.JUDGE_LIMIT)


class Dispatch(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.repo = Path(d.name).resolve() / "repo"; self.wt = Path(d.name).resolve() / "compose-sign-in"
        self.repo.mkdir(); self.projects, self.leads, self.state = {}, {}, {"dispatched": {}}
        self.plan = plan_of(NEB_9=[story("NEB-10"), story("NEB-11"), story("NEB-12")])
        self.plan["features"]["NEB-9"].update(title="Sign in", blockers=[])
        self.proj = self.wt / ".compose" / "sign-in"
        for name, new in (("all_projects", lambda repo: dict(self.projects)), ("leads_or_none", lambda repo: None if self.leads is None else dict(self.leads))):
            mod = ft.fc_core if name == "all_projects" else ft
            pa = unittest.mock.patch.object(mod, name, new); pa.start(); self.addCleanup(pa.stop)
        self.cli = unittest.mock.Mock(side_effect=self.fake_cli); self.prompt = unittest.mock.Mock(return_value=True)
        for name, m in (("compose_cli", self.cli), ("prompt_lead", self.prompt)):
            pa = unittest.mock.patch.object(ft, name, m); pa.start(); self.addCleanup(pa.stop)

    def make_project(self, slug, linear):
        self.proj = self.wt / ".compose" / slug; self.proj.mkdir(parents=True, exist_ok=True)
        (self.proj / "linear.json").write_text(json.dumps(linear)); self.projects[slug] = (self.wt, self.proj)

    def fake_cli(self, args, stdin=None, pane=None):
        if args[0] == "init":
            self.make_project(args[args.index("--project") + 1], {})
            (self.proj / "linear.json").unlink()
        return ""

    def test_a_feature_with_no_project_gets_one_scoped_to_the_story_and_a_new_team_lead(self):
        line = ft.dispatch(self.repo, self.plan, entry("NEB-10", "NEB-9"), "cut", self.state, 100.0)
        init, start = self.cli.call_args_list
        self.assertEqual(init.args[0], ["init", "--repo", str(self.repo), "--project", "sign-in"])
        self.assertEqual(start.args[0][:6], ["crew", "start", "--repo", str(self.wt), "--project", "sign-in"])
        self.assertIn(ft.HOW["cut"], start.kwargs["stdin"]); self.assertIn("sections/0f.md", start.kwargs["stdin"])
        self.assertEqual(json.loads((self.proj / "linear.json").read_text()), {"feature": "NEB-9", "stories": ["NEB-10"]})
        self.assertEqual(self.state["dispatched"]["NEB-10"], {"feature": "NEB-9", "project": "sign-in", "kind": "cut", "at": 100.0})
        self.assertEqual(line, "event factory=dispatched story=NEB-10 project=sign-in kind=cut  (its team lead builds it; relay it: compose watch --project sign-in --from orchestrator)")

    def test_a_live_team_lead_is_prompted_and_only_a_scoped_project_gains_the_story(self):
        self.make_project("sign-in", {"feature": "NEB-9", "stories": ["NEB-10"]}); self.leads["sign-in"] = "sign-in-orchestrator"
        ft.dispatch(self.repo, self.plan, entry("NEB-11", "NEB-9"), "build", self.state, 100.0)
        self.cli.assert_not_called()
        self.prompt.assert_called_once_with("sign-in-orchestrator", ft.lead_prompt("NEB-11", "NEB-9", "Sign in", "build"))
        self.assertEqual(json.loads((self.proj / "linear.json").read_text())["stories"], ["NEB-10", "NEB-11"])
        self.make_project("sign-in", {"feature": "NEB-9"})
        ft.dispatch(self.repo, self.plan, entry("NEB-12", "NEB-9"), "build", self.state, 100.0)
        self.assertEqual(json.loads((self.proj / "linear.json").read_text()), {"feature": "NEB-9"})

    def test_a_lost_team_lead_is_started_again_without_a_second_init(self):
        self.make_project("sign-in", {"feature": "NEB-9", "stories": ["NEB-10"]})
        ft.dispatch(self.repo, self.plan, entry("NEB-11", "NEB-9"), "build", self.state, 100.0)
        self.assertEqual([c.args[0][0] for c in self.cli.call_args_list], ["crew"])
        self.prompt.assert_not_called()

    def test_a_feature_after_a_verified_unmerged_feature_branches_from_its_project(self):
        self.make_project("profile", {"feature": "NEB-5"})
        self.plan["features"]["NEB-9"]["blockers"] = [{"id": "NEB-5", "state": "In Review", "labels": []}]
        ft.dispatch(self.repo, self.plan, entry("NEB-10", "NEB-9"), "build", self.state, 100.0)
        self.assertEqual(self.cli.call_args_list[0].args[0][-2:], ["--after", "profile"])

    def test_a_failed_start_records_nothing(self):
        def boom(args, stdin=None, pane=None):
            if args[0] == "crew":
                raise RuntimeError("herdr is not running")
            return self.fake_cli(args, stdin, pane)
        self.cli.side_effect = boom
        with self.assertRaises(RuntimeError):
            ft.dispatch(self.repo, self.plan, entry("NEB-10", "NEB-9"), "build", self.state, 100.0)
        self.assertEqual(self.state["dispatched"], {})

    def test_a_failed_prompt_to_a_live_lead_records_nothing_and_adds_no_story(self):
        self.make_project("sign-in", {"feature": "NEB-9", "stories": ["NEB-10"]}); self.leads["sign-in"] = "sign-in-orchestrator"
        self.prompt.return_value = False
        with self.assertRaises(RuntimeError):
            ft.dispatch(self.repo, self.plan, entry("NEB-11", "NEB-9"), "build", self.state, 100.0)
        self.assertEqual((self.state["dispatched"], json.loads((self.proj / "linear.json").read_text())["stories"]), ({}, ["NEB-10"]))

    def test_herdr_not_answering_starts_and_prompts_nothing(self):
        self.make_project("sign-in", {"feature": "NEB-9", "stories": ["NEB-10"]}); self.leads = None
        with self.assertRaises(RuntimeError) as e:
            ft.dispatch(self.repo, self.plan, entry("NEB-11", "NEB-9"), "build", self.state, 100.0)
        self.assertEqual(str(e.exception), "herdr did not answer; the next turn tries again")
        self.cli.assert_not_called(); self.prompt.assert_not_called()
        self.assertEqual((self.state["dispatched"], json.loads((self.proj / "linear.json").read_text())["stories"]), ({}, ["NEB-10"]))

    def test_the_stored_manager_pane_makes_the_project_and_starts_the_lead(self):
        self.state["manager_pane"] = "p9"
        ft.dispatch(self.repo, self.plan, entry("NEB-10", "NEB-9"), "build", self.state, 100.0)
        self.assertEqual([c.kwargs.get("pane") for c in self.cli.call_args_list], ["p9", "p9"])

    def test_the_turn_after_a_failed_start_retries_with_only_crew_start(self):
        real = self.cli.side_effect
        def boom(args, stdin=None, pane=None):
            if args[0] == "crew":
                raise RuntimeError("herdr is not running")
            return real(args, stdin, pane)
        self.cli.side_effect = boom
        with self.assertRaises(RuntimeError):
            ft.dispatch(self.repo, self.plan, entry("NEB-10", "NEB-9"), "build", self.state, 100.0)
        self.cli.reset_mock(); self.cli.side_effect = real
        ft.dispatch(self.repo, self.plan, entry("NEB-10", "NEB-9"), "build", self.state, 101.0)
        self.assertEqual([c.args[0][0] for c in self.cli.call_args_list], ["crew"])
        self.assertEqual(self.state["dispatched"]["NEB-10"]["at"], 101.0)

    def test_a_live_lead_with_a_long_slug_is_prompted_not_started_again(self):
        self.plan["features"]["NEB-9"]["title"] = "Onboarding checklist for teams"
        self.make_project("onboarding-checklist-for", {"feature": "NEB-9"}); self.leads["onboarding-checklist-for"] = "onboarding-checklis-orchestrator"
        ft.dispatch(self.repo, self.plan, entry("NEB-11", "NEB-9"), "build", self.state, 100.0)
        self.cli.assert_not_called()
        self.assertEqual(self.prompt.call_args.args[0], "onboarding-checklis-orchestrator")

    def test_a_project_init_made_but_never_linked_is_taken_up_not_numbered(self):
        self.make_project("sign-in", {}); (self.proj / "linear.json").unlink()
        ft.dispatch(self.repo, self.plan, entry("NEB-10", "NEB-9"), "build", self.state, 100.0)
        self.assertEqual([c.args[0][0] for c in self.cli.call_args_list], ["crew"])
        self.assertEqual(json.loads((self.proj / "linear.json").read_text()), {"feature": "NEB-9", "stories": ["NEB-10"]})

    def test_a_leftover_sibling_folder_or_branch_makes_a_slug_taken(self):
        (self.repo.parent / "compose-sign-in").mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "branch", "compose/sign-in-2"], check=True)
        ft.dispatch(self.repo, self.plan, entry("NEB-10", "NEB-9"), "build", self.state, 100.0)
        self.assertEqual(self.cli.call_args_list[0].args[0][-1], "sign-in-3")

    def test_slugs_are_short_kebab_and_never_taken(self):
        self.assertEqual(ft.slug_for("Sign in with Google, Apple, and email", set()), "sign-in-with-google")
        self.assertEqual(ft.slug_for("Sign in", {"sign-in"}), "sign-in-2")


class ComposeCli(unittest.TestCase):
    def test_leads_or_none_tells_a_failed_agent_list_from_no_leads(self):
        repo = Path("/repo")
        with unittest.mock.patch.object(ft.fc_herdr, "herdr", return_value=None):
            self.assertIsNone(ft.leads_or_none(repo))
        with unittest.mock.patch.object(ft.fc_herdr, "herdr", return_value={"agents": []}), \
             unittest.mock.patch.object(ft.fc_reconcile, "leads_among", return_value={"sign-in": "sign-in-orchestrator"}) as among:
            self.assertEqual(ft.leads_or_none(repo), {"sign-in": "sign-in-orchestrator"})
        among.assert_called_once_with(repo, [])

    def test_the_stored_pane_is_the_childs_herdr_pane_and_the_role_is_dropped(self):
        done = subprocess.CompletedProcess([], 0, "ok", "")
        with unittest.mock.patch.dict(os.environ, {"HERDR_PANE_ID": "old", "COMPOSE_ROLE": "orchestrator"}), \
             unittest.mock.patch.object(ft.subprocess, "run", return_value=done) as run:
            self.assertEqual(ft.compose_cli(["status"], pane="p9"), "ok")
            ft.compose_cli(["status"])
        envs = [c.kwargs["env"] for c in run.call_args_list]
        self.assertEqual([e["HERDR_PANE_ID"] for e in envs], ["p9", "old"])
        self.assertFalse(any("COMPOSE_ROLE" in e for e in envs))


FEATURE = "## Decisions so far\n\n- D1: Google only (product owner)\n- D2: 30 day sessions\n"


class PlanChange(unittest.TestCase):
    def setUp(self):
        self.repo, self.proj = Path("/repo"), Path("/repo/.compose/sign-in")
        self.rests_on = unittest.mock.Mock(); self.hold = unittest.mock.Mock()
        for name, m in (("rests_on", self.rests_on), ("hold_tasks", self.hold)):
            pa = unittest.mock.patch.object(ft.fc_change, name, m); pa.start(); self.addCleanup(pa.stop)

    def test_open_questions_edits_are_no_change_and_scope_edits_are(self):
        a = ft.fingerprint(FEATURE, "## Scope\n\nGoogle only\n\n## Open questions\n\n- x (readiness check, 2026-10-26)\n")
        b = ft.fingerprint(FEATURE, "## Scope\n\nGoogle only\n\n## Open questions\n\nNone\n")
        self.assertEqual(ft.changed_refs(a, b, "NEB-10"), [])
        self.assertEqual(ft.changed_refs(a, ft.fingerprint(FEATURE, "## Scope\n\nGoogle and Apple\n"), "NEB-10"), ["NEB-10"])

    def test_a_changed_decision_passes_its_ids_and_one_without_an_id_passes_the_story(self):
        a = ft.fingerprint("## Decisions so far\n\n- D1: Google only (product owner)\n- D2: 30 day sessions\n", "s")
        b = ft.fingerprint("## Decisions so far\n\n- ~~D1: Google only~~ (C1)\n- D2: 30 day sessions\n- D3: Google and Apple\n", "s")
        self.assertEqual(ft.changed_refs(a, b, "NEB-10"), ["D1"])  # struck; the added D3 changes no course
        reworded = ft.fingerprint("## Decisions so far\n\n- D1: Google only (product owner)\n- D2: 7 day sessions\n", "s")
        self.assertEqual(ft.changed_refs(a, reworded, "NEB-10"), ["D2"])
        self.assertEqual(ft.changed_refs(a, ft.fingerprint("## Decisions so far\n\n- D2: 30 day sessions\n", "s"), "NEB-10"), ["D1"])
        c, d = ft.fingerprint("## Decisions so far\n\n- one store\n", "s"), ft.fingerprint("## Decisions so far\n\n- two stores\n", "s")
        self.assertEqual(ft.changed_refs(c, d, "NEB-10"), ["NEB-10"])
        self.assertEqual(ft.changed_refs(c, ft.fingerprint("## Decisions so far\n\n- one store\n- a cache\n", "s"), "NEB-10"), [])

    def test_confirming_an_away_decision_changes_nothing(self):
        a = ft.fingerprint("## Decisions so far\n\n- D1: Google only (away, 2026-10-26)\n- keep one store (away, 2026-10-26)\n", "s")
        b = ft.fingerprint("## Decisions so far\n\n- D1: Google only (product owner)\n- keep one store (product owner)\n", "s")
        self.assertEqual(ft.changed_refs(a, b, "NEB-10"), [])
        away = ft.fingerprint("## Decisions so far\n\n- Q1: A. Google only (away recommendation, unconfirmed, 2026-10-26)\n", "s")
        said = ft.fingerprint('## Decisions so far\n\n- Q1: A. Google only (product owner, 2026-10-27; said: "yes (Google only)")\n', "s")
        self.assertEqual(ft.changed_refs(away, said, "NEB-10"), [])  # a parenthesis in their words is still the tag

    def test_a_changed_rider_before_the_tag_is_a_change(self):
        a = ft.fingerprint("## Decisions so far\n\n- Q1: A. Google only (no Apple) (away recommendation, unconfirmed, 2026-10-26)\n", "s")
        b = ft.fingerprint('## Decisions so far\n\n- Q1: A. Google only (Apple later) (product owner, 2026-10-27; said: "yes")\n', "s")
        self.assertEqual(ft.changed_refs(a, b, "NEB-10"), ["Q1"])

    def test_hold_changed_holds_what_rests_on_the_refs_or_says_nothing_does(self):
        self.rests_on.return_value = {"002": "cites D1", "003": "under NEB-10"}
        line = ft.hold_changed(self.repo, self.proj, "NEB-10", ["D1"], "D1 changed on NEB-9")
        self.hold.assert_called_once_with(self.proj, ["002", "003"], "factory: D1 changed on NEB-9")
        self.assertEqual(line, 'event factory=held story=NEB-10 tasks=002,003 why="D1 changed on NEB-9"  '
                               "(re-plan it with the product owner: keep, part, or discard each held task with compose change, step 8)")
        self.rests_on.return_value = {}; self.hold.reset_mock()
        line = ft.hold_changed(self.repo, self.proj, "NEB-10", ["NEB-10"], "NEB-10 changed")
        self.hold.assert_not_called()
        self.assertEqual(line, 'event factory=changed story=NEB-10 why="NEB-10 changed"  (nothing running rests on it)')


class Turn(unittest.TestCase):
    def setUp(self):
        self.repo, self.wt = Path("/repo"), Path("/repo-wt")
        self.state = json.loads(json.dumps(st.EMPTY))
        self.calls, self.leads, self.results, self.descriptions = [], {}, {}, {}
        self.plan = {"tracks": [{"name": "t", "items": ["NEB-9"]}], "features": {}}
        self.entries = []
        self.add_entry("NEB-10", "NEB-9", labels=("Ready",)); self.add_entry("NEB-11", "NEB-9", labels=("Ready",))
        self.plan["features"]["NEB-9"].update(title="Sign in", state="Todo", labels=["Feature"], blockers=[])
        self.descriptions["NEB-9"] = FEATURE
        for s in ("NEB-10", "NEB-11"):
            self.descriptions[s] = "## Scope\n\nsame\n"; self.results[s] = rd(s)
        self.reconciled = {"added": [], "removed": [], "ready": [], "proposed": [], "doc": "u"}
        n = [0]
        def product_plan(repo):
            n[0] += 1; self.calls.append(f"product_plan:{n[0]}")
            return self.plan_value if hasattr(self, "plan_value") else self.plan
        def reconcile(repo, dry_run=False):
            self.calls.append("reconcile"); return self.reconciled
        def dispatch(repo, plan, e, kind, state, now):
            state["dispatched"][e["id"]] = {"feature": e["feature"], "project": "sign-in", "kind": kind, "at": now}
            return f"event factory=dispatched story={e['id']}"
        self.product_plan = unittest.mock.Mock(side_effect=product_plan)
        self.reconcile = unittest.mock.Mock(side_effect=reconcile)
        self.dispatch = unittest.mock.Mock(side_effect=dispatch)
        self.held = unittest.mock.Mock(return_value=False)
        self.hold_changed = unittest.mock.Mock(return_value="event factory=held story=x")
        issue = lambda ident: {"id": ident, "description": self.descriptions.get(ident, "")}
        for mod, name, m in ((ft.fc_delivery, "product_plan", self.product_plan), (ft.fc_delivery, "reconcile", self.reconcile),
                             (ft.fc_plan, "order_entries", lambda tracks, features: [dict(e) for e in self.entries]),
                             (ft.fc_readiness, "story_readiness", lambda repo, s, **kw: self.results[s]),
                             (ft.fc_load, "held", self.held), (ft.fc_linear, "linear_issue", issue),
                             (ft, "leads_or_none", lambda repo: None if self.leads is None else dict(self.leads)),
                             (ft, "dispatch", self.dispatch), (ft, "hold_changed", self.hold_changed),
                             (ft, "project_for", lambda repo, feature: ("sign-in", self.wt, self.wt / "sign-in"))):
            pa = unittest.mock.patch.object(mod, name, m); pa.start(); self.addCleanup(pa.stop)

    def add_entry(self, ident, feature, waits_on=(), labels=()):
        f = self.plan["features"].setdefault(feature, {"id": feature, "title": feature, "state": "Todo", "labels": ["Feature"], "stories": [], "blockers": []})
        f["stories"].append(story(ident, "Todo", *labels))
        self.entries.append(entry(ident, feature, waits_on=waits_on))

    def set_states(self, **states):
        for f in self.plan["features"].values():
            for s in f["stories"]:
                s["state"] = states.get(s["id"], s["state"])
        for e in self.entries:
            e["state"] = states.get(e["id"], e["state"])

    def set_waits(self, ident, waits_on):
        for e in self.entries:
            if e["id"] == ident:
                e["waits_on"] = list(waits_on)

    def make_nothing_ready(self, missing):
        for s in ("NEB-10", "NEB-11"):
            self.results[s] = rd(s, ready=False, specced=False, missing=[{"item": missing.split(":")[0], "question": missing.split(": ", 1)[1]}])

    def test_no_product_plan_offers_the_bootstrap_once_and_dispatches_nothing(self):
        self.product_plan.side_effect = None; self.product_plan.return_value = None
        lines = ft.turn(self.repo, self.state, 100.0) + ft.turn(self.repo, self.state, 200.0)
        self.assertEqual(lines, ["event factory=bootstrap  (no product delivery plan yet: plan it with the product owner from "
                                 "compose delivery seed (sections/0p.md); nothing is dispatched before it exists)"])
        self.reconcile.assert_not_called(); self.dispatch.assert_not_called()

    def test_a_turn_reconciles_then_fills_the_free_slots_and_the_load_hold_stops_it(self):
        ft.turn(self.repo, self.state, 100.0)
        self.assertLess(self.calls.index("reconcile"), self.calls.index("product_plan:2"))
        self.assertEqual([c.args[2]["id"] for c in self.dispatch.call_args_list], ["NEB-10", "NEB-11"])
        self.dispatch.reset_mock(); self.state["dispatched"] = {}; self.held.return_value = True
        ft.turn(self.repo, self.state, 200.0)
        self.dispatch.assert_not_called()

    def test_a_restart_dispatches_nothing_twice(self):  # NEB-10 dispatched before the crash, NEB-11 started by hand
        self.state["dispatched"] = {"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        self.set_states(**{"NEB-11": "In Progress"}); self.leads["sign-in"] = "sign-in-orchestrator"
        ft.turn(self.repo, self.state, 100.0)
        self.dispatch.assert_not_called()

    def test_nothing_ready_offers_once_a_decline_snoozes_and_planning_holds_it(self):
        self.make_nothing_ready(missing="scope: What is out?")
        self.assertEqual(ft.turn(self.repo, self.state, 100.0), ['event factory=offer ticket=NEB-10 layer=story why="scope: What is out?"  '
                         "(offer to plan it with the product owner (sections/0f.md); they may pick another ticket or decline)"])
        self.assertEqual((self.state["offer"]["id"], ft.turn(self.repo, self.state, 160.0)), ("NEB-10", []))
        self.state["said"].pop("offer"); self.state["declined_until"] = 3700.0
        self.assertEqual((ft.turn(self.repo, self.state, 200.0), len(ft.turn(self.repo, self.state, 3701.0))), ([], 1))
        self.state["said"].pop("offer"); self.state["planning"] = {"id": "NEB-30", "at": 1.0, "out_of_order": True}
        self.assertEqual(ft.turn(self.repo, self.state, 3800.0), [])

    def test_an_out_of_order_ticket_asks_once_to_move_up_when_specced(self):
        self.state["out_of_order"] = ["NEB-31"]
        self.add_entry("NEB-31", "NEB-30", waits_on=("NEB-9",)); self.results["NEB-31"] = rd("NEB-31", ready=False, has_tasks=False)
        lines = [l for l in ft.turn(self.repo, self.state, 100.0) + ft.turn(self.repo, self.state, 200.0) if "move-up" in l]
        self.assertEqual(lines, ["event factory=move-up ticket=NEB-31 waits_on=NEB-9  (planned out of order and now specced: ask the "
                                 "product owner whether to move it up; on yes, write the tracks again (sections/0p.md))"])
        self.assertEqual(self.state["out_of_order"], [])

    def test_in_review_frees_the_slot_and_a_lost_team_lead_requeues(self):
        self.state["dispatched"] = {s: {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0} for s in ("NEB-10", "NEB-11")}
        self.state["prints"]["NEB-10"] = {"decisions": [], "story": "x", "waits_on": []}
        self.set_states(**{"NEB-10": "In Review"})  # NEB-11 still Todo, no live lead
        lines = ft.turn(self.repo, self.state, 100.0)
        self.assertIn("event factory=verified story=NEB-10 project=sign-in  (verified: waiting for the product owner's look and merge)", lines)
        self.assertIn("event factory=lead-lost story=NEB-11 project=sign-in  (its team lead is gone: the story goes back in the queue)", lines)
        self.assertNotIn("NEB-10", self.state["dispatched"]); self.assertNotIn("NEB-10", self.state["prints"])
        self.assertEqual([c.args[2]["id"] for c in self.dispatch.call_args_list], ["NEB-11"])  # requeued and started again on the same turn
        self.assertEqual(list(self.state["dispatched"]), ["NEB-11"])

    def test_a_changed_or_reordered_in_flight_story_is_held_once(self):
        self.state["dispatched"] = {"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        self.set_states(**{"NEB-10": "In Progress"}); self.leads["sign-in"] = "sign-in-orchestrator"
        ft.turn(self.repo, self.state, 100.0)  # the first look only records the fingerprint
        self.descriptions["NEB-10"] = "## Scope\n\nchanged\n"
        ft.turn(self.repo, self.state, 200.0); ft.turn(self.repo, self.state, 300.0)
        self.assertEqual([c.args[2:4] for c in self.hold_changed.call_args_list], [("NEB-10", ["NEB-10"])])
        self.set_waits("NEB-10", ("NEB-5",)); ft.turn(self.repo, self.state, 400.0)
        self.assertEqual(self.hold_changed.call_args.args[4], "the product delivery plan now puts NEB-9 after NEB-5")

    def test_a_failed_hold_is_tried_again_on_the_next_turn(self):
        self.state["dispatched"] = {"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        self.set_states(**{"NEB-10": "In Progress"}); self.leads["sign-in"] = "sign-in-orchestrator"
        ft.turn(self.repo, self.state, 100.0)
        self.descriptions["NEB-10"] = "## Scope\n\nchanged\n"
        self.hold_changed.side_effect = [SystemExit("hold: disk full"), "event factory=held story=NEB-10"]
        self.assertEqual(ft.turn(self.repo, self.state, 200.0), ['event factory=turn-failed why="hold: disk full"  (the next turn tries again)'])
        self.assertIn("event factory=held story=NEB-10", ft.turn(self.repo, self.state, 300.0))

    def test_a_judge_that_raises_is_a_judge_failed_line_not_a_crash(self):
        self.results["NEB-10"] = None
        def boom(repo, s, **kw):
            if s == "NEB-10":
                raise RuntimeError("linear timed out")
            return self.results[s]
        with unittest.mock.patch.object(ft.fc_readiness, "story_readiness", boom):
            lines = ft.turn(self.repo, self.state, 100.0)
        self.assertIn('event factory=judge-failed story=NEB-10 why="linear timed out"', lines)

    def test_a_bad_delivery_plan_is_one_turn_failed_line(self):
        import fc_plan
        self.product_plan.side_effect = fc_plan.TracksError("track a: no such wave")
        lines = ft.turn(self.repo, self.state, 100.0) + ft.turn(self.repo, self.state, 200.0)
        self.assertEqual(lines, ['event factory=turn-failed why="track a: no such wave"  (the next turn tries again)'])

    def test_a_story_and_a_decision_changing_together_name_each_cause(self):
        self.state["dispatched"] = {"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        self.set_states(**{"NEB-10": "In Progress"}); self.leads["sign-in"] = "sign-in-orchestrator"
        ft.turn(self.repo, self.state, 100.0)
        self.descriptions["NEB-10"] = "## Scope\n\nchanged\n"; self.descriptions["NEB-9"] = FEATURE.replace("30 day", "7 day")
        ft.turn(self.repo, self.state, 200.0)
        self.assertEqual(self.hold_changed.call_args.args[4], "D2 changed on NEB-9; NEB-10 changed")

    def test_proposals_and_failures_are_said_once_and_the_next_turn_tries_again(self):
        self.reconciled = {"added": [], "removed": [], "ready": [], "proposed": ["NEB-30 is not in the plan: add it or cancel it."], "doc": "u"}
        lines = ft.turn(self.repo, self.state, 100.0) + ft.turn(self.repo, self.state, 200.0)
        self.assertEqual(len([l for l in lines if "factory=proposed" in l]), 1)
        self.reconcile.side_effect = [SystemExit("delivery reconcile: timed out; run it again")] * 2 + [self.reconciled]
        self.assertEqual((ft.turn(self.repo, self.state, 300.0), ft.turn(self.repo, self.state, 400.0)),
                         (['event factory=turn-failed why="delivery reconcile: timed out; run it again"  (the next turn tries again)'], []))
        self.dispatch.reset_mock(); ft.turn(self.repo, self.state, 500.0)
        self.dispatch.assert_called()


    def dispatched(self):
        return [c.args[2]["id"] for c in self.dispatch.call_args_list]

    def fail_dispatch(self, failing):
        real = self.dispatch.side_effect
        def dispatch(repo, plan, e, kind, state, now):
            if e["id"] in failing:
                raise failing[e["id"]]
            return real(repo, plan, e, kind, state, now)
        self.dispatch.side_effect = dispatch

    def test_herdr_not_answering_loses_no_lead_and_starts_nothing_twice(self):
        self.state["dispatched"] = {"NEB-11": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        self.leads = None
        lines = ft.turn(self.repo, self.state, 100.0)
        self.assertFalse([l for l in lines if "lead-lost" in l])
        self.assertIn("NEB-11", self.state["dispatched"]); self.assertNotIn("NEB-11", self.dispatched())

    def test_a_dispatch_that_herdr_fails_ends_the_turns_dispatch_without_a_backoff(self):
        self.fail_dispatch({"NEB-10": ft.HerdrDown("herdr did not answer; the next turn tries again")})
        lines = ft.turn(self.repo, self.state, 100.0)
        self.assertEqual((self.dispatched(), self.state["backoff"]), (["NEB-10"], {}))
        self.assertIn('event factory=turn-failed why="herdr did not answer"  (the next turn tries again)', lines)
        self.held.return_value = True; ft.turn(self.repo, self.state, 200.0)  # herdr answers, nothing to start
        self.assertNotIn("herdr", self.state["said"])

    def test_a_crash_after_a_dispatch_keeps_the_lines_already_made(self):
        self.fail_dispatch({"NEB-11": TypeError("bad record")})
        with unittest.mock.patch("traceback.print_exc"):
            lines = ft.turn(self.repo, self.state, 100.0)
        self.assertEqual(lines, ["event factory=dispatched story=NEB-10",
                                 'event factory=turn-failed why="TypeError: bad record"  (the next turn tries again)'])

    def test_the_same_crash_is_said_once_until_a_turn_goes_through(self):
        crash = 'event factory=turn-failed why="TypeError: bad record"  (the next turn tries again)'
        with unittest.mock.patch("traceback.print_exc"), unittest.mock.patch.object(ft, "_turn", side_effect=TypeError("bad record")):
            self.assertEqual([ft.turn(self.repo, self.state, now) for now in (100.0, 200.0)], [[crash], []])
        ft.turn(self.repo, self.state, 300.0)
        self.assertNotIn("turn-crash", self.state["said"])
        with unittest.mock.patch("traceback.print_exc"), unittest.mock.patch.object(ft, "_turn", side_effect=TypeError("bad record")):
            self.assertEqual(ft.turn(self.repo, self.state, 400.0), [crash])

    def test_a_failing_dispatch_backs_off_and_the_rest_still_start(self):
        self.leads["sign-in"] = "sign-in-orchestrator"
        self.fail_dispatch({"NEB-10": RuntimeError("crew start: no space")})
        lines = ft.turn(self.repo, self.state, 100.0)
        self.assertEqual(self.dispatched(), ["NEB-10", "NEB-11"])
        self.assertEqual(lines[0], 'event factory=turn-failed story=NEB-10 why="crew start: no space"  (it waits 3 turns; the rest go on)')
        self.assertEqual(self.state["backoff"], {"NEB-10": {"why": "crew start: no space", "until_turn": 4}})
        for now in (200.0, 300.0, 400.0):
            self.dispatch.reset_mock(); self.assertEqual(ft.turn(self.repo, self.state, now), [])
            self.dispatch.assert_not_called()
        self.dispatch.reset_mock(); self.assertEqual(ft.turn(self.repo, self.state, 500.0), [])  # the same why is not said again
        self.assertEqual(self.dispatched(), ["NEB-10"])
        self.fail_dispatch({}); self.dispatch.side_effect = None; self.dispatch.return_value = "event factory=dispatched story=NEB-10"
        for now in (600.0, 700.0, 800.0, 900.0):
            ft.turn(self.repo, self.state, now)
        self.assertEqual(self.dispatched()[-1], "NEB-10")
        self.assertEqual((self.state["backoff"], [k for k in self.state["said"] if k.startswith("backoff:")]), ({}, []))

    def test_a_stop_on_disk_during_the_turn_stops_the_next_dispatch(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup); self.repo = Path(d.name).resolve()
        real = self.dispatch.side_effect
        def dispatch(repo, plan, e, kind, state, now):
            s = st.load_state(repo); s["stopped"] = 5.0; st.save_state(repo, s)
            return real(repo, plan, e, kind, state, now)
        self.dispatch.side_effect = dispatch
        ft.turn(self.repo, self.state, 100.0)
        self.assertEqual(self.dispatched(), ["NEB-10"])

    def test_a_sigterm_during_the_turn_stops_dispatch_and_turn_still_takes_three_arguments(self):
        ft.turn(self.repo, self.state, 100.0, stopping=lambda: True)
        self.dispatch.assert_not_called()
        self.state["dispatched"] = {}; ft.turn(self.repo, self.state, 200.0)
        self.assertEqual(self.dispatched(), ["NEB-10", "NEB-11"])

    def test_saved_gets_each_dispatch_as_it_happens(self):
        saved = unittest.mock.Mock()
        ft.turn(self.repo, self.state, 100.0, saved=saved)
        rec = lambda s: {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 100.0}
        self.assertEqual(saved.call_args_list, [unittest.mock.call("NEB-10", rec("NEB-10")), unittest.mock.call("NEB-11", rec("NEB-11"))])

    def test_an_in_progress_story_without_its_lead_is_said_once_and_dropped(self):
        self.state["dispatched"] = {"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}}
        self.set_states(**{"NEB-10": "In Progress"})
        lines = ft.turn(self.repo, self.state, 100.0) + ft.turn(self.repo, self.state, 200.0)
        self.assertEqual([l for l in lines if "lead-lost story=NEB-10" in l], ["event factory=lead-lost story=NEB-10 project=sign-in state=In-Progress  "
                         "(its team lead is gone and the story is not in Todo: start its lead again or move the story back to Todo)"])
        self.assertNotIn("NEB-10", self.state["dispatched"]); self.assertNotIn("NEB-10", self.dispatched())

    def test_a_dispatched_story_that_left_the_plan_is_dropped(self):
        self.state["dispatched"] = {"NEB-50": {"feature": "NEB-49", "project": "gone", "kind": "build", "at": 1.0}}
        self.assertIn("event factory=dropped story=NEB-50  (it left the product delivery plan)", ft.turn(self.repo, self.state, 100.0))
        self.assertNotIn("NEB-50", self.state["dispatched"])

    def test_planning_older_than_a_day_no_longer_holds_offers(self):
        self.make_nothing_ready(missing="scope: What is out?")
        self.state["planning"] = {"id": "NEB-30", "at": 1.0}
        self.assertEqual(ft.turn(self.repo, self.state, 1.0 + 23 * 3600), [])
        self.assertEqual(len(ft.turn(self.repo, self.state, 1.0 + 25 * 3600)), 1)

    def test_move_up_judges_at_most_two_stories_a_turn(self):
        self.state["out_of_order"] = ["NEB-30"]
        for n in range(31, 36):
            self.add_entry(f"NEB-{n}", "NEB-30", waits_on=("NEB-9",)); self.results[f"NEB-{n}"] = rd(f"NEB-{n}", ready=False, specced=False)
        seen = []
        def readiness(repo, s, **kw):
            seen.append((s, kw.get("write"))); return self.results[s]
        with unittest.mock.patch.object(ft.fc_readiness, "story_readiness", readiness):
            ft.turn(self.repo, self.state, 100.0)
        self.assertEqual(len([s for s, write in seen if write is False]), ft.MOVE_UP_JUDGES)

    def prd(self, found):
        """Patch the PRD lookup: found True, False, or an exception to raise."""
        doc = {"id": "d", "title": "PRD", "url": "u/prd"}
        kw = {"side_effect": found} if isinstance(found, Exception) else {"return_value": doc if found else None}
        self.find_prd = unittest.mock.patch.object(ft.fc_prd, "find_prd", **kw).start(); self.addCleanup(unittest.mock.patch.stopall)

    def test_no_prd_and_no_plan_dispatches_nothing_and_says_prd_missing_once(self):
        self.prd(False)
        self.product_plan.side_effect = None; self.product_plan.return_value = None
        lines = ft.turn(self.repo, self.state, 100.0) + ft.turn(self.repo, self.state, 500.0)
        self.assertEqual(lines, [ft.PRD_FIRST])
        self.assertIn("event factory=prd-missing", ft.PRD_FIRST); self.assertIn("compose prd seed (sections/0q.md)", ft.PRD_FIRST)
        self.reconcile.assert_not_called(); self.dispatch.assert_not_called()
        self.assertEqual(self.find_prd.call_count, 2)  # missing: looked up again after 5 minutes

    def test_the_prd_is_looked_up_before_the_delivery_plan(self):
        self.prd(False)
        self.find_prd.side_effect = lambda repo: self.calls.append("prd") or None
        ft.turn(self.repo, self.state, 100.0)
        self.assertLess(self.calls.index("prd"), self.calls.index("product_plan:1"))

    def test_once_the_prd_is_found_a_missing_plan_offers_the_bootstrap(self):
        self.prd(False)
        self.product_plan.side_effect = None; self.product_plan.return_value = None
        ft.turn(self.repo, self.state, 100.0)
        self.find_prd.return_value = {"id": "d", "title": "PRD", "url": "u/prd"}
        self.assertEqual([l.split("  ")[0] for l in ft.turn(self.repo, self.state, 500.0)], ["event factory=bootstrap"])
        self.assertNotIn("prd-missing", self.state["said"])

    def test_no_prd_but_a_plan_keeps_building_and_offers_the_prd_once_a_day_first(self):
        self.prd(False)
        lines = ft.turn(self.repo, self.state, 100.0)
        self.assertEqual([c.args[2]["id"] for c in self.dispatch.call_args_list], ["NEB-10", "NEB-11"])
        self.assertIn(ft.PRD_OFFER, lines)
        self.assertIn("event factory=prd-missing", ft.PRD_OFFER); self.assertIn("building goes on", ft.PRD_OFFER)
        self.assertNotIn(ft.PRD_OFFER, ft.turn(self.repo, self.state, 1000.0))
        self.assertIn(ft.PRD_OFFER, ft.turn(self.repo, self.state, 100.0 + 86400))

    def test_the_prd_offer_comes_ahead_of_any_ticket_offer(self):
        self.prd(False)
        self.make_nothing_ready(missing="scope: What is out?")
        lines = ft.turn(self.repo, self.state, 100.0)
        self.assertEqual([l.split("  ")[0].split(" ticket=")[0] for l in lines], ["event factory=prd-missing", "event factory=offer"])

    def test_the_prd_offer_waits_while_planning_or_declined(self):
        self.prd(False)
        self.state["planning"] = {"id": "PRD", "at": 50.0}
        self.assertNotIn(ft.PRD_OFFER, ft.turn(self.repo, self.state, 100.0))
        self.state["planning"], self.state["declined_until"] = None, 3700.0
        self.assertNotIn(ft.PRD_OFFER, ft.turn(self.repo, self.state, 200.0))
        self.assertIn(ft.PRD_OFFER, ft.turn(self.repo, self.state, 3701.0))

    def test_a_found_prd_offers_nothing_more(self):
        self.prd(True)
        self.assertEqual([l for l in ft.turn(self.repo, self.state, 100.0) if "prd" in l], [])

    def test_a_failed_prd_lookup_never_blocks_building(self):
        self.prd(RuntimeError("linear down"))
        lines = ft.turn(self.repo, self.state, 100.0)
        self.assertEqual([c.args[2]["id"] for c in self.dispatch.call_args_list], ["NEB-10", "NEB-11"])
        self.assertEqual([l for l in lines if "prd" in l], [])
        self.assertEqual(self.state["prd"], {})
        ft.turn(self.repo, self.state, 101.0)
        self.assertEqual(self.find_prd.call_count, 2)  # tried again on the next turn

    def test_a_failed_prd_lookup_with_no_plan_still_offers_the_bootstrap(self):
        self.prd(SystemExit("no .compose/linear.json"))
        self.product_plan.side_effect = None; self.product_plan.return_value = None
        self.assertEqual([l.split("  ")[0] for l in ft.turn(self.repo, self.state, 100.0)], ["event factory=bootstrap"])



class Verbs(unittest.TestCase):
    def setUp(self):
        import argparse, fcntl, time
        from unittest import mock
        self.mock, self.fcntl, self.time, self.argparse = mock, fcntl, time, argparse
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.repo = Path(d.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self.sleeper.wait); self.addCleanup(self.sleeper.kill)
        self.proj = self.repo / "wt" / "sign-in"
        self.rests_on, self.hold = mock.Mock(return_value={}), mock.Mock(return_value=[])
        for mod, name, m in ((fc.fc_factory.fc_change, "rests_on", self.rests_on), (fc.fc_factory.fc_change, "hold_tasks", self.hold),
                             (fc.fc_factory.fc_factoryturn, "project_for", lambda repo, feature: ("sign-in", self.repo / "wt", self.proj) if feature == "NEB-9" else None),
                             (fc.fc_factory.fc_config, "LINEAR_KEY", "k")):
            pa = mock.patch.object(mod, name, m); pa.start(); self.addCleanup(pa.stop)
        pa = mock.patch.dict(fc.fc_factory.fc_config.FACTORY, {"turn_seconds": 0}); pa.start(); self.addCleanup(pa.stop)  # no sleeping between turns
        pa = mock.patch.object(fc.fc_factory.fc_watch, "watch_key", return_value="t"); pa.start(); self.addCleanup(pa.stop)

    def hold_lock(self):
        """Stand in for a live loop: hold the repo's loop lock until the test ends."""
        f = open(fc.fc_factory.lock_path(self.repo), "a"); self.fcntl.flock(f, self.fcntl.LOCK_EX)
        self.addCleanup(f.close)

    def save(self, **kv):
        s = st.load_state(self.repo); s.update(kv); st.save_state(self.repo, s)

    def verb(self, action, target=None, hold=None, done=False, timeout=None, expect_exit=0):
        import contextlib, io
        a = self.argparse.Namespace(action=action, target=target, hold=hold, done=done, timeout=timeout, json=False, now=False, repo=str(self.repo))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            if expect_exit:
                with self.assertRaises(SystemExit) as e:
                    fc.fc_factory.cmd_factory(a)
                self.assertEqual(e.exception.code, expect_exit)
            else:
                fc.fc_factory.cmd_factory(a)
        return out.getvalue()

    def test_start_refuses_outside_herdr_and_while_a_loop_runs(self):
        mock = self.mock
        with mock.patch.dict(os.environ, {"HERDR_ENV": ""}), self.assertRaises(SystemExit) as e:
            self.verb("start")
        self.assertEqual(e.exception.code, "factory: start it inside herdr: the team leads it starts live there (sections/0h.md)")
        self.save(pid=os.getpid()); self.hold_lock()
        with mock.patch.dict(os.environ, {"HERDR_ENV": "1"}), mock.patch.object(fc.fc_factory.fc_herdr, "herdr_env", return_value=True), self.assertRaises(SystemExit) as e:
            self.verb("start")
        self.assertEqual(e.exception.code, f"factory: already running (pid {os.getpid()}); compose factory status shows it")

    def test_start_spawns_the_loop_and_says_so(self):
        mock = self.mock
        self.save(stopped=5.0, pid=999999, manager_pane="old")  # the last loop's pid: no loop-lost before the new one takes the lock
        def spawn(repo):
            self.assertEqual((st.load_state(repo)["pid"], st.load_state(repo)["stopped"]), (None, None))
            self.hold_lock(); return 4242  # the loop is up and holds its lock
        with mock.patch.object(fc.fc_factory.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_factory, "spawn_loop", spawn), \
             mock.patch.dict(os.environ, {"HERDR_PANE_ID": "p9"}):
            out = self.verb("start")
        self.assertEqual(out, "factory: started (pid 4242), day pace 2 tickets at once; compose factory watch wakes you on its events\n")
        s = st.load_state(self.repo)
        self.assertEqual((s["pid"], s["stopped"], s["manager_pane"]), (None, None, "p9"))

    def test_start_says_so_when_the_loop_dies_at_once(self):
        mock = self.mock
        st.state_path(self.repo).parent.mkdir(exist_ok=True)
        st.state_path(self.repo).with_name("factory.log").write_text("Traceback\nValueError: no key\n")
        spawn = lambda repo: subprocess.Popen([sys.executable, "-c", "pass"]).pid
        with mock.patch.object(fc.fc_factory.fc_herdr, "herdr_env", return_value=True), mock.patch.object(fc.fc_factory, "spawn_loop", spawn), \
             self.assertRaises(SystemExit) as e:
            self.verb("start")
        self.assertEqual(e.exception.code, "factory: the loop did not start; see .compose/factory.log: ValueError: no key")

    def test_a_stop_during_a_turn_survives_it_and_ends_the_loop(self):
        mock = self.mock
        turns = []
        def turn(repo, state, now, **kw):
            turns.append(now); self.verb("stop"); self.verb("pace", "3"); return ["event factory=x"]
        with mock.patch.object(fc.fc_factoryturn, "turn", turn):
            fc.fc_factory.run_loop(self.repo, turns=3)
        s = st.load_state(self.repo)
        self.assertEqual((len(turns), s["day_tickets"]), (1, 3))
        self.assertIsNotNone(s["stopped"])

    def test_a_decline_and_a_plan_during_a_turn_survive_it(self):
        mock = self.mock
        self.save(said={"offer": "x"}, out_of_order=["NEB-30", "NEB-31"])
        def turn(repo, state, now, **kw):
            state["out_of_order"].remove("NEB-31")
            self.verb("decline"); self.verb("plan", "NEB-40"); return []
        with mock.patch.object(fc.fc_factoryturn, "turn", turn):
            fc.fc_factory.run_loop(self.repo, turns=1)
        s = st.load_state(self.repo)
        self.assertEqual((s["said"], s["out_of_order"], s["planning"]["id"]), ({}, ["NEB-30", "NEB-40"], "NEB-40"))
        self.assertIsNotNone(s["declined_until"])

    def test_sigterm_ends_the_loop_after_the_turn_it_is_in(self):
        import signal
        mock = self.mock
        turns = []
        def turn(repo, state, now, **kw):
            turns.append(kw["stopping"]()); os.kill(os.getpid(), signal.SIGTERM); turns.append(kw["stopping"]()); return ["event factory=x"]
        with mock.patch.object(fc.fc_factoryturn, "turn", turn):
            fc.fc_factory.run_loop(self.repo)
        self.assertEqual((turns, len(st.events_since(self.repo, 0))), ([False, True], 1))

    def test_each_dispatch_reaches_disk_during_the_turn(self):
        mock = self.mock
        rec, on_disk = {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}, []
        def turn(repo, state, now, **kw):
            state["dispatched"]["NEB-10"] = rec; kw["saved"]("NEB-10", rec)
            on_disk.append(st.load_state(repo)["dispatched"]); return []
        with mock.patch.object(fc.fc_factoryturn, "turn", turn):
            fc.fc_factory.run_loop(self.repo, turns=1)
        self.assertEqual((on_disk, st.load_state(self.repo)["dispatched"]), ([{"NEB-10": rec}], {"NEB-10": rec}))

    def test_a_state_write_that_fails_is_a_turn_failed_event_and_the_loop_goes_on(self):
        mock = self.mock
        real, calls = fc.fc_factorystate.load_state, []
        def load(repo):
            calls.append(1)
            if len(calls) == 2:  # the first turn's read
                raise OSError("disk full")
            return real(repo)
        with mock.patch.object(fc.fc_factorystate, "load_state", load), \
             mock.patch("traceback.print_exc"), mock.patch.object(fc.fc_factoryturn, "turn", return_value=[]):
            fc.fc_factory.run_loop(self.repo, turns=2)
        self.assertEqual([e["line"] for e in st.events_since(self.repo, 0)], ['event factory=turn-failed why="OSError: disk full"  (the next turn tries again)'])

    def test_spawn_loop_runs_the_loop_in_a_session_of_its_own(self):
        mock = self.mock
        (self.repo / ".compose").mkdir(exist_ok=True)
        with mock.patch.object(subprocess, "Popen") as popen, mock.patch.object(fc.fc_factorystate, "state_path", return_value=self.repo / ".compose" / "factory.json"):
            popen.return_value.pid = 77
            self.assertEqual(fc.fc_factory.spawn_loop(self.repo), 77)
        self.assertEqual(popen.call_args.args[0], [sys.executable, str(BIN / "compose.py"), "factory", "loop", "--repo", str(self.repo)])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_one_loop_per_repo_appends_each_turns_lines(self):
        mock, fcntl = self.mock, self.fcntl
        with open(fc.fc_factory.lock_path(self.repo), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with self.assertRaises(SystemExit):
                fc.fc_factory.run_loop(self.repo, turns=1)
        with mock.patch.object(fc.fc_factoryturn, "turn", return_value=["event factory=x"]):
            fc.fc_factory.run_loop(self.repo, turns=2)
        self.assertEqual([e["line"] for e in st.events_since(self.repo, 0)], ["event factory=x"] * 2)
        self.assertEqual(st.load_state(self.repo)["pid"], os.getpid())

    def test_a_turn_that_raises_is_one_event_and_the_loop_goes_on(self):
        mock = self.mock
        with mock.patch.object(fc.fc_factoryturn, "turn", side_effect=[ValueError("boom"), ["event factory=x"]]), mock.patch("traceback.print_exc"), \
             mock.patch.object(fc.fc_factory.time, "sleep"):
            fc.fc_factory.run_loop(self.repo, turns=2)
        self.assertEqual([e["line"] for e in st.events_since(self.repo, 0)],
                         ['event factory=turn-failed why="ValueError: boom"  (the next turn tries again)', "event factory=x"])

    def test_alive(self):
        self.assertFalse(fc.fc_factory.alive(None))
        self.assertTrue(fc.fc_factory.alive(self.sleeper.pid))
        self.assertFalse(fc.fc_factory.alive(999999))

    def test_stop_lets_work_finish_and_hold_pauses_it(self):
        self.hold_lock()
        self.save(pid=self.sleeper.pid, dispatched={"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0},
                                                    "NEB-11": {"feature": "NEB-8", "project": "gone", "kind": "build", "at": 1.0}})
        self.rests_on.return_value = {"002": "under NEB-10"}
        out = self.verb("stop", hold="the product owner is reworking sign-in")
        self.hold.assert_called_once_with(self.proj, ["002"], "factory stopped: the product owner is reworking sign-in")
        self.assertEqual(out, "factory: stopped; held 1 tasks\n")
        self.assertIsNotNone(st.load_state(self.repo)["stopped"])
        self.sleeper.wait(timeout=5)

    def test_stop_without_hold_holds_nothing(self):
        self.hold_lock(); self.save(pid=self.sleeper.pid)
        self.assertEqual(self.verb("stop"), "factory: stopped; running work finishes\n")
        self.hold.assert_not_called()
        self.sleeper.wait(timeout=5)

    def test_watch_prints_new_events_and_says_when_the_loop_is_gone(self):
        self.save(pid=999999)  # no such process, never stopped
        fc.fc_factory._seen_path(self.repo).write_text(json.dumps({"seen": 0}))
        st.add_event(self.repo, "event factory=offer ticket=NEB-10")
        out = self.verb("watch", timeout=5).splitlines()
        self.assertTrue(out[0].endswith("event factory=offer ticket=NEB-10"))
        self.assertTrue(out[1].endswith("event factory=loop-lost  (the loop is not running: compose factory start runs it again; its state is on disk)"))
        self.assertTrue(self.verb("watch", timeout=1, expect_exit=2).strip().endswith("event timeout"))

    def test_a_new_watcher_starts_at_the_logs_end_and_records_its_pane(self):
        for i in range(3):
            st.add_event(self.repo, f"event factory=x n={i}")
        with self.mock.patch.dict(os.environ, {"HERDR_PANE_ID": "p9"}):
            out = self.verb("watch", timeout=5)
        self.assertRegex(out, r"^\[[\d:]+\] event factory=watching from=3  \(earlier events: compose factory status\)\n$")
        self.assertEqual(st.load_state(self.repo)["manager_pane"], "p9")
        st.add_event(self.repo, "event factory=y")
        self.assertTrue(self.verb("watch", timeout=5).strip().endswith("event factory=y"))

    def test_pace_sets_the_cap_and_decline_snoozes_offers(self):
        self.assertEqual(self.verb("pace", "3"), "factory: day pace 3 tickets at once (was 2); nothing running is stopped\n")
        self.assertEqual(st.load_state(self.repo)["day_tickets"], 3)
        for bad in ("0", "x"):
            with self.assertRaises(SystemExit) as e:
                self.verb("pace", bad)
            self.assertEqual(e.exception.code, f"factory pace takes a whole number of 1 or more, not {bad}")
        self.save(said={"offer": "x"})
        out = self.verb("decline")
        until = st.load_state(self.repo)["declined_until"]
        self.assertAlmostEqual(until, self.time.time() + 3600, delta=30)
        self.assertEqual(out, f"factory: no planning offers until {self.time.strftime('%H:%M', self.time.localtime(until))}; ready work keeps building\n")
        self.assertNotIn("offer", st.load_state(self.repo)["said"])

    def test_plan_marks_out_of_order_unless_it_is_the_offer(self):
        self.save(offer={"id": "NEB-10", "layer": "story", "at": 1.0})
        self.assertIn("(out of order: once it is specced you are asked whether to move it up)", self.verb("plan", "NEB-30"))
        s = st.load_state(self.repo)
        self.assertEqual((s["planning"]["id"], s["out_of_order"]), ("NEB-30", ["NEB-30"]))
        self.verb("plan", "NEB-30")
        self.assertEqual(st.load_state(self.repo)["out_of_order"], ["NEB-30"])
        self.verb("plan", done=True)
        self.assertIsNone(st.load_state(self.repo)["planning"])
        self.assertEqual(self.verb("plan", "NEB-10"), "factory: planning NEB-10\n")
        self.assertEqual(st.load_state(self.repo)["out_of_order"], ["NEB-30"])

    def test_plan_prd_pauses_offers_and_is_never_out_of_order(self):
        self.save(offer={"id": "NEB-10", "layer": "story", "at": 1.0})
        self.assertEqual(self.verb("plan", "PRD"), "factory: planning PRD\n")
        s = st.load_state(self.repo)
        self.assertEqual((s["planning"]["id"], s["out_of_order"]), ("PRD", []))
        self.assertEqual(self.verb("plan", "prd"), "factory: planning PRD\n")
        self.verb("plan", done=True)
        self.assertIsNone(st.load_state(self.repo)["planning"])

    def test_status_names_the_pace_what_is_in_flight_and_the_offer(self):
        self.hold_lock()
        started = self.time.mktime(self.time.strptime(self.time.strftime("%Y-%m-%d") + " 09:00:00", "%Y-%m-%d %H:%M:%S"))
        self.save(pid=self.sleeper.pid, started=started, offer={"id": "NEB-12", "layer": "stories", "at": 1.0},
                  dispatched={"NEB-10": {"feature": "NEB-9", "project": "sign-in", "kind": "build", "at": 1.0}})
        self.assertEqual(self.verb("status").splitlines(), [f"factory: running (pid {self.sleeper.pid}, since 09:00:00), day pace 2 tickets at once",
                                                            "  dispatched: NEB-10 (sign-in, build)", "  last offer: NEB-12 (stories)"])


if __name__ == "__main__":
    unittest.main()
