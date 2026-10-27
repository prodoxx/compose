import hermetic  # first: no test sees the real home
import io, os, unittest
from unittest import mock
from cleanup_kit import FakeLinear, fc
os.environ["HERDR_ENV"] = ""


class CleanLinearLine(unittest.TestCase):
    """Once the project is linked to a feature, clean always says what it did about Linear."""
    NOTHING = "linear  nothing proven merged; states unchanged"

    def lines(self, feature, metas, project_merged, key="k", linear=None):
        gql = linear or mock.Mock(side_effect=AssertionError("Linear called"))
        out = io.StringIO()
        with mock.patch.object(fc.fc_config, "LINEAR_KEY", key), mock.patch.object(fc.fc_linear, "_linear_warned", []), \
             mock.patch.object(fc.fc_linear, "linear_gql", gql), mock.patch("sys.stdout", out):
            fc.fc_linear.linear_merged(feature, metas, project_merged)
        return [line.split("] ", 1)[1] for line in out.getvalue().splitlines()]

    def test_with_a_key_and_nothing_merged_one_line_says_states_are_unchanged(self):
        self.assertEqual(self.lines("ENG-1", [], False), [self.NOTHING])

    def test_without_a_key_and_nothing_merged_one_line_says_linear_is_skipped(self):
        out = self.lines("ENG-1", [], False, key="")
        self.assertEqual(len(out), 1)
        self.assertIn("linear skipped", out[0])

    def test_no_feature_and_nothing_merged_says_nothing(self):
        for key in ("k", ""):
            for project_merged in (False, True):
                self.assertEqual(self.lines(None, [], project_merged, key=key), [])

    def test_merged_tasks_move_to_done_as_before(self):
        linear = FakeLinear(ENG_1=("In Review", None), ENG_2=("In Review", "ENG-1"), ENG_3=("In Review", "ENG-2"), ENG_4=("Done", "ENG-2"))
        out = self.lines("ENG-1", [{"id": "001", "linear": "ENG-3"}], False, linear=linear)
        self.assertEqual(out, ["linear ENG-3 -> Done", "linear ENG-2 -> Done"])
        self.assertEqual(linear.states("ENG-3", "ENG-2", "ENG-1"), ["Done", "Done", "In Review"])

    def test_merged_tasks_without_a_key_are_skipped_once_without_calling_linear(self):
        out = self.lines("ENG-1", [{"id": "001", "linear": "ENG-3"}], True, key="")
        self.assertEqual(len(out), 1)
        self.assertIn("linear skipped", out[0])

    def test_a_merged_project_with_no_merged_task_still_rolls_the_feature_up(self):
        linear = FakeLinear(ENG_1=("In Review", None), ENG_2=("Done", "ENG-1"))
        self.assertEqual(self.lines("ENG-1", [], True, linear=linear), ["linear ENG-1 -> Done"])


if __name__ == "__main__":
    unittest.main()
