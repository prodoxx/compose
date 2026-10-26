import hermetic  # first: no test sees the real home
import ast
import hashlib
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"

# The registry lives in tests/modules: rank.txt lists the modules lowest first (each imports only those above it), and
# <module>.txt lists every name the module defines, in order, one per line, so parallel branches touch different lines.
REGISTRY = Path(__file__).resolve().parent / "modules"


def listed(name):
    return tuple(line for line in (REGISTRY / f"{name}.txt").read_text().splitlines() if line)


RANK = listed("rank")
MODULES = {m: listed(m) for m in RANK}
HELPERS = {"fc_cost", "fc_mcp", "fc_sessions"}
SETDEFAULTS = ["os.environ.setdefault('GRAFT_NO_GITIGNORE', '1')", "os.environ.setdefault('GRAFT_NO_IGNORE', '1')"]
CONFIG_ONLY = ("DEFAULT_MODEL", "FALLBACK_MODEL", "FALLBACK_AFTER", "TEST_READER", "OPENROUTER_KEY", "LINEAR_KEY", "WORKSPACE", "CREW_MODELS", "CREW_EFFORT", "CREW_FALLBACK")
LAUNCHER_SHA = "471a44a7ee5817b057bddaf43972cd238de82763ea64f7d7443808aec3a6b716"
SUBCOMMANDS = list(listed("subcommands"))


def parse(name):
    return ast.parse((BIN / name).read_text())


def defined(tree):
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(n.name)
        elif isinstance(n, ast.Assign):
            out += [x.id for t in n.targets for x in ast.walk(t) if isinstance(x, ast.Name)]
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.append(n.target.id)
    return out


def fc_imports(tree):
    """Every fc_* module named by an import anywhere in the file (always `import fc_x`, never `from fc_x import`)."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {a.name for a in n.names if a.name.startswith("fc_")}
        elif isinstance(n, ast.ImportFrom) and (n.module or "").startswith("fc_"):
            names.add(n.module)
    return names


def expressions(tree):
    return [ast.unparse(n) for n in tree.body if isinstance(n, ast.Expr) and not isinstance(n.value, ast.Constant)]


def names(m):
    return [n for n in MODULES[m] if not n.startswith("EXPR_")]


def bad_refs(tree):
    """`fc_x.attr` uses whose fc_x is a ranked module but is not imported here or does not define attr."""
    imported = fc_imports(tree)
    return sorted(
        f"{n.value.id}.{n.attr}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in MODULES
        and (n.value.id not in imported or n.attr not in MODULES[n.value.id])
    )


class ModuleChecks:
    module = ""

    def tree(self):
        path = BIN / f"{self.module}.py"
        self.assertTrue(path.exists(), f"{path.name} does not exist")
        return parse(path.name)

    def test_defines_exactly_its_names_in_order(self):
        self.assertEqual(defined(self.tree()), names(self.module))

    def test_imports_only_lower_rank(self):
        tree = self.tree()
        allowed = set(RANK[: RANK.index(self.module)]) | HELPERS
        self.assertLessEqual(fc_imports(tree), allowed)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                self.assertFalse((n.module or "").startswith("fc_"), f"from {n.module} import ...")
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module]
                self.assertNotIn("compose", mods)

    def test_collaborators_resolve(self):
        self.assertEqual(bad_refs(self.tree()), [])

    def test_at_most_600_lines(self):
        self.tree()
        self.assertLessEqual(len((BIN / f"{self.module}.py").read_text().splitlines()), 600)

    def test_gone_from_compose(self):
        self.tree()
        entry = parse("compose.py")
        self.assertEqual(set(names(self.module)) & set(defined(entry)), set())
        self.assertEqual(bad_refs(entry), [])


class FcPrd(ModuleChecks, unittest.TestCase):
    module = "fc_prd"


class FcDelivery(ModuleChecks, unittest.TestCase):
    module = "fc_delivery"


class FcConfig(ModuleChecks, unittest.TestCase):
    module = "fc_config"

    def test_setdefaults_moved(self):
        self.assertEqual(expressions(self.tree()), SETDEFAULTS)
        self.assertEqual(set(expressions(parse("compose.py"))) & set(SETDEFAULTS), set())


class FcFactorystate(ModuleChecks, unittest.TestCase):
    module = "fc_factorystate"


class FcFactoryturn(ModuleChecks, unittest.TestCase):
    module = "fc_factoryturn"


class FcNightstate(ModuleChecks, unittest.TestCase):
    module = "fc_nightstate"


class FcAnswer(ModuleChecks, unittest.TestCase):
    module = "fc_answer"


class FcNightprep(ModuleChecks, unittest.TestCase):
    module = "fc_nightprep"


class FcDigest(ModuleChecks, unittest.TestCase):
    module = "fc_digest"


class FcNight(ModuleChecks, unittest.TestCase):
    module = "fc_night"


class FcFactory(ModuleChecks, unittest.TestCase):
    module = "fc_factory"


class FcInit(ModuleChecks, unittest.TestCase):
    module = "fc_init"

    def test_cmd_trail_sets_trail_up_in_the_project_worktree(self):
        sys.path.insert(0, str(BIN))
        import fc_core, fc_herdr, fc_init

        a = object()
        with mock.patch.object(fc_core, "project_dir", return_value=Path("/p")) as pd, \
             mock.patch.object(fc_herdr, "project_worktree", return_value=Path("/wt")) as pw, \
             mock.patch.object(fc_init, "setup_trail") as st:
            fc_init.cmd_trail(a)
        pd.assert_called_once_with(a)
        pw.assert_called_once_with(Path("/p"))
        st.assert_called_once_with(Path("/wt"))


class Residue(unittest.TestCase):
    def test_compose_keeps_only_the_entry_point(self):
        tree = parse("compose.py")
        self.assertEqual(set(defined(tree)), {"DETACHED", "detach", "follow", "main"})
        self.assertEqual(expressions(tree), ["sys.path.insert(0, str(Path(__file__).resolve().parent))"])
        rest = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.Expr, ast.If))]
        self.assertEqual(rest, [])
        guards = [ast.unparse(n.test) for n in tree.body if isinstance(n, ast.If)]
        self.assertEqual(guards, ["__name__ == '__main__'"])

    def test_import_graph_is_acyclic_in_rank_order(self):
        for i, m in enumerate(RANK):
            with self.subTest(m):
                self.assertLessEqual(fc_imports(parse(f"{m}.py")), set(RANK[:i]) | HELPERS)

    def test_every_module_is_ranked_and_every_list_belongs_to_one(self):
        self.assertEqual(sorted(p.stem for p in BIN.glob("fc_*.py") if p.stem not in HELPERS), sorted(RANK))
        self.assertEqual(len(set(RANK)), len(RANK))
        self.assertEqual(sorted(p.stem for p in REGISTRY.glob("*.txt")), sorted([*RANK, "rank", "subcommands"]))

    def test_settings_have_one_owner(self):
        owners = {m: set(defined(parse(f"{m}.py"))) for m in RANK}
        owners["compose"] = set(defined(parse("compose.py")))
        for name, owner in [(n, "fc_config") for n in CONFIG_ONLY] + [("STACK_STOP", "fc_layers")]:
            with self.subTest(name):
                self.assertEqual([m for m, d in owners.items() if name in d], [owner])

    def test_launcher_unchanged(self):
        self.assertEqual(hashlib.sha256((BIN / "compose").read_bytes()).hexdigest(), LAUNCHER_SHA)

    def test_help_lists_the_same_subcommands(self):
        out = subprocess.run([sys.executable, str(BIN / "compose"), "--help"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(re.findall(r"^    ([a-z][\w-]*)(?: |$)", out.split("positional arguments:")[1], re.M), SUBCOMMANDS)

    def test_help_names_trail_not_graft(self):
        env = {**os.environ, "COLUMNS": "400"}
        out = subprocess.run([sys.executable, str(BIN / "compose"), "--help"], capture_output=True, text=True, check=True, env=env).stdout
        lines = [" ".join(line.split()) for line in out.splitlines()]
        self.assertIn("trail set trail up in the project's worktree again: wire it for Claude Code and build the graph", lines)
        self.assertIn("explore print the explore teammate's brief (a fast trail-only map of an existing repo); --extra adds notes", lines)
        self.assertEqual(re.findall(r"(?<![\w/.@-])graft(?![\w/-])", out), [])
        self.assertIn("graft/", out)

    def test_old_graft_subcommand_is_rejected(self):
        env = {**os.environ, "COMPOSE_NO_GRAFT": "1", "COMPOSE_NO_TRAIL": "1"}
        p = subprocess.run([sys.executable, str(BIN / "compose"), "graft", "--project", "no-such-project"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("invalid choice: 'graft'", p.stderr)


class TestModules(unittest.TestCase):
    def test_every_test_module_clears_herdr_env(self):
        """Run from a herdr pane, a test that reaches herdr_space would open a real space; discover never imports tests/__init__.py."""
        for f in sorted((ROOT / "tests").glob("test_*.py")):
            with self.subTest(f.name):
                self.assertRegex(f.read_text(), r'(?m)^os\.environ\["HERDR_ENV"\] = ""$')


def module_class(m):
    """The module's own test class when it has extra tests, else a plain ModuleChecks case, named FcLinearCmd for fc_linear_cmd."""
    name = "".join(part.capitalize() for part in m.split("_"))
    if name not in globals():
        globals()[name] = type(name, (ModuleChecks, unittest.TestCase), {"module": m})
    return globals()[name]


MODULE_CLASSES = {m: module_class(m) for m in RANK}


def load_tests(loader, tests, pattern):
    if pattern is None:
        return tests
    suite = unittest.TestSuite()
    present = [m for m in RANK if (BIN / f"{m}.py").exists()]
    for m in present:
        suite.addTests(loader.loadTestsFromTestCase(MODULE_CLASSES[m]))
    if len(present) == len(RANK):
        suite.addTests(loader.loadTestsFromTestCase(Residue))
    suite.addTests(loader.loadTestsFromTestCase(TestModules))
    return suite
