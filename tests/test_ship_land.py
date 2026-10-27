import hermetic  # first: no test sees the real home
import argparse, io, json, os, re, subprocess, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
import importlib.machinery, importlib.util, sys
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


class FakedLand(unittest.TestCase):
    """`cmd_land` over a fake GitHub (a pull request per branch, every call recorded) and a fake git: no process starts."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        (self.proj / "verify.json").write_text("{}")
        self.calls, self.prs, self.branches, self.view_fail = [], {}, [], ""
        for patch in (mock.patch.object(fc.fc_ship, "gh", self.gh), mock.patch.object(fc.fc_core, "sh", self.sh),
                      mock.patch.object(fc.fc_core, "checkout", lambda path: fc.fc_core.Checkout((self.repo, self.repo / ".git", Path("/common")))),
                      mock.patch.object(fc.fc_layers, "layer_branches", lambda repo, proj: self.branches),
                      mock.patch.object(fc.fc_stack, "layer_base", lambda repo, proj, trunk: "main"),
                      mock.patch.object(fc.fc_worktree, "stack_worktree", lambda repo, proj, trunk: self.repo),
                      mock.patch.object(fc.fc_ship, "LAND_POLL", 0), mock.patch.object(fc.fc_ship, "LAND_SETTLE", 0),
                      mock.patch("subprocess.run", side_effect=AssertionError("subprocess.run")), mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen"))):
            patch.start(); self.addCleanup(patch.stop)

    def submitted(self, layers, prs=None, **kw):
        """`layers` layers, the first `prs` of them (all by default) with an open pull request, which `kw` sets."""
        self.calls.clear()
        self.branches = [f"compose/p-layer-0{i}" for i in range(1, layers + 1)]
        self.prs = {b: {"number": i, "baseRefName": "main", "state": "OPEN", "isDraft": False, "headRefOid": self.tip(b), "url": f"https://github.test/p/pull/{i}",
                        "mergeStateStatus": "CLEAN", "checks": "pass", "refuse": "", "stacked": False, **kw}
                    for i, b in enumerate(self.branches[:prs], 1)}

    def tip(self, branch):
        return f"sha-of-{branch}"

    def pr(self, which):
        return next((p for b, p in self.prs.items() if which in (b, str(p["number"]))), None)

    def done(self, args, out="", err="", code=0):
        return subprocess.CompletedProcess(args, code, out, err)

    def gh(self, args, cwd):
        self.calls.append(" ".join(args))
        if args[:2] == ["pr", "view"]:
            p = self.pr(args[2])
            return self.done(args, json.dumps(p)) if p and not self.view_fail else self.done(args, err=self.view_fail or "no pull requests found for branch", code=1)
        if args[:2] == ["pr", "checks"]:
            return self.done(args, json.dumps([] if self.pr(args[2])["checks"] == "none" else [{"name": "ci", "bucket": self.pr(args[2])["checks"]}]))
        if args[:2] == ["api", "graphql"]:
            return self.done(args, json.dumps({"data": {"repository": {"pullRequest": {"stack": {"size": 2} if self.pr(next(a[2:] for a in args if a.startswith("n=")))["stacked"] else None}}}}))
        p = self.pr(args[2]) if args[0] == "pr" else self.pr(re.search(r"pulls/(\d+)/", args[3]).group(1))
        if args[:2] == ["pr", "merge"] and p["refuse"]:
            return self.done(args, err=p["refuse"], code=1)
        if args[:2] == ["api", "-X"] and p["refuse"]:
            return self.done(args, json.dumps({"status": "failed", "details": {"message": p["refuse"]}}))
        if args[:2] == ["pr", "update-branch"]:
            p.update(mergeStateStatus="CLEAN", headRefOid=p["headRefOid"] + "-rebased")
        if args[:2] in (["pr", "merge"], ["api", "-X"]):
            p["state"] = "MERGED"
        return self.done(args, json.dumps({"status": "merged"}))

    def sh(self, cmd, cwd, **kw):
        out = {"worktree": f"worktree {self.repo}\n", "branch": "compose/p\n", "rev-parse": self.tip(cmd[-1])}.get(cmd[1], "")
        return self.done(cmd, out, code=1 if cmd[1:3] == ["merge-base", "--is-ancestor"] else 0)  # the layer above is rebased already

    def land(self, **kw):
        """The said and printed lines of `compose land` and its exit code."""
        out, buf, code = [], io.StringIO(), 0
        a = argparse.Namespace(repo=str(self.repo), project="p", trunk="compose/p", **{"yes": True, "method": None, "timeout": 1, **kw})
        with mock.patch.object(fc.fc_core, "say", out.append), redirect_stdout(buf):
            try:
                fc.fc_ship.cmd_land(a)
            except SystemExit as e:
                code = e.code
        return out + buf.getvalue().splitlines(), code

    def merged(self):
        return [p["state"] == "MERGED" for p in self.prs.values()]


class FakedLandPlan(FakedLand):
    def test_without_yes_the_plan_stops_at_the_first_layer_without_a_pr_and_the_method_follows_the_flag_then_verify_json(self):
        self.submitted(3, prs=2)
        out, code = self.land(yes=False)
        self.assertEqual(code, 0)
        self.assertIn("land   plan  2 of 3 layer(s) submitted, verify not run; landing 2 into main by squash, bottom up:", out)
        self.assertIn("land   nothing was merged or pushed. Re-run with --yes once the user has said to land this stack", out)
        self.assertEqual([c for c in self.calls if not c.startswith("pr view")], [])
        (self.proj / "results" / "verify.json").write_text(json.dumps({"passed": True}))
        (self.proj / "verify.json").write_text(json.dumps({"land_method": "rebase"}))
        self.assertIn("land   plan  2 of 3 layer(s) submitted, verify PASS; landing 2 into main by rebase, bottom up:", self.land(yes=False)[0])
        (self.proj / "results" / "verify.json").write_text(json.dumps({"passed": False}))
        self.assertIn("land   plan  2 of 3 layer(s) submitted, verify FAIL; landing 2 into main by merge, bottom up:", self.land(yes=False, method="merge")[0])


class FakedLandStops(FakedLand):
    def test_it_stops_with_nothing_merged_on_a_gh_failure_and_on_a_bottom_layer_without_a_pr(self):
        self.submitted(2)
        self.view_fail = "error connecting to api.github.com"
        out, code = self.land()
        self.assertEqual(code, 1)
        self.assertIn("land   STOP  gh pr view failed for compose/p-layer-01: error connecting to api.github.com", out)
        self.submitted(2, prs=0)
        self.view_fail = ""
        out, code = self.land()
        self.assertEqual(code, 1)
        self.assertIn("land   STOP  compose/p-layer-01, compose/p-layer-02 has no pull request: `compose stack --submit` first", out)
        self.assertFalse([c for c in self.calls if not c.startswith("pr view")])

    def test_with_yes_it_refuses_while_a_verify_is_live(self):
        self.submitted(2)
        live = fc.fc_update.live_dir(); live.mkdir(parents=True, exist_ok=True)
        pid = os.getppid()  # a process that is alive and is not this one
        record = live / f"{pid}.json"
        record.write_text(json.dumps({"pid": pid, "kind": "verify", "project": "p", "repo": str(self.repo), "since": "21:01"}))
        self.addCleanup(record.unlink, missing_ok=True)  # the live dir outlives a test, and its records are read by every later land
        out, code = self.land()
        self.assertIn(f"verify is running on p (pid {pid}", code)
        self.assertFalse([c for c in self.calls if not c.startswith("pr view")])

    def test_the_first_layer_stops_the_land_with_nothing_merged_and_says_why(self):
        for why, state, kw in (("required checks on #1 (compose/p-layer-01): fail: ci", {"checks": "fail"}, {}),
                               ("required checks on #1 (compose/p-layer-01) still pending after 0 minutes; re-run to resume", {"checks": "pending"}, {"timeout": 0}),
                               ("gh pr merge #1 (compose/p-layer-01): Pull request is not mergeable", {"refuse": "Pull request is not mergeable"}, {}),
                               ("PR #1 (compose/p-layer-01) has commits compose did not push (other, local sha-of-co); `compose stack --submit` first", {"headRefOid": "other"}, {})):
            with self.subTest(why):
                self.submitted(2)
                self.prs["compose/p-layer-01"].update(state)
                out, code = self.land(**kw)
                self.assertEqual(code, 1)
                self.assertIn(f"land   STOP  {why}. Layers below it are merged; re-run `compose land` to resume", out)
                self.assertEqual(self.merged(), [False, False])


class FakedLandMerges(FakedLand):
    def test_a_behind_pr_is_updated_before_it_merges_and_a_partial_land_says_what_is_left(self):
        self.submitted(3, prs=2)
        self.prs["compose/p-layer-01"]["mergeStateStatus"] = "BEHIND"
        (self.proj / "verify.json").write_text(json.dumps({"land_method": "rebase"}))
        out, code = self.land()
        self.assertEqual(code, 0, out)
        self.assertEqual([c for c in self.calls if c.startswith(("pr update-branch", "pr merge"))], ["pr update-branch 1 --rebase", "pr merge 1 --rebase", "pr merge 2 --rebase"])
        self.assertEqual(out[-3:], ["land   done  2 layer(s) merged into main", "land   left  1 layer(s) not submitted: they rebuild on main at the next compose stack", "next: compose stack --submit --project p"])

    def test_a_pr_in_a_github_stack_merges_through_the_async_api_and_a_failed_status_stops(self):
        self.submitted(2, stacked=True)
        out, code = self.land()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.merged(), [True, True])
        puts = [f"api -X PUT repos/{{owner}}/{{repo}}/pulls/{n}/merge-async -f sha=sha-of-compose/p-layer-0{n} -f merge_method=squash -f merge_action=direct_merge" for n in (1, 2)]
        self.assertEqual([c for c in self.calls if c.startswith(("pr merge", "api -X"))], puts)
        self.submitted(2, stacked=True)
        self.prs["compose/p-layer-01"]["refuse"] = "Required status check is expected"
        out, code = self.land()
        self.assertEqual(code, 1)
        self.assertIn("land   STOP  merge of #1 failed: Required status check is expected (compose/p-layer-01). Layers below it are merged; re-run `compose land` to resume", out)
        self.assertEqual(self.merged(), [False, False])


if __name__ == "__main__":
    unittest.main()
