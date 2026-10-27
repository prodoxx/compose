import hermetic  # first: no test sees the real home
import os, unittest
from ship_kit import Ship, fc
os.environ["COMPOSE_NO_TREEHOUSE"] = "1"
os.environ["HERDR_ENV"] = ""


class ChangedLayer(Ship):
    LAYERS = [f"compose/p-layer-0{i}" for i in (1, 2, 3)]

    def heads(self, local=False):
        git = (lambda *a: self.git(*a)) if local else (lambda *a: self.git("--git-dir", str(self.origin), *a))
        return [git("rev-parse", b if local else f"refs/heads/{b}") for b in self.LAYERS]

    def stop_line(self, i, before, cause):
        b, tip = self.LAYERS[i - 1], self.heads(local=True)[i - 1]
        return (f"stack  STOP  layer {i} ({b}) changed since PR #{i}: GitHub has {before[i - 1][:9]}, here {tip[:9]}; {cause}. Nothing was pushed. "
                "`compose stack --submit --update` pushes it with --force-with-lease")

    def change_001(self):
        self.assertEqual(self.submit()[1], 0)
        self.git("checkout", "-q", "compose/p-001"); self.write("001.src", "d\n"); self.commit("Change 001"); self.git("checkout", "-q", "compose/p")
        return self.heads()

    def test_a_changed_layer_stops_the_submit_and_a_refused_push_pushes_nothing_after_it_and_update_pushes_bottom_up(self):
        before = self.change_001()
        out, code = self.submit()
        self.assertEqual(code, 1)
        self.assertIn(self.stop_line(2, before, "its own files changed: 001.src"), out)
        self.assertEqual(self.heads(), before)
        self.set_pr(2, state="CLOSED")  # a layer whose pull request is not open is not compared
        out, code = self.submit()
        self.assertEqual(code, 1)
        self.assertIn(self.stop_line(3, before, "a layer below it or the base moved"), out)
        self.set_pr(2, state="OPEN")
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho nope >&2\nexit 1\n"); hook.chmod(0o755)
        out, code = self.run_cmd(fc.fc_ship.cmd_stack, submit=True, update=True)
        self.assertEqual(code, 1)
        self.assertTrue(any("stack  STOP  push of compose/p-layer-02 refused: " in l for l in out), out)
        self.assertEqual(self.heads(), before)
        hook.unlink()
        out, code = self.run_cmd(fc.fc_ship.cmd_stack, submit=True, update=True)
        self.assertEqual(code, 0, out)
        tips = self.heads(local=True)
        self.assertEqual(self.heads(), tips)
        lines = [f"submit update {b} {tips[i][:9]} (was {before[i][:9]})" for i, b in enumerate(self.LAYERS) if i]
        at = [next(n for n, l in enumerate(out) if want in l) for want in lines]
        self.assertEqual(at, sorted(at))
        self.assertEqual(len(self.prs()), 3)
        out, code = self.submit()
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
