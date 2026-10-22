import hermetic  # first: no test sees the real home
import contextlib, hashlib, importlib.machinery, importlib.util, io, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import fc_shrink


class Shrink(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.src = self.d / "shot.png"
        self.src.write_bytes(b"png bytes")
        self.copy = self.d / "out" / f"shot-{hashlib.sha256(b'png bytes').hexdigest()[:8]}.jpg"

    def _shrink(self, w, h, out="out", rc=0, write=True):
        def fake_run(argv, *a, **kw):
            if "-g" in argv:  # size of the source (1000x667 for the copy)
                pw, ph = (w, h) if argv[-1] == str(self.src) else (1000, 667)
                return subprocess.CompletedProcess(argv, 0, stdout=f"  pixelWidth: {pw}\n  pixelHeight: {ph}\n", stderr="")
            if write:
                Path(argv[argv.index("--out") + 1]).write_bytes(b"jpg")
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")
        with mock.patch("shutil.which", return_value="/usr/bin/sips"), mock.patch("subprocess.run", side_effect=fake_run) as run:
            got = fc_shrink.shrink(self.src, None if out is None else self.d / out)
        return got, run

    def _convert(self, run):
        return next(c.args[0] for c in run.call_args_list if "--out" in c.args[0])

    def test_a_large_image_is_converted_with_Z(self):
        got, run = self._shrink(2400, 1600)
        conv = self._convert(run)
        self.assertEqual(conv[conv.index("-Z") + 1], "1000")
        self.assertEqual(conv[conv.index("format") + 1], "jpeg")
        self.assertEqual(conv[conv.index("formatOptions") + 1], "72")
        self.assertEqual(conv[conv.index("--out") + 1], str(self.copy))
        self.assertEqual(got, fc_shrink.Shrunk(self.copy, fc_shrink.Size(2400, 1600, 9), fc_shrink.Size(1000, 667, 3), ""))

    def test_only_a_long_edge_over_max_edge_gets_Z(self):
        for w, h, big in [(400, 300, False), (1000, 500, False), (500, 1001, True)]:
            with self.subTest(w=w, h=h):
                _, run = self._shrink(w, h)
                self.assertEqual("-Z" in self._convert(run), big)

    def test_the_default_dir_is_compose_shrink_in_the_temp_dir(self):
        with mock.patch("tempfile.gettempdir", return_value=str(self.d / "tmp")):
            got, _ = self._shrink(2400, 1600, out=None)
        self.assertEqual(got.path, self.d / "tmp" / "compose-shrink" / self.copy.name)

    def test_without_sips_the_source_is_passed_through(self):
        with mock.patch("shutil.which", return_value=None):
            got = fc_shrink.shrink(self.src, self.d / "out")
        self.assertEqual(got.path, self.src)
        self.assertEqual(got.after, got.before)
        self.assertEqual((got.after.width, got.after.height), (0, 0))
        self.assertEqual(got.warning, f"compose shrink: sips not found, using the original {self.src}")

    def test_a_failed_or_empty_sips_run_passes_the_source_through(self):
        for kw in ({"rc": 13}, {"write": False}):
            with self.subTest(**kw):
                got, _ = self._shrink(2400, 1600, **kw)
                self.assertEqual(got.path, self.src)
                self.assertEqual(got.warning, f"compose shrink: sips could not shrink {self.src}, using the original")

    def test_the_source_is_never_changed_and_nothing_is_printed(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._shrink(2400, 1600)
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(self.src.read_bytes(), b"png bytes")


class Command(unittest.TestCase):
    def _run(self, images, returns, out=None, max_edge=1000, quality=72):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(fc_shrink, "shrink", side_effect=returns) as sh, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = fc_shrink.cmd_shrink(SimpleNamespace(images=images, out=out, max_edge=max_edge, quality=quality))
        return result, stdout.getvalue(), stderr.getvalue(), sh

    def test_each_shrunk_image_prints_one_line_in_argument_order(self):
        first = fc_shrink.Shrunk(Path("/tmp/compose-shrink/shot-1a2b3c4d.jpg"), fc_shrink.Size(2400, 1600, 812345), fc_shrink.Size(1000, 667, 98765), "")
        second = fc_shrink.Shrunk(Path("/tmp/compose-shrink/side-5e6f7a8b.jpg"), fc_shrink.Size(3000, 2000, 900000), fc_shrink.Size(1000, 667, 120000), "")
        _, out, err, sh = self._run(["a.png", "b.png"], [first, second])
        self.assertEqual(out, "/tmp/compose-shrink/shot-1a2b3c4d.jpg  2400x1600 812345 B -> 1000x667 98765 B\n"
                              "/tmp/compose-shrink/side-5e6f7a8b.jpg  3000x2000 900000 B -> 1000x667 120000 B\n")
        self.assertEqual(err, "")
        self.assertEqual(sh.call_args_list, [mock.call(Path("a.png"), None, 1000, 72), mock.call(Path("b.png"), None, 1000, 72)])

    def test_a_pass_through_prints_unchanged_and_its_warning_on_stderr(self):
        warning = "compose shrink: sips not found, using the original a.png"
        passed = fc_shrink.Shrunk(Path("a.png"), fc_shrink.Size(0, 0, 5), fc_shrink.Size(0, 0, 5), warning)
        shrunk = fc_shrink.Shrunk(Path("/tmp/compose-shrink/b-5e6f7a8b.jpg"), fc_shrink.Size(3000, 2000, 900000), fc_shrink.Size(1000, 667, 120000), "")
        _, out, err, _ = self._run(["a.png", "b.png"], [passed, shrunk])
        self.assertEqual(out, "a.png  unchanged\n/tmp/compose-shrink/b-5e6f7a8b.jpg  3000x2000 900000 B -> 1000x667 120000 B\n")
        self.assertEqual(err, warning + "\n")

    def test_the_flags_reach_shrink_for_every_image(self):
        shrunk = fc_shrink.Shrunk(Path("/tmp/x.jpg"), fc_shrink.Size(1, 1, 1), fc_shrink.Size(1, 1, 1), "")
        _, _, _, sh = self._run(["a.png", "b.png"], [shrunk, shrunk], out="d", max_edge=500, quality=60)
        self.assertEqual(sh.call_args_list, [mock.call(Path("a.png"), Path("d"), 500, 60), mock.call(Path("b.png"), Path("d"), 500, 60)])

    def test_the_command_returns_normally_when_every_image_passed_through(self):
        warning = "compose shrink: sips could not shrink a.png, using the original"
        passed = fc_shrink.Shrunk(Path("a.png"), fc_shrink.Size(0, 0, 5), fc_shrink.Size(0, 0, 5), warning)
        result, out, err, _ = self._run(["a.png"], [passed])
        self.assertIsNone(result)
        self.assertEqual(out, "a.png  unchanged\n")
        self.assertEqual(err, warning + "\n")


class Args(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bin_dir = Path(__file__).resolve().parents[1] / "bin"
        spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(bin_dir / "compose.py")))
        cls.fc = importlib.util.module_from_spec(spec)
        for m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]:
            del sys.modules[m]
        spec.loader.exec_module(cls.fc)

    def _args(self, *argv):
        with mock.patch.object(sys, "argv", ["compose", "shrink", *argv]), mock.patch.object(self.fc.fc_shrink, "cmd_shrink") as cmd:
            self.fc.main()
        return cmd.call_args.args[0]

    def test_the_defaults_reach_cmd_shrink(self):
        a = self._args("a.png", "b.png")
        self.assertEqual((a.images, a.out, a.max_edge, a.quality), (["a.png", "b.png"], None, 1000, 72))

    def test_each_flag_reaches_cmd_shrink(self):
        a = self._args("--out", "d", "--max-edge", "500", "--quality", "60", "a.png")
        self.assertEqual((a.images, a.out, a.max_edge, a.quality), (["a.png"], "d", 500, 60))

    def test_a_flag_before_the_images_still_takes_them(self):
        self.assertEqual(self._args("--quality", "60", "a.png").images, ["a.png"])

    def test_a_non_integer_size_or_quality_is_a_usage_error(self):
        self.assertEqual(self._args("--max-edge", "500", "--quality", "60", "a.png").max_edge, 500)
        for argv in (["--max-edge", "big", "a.png"], ["--quality", "high", "a.png"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                self._args(*argv)
            self.assertEqual(cm.exception.code, 2)

    def test_no_image_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            self._args()
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
