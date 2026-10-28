import hermetic  # first: no test sees the real home
import importlib.machinery, importlib.util, os, sys, unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_plan  # noqa: E402  # compose.py puts bin on sys.path; nothing imports fc_plan before 007


def story(ident, state="Todo", *blockers, labels=()):
    return {"id": ident, "title": "T " + ident, "url": "u/" + ident, "state": state, "labels": list(labels),
            "blockers": [{"id": b[0], "state": b[1], "labels": list(b[2]) if len(b) > 2 else []} for b in blockers]}

def feat(ident, state="Todo", *blockers, labels=("Feature",), stories=(), parent=None, plan_url=None):
    return {"id": ident, "title": "T " + ident, "url": "u/" + ident, "state": state, "labels": list(labels), "parent": parent,
            "plan_url": plan_url, "stories": list(stories),
            "blockers": [{"id": b[0], "state": b[1], "labels": list(b[2]) if len(b) > 2 else []} for b in blockers]}

def lane(ident, *stories):
    return {"id": ident, "title": "L " + ident, "url": "u/" + ident, "stories": list(stories)}


class StartToday(unittest.TestCase):
    LINK = "* Delivery plan: [NEB-1: delivery plan](https://linear.app/x/document/d1)"

    def link(self, description):
        return fc_plan.with_plan_link(description, "NEB-1: delivery plan", "https://linear.app/x/document/d1")

    def test_link_goes_last_in_notes_before_the_next_section(self):
        lines = self.link("## Goal\n\nShip it.\n\n## Notes\n\n* old note\n\n## Constraints\n\n* none\n").splitlines()
        self.assertEqual(lines.count(self.LINK), 1)
        i = lines.index(self.LINK)
        self.assertLess(lines.index("* old note"), i)
        self.assertEqual([l for l in lines[i + 1:] if l.strip()], ["## Constraints", "* none"])
        self.assertEqual([l for l in lines[:i] if l.strip()], ["## Goal", "Ship it.", "## Notes", "* old note"])

    def test_link_ends_a_last_notes_section_or_adds_one_and_is_never_added_twice(self):
        lines = self.link("## Goal\n\nShip it.\n\n## Notes\n\n* old note\n").splitlines()
        self.assertEqual([l for l in lines if l.strip()], ["## Goal", "Ship it.", "## Notes", "* old note", self.LINK])
        lines = self.link("## Goal\n\nShip it.\n").splitlines()
        self.assertEqual([l for l in lines if l.strip()], ["## Goal", "Ship it.", "## Notes", self.LINK])
        linked = "## Notes\n\n" + self.LINK + "\n"
        self.assertEqual(self.link(linked), linked)
        self.assertEqual(self.link(self.link("## Goal\n\nShip it.\n")).splitlines().count(self.LINK), 1)

    def test_start_today_lists_todo_or_backlog_stories_whose_blockers_are_finished(self):
        lanes = [lane("NEB-1", story("NEB-3"), story("NEB-4", "Todo", ("NEB-3", "Todo")), story("NEB-5", "Todo", ("NEB-9", "In Review", ["Design"])),
                      story("NEB-6", "In Progress"), story("NEB-7", "Done"), story("NEB-8", "Todo", ("NEB-20", "In Review"))),
                 lane("NEB-2", story("NEB-12", "backlog", ("NEB-21", "Done"), ("NEB-22", "Canceled")), story("NEB-13", "Backlog", ("NEB-21", "Done"), ("NEB-23", "In Progress")))]
        self.assertEqual(fc_plan.start_today(lanes).splitlines(), ["1. [NEB-3](u/NEB-3) T NEB-3", "2. [NEB-5](u/NEB-5) T NEB-5", "3. [NEB-12](u/NEB-12) T NEB-12"])
        self.assertEqual(fc_plan.plan_sections(lanes)["## Start today"], fc_plan.start_today(lanes))

class PlanLanes(unittest.TestCase):
    def lines(self, lanes):
        return fc_plan.lanes_text(lanes).strip("\n").split("\n")

    def test_lanes_in_order_with_stories_in_build_order_and_needs_lines(self):
        lanes = [lane("NEB-1", story("NEB-12", "Todo", ("NEB-5", "Todo")), story("NEB-10"), story("NEB-7", "canceled"),
                      story("NEB-5", "In Progress", ("NEB-31", "Todo"), ("NEB-99", "Todo"), ("NEB-30", "Done")), story("NEB-9", "Todo", ("NEB-10", "Todo"))),
                 lane("NEB-2", story("NEB-31", "Todo", ("NEB-30", "Done")), story("NEB-30", "Done")),
                 lane("NEB-3")]
        self.assertEqual(self.lines(lanes), [
            "### [NEB-1](u/NEB-1) L NEB-1", "",
            "1. [NEB-5](u/NEB-5) T NEB-5 (In Progress)", "   Needs: NEB-31, NEB-30",
            "2. [NEB-10](u/NEB-10) T NEB-10 (Todo)", "3. [NEB-9](u/NEB-9) T NEB-9 (Todo)", "4. [NEB-12](u/NEB-12) T NEB-12 (Todo)", "",
            "### [NEB-2](u/NEB-2) L NEB-2", "",
            "1. [NEB-30](u/NEB-30) T NEB-30 (Done)", "2. [NEB-31](u/NEB-31) T NEB-31 (Todo)", "",
            "### [NEB-3](u/NEB-3) L NEB-3", "", "No stories yet."])
        self.assertEqual(list(fc_plan.plan_sections(lanes))[:2], ["## Start today", "## Lanes"])
        self.assertEqual(fc_plan.plan_sections(lanes)["## Lanes"], fc_plan.lanes_text(lanes))

    def test_stories_caught_in_a_cycle_follow_by_issue_number(self):
        lanes = [lane("NEB-4", story("NEB-41", "Todo", ("NEB-40", "Todo")), story("NEB-40", "Todo", ("NEB-41", "Todo")),
                      story("NEB-43", "Todo", ("NEB-45", "Todo")), story("NEB-45", "Backlog"), story("NEB-44", "CANCELED"))]
        self.assertEqual(self.lines(lanes), [
            "### [NEB-4](u/NEB-4) L NEB-4", "",
            "1. [NEB-45](u/NEB-45) T NEB-45 (Backlog)", "2. [NEB-43](u/NEB-43) T NEB-43 (Todo)",
            "3. [NEB-40](u/NEB-40) T NEB-40 (Todo)", "4. [NEB-41](u/NEB-41) T NEB-41 (Todo)"])


class PlanWaves(unittest.TestCase):
    LANES = [lane("NEB-1", story("NEB-10"), story("NEB-12", "Done"), story("NEB-11", "In Progress", ("NEB-12", "Done"), ("NEB-99", "Todo")),
                  story("NEB-14", "Todo", ("NEB-21", "Todo")), story("NEB-13", "Todo", ("NEB-10", "Todo"), ("NEB-14", "Todo"))),
             lane("NEB-2", story("NEB-21", "Todo", ("NEB-10", "Todo")), story("NEB-22", "In Review", labels=["Design"]),
                  story("NEB-9", "Todo", ("NEB-22", "In Review", ["Design"])), story("NEB-25", "Canceled", ("NEB-10", "Todo")),
                  story("NEB-30", "Todo", ("NEB-31", "Todo")), story("NEB-31", "Todo", ("NEB-30", "Todo")))]

    def test_waves_group_unfinished_stories_across_lanes_by_issue_number_leaving_out_a_cycle(self):
        self.assertEqual(fc_plan.waves_text(self.LANES).strip().splitlines(), [
            "| Wave | Stories | Count |", "| -- | -- | -- |", "| 1 | NEB-9, NEB-10, NEB-11 | 3 |", "| 2 | NEB-21 | 1 |", "| 3 | NEB-14 | 1 |", "| 4 | NEB-13 | 1 |"])

class CriticalPath(unittest.TestCase):
    def lines(self, lanes):
        return fc_plan.critical_path_text(lanes).strip("\n").split("\n")

    def test_longest_chain_runs_across_lanes_first_blocker_first(self):
        lanes = [lane("NEB-90", story("NEB-2"), story("NEB-3", "Todo", ("NEB-2", "Todo")), story("NEB-5", "In Progress", ("NEB-4", "Todo")),
                      story("NEB-7", "Todo", ("NEB-6", "Todo"), ("NEB-2", "Todo"))),
                 lane("NEB-91", story("NEB-6", "Todo", ("NEB-5", "In Progress")), story("NEB-4"))]
        self.assertEqual(self.lines(lanes), ["1. NEB-4 -> NEB-5 -> NEB-6 -> NEB-7 (4 stories)"])
        sections = fc_plan.plan_sections(lanes)
        self.assertEqual(sections["## Critical path"], fc_plan.critical_path_text(lanes))
        keys = list(sections)
        self.assertEqual(keys.index("## Critical path"), keys.index("## Waves") + 1)

    def test_every_chain_tied_for_longest_is_listed_by_issue_number(self):
        lanes = [lane("NEB-90", story("NEB-13"), story("NEB-21", "Todo", ("NEB-20", "Todo")), story("NEB-20", "Todo", ("NEB-9", "Todo"), ("NEB-13", "Todo")),
                      story("NEB-5"), story("NEB-6", "Todo", ("NEB-5", "Todo"))),
                 lane("NEB-91", story("NEB-12", "Todo", ("NEB-11", "Todo")), story("NEB-11", "Todo", ("NEB-10", "Todo")), story("NEB-10"), story("NEB-9"))]
        self.assertEqual(self.lines(lanes), ["1. NEB-9 -> NEB-20 -> NEB-21 (3 stories)", "2. NEB-10 -> NEB-11 -> NEB-12 (3 stories)",
                                             "3. NEB-13 -> NEB-20 -> NEB-21 (3 stories)"])

    def test_finished_stories_and_blockers_outside_the_map_add_no_step(self):
        lanes = [lane("NEB-90", story("NEB-3", "Done"), story("NEB-4", "Todo", ("NEB-3", "Done"), ("NEB-50", "Todo")),
                      story("NEB-5", "Todo", ("NEB-4", "Todo")), story("NEB-6", "Canceled", ("NEB-5", "Todo"))),
                 lane("NEB-91", story("NEB-7", "In Review", ("NEB-6", "Canceled"), labels=["Design"]),
                      story("NEB-8", "Todo", ("NEB-7", "In Review", ["Design"])), story("NEB-9", "In Review", ("NEB-5", "Todo")))]
        self.assertEqual(self.lines(lanes), ["1. NEB-4 -> NEB-5 -> NEB-9 (3 stories)"])

class PlanLinkPlacement(unittest.TestCase):
    URL = "https://linear.app/x/document/d1"
    LINK = "* Delivery plan: [NEB-1: delivery plan](https://linear.app/x/document/d1)"

    def link(self, description):
        return fc_plan.with_plan_link(description, "NEB-1: delivery plan", self.URL)

    def test_link_follows_notes_own_lines_before_its_first_subsection(self):
        desc = ("## Goal\n\n### Detail\n\nShip it.\n\n## Notes\n\n* old note\n* second\n\n"
                "### Risks\n\n* slow\n\n### Later\n\n* more\n\n## Constraints\n\n* none\n")
        out = self.link(desc)
        self.assertTrue(out.startswith(desc[:desc.index("\n\n### Risks")] + "\n" + self.LINK + "\n"))
        self.assertTrue(out.endswith(desc[desc.index("### Risks"):]))
        self.assertEqual([l for l in out.splitlines() if l.strip()], [
            "## Goal", "### Detail", "Ship it.", "## Notes", "* old note", "* second", self.LINK, "### Risks", "* slow", "### Later", "* more", "## Constraints", "* none"])

    def test_link_follows_the_last_own_line_of_notes_without_subsections(self):
        desc = "## Notes\n\n* a\n\nSome text.\n\n\n## Constraints\n\n### Hard\n\n* none\n"
        out = self.link(desc)
        self.assertTrue(out.startswith("## Notes\n\n* a\n\nSome text.\n" + self.LINK + "\n"))
        self.assertTrue(out.endswith(desc[desc.index("## Constraints"):]))
        self.assertEqual(out.splitlines().count(self.LINK), 1)
        out = self.link("## Goal\n\nShip it.\n\n## Notes\n\n* a\n\n\n")
        self.assertTrue(out.startswith("## Goal\n\nShip it.\n\n## Notes\n\n* a\n" + self.LINK))
        self.assertEqual([l for l in out.splitlines() if l.strip()][-2:], ["* a", self.LINK])

    def test_notes_with_only_subsections_get_the_link_first_between_blank_lines(self):
        desc = "## Goal\n\nShip it.\n\n## Notes\n\n### Risks\n\n* slow\n\n## Constraints\n\n* none\n"
        self.assertEqual(self.link(desc), desc.replace("## Notes\n\n", "## Notes\n\n" + self.LINK + "\n\n"))
        self.assertEqual(self.link("## Notes\n### Risks\n* slow\n"), "## Notes\n\n" + self.LINK + "\n\n### Risks\n* slow\n")

TRACKS = "1. Sign-in: NEB-7, [NEB-9](u/NEB-9)\n\n2. Onboarding: NEB-12 (after NEB-7)\n3. Billing: NEB-15 (after Sign-in, Onboarding)\n4. Search: NEB-20\n"

class ProductTracks(unittest.TestCase):
    def test_tracks_parse_in_list_order_with_links_and_after(self):
        t = fc_plan.parse_tracks(TRACKS)
        self.assertEqual([(x["rank"], x["name"], x["items"], x["after"]) for x in t], [
            (1, "Sign-in", ["NEB-7", "NEB-9"], []), (2, "Onboarding", ["NEB-12"], ["NEB-7"]),
            (3, "Billing", ["NEB-15"], ["Sign-in", "Onboarding"]), (4, "Search", ["NEB-20"], [])])
        self.assertEqual(fc_plan.parse_tracks(fc_plan.PRODUCT_NEXT + "\n"), [])

    def test_a_bad_section_names_the_line(self):
        for text, msg in (("1. Sign-in NEB-7", 'line 1: not a track: "1. Sign-in NEB-7" (write "<n>. <name>: <id>, <id> (after <track>, ...)")'),
                          ("1. A: NEB-7\n2. B: NEB-7", "line 2: B names NEB-7, already in track A"),
                          ("1. A: NEB-7\n2. a: NEB-8", "line 2: a second track named a"),
                          ("1. A: NEB-7 (after Pay)", "line 1: A comes after Pay, which is no track here"),
                          ("1. A: NEB-7 (after NEB-31)", "line 1: A comes after NEB-31, which no track names"),
                          ("1. A: NEB-7 (after A)", "line 1: A comes after itself"),
                          ("1. A: NEB-1, NEB-1", "line 1: A names NEB-1 twice"),
                          ("1. A: NEB-7 (after C)\n2. B: NEB-8 (after A)\n3. C: NEB-9 (after B)", "the tracks wait on each other in a loop: A -> C -> B -> A"),
                          ("1. A: NEB-7 (after B)\n2. B: NEB-8 (after A)", "the tracks wait on each other in a loop: A -> B -> A")):
            with self.subTest(text), self.assertRaises(fc_plan.TracksError) as e:
                fc_plan.parse_tracks(text)
            self.assertEqual(str(e.exception), msg)
        self.assertTrue(issubclass(fc_plan.TracksError, ValueError))

    def test_waves_are_parallel_by_default_and_after_forces_order(self):
        waves = fc_plan.track_waves(fc_plan.parse_tracks(TRACKS))
        self.assertEqual([[t["name"] for t in w] for w in waves], [["Sign-in", "Search"], ["Onboarding"], ["Billing"]])

    def test_blocks_follow_tracks_and_after(self):
        self.assertEqual(fc_plan.derived_blocks(fc_plan.parse_tracks(TRACKS)),
                         {("NEB-7", "NEB-9"), ("NEB-7", "NEB-12"), ("NEB-9", "NEB-15"), ("NEB-12", "NEB-15")})

    def test_blocks_bridge_over_an_unusable_item_so_the_order_holds(self):
        text = "1. Sign-in: NEB-7, NEB-8, NEB-9\n2. Onboarding: NEB-12 (after Sign-in)\n3. Billing: NEB-15 (after NEB-8)\n"
        for skip, want in (({"NEB-8"}, {("NEB-7", "NEB-9"), ("NEB-9", "NEB-12"), ("NEB-7", "NEB-15")}),  # a middle item, and an `after` id
                           ({"NEB-9"}, {("NEB-7", "NEB-8"), ("NEB-8", "NEB-12"), ("NEB-8", "NEB-15")}),  # the last item of an `after` track
                           ({"NEB-8", "NEB-9"}, {("NEB-7", "NEB-12"), ("NEB-7", "NEB-15")}),
                           ({"NEB-7", "NEB-8", "NEB-9"}, set()),  # a track with nothing usable leaves the next one free
                           ({"NEB-12"}, {("NEB-7", "NEB-8"), ("NEB-8", "NEB-9"), ("NEB-8", "NEB-15")})):
            with self.subTest(skip):
                self.assertEqual(fc_plan.derived_blocks(fc_plan.parse_tracks(text), skip), want)

    def test_a_track_after_a_track_with_nothing_usable_waits_on_what_that_track_waits_on(self):
        text = "1. A: NEB-7\n2. B: NEB-8 (after A)\n3. C: NEB-9 (after B)\n"
        self.assertEqual(fc_plan.derived_blocks(fc_plan.parse_tracks(text), {"NEB-8"}), {("NEB-7", "NEB-9")})

    def test_diff_touches_only_relations_between_plan_features(self):
        current = {"NEB-9": {"NEB-7"}, "NEB-12": {"NEB-5", "NEB-20", "NEB-40"}, "NEB-20": {"NEB-12"}}  # NEB-40 is no plan feature
        added, removed = fc_plan.blocks_diff({("NEB-7", "NEB-9"), ("NEB-7", "NEB-12")}, current, {"NEB-5", "NEB-7", "NEB-9", "NEB-12", "NEB-20"})
        self.assertEqual(added, [("NEB-7", "NEB-12")])
        self.assertEqual(removed, [("NEB-5", "NEB-12"), ("NEB-20", "NEB-12"), ("NEB-12", "NEB-20")])

    def test_a_verified_base_counts_as_built(self):
        self.assertEqual([fc_plan.base_built(s, l) for s, l in (("In Review", []), ("Done", []), ("In Progress", ["Design"]))], [True, True, False])


class ProductSections(unittest.TestCase):
    def setUp(self):
        self.tracks = fc_plan.parse_tracks(TRACKS)
        self.f = {f["id"]: f for f in (
            feat("NEB-7", "Done"), feat("NEB-9", "Todo", ("NEB-7", "Done"), plan_url="u/d9",
                                        stories=[story("NEB-11", "Todo", ("NEB-10", "Todo")), story("NEB-10"), story("NEB-13", "Done")]),
            feat("NEB-12", "In Progress", ("NEB-7", "Done")), feat("NEB-15", "In Progress", ("NEB-9", "Todo"), ("NEB-12", "In Progress")),
            feat("NEB-20", "Canceled"), feat("NEB-30", "Backlog"), feat("NEB-31", "Todo", labels=("Story",), parent="NEB-30"))}

    def test_order_walks_waves_then_rank_then_track_then_lane(self):
        order = fc_plan.order_entries(self.tracks, self.f)
        self.assertEqual([(e["id"], e["kind"], e["feature"], e["track"], e["wave"], e["waits_on"]) for e in order], [
            ("NEB-10", "story", "NEB-9", "Sign-in", 1, []), ("NEB-11", "story", "NEB-9", "Sign-in", 1, []),
            ("NEB-12", "feature", "NEB-12", "Onboarding", 2, []), ("NEB-15", "feature", "NEB-15", "Billing", 3, ["NEB-9", "NEB-12"])])

    def test_order_waits_across_a_canceled_or_unknown_item_on_the_one_before_it(self):
        for gone in ({"NEB-8": feat("NEB-8", "Canceled")}, {}, {"NEB-8": feat("NEB-8", labels=("Story",))}):
            f = {"NEB-7": feat("NEB-7", "In Progress"), "NEB-9": feat("NEB-9"), "NEB-12": feat("NEB-12"), **gone}
            for text, item in (("1. A: NEB-7, NEB-8, NEB-9\n", "NEB-9"), ("1. A: NEB-7, NEB-8\n2. B: NEB-12 (after A)\n", "NEB-12")):
                with self.subTest(gone=list(gone.values()), text=text):
                    order = fc_plan.order_entries(fc_plan.parse_tracks(text), f)
                    self.assertEqual([e["waits_on"] for e in order if e["id"] == item], [["NEB-7"]])
                    self.assertFalse([e for e in order if e["id"] == "NEB-8"])

    def test_tracks_in_a_wave_follow_the_list_order_not_the_numbers_written(self):
        tracks = fc_plan.parse_tracks("2. A: NEB-7\n1. B: NEB-9\n")
        f = {"NEB-7": feat("NEB-7"), "NEB-9": feat("NEB-9")}
        self.assertEqual([e["track"] for e in fc_plan.order_entries(tracks, f)], ["A", "B"])
        self.assertIn("- A: [NEB-7](u/NEB-7) T NEB-7 (Todo)\n- B: ", fc_plan.product_sections(tracks, f, [], [])["## Waves"])

    def test_proposals_never_fix_destructively_and_say_why(self):
        self.tracks[3]["items"] += ["NEB-31", "NEB-44"]
        self.assertEqual(fc_plan.product_proposals(self.tracks, self.f), [
            "NEB-15 T NEB-15 is In Progress but waits on NEB-9, NEB-12 in the plan: its started work goes on; hold it if it must wait.",
            "NEB-20 T NEB-20 is Canceled but in track Search: drop it from the track, or reopen it.",
            "NEB-31 T NEB-31 in track Search is not a feature: name its feature (NEB-30) instead, or label it Feature.",
            "NEB-44 is in track Search but Linear has no issue NEB-44 in this project: drop it from the track.",
            "NEB-30 T NEB-30 is an open feature the plan does not name: add it to a track, or cancel it."])

    def test_data_sections_link_each_feature_and_its_own_plan(self):
        s = fc_plan.product_sections(self.tracks, self.f, ["NEB-10"], ["x."])
        self.assertEqual(list(s), list(fc_plan.PRODUCT_DATA))
        self.assertIn("### Wave 1\n\n- Sign-in: [NEB-7](u/NEB-7) T NEB-7 (Done) -> [NEB-9](u/NEB-9) T NEB-9 (Todo, [its plan](u/d9))\n"
                      "- Search: [NEB-20](u/NEB-20) T NEB-20 (Canceled)", s["## Waves"])
        self.assertIn("### Wave 3\n\n- Billing (after Sign-in, Onboarding): [NEB-15](u/NEB-15) T NEB-15 (In Progress)", s["## Waves"])
        self.assertEqual((s["## Start today"], s["## Proposed fixes"]), ("1. NEB-10 T NEB-10 (feature NEB-9, track Sign-in)", "1. x."))
        e = fc_plan.product_sections([], {}, [], [])
        self.assertEqual((e["## Waves"], e["## Start today"], e["## Proposed fixes"]), ("No tracks yet.", "Nothing can start now.", "None."))

class ProductPlace(unittest.TestCase):
    def test_a_missing_section_goes_where_the_skeleton_puts_it(self):
        body = "## Why this order\n\nAuth first.\n\n## History\n\n- 2026-01-01: first\n"
        out = fc_plan.replace_product_section(body, "## Tracks", "## Tracks\n\n1. A: NEB-7")
        self.assertEqual(out, "## Tracks\n\n1. A: NEB-7\n\n" + body)
        out = fc_plan.replace_product_section(out, "## Waves", "w")
        self.assertEqual([l for l in out.splitlines() if l.startswith("## ")], ["## Tracks", "## Why this order", "## Waves", "## History"])
        self.assertEqual(fc_plan.replace_product_section("x\n", "## Waves", "w"), fc_plan.replace_section("x\n", "## Waves", "w"))  # nothing after it: appended
        self.assertEqual(fc_plan.replace_product_section(out, "## Waves", "v"), fc_plan.replace_section(out, "## Waves", "v"))  # present: replaced in place


class LinearMarkdown(unittest.TestCase):
    def test_what_linear_rewrites_on_save_is_the_same_text(self):
        ours = "## Tracks\n\n- A: [NEB-7](https://linear.app/x/issue/NEB-7/a-(b))\n  + nested  \n"
        theirs = "## Tracks\n\n* A: [NEB-7](<https://linear.app/x/issue/NEB-7/a-(b)>)\n  * nested"  # bullets as *, link targets in <>, trailing space gone
        self.assertTrue(fc_plan.same_markdown(ours, theirs))
        self.assertFalse(fc_plan.same_markdown(ours, theirs.replace("NEB-7/a", "NEB-8/a")))
        self.assertFalse(fc_plan.same_markdown("1. A\n2. B", "1. B\n2. A"))  # order is content
        self.assertEqual(fc_plan.linear_markdown("x - y\n-z"), "x - y\n-z")  # a dash that is no bullet stays

    def test_linear_puts_a_blank_line_between_a_paragraph_and_the_list_after_it(self):
        # NEB-2191..2194: `Code involved:` written right above its bullets came back with a blank line between
        ours = "Code involved:\n- a.tsx\n- b.tsx\n\nQuestions:\n1. Why?\n   Recommendation: x\n2. How?\n"
        theirs = "Code involved:\n\n* a.tsx\n* b.tsx\n\nQuestions:\n\n1. Why?\n   Recommendation: x\n2. How?"
        self.assertTrue(fc_plan.same_markdown(ours, theirs))
        self.assertEqual(fc_plan.linear_markdown("- a\n  - b\nlazy\n- c"), "* a\n  * b\nlazy\n* c")  # inside a list nothing is added
        self.assertFalse(fc_plan.same_markdown("Items:\n- a", "Items:\n\n\n* a"))  # one blank line is Linear's; more is the writer's

class ProductSeed(unittest.TestCase):
    def setUp(self):
        self.f = {f["id"]: f for f in (feat("NEB-7"), feat("NEB-9", "Todo", ("NEB-7", "Todo")), feat("NEB-12", "Todo", ("NEB-7", "Todo")),
                                       feat("NEB-15", "Todo", ("NEB-9", "Todo"), ("NEB-12", "Todo"), ("NEB-3", "Done")), feat("NEB-20"), feat("NEB-2", "Done"))}
        self.f["NEB-7"]["title"] = "Sign-in: email"

    def test_chains_become_tracks_and_a_branch_names_the_feature_it_waits_on(self):
        self.assertEqual(fc_plan.seed_tracks(self.f).splitlines(), [
            "1. Sign-in - email: NEB-7, NEB-9", "2. T NEB-12: NEB-12 (after NEB-7)",
            "3. T NEB-15: NEB-15 (after Sign-in - email, T NEB-12)", "4. T NEB-20: NEB-20"])

    def test_the_seed_parses_and_keeps_every_open_relation(self):
        self.assertEqual(fc_plan.derived_blocks(fc_plan.parse_tracks(fc_plan.seed_tracks(self.f))),
                         {("NEB-7", "NEB-9"), ("NEB-7", "NEB-12"), ("NEB-9", "NEB-15"), ("NEB-12", "NEB-15")})

    def test_a_taken_track_name_gets_the_id_appended(self):
        f = {"NEB-4": feat("NEB-4"), "NEB-5": feat("NEB-5")}
        f["NEB-4"]["title"] = f["NEB-5"]["title"] = "Billing"
        self.assertEqual(fc_plan.seed_tracks(f).splitlines(), ["1. Billing: NEB-4", "2. Billing NEB-5: NEB-5"])

    def test_the_leftovers_of_a_blocker_cycle_are_placed_by_number(self):
        f = {"NEB-8": feat("NEB-8", "Todo", ("NEB-6", "Todo")), "NEB-6": feat("NEB-6", "Todo", ("NEB-8", "Todo"))}
        self.assertEqual(fc_plan.seed_tracks(f).splitlines()[0], "1. T NEB-6: NEB-6, NEB-8")

    def test_a_title_with_a_comma_or_after_still_parses(self):
        self.f["NEB-20"]["title"] = "Export, then (after hours) mail"
        self.f["NEB-7"]["title"] = "Sign-in, (after: email)"
        text = fc_plan.seed_tracks(self.f)
        self.assertEqual(fc_plan.derived_blocks(fc_plan.parse_tracks(text)),
                         {("NEB-7", "NEB-9"), ("NEB-7", "NEB-12"), ("NEB-9", "NEB-15"), ("NEB-12", "NEB-15")})
        self.assertEqual([t["items"] for t in fc_plan.parse_tracks(text)][-1], ["NEB-20"])
        self.assertEqual([t["name"] for t in fc_plan.parse_tracks(text)], ["Sign-in - after - email", "T NEB-12", "T NEB-15", "Export - then after hours mail"])
