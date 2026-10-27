import hermetic  # first: no test sees the real home
import ast
import os
import unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"

# tests/modules/rank.txt lists the modules lowest first: each imports only those above it.
RANK = tuple(l for l in (Path(__file__).resolve().parent / "modules" / "rank.txt").read_text().splitlines() if l)
HELPERS = {"fc_cost", "fc_mcp", "fc_sessions"}
CONFIG_ONLY = ("DEFAULT_MODEL", "FALLBACK_MODEL", "FALLBACK_AFTER", "TEST_READER", "OPENROUTER_KEY", "LINEAR_KEY", "WORKSPACE", "CREW_MODELS", "CREW_EFFORT", "CREW_FALLBACK")


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


TREES = {m: parse(f"{m}.py") for m in RANK if (BIN / f"{m}.py").exists()}
DEFINED = {m: set(defined(t)) for m, t in TREES.items()}


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


def bad_refs(tree):
    """`fc_x.attr` uses whose fc_x is a ranked module but is not imported here or does not define attr."""
    imported = fc_imports(tree)
    return sorted(
        f"{n.value.id}.{n.attr}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in DEFINED
        and (n.value.id not in imported or n.attr not in DEFINED[n.value.id])
    )


class Modules(unittest.TestCase):
    def test_every_module_is_ranked_once(self):
        self.assertEqual(sorted(p.stem for p in BIN.glob("fc_*.py") if p.stem not in HELPERS), sorted(RANK))
        self.assertEqual(len(set(RANK)), len(RANK))

    def test_each_imports_only_lower_rank_and_never_from_import(self):
        for i, m in enumerate(RANK):
            with self.subTest(m):
                tree = TREES[m]
                self.assertLessEqual(fc_imports(tree), set(RANK[:i]) | HELPERS)
                for n in ast.walk(tree):
                    if isinstance(n, ast.ImportFrom):
                        self.assertFalse((n.module or "").startswith("fc_"), f"from {n.module} import ...")
                    if isinstance(n, (ast.Import, ast.ImportFrom)):
                        self.assertNotIn("compose", [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module])

    def test_collaborators_resolve(self):
        for m in [*RANK, "compose"]:
            with self.subTest(m):
                self.assertEqual(bad_refs(TREES.get(m) or parse(f"{m}.py")), [])

    def test_each_is_at_most_600_lines(self):
        for m in RANK:
            with self.subTest(m):
                self.assertLessEqual(len((BIN / f"{m}.py").read_text().splitlines()), 600)

    def test_compose_keeps_only_the_entry_point(self):
        tree = parse("compose.py")
        self.assertEqual(set(defined(tree)), {"DETACHED", "detach", "follow", "main"})
        self.assertEqual(expressions(tree), ["sys.path.insert(0, str(Path(__file__).resolve().parent))"])
        rest = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.Expr, ast.If))]
        self.assertEqual(rest, [])
        self.assertEqual([ast.unparse(n.test) for n in tree.body if isinstance(n, ast.If)], ["__name__ == '__main__'"])

    def test_settings_have_one_owner(self):
        owners = {**DEFINED, "compose": set(defined(parse("compose.py")))}
        for name, owner in [(n, "fc_config") for n in CONFIG_ONLY] + [("STACK_STOP", "fc_layers")]:
            with self.subTest(name):
                self.assertEqual([m for m, d in owners.items() if name in d], [owner])


class TestModules(unittest.TestCase):
    def test_every_test_module_clears_herdr_env(self):
        """Run from a herdr pane, a test that reaches herdr_space would open a real space; discover never imports tests/__init__.py."""
        for f in sorted((ROOT / "tests").glob("test_*.py")):
            with self.subTest(f.name):
                self.assertRegex(f.read_text(), r'(?m)^os\.environ\["HERDR_ENV"\] = ""$')
