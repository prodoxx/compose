import hermetic  # first: no test sees the real home
import contextlib, hashlib, io, os, subprocess, sys, tempfile, unittest
from pathlib import Path
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
    pass


class Args(unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
