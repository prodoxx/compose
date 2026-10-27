import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, stat, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import shutil, sys
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"  # these cover the plain-worktree path, not the treehouse pool
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)

ENV = {"HERDR_ENV": ""}

# A stand-in trail: logs every call, lists what init would write and writes it, and builds the map where trail 0.23 would.
FAKE_TRAIL = r"""#!/bin/bash
echo "$*" >> "$TRAIL_LOG"
d="${@: -1}"
case "$1 $2" in
  "--version "*) echo 9.9.9;;
  "init --no-agents")
    case "$*" in *--dry-run*)
      printf 'would write \342\200\224 this repo:\n  .claude/settings.json        trail SessionStart hook, shared\n  .trail/hook.cjs              hooks shim, shared\n  .claude/settings.local.json  trail statusline + allowlist, yours\n  .trail/statusline.cjs        statusline shim, git-ignored\n\nwould write \342\200\224 your machine, affects ALL repos:\n  ~/.claude/skills/trail/SKILL.md    trail skill\n\nnothing was written (--dry-run)\n' >&2
      exit 0;; esac
    mkdir -p "$d/.claude" "$d/.trail/learnings"; echo x > "$d/.trail/hook.cjs"; echo x > "$d/.trail/statusline.cjs"
    echo statusline.cjs > "$d/.trail/.gitignore"; echo '{}' > "$d/.claude/settings.json"; echo '{}' > "$d/.claude/settings.local.json";;
  "build --no-gitignore") g="$d/graft"
    if [ -d "$d/.trail/cache" ] || [ -d "$d/.trail/learnings" ] || [ -f "$d/.trail/hook.cjs" ]; then g="$d/.trail/cache"; fi
    mkdir -p "$g"; echo index > "$g/INDEX.md";;
  "ask "*) [ -f "$d/graft/INDEX.md" ] || [ -f "$d/.trail/cache/INDEX.md" ] || exit 1
    printf '[trail] tokens saved ≈ 7,434 (95%%) — this pack ≈ 384 tok vs reading the 1 source file(s) whole ≈ 7,818 tok.\n\n'
    printf 'trail ask — "%s"  (lexical)\n\n' "$2"
    if [ "$2" = main ]; then cat <<'OUT'
from Reggie's learning · Oct 17 · Use @trailhq/trail-code and call `trail`, not `graft`
  decided     graft's npm package was renamed to `@trailhq/trail-code`.
  .trail/learnings/2026-10-17-use-trailhq-trail-code.md

[trail] starting from this learning gets you to a PR 3x faster.

OUT
    else printf '· no learnings about this yet\n\n'; fi
    printf '1. main · function  [symbol]\n   server/main.go:L1-L1\n';;
esac
"""


def repo_with(files):
    """A plain folder holding `files` and the names of the ones a repository would track (all of them); git is never asked."""
    r = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (r / name).parent.mkdir(parents=True, exist_ok=True)
        (r / name).write_text(text)
    return r, set(files)


class FakeTrail(unittest.TestCase):
    def setUp(self):
        bindir = Path(tempfile.mkdtemp())
        (bindir / "trail").write_text(FAKE_TRAIL)
        (bindir / "trail").chmod(stat.S_IRWXU)
        self.log = bindir / "calls.log"
        env = {**ENV, "PATH": f"{bindir}:{os.environ['PATH']}", "TRAIL_LOG": str(self.log)}
        self.env = mock.patch.dict(os.environ, env); self.env.start(); self.addCleanup(self.env.stop)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def tracking(self, *repos):
        """git's answer to what each (folder, tracked names) pair tracks, for the two `ls-files` questions setup asks; the rest of `sh` is real. Returns the rules written to info/exclude."""
        tracked, real, excluded = {r: t for r, t in repos}, fc.fc_core.sh, []
        def sh(cmd, cwd, **kw):
            if cmd[:2] != ["git", "ls-files"]:
                return real(cmd, cwd, **kw)
            have = tracked.get(Path(cwd), set())
            return SimpleNamespace(returncode=0 if cmd[-1] in have else 1, stdout="\n".join(sorted(have)))
        for target, new in ((fc.fc_core, "sh"), (fc.fc_worktree, "exclude_paths")):
            p = mock.patch.object(target, new, sh if new == "sh" else lambda wt, rules: excluded.extend(rules)); p.start(); self.addCleanup(p.stop)
        return excluded


class TrailSetup(FakeTrail):
    """An existing repo gets trail wired and built before anyone explores it; trail's files never reach git, and graft never runs."""
    def setup(self, repo, which=shutil.which):
        graft = self.log.parent / "graft"  # a logging graft on PATH that must stay silent
        graft.write_text(f'#!/bin/sh\necho "$*" >> {graft}.log\n'); graft.chmod(stat.S_IRWXU)
        with mock.patch.object(shutil, "which", side_effect=which), mock.patch.object(fc.fc_core, "say") as say:
            fc.fc_init.setup_trail(repo)
        self.assertFalse(Path(f"{graft}.log").exists())
        return [c[0][0] for c in say.call_args_list]

    def test_existing_repo_is_wired_built_into_trail_cache_and_kept_out_of_git(self):
        repo, tracked = repo_with({"server/main.go": "package main\n", "README.md": "x"})
        excluded = self.tracking((repo, tracked))
        self.assertEqual(self.setup(repo), ["trail    wired for Claude Code (4 files, kept out of git)", "trail    graph built"])
        init, build = "init --no-agents --no-global --no-build", f"build --no-gitignore --no-ignore {repo}"
        self.assertEqual(self.calls(), [f"{init} --dry-run {repo}", f"{init} {repo}", build])
        self.assertTrue((repo / ".trail" / "cache" / "INDEX.md").exists() and not (repo / "graft").exists())
        self.assertLessEqual({"/.trail/", "/.claude/settings.json", "/.claude/settings.local.json", "/.trail/statusline.cjs"}, set(excluded))
        self.setup(repo)  # again: already wired, so only a rebuild
        self.assertEqual(self.calls()[3:], [build])

    def test_a_tracked_file_init_would_edit_means_build_only_still_in_trail_cache(self):
        repo, tracked = repo_with({"server/main.go": "package main\n", ".claude/settings.json": "{}"})
        self.tracking((repo, tracked))
        self.assertEqual(self.setup(repo)[0], "trail    init skipped: it would edit tracked .claude/settings.json; building the graph only")
        self.assertFalse(any(c.startswith("init") and "--dry-run" not in c for c in self.calls()))
        self.assertTrue((repo / ".trail" / "cache" / "INDEX.md").exists() and not (repo / "graft").exists())

    def test_tracked_wiring_gets_a_stamp_in_trail_cache_in_the_repo_and_a_clone(self):
        repo, tracked = repo_with({"server/main.go": "package main\n", ".trail/hook.cjs": "x"})
        clone = Path(tempfile.mkdtemp()) / "clone"; clone.mkdir()
        self.tracking((repo, tracked), (clone, tracked))
        stamp = Path(".trail") / "cache" / ".cache" / "wiring-stamp.json"
        self.setup(repo)
        self.assertEqual(json.loads((repo / stamp).read_text())["version"], "9.9.9")
        fc.fc_worktree.stamp_trail_wiring(clone)
        self.assertEqual(json.loads((clone / stamp).read_text())["version"], "9.9.9")
        (clone / stamp).write_text('{"version": "1.0.0"}')  # a stamp from an older trail is renewed
        fc.fc_worktree.stamp_trail_wiring(clone)
        self.assertEqual(json.loads((clone / stamp).read_text())["version"], "9.9.9")
        self.assertFalse((repo / "graft").exists())  # no stamp or map in graft/

    def test_untracked_wiring_or_no_trail_gets_no_stamp(self):
        untracked, none = repo_with({"main.go": "x"}); (untracked / ".trail").mkdir(); (untracked / ".trail" / "hook.cjs").write_text("x")
        tracked, files = repo_with({"main.go": "x", ".trail/hook.cjs": "x"})
        self.tracking((untracked, none), (tracked, files))
        fc.fc_worktree.stamp_trail_wiring(untracked)
        with mock.patch.object(shutil, "which", return_value=None):
            fc.fc_worktree.stamp_trail_wiring(tracked)
        self.assertEqual((self.calls(), list(untracked.glob(".trail/cache")), list(tracked.glob(".trail/cache"))), ([], [], []))

    def test_skips_say_why_and_start_nothing(self):
        code, docs = repo_with({"main.go": "package main\n"}), repo_with({"README.md": "x", "LICENSE": "x"})
        self.tracking((code[0], code[1]), (docs[0], docs[1]))
        with mock.patch.dict(os.environ, {"COMPOSE_NO_TRAIL": "1"}):
            self.assertEqual(self.setup(code[0]), ["trail    skipped: COMPOSE_NO_TRAIL is set"])
        self.assertEqual(self.setup(docs[0]), ["trail    skipped: a new repository has no code to map yet"])
        graft_only = lambda name: "/usr/local/bin/graft" if name == "graft" else None  # graft on PATH is no stand-in for trail
        self.assertEqual(self.setup(code[0], graft_only), ["trail    not installed: `npm i -g @trailhq/trail-code`, then `compose trail --project <slug>`"])
        self.assertEqual(self.calls(), [])

    def test_release_slot_removes_trail_whole(self):
        slot = Path(tempfile.mkdtemp()); (slot / ".trail" / "learnings").mkdir(parents=True); (slot / "graft").mkdir()
        with mock.patch.object(fc.fc_worktree, "treehouse") as th, mock.patch.object(fc.fc_core, "all_projects", return_value={}):
            fc.fc_worktree.release_slot(slot, slot)
        self.assertEqual((list(slot.iterdir()), th.call_args[0][1]), ([], "return"))


if __name__ == "__main__":
    unittest.main()
