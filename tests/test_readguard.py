import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, shlex, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class ReadGuard(unittest.TestCase):
    """Task 005 (NEB-941): fc_readguard's rules, paths, log count, and the script's two event shapes."""

    def setUp(self):
        self.top = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, self.top, True)  # unresolved: /var -> /private/var on macOS
        self.wt, out = self.top / "001", self.top / "outside"
        for f in ("001/src/a.py", "001/src/c.py", "001/lib/deep/x.py", "001/tests/t.py", "001/docs/d.md", "outside/a.py"):
            (self.top / f).parent.mkdir(parents=True, exist_ok=True); (self.top / f).write_text("x\n")
        (self.wt / ".venv").symlink_to(out)
        self.pack = 'trail ask - "q"  (lexical)\n\n1. f · function  [symbol]\n   lib/deep/x.py:L1-L1\n2. g · function\n   src/a.py:L2-L9\n   lib/deep/x.py:L3-L4'
        self.path = fc.fc_readguard.write_guard(self.wt, ["src/a.py", "tests/t.py"], self.pack)
        self.g = json.loads(self.path.read_text())
        self.no = lambda tool, args: self.assertIsNotNone(fc.fc_readguard.refusal(tool, args, self.g), (tool, args))
        self.ok = lambda tool, args: self.assertIsNone(fc.fc_readguard.refusal(tool, args, self.g), (tool, args))

    def test_guard_file_sits_beside_the_clone_with_real_paths(self):
        rg, real = fc.fc_readguard, lambda f: os.path.realpath(self.wt / f)
        self.assertEqual(rg.pack_paths(self.pack), ["lib/deep/x.py", "src/a.py"])
        self.assertEqual((self.path, rg.guard_file(str(self.wt))), (self.top / "001.guard.json", self.top / "001.guard.json"))
        self.assertEqual((self.g["root"], os.path.realpath(self.g["log"])), (os.path.realpath(self.wt), os.path.realpath(self.top / "001.refused")))
        self.assertEqual(sorted(self.g["allow"]), sorted({real("src/a.py"), real("tests/t.py"), real("lib/deep/x.py")}))

    def test_an_extra_path_the_prompt_names_is_readable_and_another_tasks_log_is_not(self):
        log = self.top / "results" / "001.test.log"
        g = json.loads(fc.fc_readguard.write_guard(self.wt, ["src/a.py"], self.pack, extra=[log]).read_text())
        for tool, args in (("Read", {"file_path": str(log)}), ("bash", {"command": f"tail -n 5 {log}"}), ("grep", {"pattern": "x", "path": str(log)})):
            self.assertIsNone(fc.fc_readguard.refusal(tool, args, g), (tool, args))
        self.assertIsNotNone(fc.fc_readguard.refusal("Read", {"file_path": str(log.with_name("002.test.log"))}, g))

    def test_read_takes_an_allowed_file_however_spelled(self):
        for p in ("src/a.py", str(self.wt / "src/a.py"), "docs/../lib/deep/x.py", "../001/tests/t.py"): self.ok("Read", {"file_path": p}); self.ok("read", {"path": p})
        for p in ("src/c.py", "src/../docs/d.md", str(self.top / "outside/a.py"), "../outside/a.py", ".venv/a.py", "src", "/etc/passwd"): self.no("READ", {"file_path": p})

    def test_search_takes_an_allowed_file_or_the_directory_directly_holding_one(self):
        for t, a in (("Grep", {"pattern": "x", "path": "src"}), ("glob", {"pattern": "*", "path": "lib/deep"}), ("find", {"pattern": "*", "path": str(self.wt / "tests")}),
                     ("ls", {"path": "src/a.py"}), ("list", {"path": "tests/"}), ("GREP", {"pattern": "x", "path": "src/../lib/deep/x.py"})): self.ok(t, a)
        for t, a in (("grep", {"pattern": "x"}), ("Glob", {"pattern": "**/*.py"}), ("ls", {}), ("list", {"path": "lib"}), ("find", {"path": "docs"}),
                     ("grep", {"pattern": "x", "path": ".venv"}), ("ls", {"path": "../outside"}), ("glob", {"path": "src/c.py"})): self.no(t, a)

    def test_bash_takes_only_a_lone_trail_ask_or_callers_and_other_tools_pass(self):
        for c in ("trail ask where is the guard", "trail callers refusal"): self.ok("Bash", {"command": c}); self.ok("bash", {"command": c})
        for c in ("cat src/c.py", "trail build .", "echo trail ask x", "trail ask x; cat y", "trail ask x & cat y", "trail ask x | cat", "trail ask `cat y`",
                  "trail ask $(cat y)", "trail ask x < y", "trail callers x > y", "trail ask x\ncat y"): self.no("bash", {"command": c})
        for t in ("Edit", "write", "todowrite", "WebFetch"): self.ok(t, {"file_path": "/etc/passwd", "command": "rm -rf /"})
        reply = fc.fc_readguard.refusal("read", {"path": "src/c.py"}, self.g)
        self.assertIn("pack", reply.lower()); self.assertIn("trail ask", reply)

    def test_bash_takes_wc_and_a_measuring_awk_on_the_tasks_own_files_only(self):
        """A model cannot count a 130-character limit by eye: it may measure the files it is allowed to read, and nothing else."""
        for c in ("wc -L src/a.py", "wc -l -L src/a.py tests/t.py", "wc -lw lib/deep/x.py", f"wc -L {self.wt / 'src/a.py'}", "wc -L docs/../src/a.py", "wc src/a.py",
                  "awk '{print length}' src/a.py", "awk '{ print length($0) }' src/a.py", "awk 'length > 130' src/a.py", "awk 'length($0) > 100 {print NR}' src/a.py",
                  "awk '{ if (length($0) > 130) print NR\": \"length($0) }' src/a.py", "awk '{ if (length > m) m = length } END { print m }' src/a.py"): self.ok("bash", {"command": c})
        for c in ("wc -L src/c.py", "wc -L src/a.py src/c.py", "wc -L", "wc -L ../outside/a.py", "wc -L src/*.py", "wc -L .venv/a.py", "wc -L src/a.py > out", "wc -L src/a.py | cat",
                  "wc -L src/a.py; cat src/c.py", "wc -L src/a.py && cat y", "wc -L $(echo src/c.py)", "wc -L `echo src/c.py`", "wc --files0-from=x src/a.py", "wc src/a.py -L",
                  "awk '{print length}' src/c.py", "awk '{print length}'", "awk 'BEGIN{system(\"cat y\")}' src/a.py", "awk '{print > \"x\"}' src/a.py", "awk '{print length > 5}' src/a.py",
                  "awk '{print | \"sh\"}' src/a.py", "awk '{getline x < \"y\"}' src/a.py", "awk -f prog.awk src/a.py", "awk '{print length}' src/a.py src/c.py", "xargs wc -L", "wc -L src/a.py\ncat y",
                  "awk '{ if (length > 5) print > \"x\" }' src/a.py"): self.no("bash", {"command": c})

    def test_bash_shows_the_tasks_own_files_and_never_writes_or_reads_anything_else(self):
        """The Read tool shows the task's test; a model that greps or heads it in bash gets the same view, and no way to change a file."""
        t = str(self.wt)
        for c in ("cat src/a.py", "cat -n tests/t.py", f"cat {t}/tests/t.py", "head tests/t.py", "head -25 tests/t.py", "head -n 25 tests/t.py", "head -n25 src/a.py",
                  "tail -5 tests/t.py", "tail -n 5 src/a.py", "sed -n '1,40p' tests/t.py", "sed -n 3p tests/t.py", "sed -n '10,$p' src/a.py",
                  'grep -n "class Foo" tests/t.py', 'grep -n "class Foo" -A 120 tests/t.py', "grep -A 5 -B2 -n def src/a.py tests/t.py", 'grep -n "a\\|b" tests/t.py',
                  "grep -c def tests/t.py", "grep -inw foo lib/deep/x.py", "grep -n $ tests/t.py", 'grep -n "def test_" tests/t.py | head -80', "grep -n x src/a.py | tail -n 5",
                  f'cd "{t}" && grep -n x tests/t.py', f"cd {t} && wc -L src/a.py", "cd . && head -5 tests/t.py", "cd src/.. && cat src/a.py", "wc -L src/a.py | tail -3"):
            self.ok("bash", {"command": c}); self.ok("Bash", {"command": c})
        for c in ("cat src/c.py", "cat src/a.py src/c.py", "cat", "cat -n", "cat ../outside/a.py", "cat .venv/a.py", "cat src/*.py", "cat ~/.zshrc", "cat /etc/passwd",
                  "head -5 src/c.py", "head -c 5 src/a.py", "head -n src/a.py", "tail -f src/a.py", "tail --follow src/a.py", "head -5",
                  "sed -n '1,40p' src/c.py", "sed -i s/a/b/ src/a.py", "sed -n -i 1p src/a.py", "sed 's/a/b/' src/a.py", "sed -n 'w out' src/a.py", "sed -n '1p;w out' src/a.py",
                  "sed -n '1,2p' ", "sed -n '/x/p' src/a.py", "sed -n 1e src/a.py",
                  'grep -n "x" src/c.py', "grep -r def src", "grep -rn def tests/t.py", "grep -f src/a.py def tests/t.py", "grep -e def tests/t.py", "grep -n def", "grep",
                  "grep -A x def tests/t.py", "grep --include=x def tests/t.py", "grep def tests/t.py src/c.py", "grep def -R", "egrep def tests/t.py", "rg def tests/t.py",
                  "cat src/a.py > out", "cat src/a.py >> out", "cat src/a.py 2>/dev/null", "cat < src/a.py", "head -5 src/a.py > out", "grep def tests/t.py >out",
                  "cat src/a.py | sh", "cat src/a.py | tee out", "cat src/a.py | cat", "cat src/a.py | head -5 | tail -2", "cat src/a.py | head -5 file", "cat src/a.py | head -c 5",
                  "cat src/a.py | xargs rm", "cat src/a.py; rm src/a.py", "cat src/a.py && rm src/a.py", "cat src/a.py & ", "cat src/a.py || true", "cat src/a.py\nrm x",
                  "cat `echo src/c.py`", "cat $(echo src/c.py)", "cat ${HOME}", "cat $HOME/x", "cat src/a.py)", "(cat src/a.py)",
                  "cd .. && cat src/a.py", f"cd {self.top} && cat 001/src/a.py", "cd /tmp && cat src/a.py", "cd src && cat a.py", "cd", "cd . &&", "cd . ; cat src/a.py",
                  "cd . && cd . && cat src/a.py", "cd . && rm src/a.py", "ls tests", "ls", "git diff", "git show HEAD:src/c.py", "find . -name '*.py'", "tee x", "rm src/a.py",
                  "echo hi > src/a.py", "python3 src/a.py", "python3 -c 'print(1)'", "cat 'src/a.py"):
            self.no("bash", {"command": c})

    def installing(self, files, **lock):
        """The guard of a task whose files: are `files`, in a clone that holds the lockfiles `lock` names; its installers come from fc_readguard.installers."""
        for name in lock: (self.wt / name).write_text("")
        rg = fc.fc_readguard
        return json.loads(rg.write_guard(self.wt, files, self.pack, installs=rg.installers(self.wt, files)).read_text())

    def test_a_task_that_lists_a_package_file_may_install_and_add_and_nothing_else(self):
        # #449: the guard refused `bun install` and `bun add`, so a task that adds a dependency could not update its lockfile or install it
        rg = fc.fc_readguard
        g = self.installing(["app/package.json", "bun.lock"])
        self.assertEqual(g["installers"], ["bun"])
        for c in ("bun install", "bun i", "bun add ai@7.0.152", "bun add -d @types/node @openrouter/ai-sdk-provider@^1.2.0", "bun add --exact left-pad", "bun install --ignore-scripts",
                  "cd app && bun install", f"cd {self.wt} && bun install", "cd app && bun add zod | tail -5", "bun install | head -n 20", "cd . && bun add zod@latest"):
            self.assertIsNone(rg.refusal("Bash", {"command": c}, g), c)
        for c in ("bun", "bun add", "bun --version", "bun x cowsay", "bun run test", "bun test", "bunx prisma generate", "bun install > out", "bun install 2>&1 | tail -30", "bun install; rm x",
                  "bun install && bun test", "bun add zod | sh", "bun add zod | tee x", "bun add $(cat x)", "bun add `x`", "bun add ./local", "bun add ../outside", "bun add https://x.test/a.tgz",
                  "bun add github:a/b", "bun add zod --registry https://x.test", "bun add -g zod", "bun add --cwd .. zod", "bun install --cwd ..", "cd .. && bun install", "cd /tmp && bun install",
                  "export PATH=$HOME/.bun/bin:$PATH && bun install", "~/.bun/bin/bun install", "npm install", "npm view zod", "pnpm add zod", "yarn add zod", "cat bun.lock | head -5 > x"):
            self.assertIsNotNone(rg.refusal("Bash", {"command": c}, g), c)

    def test_an_install_is_allowed_only_for_the_manager_of_the_lockfile_and_for_a_task_that_lists_a_package_file(self):
        rg = fc.fc_readguard
        self.assertEqual(self.installing(["src/a.py"])["installers"], [])  # no package file in files:
        self.assertEqual(rg.installers(self.wt, ["package.json"]), ["npm"])  # a package.json and no lockfile anywhere
        self.assertEqual(rg.installers(self.wt, ["web/package.json", "src/a.py"]), ["npm"])
        (self.wt / "web").mkdir(); (self.wt / "web" / "pnpm-lock.yaml").write_text("")
        self.assertEqual(rg.installers(self.wt, ["web/package.json"]), ["pnpm"])  # the lockfile beside the package.json decides
        self.assertEqual(rg.installers(self.wt, ["yarn.lock"]), ["yarn"])
        g = self.installing(["package.json", "package-lock.json"])
        self.assertIsNone(rg.refusal("Bash", {"command": "npm install left-pad"}, g))
        self.assertIsNotNone(rg.refusal("Bash", {"command": "bun install"}, g))
        self.assertIsNotNone(rg.refusal("Bash", {"command": "bun install"}, self.g))  # the plain guard of setUp: no installers

    def test_a_python_task_that_lists_its_package_file_may_lock_sync_and_add_with_uv_and_nothing_else(self):
        # a task with `files: pyproject.toml, uv.lock` was told "Regenerate uv.lock with uv lock" and the guard blocked every uv command
        rg = fc.fc_readguard
        g = self.installing(["pyproject.toml", "uv.lock"])
        self.assertEqual(g["installers"], ["uv"])
        for c in ("uv lock", "uv sync", "uv add httpx", "uv add 'httpx[http2]>=0.27,<1'", 'uv add "pyjwt[crypto]==2.9.0" fastapi', "uv add --dev pytest", "uv add httpx~=0.27.0",
                  "cd . && uv lock | tail -5", f"cd {self.wt} && uv sync", "uv add uvicorn | head -n 20"):
            self.assertIsNone(rg.refusal("Bash", {"command": c}, g), c)
        for c in ("uv", "uv add", "uv run pytest", "uv pip install httpx", "uv lock --upgrade", "uv sync --frozen", "uv lock httpx", "uv add ./local", "uv add ../outside", "uv add git+https://x.test/a.git",
                  "uv add https://x.test/a.whl", "uv add httpx --index https://x.test", "uv add -r requirements.txt", "uv add --script x.py httpx", "uv add httpx>=1", "uv lock > out", "uv lock; rm x",
                  "uv lock && uv run pytest", "uv add $(cat x)", "uv tool install ruff", "uv python install 3.12", "uv self update", "uv init", "uv venv", "uv build", "uv install", "uvx ruff check .",
                  "cd .. && uv lock", "~/.local/bin/uv lock", "pip install httpx", "poetry lock", "bun install", "npm install"):
            self.assertIsNotNone(rg.refusal("Bash", {"command": c}, g), c)
        self.assertIn("`uv lock`, `uv sync` and `uv add <package>`", rg.refusal("Bash", {"command": "uv run pytest"}, g))  # the refusal names the forms that do work
        self.assertIn("`uv lock`, `uv sync` and `uv add <package>`", rg.refusal("Read", {"file_path": "src/c.py"}, g))

    def test_a_lockfile_gives_a_task_only_the_manager_of_its_own_package_file(self):
        rg = fc.fc_readguard
        self.assertEqual(rg.installers(self.wt, ["pyproject.toml"]), [])  # no uv.lock anywhere: poetry or pip, and neither is allowed
        self.assertEqual(rg.installers(self.wt, ["uv.lock"]), ["uv"])
        (self.wt / "uv.lock").write_text("")
        self.assertEqual(rg.installers(self.wt, ["pyproject.toml"]), ["uv"])
        self.assertEqual(rg.installers(self.wt, ["package.json"]), ["npm"])  # a uv.lock at the root never hands a JavaScript task uv
        (self.wt / "bun.lock").write_text("")
        self.assertEqual(rg.installers(self.wt, ["pyproject.toml"]), ["uv"])  # nor a bun.lock a Python task bun
        self.assertEqual(rg.installers(self.wt, ["package.json", "pyproject.toml"]), ["bun", "uv"])
        g = self.installing(["package.json", "pyproject.toml"])
        for c in ("bun add zod", "uv add httpx", "uv lock", "bun install"):
            self.assertIsNone(rg.refusal("Bash", {"command": c}, g), c)
        for c in ("bun lock", "uv install", "bun add 'httpx[http2]'", "uv add @types/node"):  # each manager keeps its own commands and package names
            self.assertIsNotNone(rg.refusal("Bash", {"command": c}, g), c)
        self.assertIn("`bun install` and `bun add <package>[@version]`, `uv lock`, `uv sync` and `uv add <package>`", rg.refusal("Bash", {"command": "uv run pytest"}, g))
        self.assertIn("names no package file or lockfile", rg.refusal("Bash", {"command": "uv lock"}, self.g))  # the plain guard of setUp: no installers

    def test_a_blocked_read_names_the_files_it_may_read_and_what_to_do_about_the_rest(self):
        # #451: the generic text never said which files the model may read, so it asked for the wrong thing or gave up
        reply = fc.fc_readguard.refusal("Read", {"file_path": "src/c.py"}, self.g)
        for name in ("src/a.py", "tests/t.py", "lib/deep/x.py"):
            self.assertIn(name, reply)
        self.assertIn("CONTRADICTION: brief lacks", reply)
        many = dict(self.g, allow=[str(self.wt / f"f{i}.py") for i in range(15)])
        self.assertIn("and 3 more", fc.fc_readguard.refusal("Read", {"file_path": "x.py"}, many))

    def test_a_refused_package_manager_command_says_so_instead_of_the_read_only_text(self):
        rg = fc.fc_readguard
        none = rg.refusal("Bash", {"command": "bun install"}, self.g)
        self.assertIn("may not install", none); self.assertIn("CONTRADICTION", none)
        g = self.installing(["package.json", "bun.lock"])
        for c in ("bun --version", "~/.bun/bin/bun install", "cd app && bun install 2>&1 | tail -30", "bun add zod --registry x"):
            self.assertIn("`bun install` and `bun add <package>", rg.refusal("Bash", {"command": c}, g), c)
        self.assertIn("context pack", rg.refusal("Bash", {"command": "cat src/c.py"}, g))  # any other command keeps the read-only text
        self.assertIn("`bun install` and `bun add <package>[@version]`", rg.refusal("Bash", {"command": "cat src/c.py"}, g))  # and says what else is allowed
        self.assertNotIn("bun add", rg.refusal("Bash", {"command": "cat src/c.py"}, self.g))

    def test_the_script_answers_both_event_shapes_and_logs_each_refusal(self):
        rg, me = fc.fc_readguard, os.path.realpath(fc.fc_readguard.__file__)
        self.assertEqual(rg.command(str(self.path)), f"python3 -I {shlex.quote(me)} {shlex.quote(str(self.path))}")
        self.assertEqual(rg.refused(self.wt), 0)  # refusal() alone never writes the log
        run = lambda ev: subprocess.run(rg.command(str(self.path)), shell=True, input=json.dumps(ev), capture_output=True, text=True, cwd=self.top, check=True).stdout
        claude, pi = {"tool_name": "Read", "tool_input": {"file_path": "src/c.py"}}, {"tool": "bash", "args": {"command": "cat src/c.py"}}
        reply = lambda t, a: rg.refusal(t, a, self.g)
        self.assertEqual(json.loads(run(claude)), {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                    "permissionDecisionReason": reply("Read", claude["tool_input"])}})
        self.assertEqual(run(pi).strip(), reply("bash", pi["args"]))
        self.assertEqual(run({"tool_name": "Read", "tool_input": {"file_path": "src/a.py"}}) + run({"tool": "grep", "args": {"path": "src"}}), "")
        self.assertEqual(rg.refused(self.wt), 2)
        self.assertEqual([json.loads(l) for l in Path(self.g["log"]).read_text().splitlines()], [{"tool": "Read", "args": claude["tool_input"]}, {"tool": "bash", "args": pi["args"]}])


    def test_the_script_lets_a_claude_hook_grep_the_test_and_still_denies_a_write(self):
        rg = fc.fc_readguard
        run = lambda c: subprocess.run(rg.command(str(self.path)), shell=True, input=json.dumps({"tool_name": "Bash", "tool_input": {"command": c}}),
                                       capture_output=True, text=True, cwd=self.wt, check=True).stdout
        self.assertEqual(run('grep -n "x" -A 2 tests/t.py | head -5'), "")
        self.assertEqual(rg.refused(self.wt), 0)
        self.assertEqual(json.loads(run("sed -i s/x/y/ tests/t.py"))["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(rg.refused(self.wt), 1)

    def test_the_script_exits_2_with_an_error_on_any_failure(self):
        bad = self.top / "bad.guard.json"; bad.write_text("{not json")
        ev = json.dumps({"tool": "read", "args": {"path": "src/c.py"}})
        for guard, stdin in ((self.top / "missing.guard.json", ev), (bad, ev), (self.path, "not an event")):
            r = subprocess.run(fc.fc_readguard.command(str(guard)), shell=True, input=stdin, capture_output=True, text=True, cwd=self.top)
            self.assertEqual(r.returncode, 2, (guard, stdin, r.stdout)); self.assertTrue(r.stderr.strip(), (guard, stdin))

if __name__ == "__main__":
    unittest.main()
