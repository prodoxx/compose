import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class PythonApi(unittest.TestCase):
    """Task 010 (NEB-929): python_api and section for Python imports."""

    WORDS = "Goal: spin the gadget.\ng = Gadget(); g.spin(3)  # a stopwatch, _hidden"
    FILES = ["app/main.py", "app/other.py", "app/new.py"]

    @classmethod
    def setUpClass(cls):
        root = cls.root = Path(tempfile.mkdtemp()).resolve()
        wt = cls.wt = root / "wt"
        cls.addClassCleanup(shutil.rmtree, root, True)
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(wt / ".venv")], check=True)
        cls.py = str(wt / ".venv" / "bin" / "python")
        purelib = "import sysconfig; print(sysconfig.get_paths()['purelib'])"
        site = Path(subprocess.run([cls.py, "-c", purelib], check=True, capture_output=True, text=True).stdout.strip())
        for dist, ver, mod in (("tool-kit", "1.2.3", "toolkit"), ("myrepo", "0.1", "localmod")):  # myrepo: the repo, installed editable
            info = site / f"{dist.replace('-', '_')}-{ver}.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {ver}\n")
            (info / "top_level.txt").write_text(mod + "\n")
        (site / "myrepo.pth").write_text(str(wt) + "\n")
        (site / "toolkit").mkdir()
        (site / "toolkit" / "__init__.py").write_text(
            'def shout(text, times=2):\n    """Say it loud.\n\n    More."""\n\n\nclass Gadget:\n    """A gadget."""\n\n'
            '    def spin(self, speed):\n        """Spin it."""\n\n    def stop(self):\n        """Stop it."""\n\n'
            '    def _hidden(self):\n        """Hide it."""\n')
        files = {"wt/localmod.py": "def thing(x):\n    'Repo code.'\n", "wt/app/other.py": "from toolkit import shout\n",
                 "wt/app/main.py": "import json, os\nfrom toolkit import shout, Gadget, missing\nimport absent_pkg\nfrom localmod import thing\nfrom . import rel\n",
                 "wt/broken/main.py": "from toolkit import shout\n", "wt/broken/.venv/bin/python": "#!/bin/sh\nexit 3\n",
                 "bare/app/main.py": "from toolkit import Gadget\n", "bare/app/other.py": "from toolkit import shout\n",
                 "bin/python3": f'#!/bin/sh\necho run >> {root}/runs\nexec {cls.py} "$@"\n'}
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
            (root / rel).chmod(0o755)

    def test_a_block_per_distribution_with_its_version_names_and_the_methods_words_name(self):
        blocks = fc.fc_libapi.python_api(self.wt, self.FILES, self.WORDS)
        self.assertEqual(len(blocks), 1, blocks)
        lines = blocks[0].split("\n")
        self.assertEqual((lines[0], len(lines)), ("tool-kit 1.2.3", 4), blocks)
        shout, gadget, spin = ([i for i, l in enumerate(lines) if all(w in l for w in ws)] for ws in
                               (("shout(text, times=2)", "Say it loud."), ("Gadget", "A gadget."), ("spin(", "speed", "Spin it.")))
        self.assertTrue(len(shout) == len(gadget) == len(spin) == 1 and spin[0] > max(shout[0], gadget[0]), blocks)
        self.assertNotIn("More.", blocks[0])

    def test_python3_runs_once_without_a_venv_and_a_failing_interpreter_gives_nothing(self):
        with mock.patch.dict(os.environ, {"PATH": f"{self.root / 'bin'}:{os.environ['PATH']}"}):
            blocks = fc.fc_libapi.python_api(self.root / "bare", self.FILES, self.WORDS)
        self.assertEqual([b.split("\n")[0] for b in blocks], ["tool-kit 1.2.3"])
        self.assertEqual((self.root / "runs").read_text(), "run\n")
        self.assertEqual(fc.fc_libapi.python_api(self.wt, ["broken/main.py"], self.WORDS), [])
        self.assertEqual(fc.fc_libapi.section(self.wt, ["broken/main.py", "app/new.py"], self.WORDS), "")

    def test_section_has_a_header_fits_the_cap_and_ends_on_a_whole_line(self):
        full = fc.fc_libapi.section(self.wt, self.FILES, self.WORDS, cap=10 ** 6)
        self.assertTrue(full.startswith("LIBRARY API") and fc.fc_libapi.python_api(self.wt, self.FILES, self.WORDS)[0] in full, full)
        self.assertEqual(fc.fc_libapi.section(self.wt, self.FILES, self.WORDS), full)
        lines = full.rstrip("\n").split("\n")
        cap = len("\n".join(lines[:-1])) + 3
        cut = fc.fc_libapi.section(self.wt, self.FILES, self.WORDS, cap=cap)
        self.assertEqual((cut.rstrip("\n"), len(cut) <= cap), ("\n".join(lines[:-1]), True), cut)


class JsApi(unittest.TestCase):
    """Task 011 (NEB-931): js_api and section for JS/TS imports."""

    WORDS = "Goal: spin the widget faster."
    FILES = ["src/app.ts", "web/src/util.mjs", "web/old.cjs", "src/side.jsx", "notes.md", "src/new.tsx", "src/tool.py"]
    WIDGET = ["export default function create(): Widget;", "export declare function makeWidget(size: number): Widget;",
              "export declare class Widget {"]
    CHUNK = "export declare function chunk(a: any[], n: number): any[][];"

    def setUp(self):
        wt = self.wt = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, wt, True)
        pkg = lambda name, ver, **kv: json.dumps({"name": name, "version": ver, **kv})
        files = {"src/app.ts": 'import create, { makeWidget, Widget } from "@acme/widgets/extra";\nimport { here } from "./local";\n'
                               'import fs from "node:fs";\nimport gone from "missing-pkg";\nimport { bare } from "nodecl";\n',
                 "web/src/util.mjs": 'import { chunk } from "lodashy/fp";\n', "web/old.cjs": 'const r = require("reqpkg");\n',
                 "src/side.jsx": 'import "sidefx/register";\n', "notes.md": 'import { other } from "other";\n',
                 "src/tool.py": "import toolkit\n", "node_modules/other/package.json": pkg("other", "1.0.0", types="o.d.ts"),
                 "node_modules/other/o.d.ts": "export declare const other: number;\n",
                 "node_modules/@acme/widgets/package.json": pkg("@acme/widgets", "2.0.1", types="dist/index.d.ts", main="lib/index.js"),
                 "node_modules/@acme/widgets/dist/index.d.ts": "\n".join(self.WIDGET[1:] + [
                     "    spin(speed: number): void;", "    stop(): void;", "}", self.WIDGET[0], "export declare const unused: number;\n"]),
                 "node_modules/@acme/widgets/lib/index.d.ts": "export declare function makeWidget(): never;\n",  # types wins over main
                 "node_modules/lodashy/package.json": pkg("lodashy", "4.1.0", main="lib/index.js"),  # web/node_modules is nearer
                 "web/node_modules/lodashy/package.json": pkg("lodashy", "5.0.0", main="lib/index.js"),
                 "web/node_modules/lodashy/lib/index.d.ts": self.CHUNK + "\n",
                 "node_modules/reqpkg/package.json": pkg("reqpkg", "0.3.0", typings="types/r.d.ts"),
                 "node_modules/reqpkg/types/r.d.ts": "export declare const r: number;\n",
                 "node_modules/sidefx/package.json": pkg("sidefx", "1.1.0", types="index.d.ts"),
                 "node_modules/sidefx/index.d.ts": "export {};\n",
                 "node_modules/nodecl/package.json": pkg("nodecl", "1.0.0", main="index.js"),
                 "node_modules/nodecl/index.js": "exports.bare = 1;\n"}
        for rel, text in files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(text)

    def test_a_block_per_imported_package_with_its_version_export_lines_and_the_methods_words_name(self):
        blocks = fc.fc_libapi.js_api(self.wt, self.FILES, self.WORDS)
        self.assertEqual(sorted(b.split("\n")[0] for b in blocks), ["@acme/widgets 2.0.1", "lodashy 5.0.0", "reqpkg 0.3.0", "sidefx 1.1.0"])
        by = {b.split("\n")[0]: b.split("\n")[1:] for b in blocks}
        w = by["@acme/widgets 2.0.1"]
        self.assertEqual((sorted(w[:3]), [l.strip() for l in w[3:]]), (sorted(self.WIDGET), ["spin(speed: number): void;"]), w)
        self.assertEqual(by["lodashy 5.0.0"], [self.CHUNK])

    def test_section_puts_the_js_blocks_after_the_python_ones_within_the_cap(self):
        with mock.patch.object(fc.fc_libapi, "python_api", return_value=["py-kit 1.0\ndef f(x)"]):
            full = fc.fc_libapi.section(self.wt, self.FILES, self.WORDS, cap=10 ** 6)
            lines = full.rstrip("\n").split("\n")
            cap = len("\n".join(lines[:-1])) + 3
            cut = fc.fc_libapi.section(self.wt, self.FILES, self.WORDS, cap=cap)
        self.assertTrue(full.startswith("LIBRARY API") and 0 < full.index("def f(x)") < full.index("@acme/widgets 2.0.1"), full)
        self.assertTrue(full.index("def f(x)") < min(full.index(h) for h in ("lodashy 5.0.0", "reqpkg 0.3.0", "sidefx 1.1.0")), full)
        self.assertEqual((cut.rstrip("\n"), len(cut) <= cap), ("\n".join(lines[:-1]), True), cut)


class TopLevelFromRecord(unittest.TestCase):
    """Task 017: a distribution without top_level.txt maps to its modules through its file list (RECORD)."""

    @classmethod
    def setUpClass(cls):
        wt = cls.wt = Path(tempfile.mkdtemp()).resolve()
        cls.addClassCleanup(shutil.rmtree, wt, True)
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(wt / ".venv")], check=True)
        purelib = "import sysconfig; print(sysconfig.get_paths()['purelib'])"
        site = Path(subprocess.run([str(wt / ".venv" / "bin" / "python"), "-c", purelib], check=True, capture_output=True,
                                   text=True).stdout.strip())
        dists = (("rich-widgets", "0.4.1", None,
                  ["richw/__init__.py", "solo.py", "rich_widgets-0.4.1.dist-info/METADATA", "../../../bin/rw"]),
                 ("tagged", "2.0", "alpha", ["alpha/__init__.py", "beta.py"]),  # top_level.txt wins over RECORD
                 ("mixed", "1.0", None, ["nested/inner/core.py", "mixed-1.0.dist-info/METADATA", "../../../bin/mx"]),
                 ("nopy", "1.0", None, ["extra/data.json", "__pycache__/zed.cpython-39.pyc", "nopy-1.0.data/scripts/x.py"]),
                 ("bare", "3.0", None, None))  # neither top_level.txt nor RECORD
        for dist, ver, top, record in dists:
            info = site / f"{dist.replace('-', '_')}-{ver}.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {ver}\n")
            if top:
                (info / "top_level.txt").write_text(top + "\n")
            if record is not None:
                (info / "RECORD").write_text("".join(f"{p},,\n" for p in record))
        files = {site / "richw" / "__init__.py": 'def paint(color, width=1):\n    """Paint it."""\n',
                 site / "solo.py": 'def ping():\n    """Ping once."""\n',
                 site / "alpha" / "__init__.py": 'def a():\n    """A."""\n', site / "beta.py": 'def b():\n    """B."""\n',
                 site / "nested" / "inner" / "core.py": 'def go():\n    """Go."""\n', site / "extra" / "data.json": "{}\n",
                 site / "bare.py": 'def x():\n    """X."""\n',
                 wt / "app" / "main.py": "from richw import paint\nfrom solo import ping\n",
                 wt / "app" / "other.py": "from alpha import a\nfrom beta import b\nfrom nested.inner.core import go\n"
                                          "from extra import data\nfrom bare import x\n"}
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_a_distribution_without_top_level_maps_the_py_files_its_record_lists(self):
        self.assertEqual(fc.fc_libapi.python_api(self.wt, ["app/main.py"], ""),
                         ["rich-widgets 0.4.1\npaint(color, width=1) - Paint it.\nping() - Ping once."])

if __name__ == "__main__":
    unittest.main()
