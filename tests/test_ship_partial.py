import hermetic  # first: no test sees the real home
import json, os, subprocess, unittest
from ship_kit import Ship, fc
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


class PartialSubmit(Ship):
    def result(self, tid, status):
        (self.proj / "results" / f"{tid}.json").write_text(json.dumps({"status": status}))

    def origin_git(self, *a):
        return subprocess.run(["git", "--git-dir", str(self.origin), *a], check=True, capture_output=True, text=True).stdout.split()

    def pushed(self):
        """Origin's layer refs and SHAs, and every file in any ref but main."""
        refs = {r.removeprefix("refs/heads/"): s for s, r in zip(*[iter(self.origin_git("for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/compose"))] * 2)}
        return refs, {f for r in refs for f in self.origin_git("ls-tree", "-r", "--name-only", r)}

    def test_a_failed_task_holds_it_and_the_ones_after_back_and_a_later_submit_adds_them_once_it_is_done_on_linear(self):
        self.result("002", "failed")
        out, code = self.submit()
        self.assertEqual(code, 0, out)
        self.assertTrue(any("stack  hold  002 is failed; 002, 003 not submitted" in l for l in out), out)
        before, files = self.pushed()
        self.assertTrue({"setup.txt", "001.src", "t001.test"} <= files)
        self.assertFalse(files & {"002.src", "003.src", "t002.test", "t003.test"})
        self.assertEqual(set(json.loads((self.proj / "results" / "prs.json").read_text())), set(before))
        tasks = json.loads((self.proj / "results" / "tasks.json").read_text())  # done on Linear: not held, whatever its result
        tasks["tasks"][1]["linear_state"] = "Done"
        (self.proj / "results" / "tasks.json").write_text(json.dumps(tasks))
        out, code = self.submit()
        self.assertEqual(code, 0, out)
        after, files = self.pushed()
        self.assertTrue({"002.src", "003.src", "t002.test", "t003.test"} <= files)
        self.assertEqual({b: after[b] for b in before}, before)
        self.assertEqual(json.loads((self.proj / "results" / "prs.json").read_text()), {p["branch"]: int(n) for n, p in self.prs().items()})

    def test_a_task_after_a_higher_id_is_not_held_behind_the_tasks_between_them(self):
        """001 waits for 003, so it sits right above 003 and below 002: 002 failing holds 002 alone."""
        tasks = json.loads((self.proj / "results" / "tasks.json").read_text())
        tasks["tasks"][0]["after"] = "003"
        (self.proj / "results" / "tasks.json").write_text(json.dumps(tasks))
        self.result("002", "failed")
        out, code = self.submit()
        self.assertEqual(code, 0, out)
        self.assertTrue(any("stack  hold  002 is failed; 002 not submitted" in l for l in out), out)
        self.assertTrue({"001.src", "003.src"} <= self.pushed()[1])
        self.assertFalse(self.pushed()[1] & {"002.src", "t002.test"})

    def test_a_pending_first_task_stops_with_nothing_pushed(self):
        (self.proj / "results" / "001.json").unlink()
        out, code = self.submit()
        self.assertEqual(code, 1)
        self.assertTrue(any("stack  STOP  nothing to submit: 001 is pending" in l for l in out), out)
        self.assertEqual(self.pushed()[0], {})
        self.assertFalse(any("stack submit" in c for c in self.log()))


class LandedLayers(Ship):
    def merge(self, n):
        subprocess.run(["gh", "pr", "edit", str(n), "--base", "main"], cwd=self.repo, check=True)
        subprocess.run(["gh", "pr", "merge", str(n), "--squash"], cwd=self.repo, check=True)

    def tree(self, ref):
        return self.git("ls-tree", "-r", "--name-only", ref).splitlines()

    def assert_built_on_main(self, branch, file, below="main"):
        self.assertIn(file, self.tree(branch))
        self.assertTrue(self.git("rev-list", f"{below}..{branch}"), f"{branch} adds nothing over {below}")
        self.assertEqual(subprocess.run(["git", "merge-base", "--is-ancestor", "refs/remotes/origin/main", branch], cwd=self.repo).returncode, 0)

    def test_a_missing_base_on_origin_stops_the_stack_and_the_layers_build_on_the_new_base_while_the_primary_checkout_is_on_it(self):
        self.submit(); self.merge(1)
        self.git("remote", "set-url", "origin", str(self.origin) + "-gone")
        out, code = self.run_cmd(fc.fc_ship.cmd_stack, submit=False)
        self.assertTrue(any("STOP" in l and "landed but main cannot be fetched from origin" in l for l in out), out)
        self.git("remote", "set-url", "origin", str(self.origin))
        self.git("checkout", "-q", "main")  # a branch checked out in a worktree cannot be fetched into
        self.write("mine.txt", "not committed\n")
        before = self.git("rev-parse", "main")
        out, code = self.run_cmd(fc.fc_ship.cmd_stack, submit=False)
        self.assertEqual(code, 0, out)
        self.assertFalse(any("STOP" in l or "cannot be fetched" in l for l in out), out)
        self.assertIn("stack  landed compose/p-layer-01; building on origin/main", out)
        self.assertIn("stack  ok    (main) <- compose/p-layer-02 <- compose/p-layer-03", out)
        self.assertEqual(self.git("rev-parse", "main"), before)  # the user's branch and files are not moved under them
        self.assertEqual(self.git("status", "--porcelain"), "?? mine.txt")
        origin_main = self.git("--git-dir", str(self.origin), "rev-parse", "main")
        self.assertNotEqual(origin_main, before)
        self.assertEqual(self.git("merge-base", "compose/p-layer-02", origin_main), origin_main)  # cut on origin's base, not the stale local one
        self.assert_built_on_main("compose/p-layer-02", "002.src", origin_main)
        self.assert_built_on_main("compose/p-layer-03", "003.src", "compose/p-layer-02")

    def test_open_pull_requests_or_no_layers_file_fetch_nothing(self):
        self.submit()
        old = self.git("rev-parse", "main")
        self.git("checkout", "-q", "main"); self.write("base.txt", "b\n"); self.commit("Move the base"); self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "compose/p"); self.git("branch", "-f", "main", old)
        for _ in range(2):
            out, code = self.run_cmd(fc.fc_ship.cmd_stack, submit=False)
            self.assertFalse(any("landed" in l for l in out), out)
            self.assertEqual(self.git("rev-parse", "main"), old)
            (self.proj / "layers.json").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
