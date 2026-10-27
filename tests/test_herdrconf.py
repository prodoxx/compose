import hermetic  # first: no test sees the real home
import sys, tempfile, unittest
from pathlib import Path
import os
os.environ["HERDR_ENV"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
import fc_herdrconf, fc_init

SHIPPED = Path(__file__).resolve().parents[1] / "bin" / "herdr.toml"
LOUD = '# mine\n[keys]\nprefix = "ctrl+b"\n\n[ui.sound]\nenabled = true\n\n[theme]\nname = "x" # kept\n[ui.toast]\ndelivery = "toast"\n[ui.other]\nenabled = true\n'

class HerdrConfig(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "herdr" / "config.toml"  # parent missing until a case makes it

    def apply(self, text=None):
        if text is not None:
            self.path.parent.mkdir(exist_ok=True)
            self.path.write_text(text)
        return fc_herdrconf.apply_herdr_config(self.path, SHIPPED)

    def quiet(self, path):
        got = [fc_init.herdr_toml(path).get(k, d) for k, d in (("ui.toast.delivery", "off"), ("ui.sound.enabled", "true"))]
        self.assertEqual(got, ["off", "false"])  # herdr's defaults when absent: toast off, sound on

    def test_shipped_file_is_quiet_and_copied_when_missing(self):
        self.quiet(SHIPPED)
        self.assertEqual(self.apply()[0], "written")
        self.assertEqual(self.path.read_bytes(), SHIPPED.read_bytes())

    def test_values_in_effect_writes_nothing(self):
        text = '[ui.sound]\nenabled = false\n[keys]\nprefix = "ctrl+b"\n'  # toast absent is herdr's default, off
        self.assertEqual(self.apply(text)[0], "ok")
        self.assertEqual(self.path.read_text(), text)

    def test_edit_replaces_only_the_two_lines(self):
        self.assertEqual(self.apply(LOUD)[0], "edited")
        keep = lambda text: [l for i, l in enumerate(text.splitlines()) if i not in (5, 10)]  # all but the two value lines
        self.assertEqual(keep(self.path.read_text()), keep(LOUD))
        self.quiet(self.path)

    def test_edit_adds_missing_section_and_key(self):
        text = '[keys]\nprefix = "ctrl+b"\n[ui.toast]\nposition = "top"\ndelivery = "toast"\n'
        self.assertEqual(self.apply(text)[0], "edited")
        new = iter(self.path.read_text().splitlines())
        self.assertTrue(all(l in new for l in text.splitlines()[:4]))  # every other line kept, in order
        self.quiet(self.path)

    def test_read_only_file_or_symlink_is_manual(self):
        ro, target = self.path.with_name("ro.toml"), self.path.parents[1] / "locked" / "config.toml"
        for d in (target.parent, self.path.parent): d.mkdir()
        for f in (ro, target): f.write_text(LOUD)
        ro.chmod(0o444)
        self.path.symlink_to(target)
        target.parent.chmod(0o555)
        self.addCleanup(target.parent.chmod, 0o755)
        for p in (ro, self.path):
            state, lines = fc_herdrconf.apply_herdr_config(p, SHIPPED)
            self.assertEqual((state, ro.read_text(), target.read_text()), ("manual", LOUD, LOUD))
            text = "\n".join(lines)  # the two lines to set, and where
            self.assertTrue(all(w in text for w in ("delivery", '"off"', "enabled", "false")) and (str(p) in text or str(p.resolve()) in text))

class EditKeepsLines(unittest.TestCase):
    def edit(self, text, want):
        path = Path(tempfile.mkdtemp()) / "config.toml"
        path.write_text(text)
        self.assertEqual(fc_herdrconf.apply_herdr_config(path, SHIPPED)[0], "edited")
        self.assertEqual(path.read_text(), want)
        self.assertEqual(fc_herdrconf.apply_herdr_config(path, SHIPPED)[0], "ok")  # a rerun changes nothing
        self.assertEqual(path.read_text(), want)

    def test_replaced_lines_keep_indent_and_inline_comment(self):
        cases = [
            ('[ui.sound]\nenabled = true # on\n[ui.toast]\ndelivery = "system"  # loud\n',
             '[ui.sound]\nenabled = false # on\n[ui.toast]\ndelivery = "off"  # loud\n'),
            ('# top\n[ui.sound]\n  enabled = true\t# tab # twice\n\n[ui.toast]\n    delivery = "toast"\n# delivery = "toast"\n',
             '# top\n[ui.sound]\n  enabled = false\t# tab # twice\n\n[ui.toast]\n    delivery = "off"\n# delivery = "toast"\n'),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.edit(text, want)

    def test_appended_section_gets_exactly_one_blank_line(self):
        tail = '\n[ui.sound]\nenabled = false\n'
        cases = [
            ('[ui.toast]\ndelivery = "toast"\n', '[ui.toast]\ndelivery = "off"\n' + tail),
            ('[ui.toast]\ndelivery = "toast"\n\n', '[ui.toast]\ndelivery = "off"\n' + tail),
            ('[ui.toast]\ndelivery = "toast" # t', '[ui.toast]\ndelivery = "off" # t\n' + tail),  # no final newline
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.edit(text, want)

class Dry(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.path = self.root / "herdr" / "config.toml"

    def tree(self):
        return {p: (p.is_symlink(), oct(p.lstat().st_mode), p.is_file() and p.read_bytes()) for p in self.root.rglob("*")}

    def dry(self, path=None, text=None):
        path = path or self.path
        if text is not None:
            path.parent.mkdir(exist_ok=True)
            path.write_text(text)
        before = self.tree()
        got = fc_herdrconf.apply_herdr_config(path, SHIPPED, dry=True)
        self.assertEqual(self.tree(), before)  # nothing written, no directory made
        return got

    def test_missing_path_would_copy_without_making_its_directory(self):
        self.assertEqual(self.dry(), ("written", [f"would copy {SHIPPED} to {self.path}"]))
        self.assertFalse(self.path.parent.exists())
        locked = self.root / "locked"
        locked.mkdir(mode=0o555)
        self.addCleanup(locked.chmod, 0o755)
        missing = locked / "herdr" / "config.toml"  # a real run could not make this directory
        self.assertEqual(self.dry(missing), ("written", [f"would copy {SHIPPED} to {missing}"]))

    def test_loud_values_give_one_would_edit_line_each(self):
        state, lines = self.dry(text=LOUD)
        self.assertEqual(state, "edited")
        self.assertCountEqual(lines, [f"would edit {self.path}: [ui.sound] enabled = false",
                                      f'would edit {self.path}: [ui.toast] delivery = "off"'])
        text = '[ui.toast]\ndelivery = "off"\n'  # sound absent is herdr's default, on: only that one is loud
        self.assertEqual(self.dry(text=text), ("edited", [f"would edit {self.path}: [ui.sound] enabled = false"]))

    def test_symlink_or_unwritable_gives_todays_manual_lines(self):
        ro, link = self.root / "ro.toml", self.root / "link.toml"
        for f in (ro, self.path): f.parent.mkdir(exist_ok=True); f.write_text(LOUD)
        ro.chmod(0o444)
        link.symlink_to(self.path)  # a writable target, but path itself is a symlink
        for p in (ro, link):
            with self.subTest(path=p):
                want = fc_herdrconf.apply_herdr_config(p, SHIPPED)
                self.assertEqual(want[0], "manual")
                self.assertEqual(self.dry(p), want)
