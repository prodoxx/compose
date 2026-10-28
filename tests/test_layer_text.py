import hermetic  # first: no test sees the real home
import json, os, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock
from layers_kit import WRITTEN, fc, unit
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


class Grouping(unittest.TestCase):
    def names(self, units, budget):
        return [[u["name"] for u in g] for g in fc.fc_layers.group_units(units, budget)]

    def test_consecutive_units_fill_a_layer_up_to_the_budget_in_order(self):
        us = [unit("001", 100), unit("002", 150), unit("003", 50), unit("004", 1)]
        self.assertEqual(self.names(us, 300), [["001", "002", "003"], ["004"]])

    def test_a_task_bigger_than_the_budget_is_never_split_and_stands_alone(self):
        us = [unit("001", 10), unit("002", 500), unit("003", 10)]
        self.assertEqual(self.names(us, 300), [["001"], ["002"], ["003"]])

    def test_a_small_unit_does_not_jump_the_queue_to_fill_a_gap(self):
        us = [unit("001", 200), unit("002", 150), unit("003", 5)]
        self.assertEqual(self.names(us, 300), [["001"], ["002", "003"]])

    def test_lockfiles_are_generated_other_files_are_not(self):
        for f in ("bun.lock", "web/package-lock.json", "yarn.lock", "go.sum", "pnpm-lock.yaml"):
            self.assertTrue(fc.fc_worktree.GENERATED_RE.search(f), f)
        for f in ("src/lock.ts", "locksmith.py", "docs/bun.lock.md"):
            self.assertFalse(fc.fc_worktree.GENERATED_RE.search(f), f)

    def test_a_task_branch_does_not_count_its_lockfile_toward_the_limit(self):
        rows = "1\t0\tpackage.json\n5000\t0\tyarn.lock\n"
        sh = lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 0, rows if "--numstat" in cmd else "package.json\nyarn.lock\n" if "--name-only" in cmd and "--diff-filter=A" not in cmd and "--diff-filter=D" not in cmd else "", "")
        with mock.patch.object(fc.fc_core, "sh", sh):
            self.assertEqual(fc.fc_gate.branch_budget(Path("."), "main", allowed=["package.json", "yarn.lock"]), (1, [], []))


class PrText(unittest.TestCase):
    """A fake gh keeps one PR's title and body in a file; write_pr_text must only replace text the CLI itself wrote."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        (self.dir / "gh").write_text('''#!/usr/bin/env python3
import json, sys
a, f = sys.argv[1:], "pr.json"
try: pr = json.load(open(f))
except FileNotFoundError: pr = None
if a[:2] == ["pr", "view"]: sys.exit(1) if pr is None else print(json.dumps(pr))
if a[:2] == ["pr", "edit"]: json.dump({"title": a[a.index("--title") + 1], "body": a[a.index("--body") + 1]}, open(f, "w"))
''')
        (self.dir / "gh").chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"PATH": f"{self.dir}:{os.environ['PATH']}"}); self.env.start(); self.addCleanup(self.env.stop)

    def pr(self):
        return json.loads((self.dir / "pr.json").read_text())

    def sync(self, title, body):
        before = fc.fc_stack.gh_pr_text(self.dir, "b")
        return fc.fc_stack.write_pr_text(self.dir, self.dir, "b", before, title, body)

    def test_text_is_written_on_a_new_pr_and_on_one_the_cli_wrote_but_never_over_an_edit(self):
        self.assertTrue(self.sync("t1", "b1"))
        self.assertTrue(self.sync("t2", "b2\n(231 lines)"))  # still the CLI's own text: refreshed
        self.assertEqual(self.pr(), {"title": "t2", "body": "b2\n(231 lines)"})
        (self.dir / "pr.json").write_text(json.dumps({"title": "t2", "body": "What\nWhy\nChecks"}))  # a person rewrote the body
        self.assertFalse(self.sync("t3", "b3"))
        self.assertEqual(self.pr(), {"title": "t2", "body": "What\nWhy\nChecks"})

    def test_a_pr_from_before_the_record_existed_keeps_its_text(self):
        (self.dir / "pr.json").write_text(json.dumps({"title": "hand", "body": "written"}))
        self.assertFalse(self.sync("t", "b"))
        self.assertEqual(self.pr(), {"title": "hand", "body": "written"})


class FakeCommits(unittest.TestCase):
    """A project folder and commits that are only a sha, a subject and the files they touch: what the PR text reads of git."""
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()).resolve(); self.proj = self.repo / ".compose" / "p"
        (self.proj / "results").mkdir(parents=True)
        self.subjects, self.touched = {}, {}
        sh = lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 0, self.subjects[cmd[-1]] + "\n", "")  # `git log -1 --format=%s <sha>`
        for p in (mock.patch.object(fc.fc_core, "sh", sh), mock.patch.object(fc.fc_layers, "commit_facts", lambda repo, c: (self.touched[c], 0, {}))):
            p.start(); self.addCleanup(p.stop)

    def commit(self, subject, files=()):
        sha = f"c{len(self.subjects)}"; self.subjects[sha] = subject; self.touched[sha] = list(files)
        return sha

    def pr_file(self, name, text):
        (self.proj / "pr").mkdir(exist_ok=True)
        (self.proj / "pr" / f"{name}.md").write_text(text)


class PrTitle(FakeCommits):
    """A layer's PR is titled from its commits: one commit's subject, else its first task's title or first commit's subject and N more
    tasks or commits, never past TITLE_CHARS before its (i/n)."""
    METAS = [{"id": "001", "title": "001: Add the toggle"}, {"id": "002", "title": "Add the panel"}, {"id": "003", "title": "Add the menu"}]

    def title(self, tasks, subjects, n=2, branch="compose/p-layer-01"):
        commits = [self.commit(s) for s in subjects]
        l = {"branch": branch, "tasks": tasks, "keys": tasks, "lines": 40, "generated": {}, "commits": commits}
        with mock.patch.object(fc.fc_stack, "layer_body", return_value=""):  # the body is not this title's concern
            return fc.fc_stack.layer_pr(self.repo, self.proj, l, n, self.METAS)[0]

    def test_a_one_commit_layer_is_its_subject_and_a_groundwork_layer_its_first_subject_and_n_more(self):
        self.assertEqual(self.title(["groundwork"], ["Ignore build output"]), "Ignore build output (1/2)")
        self.assertEqual(self.title(["groundwork"] * 3, ["Ignore build output", "Pin the toolchain", "Add a lint step"]), "Ignore build output and 2 more commits (1/2)")

    def test_a_task_layer_is_its_first_tasks_title_and_n_more_counting_only_its_other_tasks(self):
        subjects = ["test 001", "toggle work", "test 002", "panel work", "menu work"]
        self.assertEqual(self.title(["001", "002", "003"], subjects, 4, "compose/p-layer-02"), "Add the toggle and 2 more tasks (2/4)")
        self.assertEqual(self.title(["001"], ["test 001", "toggle work"]), "Add the toggle (1/2)")
        self.assertEqual(self.title(["groundwork", "002", "003"], ["Ignore build output", "panel work", "menu work"]), "Add the panel and 1 more task (1/2)")

    def test_a_jira_key_on_the_project_branch_prefixes_the_title(self):
        (self.proj / "verify.json").write_text(json.dumps({"branch": "js-2888-react-beside-vue"}))
        self.assertEqual(self.title(["001", "002"], ["toggle work", "panel work"], 4, "js-2888-react-beside-vue-layer-03"), "JS-2888: Add the toggle and 1 more task (3/4)")
        self.assertEqual(self.title(["groundwork"], ["Ignore build output"]), "JS-2888: Ignore build output (1/2)")
        self.pr_file("001-002", WRITTEN)  # a written title gets the key and the count too, within the same cap
        self.assertEqual(self.title(["001", "002"], ["toggle work", "panel work"], 4, "js-2888-react-beside-vue-layer-03"), "JS-2888: Keep the toggle's state across reloads (3/4)")
        self.pr_file("001-002", WRITTEN.replace("reloads", "reloads " + "x" * 30))
        self.assertEqual(self.title(["001", "002"], ["toggle work", "panel work"], 4, "js-2888-react-beside-vue-layer-03"), "JS-2888: Add the toggle and 1 more task (3/4)")
        self.linear()
        self.assertEqual(self.title(["001", "002", "003"], ["toggle work", "panel work", "menu work"], 1), "JS-2888: Stacked [PRs]")

    def linear(self):
        issues = {"NEB-10": {"title": "Users flip the toggle (C2)", "parentId": "NEB-9"}, "NEB-20": {"title": "Open the menu", "parentId": "NEB-9"},
                  "NEB-1": {"parentId": "NEB-10"}, "NEB-2": {"parentId": "NEB-10"}, "NEB-3": {"parentId": "NEB-20"}}
        (self.proj / "results" / "tasks.json").write_text(json.dumps({"feature": {"id": "NEB-9", "title": "Stacked \\[PRs\\]"}, "tasks": [], "issues": issues}))
        self.METAS = [{**m, "linear": f"NEB-{i}"} for i, m in enumerate(PrTitle.METAS, 1)]

    def test_a_one_pr_stack_is_titled_by_its_feature_and_has_no_count(self):
        self.assertEqual(self.title(["001", "002"], ["toggle work", "panel work"], 1), "Add the toggle and 1 more task")  # no feature title known
        self.linear()
        self.assertEqual(self.title(["001", "002", "003"], ["test 001", "toggle work", "panel work", "menu work"], 1), "Stacked [PRs]")
        self.assertEqual(self.title(["groundwork"], ["Ignore build output"], 1), "Stacked [PRs]")

    def test_a_layer_holding_one_whole_story_is_titled_by_that_story(self):
        self.linear()
        self.assertEqual(self.title(["groundwork", "001", "002"], ["Ignore build output", "toggle work", "panel work"]), "Users flip the toggle (1/2)")
        self.assertEqual(self.title(["003"], ["test 003", "menu work"], 3, "compose/p-layer-03"), "Open the menu (3/3)")
        self.assertEqual(self.title(["001"], ["test 001", "toggle work"]), "Add the toggle (1/2)")  # part of a story: its task
        self.assertEqual(self.title(["002", "003"], ["panel work", "menu work"]), "Users flip the toggle; Open the menu (1/2)")  # two stories, named

    def test_a_groundwork_layer_is_groundwork_for_the_feature(self):
        self.linear()
        self.assertEqual(self.title(["groundwork"] * 2, ["Test that the briefs read the section " * 5, "Add the stubs"]), "Groundwork for Stacked [PRs] (1/2)")
        self.assertEqual(self.title(["groundwork"], ["Add the stubs"], 3), "Groundwork for Stacked [PRs] (1/3)")

    def test_a_title_past_the_cap_is_cut_at_a_word_and_keeps_its_prefix_and_count(self):
        long = "Test that the architect, change, and explore briefs read the section with linear get, then write the whole section back"
        self.assertEqual(self.title(["groundwork"], [long]), "Test that the architect, change, and explore briefs read the section... (1/2)")
        self.METAS = [{"id": "001", "title": long}, *PrTitle.METAS[1:]]
        self.assertEqual(self.title(["001", "002"], ["toggle work", "panel work"]), "Test that the architect, change, and explore briefs... and 1 more task (1/2)")
        (self.proj / "verify.json").write_text(json.dumps({"branch": "js-2888-react-beside-vue"}))
        self.assertEqual(self.title(["groundwork"], [long]), "JS-2888: Test that the architect, change, and explore briefs read the... (1/2)")
        self.assertEqual(self.title(["groundwork"], ["x" * 100]), "JS-2888: " + "x" * 60 + "... (1/2)")
        self.linear()
        self.assertEqual(self.title(["001"], ["test 001", "toggle work"], 1), "JS-2888: Stacked [PRs]")
        issues = json.loads((self.proj / "results" / "tasks.json").read_text())
        issues["issues"]["NEB-20"]["title"] = "Open the menu from anywhere in the app, with the keyboard or the pointer"
        (self.proj / "results" / "tasks.json").write_text(json.dumps(issues))
        self.assertEqual(self.title(["002", "003"], ["panel work", "menu work"]), "JS-2888: Add the panel and 1 more task (1/2)")  # the stories do not fit


class PrBody(FakeCommits):
    """A layer's PR body: Why from its stories, What changed, How it was tested and Tickets, from results files and its own commits."""
    METAS = [{"title": f"Build {m['id']}", "files": [], "after": "", **m} for m in [
             {"id": "001", "linear": "NEB-1", "test": "python3 -m unittest tests.toggle", "protected": ["t004.test", "t001.test"],
              "body": "## Goal\n\nIn `bin/toggle.py` (L10-L40), flip `state`.\n"},
             {"id": "002", "linear": "NEB-2", "test": "python3 -m unittest tests.toggle", "protected": ["t005.test"], "body": "## Goal\nShow the panel.\n"},
             {"id": "003", "linear": "NEB-3", "test": "make menu", "protected": ["t003.test"], "body": "## Goal\nOpen the menu.\n"}]]
    FEATURE = {"id": "NEB-9", "title": "Stacked PRs", "description": "Next: plan it.\n\nFix [compose 7](<https://github.com/o/r/issues/7>) and o/r#8."}
    STORIES = {"NEB-10": {"title": "Users flip the toggle (C2)", "description": "Next: tasks 001 and 002.\n\nFixes [compose 5](<https://github.com/o/r/issues/5>). "
                          "Decisions C1, C2 on NEB-9.\n\nThe toggle is lost on reload, so r\\[\"state\"\\] resets. Later text.\n\n## Acceptance criteria\n\n* it flips"},
               "NEB-20": {"title": "Open the menu", "description": "Card C4 (compose 6). Fixes o/r#6."}}

    def body(self, tasks, units, layers=None, **results):
        issues = {**{k: {**v, "parentId": "NEB-9"} for k, v in self.STORIES.items()},
                  "NEB-1": {"parentId": "NEB-10"}, "NEB-2": {"parentId": "NEB-10"}, "NEB-3": {"parentId": "NEB-20"}}
        results = {"tasks": {"feature": self.FEATURE, "tasks": [], "issues": issues}, "001": {"attempts": 1}, "002": {"attempts": 3}, **results}
        for name, data in results.items():
            (self.proj / "results" / f"{name}.json").write_text(json.dumps(data))
        (self.proj.parent / "linear.json").write_text(json.dumps({"project": "https://linear.app/acme/project/p-1", "team": "Engineering"}))
        shas = [[self.commit(subject, files) for subject, files, _ in commits] for commits in units]  # units: each of the layer's tasks' commits, as (subject, files, message body)
        l = {"branch": "compose/p-layer-01", "tasks": tasks, "keys": tasks, "lines": 40, "generated": {}, "commits": [c for cs in shas for c in cs], "units": shas}
        stack = [l, *({"branch": f"compose/p-layer-{i:02d}", "tasks": t} for i, t in enumerate(layers or [], 2))]
        with mock.patch.object(fc.fc_stack, "layer_title", return_value=""):  # the title is not this body's concern
            body = fc.fc_stack.layer_pr(self.repo, self.proj, l, 2, self.METAS, stack if layers is not None else ())[1]
        sections, lines = {}, []
        for line in filter(str.strip, body.splitlines()):
            if line.startswith("## "): sections[line[3:]] = lines = []
            else: lines.append(line.strip())
        return sections

    TASK_COMMITS = [[("Cover the toggle", ["t004.test"], ""), ("Build the toggle", ["toggle.src", "t001.test"], "")], [("Build the panel", ["panel.src"], "")]]

    def test_a_task_layer_has_every_section_in_order_from_its_stories_commits_and_results(self):
        s = self.body(["001", "002"], self.TASK_COMMITS)
        self.assertEqual(list(s), ["Why", "What changed", "How it was tested", "Tickets"])
        self.assertEqual(s["What changed"], ["- Build the toggle", "- Build the panel"])  # one line per task, its test commits folded in
        self.assertEqual(s["How it was tested"], ["- `python3 -m unittest tests.toggle`"])  # one line per command
        self.assertEqual(s["Tickets"], ["- [NEB-1](https://linear.app/acme/issue/NEB-1)", "- [NEB-2](https://linear.app/acme/issue/NEB-2)"])

    def test_why_is_the_story_title_and_first_plain_sentence_never_a_brief_card_id_or_linear_escape(self):
        s = self.body(["001", "002"], self.TASK_COMMITS)
        self.assertEqual(s["Why"][0], 'Users flip the toggle. The toggle is lost on reload, so r["state"] resets.')
        why = "\n".join(s["Why"])
        for leak in ("Goal", "bin/toggle.py", "L10", "C1", "C2", "Next", "NEB-9", "\\"):
            self.assertNotIn(leak, why)
        long = {"NEB-10": {"title": "Users flip the toggle", "description": "Why. " * 200}}
        with mock.patch.object(self, "STORIES", {**self.STORIES, **long}):
            s = self.body(["001", "002"], self.TASK_COMMITS)
        self.assertLessEqual(len("\n".join(s["Why"])), 400)

    def test_a_layer_with_several_stories_names_their_titles(self):
        s = self.body(["002", "003"], [self.TASK_COMMITS[1], []], layers=[])
        self.assertEqual(s["Why"][0], "Users flip the toggle. Open the menu.")

    def test_fixes_lands_on_the_layer_that_completes_the_issue_and_part_of_below_it(self):
        s = self.body(["001", "002"], self.TASK_COMMITS, layers=[["003"]])
        self.assertEqual(s["Why"][1:], ["- Fixes o/r#5", "- Part of o/r#7", "- Part of o/r#8"])  # #7 and #8: the feature's, its last task above
        s = self.body(["001", "002", "003"], [*self.TASK_COMMITS, []], layers=[])
        self.assertEqual(s["Why"][1:], ["- Fixes o/r#5", "- Fixes o/r#6", "- Fixes o/r#7", "- Fixes o/r#8"])
        s = self.body(["001"], self.TASK_COMMITS[:1])  # without the stack, every task not here is still to come
        self.assertEqual(s["Why"][1:], ["- Part of o/r#5", "- Part of o/r#7", "- Part of o/r#8"])

    def test_verify_is_reported_last_after_the_checks_it_passed_only_when_its_results_exist(self):
        checks = [{"cmd": "make lint", "exit": 1}, {"cmd": "make all", "exit": 0}, {"cmd": "python3 -m unittest tests.toggle", "exit": 0}]
        s = self.body(["001"], self.TASK_COMMITS[:1], verify={"passed": True, "rounds": [{"checks": checks[:1]}, {"checks": checks}]})
        self.assertEqual(s["How it was tested"], ["- `python3 -m unittest tests.toggle`", "- `make all`", "- compose verify passed"])
        s = self.body(["001"], [[("Cover more", ["t004.test"], "")]], verify={"passed": False, "rounds": [{}, {}]})
        self.assertEqual(s["How it was tested"], ["- `python3 -m unittest tests.toggle`", "- compose verify failed"])
        self.assertEqual(s["What changed"], ["- Build 001"])  # only test commits: the task's own title

    def test_how_it_was_tested_has_commands_a_reviewer_can_run_not_this_machines_paths_or_local_scripts(self):
        checks = [{"cmd": f"bun test {self.repo}/src/a.test.ts", "exit": 0}, {"cmd": f"bash {self.repo}/.compose/p/e2e/probe.sh", "exit": 0},
                  {"cmd": f"bash {Path.home()}/elsewhere/x.sh", "exit": 0}, {"cmd": "bash .compose/p/e2e/probe.sh", "exit": 0}]
        s = self.body(["001"], self.TASK_COMMITS[:1], verify={"passed": True, "rounds": [{"checks": checks}]})
        self.assertEqual(s["How it was tested"], ["- `python3 -m unittest tests.toggle`", "- `bun test src/a.test.ts`", "- compose verify passed"])

    def test_a_groundwork_layer_says_in_one_sentence_what_it_prepares_with_no_issues_tests_or_tickets(self):
        s = self.body(["groundwork", "groundwork"], [[("Ignore build output", ["a.txt"], "Keeps dist out of review.")], [("Pin the toolchain", ["b.txt"], "")]])
        self.assertEqual(s, {"Why": ["Groundwork for Stacked PRs."], "What changed": ["- Ignore build output", "- Pin the toolchain"]})
        s = self.body(["groundwork", "003"], [[("Ignore build output", ["a.txt"], "")], [("Build the menu", ["menu.src"], "")]])
        self.assertEqual(s["Why"][0], "Open the menu.")  # a task in the layer makes it a task layer
        self.assertEqual(s["Tickets"], ["- [NEB-3](https://linear.app/acme/issue/NEB-3)"])


if __name__ == "__main__":
    unittest.main()
