import hermetic  # first: no test sees the real home
import contextlib, importlib.machinery, importlib.util, io, json, os, pwd, socket, subprocess, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
os.environ["HERDR_ENV"] = ""

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(ROOT / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)
import fc_designmatch as dm  # noqa: E402

# The card of #496 as Framelink writes it once self-contained: a white bordered card, a title, a hint, a filled button with no icon.
NODE = {"name": "Resume", "nodes": [{
    "id": "7:21", "name": "Resume card", "type": "FRAME", "fills": ["#FFFFFF"], "strokes": ["#E5E7EB"], "strokeWeight": "1px", "borderRadius": "12px",
    "layout": {"mode": "column", "gap": "16px", "padding": "24px"},
    "children": [
        {"id": "7:22", "name": "Title", "type": "TEXT", "text": "Upload your resume", "fills": ["#111827"],
         "textStyle": {"fontFamily": "Inter", "fontWeight": 600, "fontSize": 18, "lineHeight": "28px"}},
        {"id": "7:23", "name": "Hint", "type": "TEXT", "text": "PDF or DOCX, up to 5 MB", "fills": ["#6B7280"],
         "textStyle": {"fontFamily": "Inter", "fontWeight": 400, "fontSize": 14, "lineHeight": "1.4286em"}},
        {"id": "7:24", "name": "Button", "type": "INSTANCE", "componentProperties": {"variant": "primary", "icon": False}, "fills": ["#2563EB"],
         "borderRadius": "8px", "layout": {"mode": "row", "gap": "8px", "padding": "10px 16px"},
         "children": [{"id": "I7:24;1:5", "name": "Label", "type": "TEXT", "text": "Upload resume", "fills": ["#FFFFFF"],
                       "textStyle": {"fontFamily": "Inter", "fontWeight": 500, "fontSize": 14, "lineHeight": "20px"}}]},
        {"id": "7:25", "name": "Hidden note", "type": "TEXT", "text": "Never shown", "visible": False}]}]}

BARE = """<!doctype html><html><body><div id="app"><div><h2>Upload your resume</h2><p>PDF or DOCX, up to 5 MB</p>
<button style="background:transparent;border:1px solid #2563eb;color:#2563eb"><svg width="16" height="16"></svg> Upload resume</button></div></div></body></html>"""
STYLED = """<!doctype html><html><head><style>
body{font-family:Inter,system-ui,sans-serif;margin:40px}
.card{background:#fff;border:1px solid oklch(0.928 0.006 264.531);border-radius:0.75rem;padding:1.5rem;width:420px}
.card>*+*{margin-top:16px}
h2{margin:0;font-size:18px;font-weight:600;line-height:28px;color:#111827}
p{margin-bottom:0;font-size:14px;line-height:20px;color:#6b7280}
button{display:inline-flex;gap:8px;background:#2563eb;color:#fff;border:0;border-radius:8px;padding:10px 16px;font:500 14px/20px Inter,sans-serif}
</style></head><body><div id="app"><div class="card"><h2>Upload your resume</h2><p>PDF or DOCX, up to 5 MB</p><button>Upload resume</button></div></div></body></html>"""


def built(p, tag, own="", text="", rect=(0, 0, 100, 20), **kv):
    """One measured element as MEASURE_JS returns it, unstyled unless `kv` says otherwise."""
    return {"p": p, "tag": tag, "cls": "", "own": own, "text": text or own, "rect": list(rect), "bg": [0, 0, 0, 0], "color": [0, 0, 0, 1], "bw": [0, 0, 0, 0], "bc": None,
            "outline": None, "rings": [], "radius": [0, 0, 0, 0], "pad": [0, 0, 0, 0], "out": False, "ff": "Times", "fs": 16, "fw": 400, "lh": None, **kv}


WORDS = "Upload your resume PDF or DOCX, up to 5 MB Upload resume"
TYPE = {"ff": "Inter, sans-serif"}

def page(card, title, hint, button):
    """A measured page: body > card > title, hint, button, each with the styles given."""
    return {"url": "http://x/resume", "els": [
        built(-1, "body", text=WORDS, rect=(0, 0, 800, 600)),
        built(0, "div", text=WORDS, rect=(40, 40, 420, 200), **card),
        built(1, "h2", "Upload your resume", rect=(65, 65, 370, 28), **title),
        built(1, "p", "PDF or DOCX, up to 5 MB", rect=(65, 109, 370, 20), **hint),
        built(1, "button", "Upload resume", rect=(65, 145, 130, 40), **button)]}

AS_DESIGNED = page({"bg": [255, 255, 255, 1], "bw": [1, 1, 1, 1], "bc": [229, 231, 235, 1], "radius": [12, 12, 12, 12], "pad": [24, 24, 24, 24]},
                   {**TYPE, "fs": 18, "fw": 600, "lh": 28, "color": [17, 24, 39, 1]}, {**TYPE, "fs": 14, "lh": 20, "color": [107, 114, 128, 1]},
                   {**TYPE, "fs": 14, "fw": 500, "lh": 20, "color": [255, 255, 255, 1], "bg": [37, 99, 235, 1], "radius": [8, 8, 8, 8], "pad": [10, 16, 10, 16]})


class Facts(unittest.TestCase):
    def test_each_visible_node_carries_what_the_design_gives_it(self):
        els = dm.elements(NODE)
        self.assertEqual([e["name"] for e in els], ["Resume card", "Title", "Hint", "Button", "Label"])  # the hidden note is no part of the look
        card, title, hint, button, _ = els
        self.assertEqual(card["props"], {"fill": (255, 255, 255, 1.0), "stroke": (229, 231, 235, 1.0), "strokeWeight": (1.0,) * 4, "radius": (12.0,) * 4,
                                         "padding": (24.0,) * 4, "gap": 16.0})
        self.assertEqual(title["props"], {"font": "Inter", "size": 18.0, "weight": 600, "lineHeight": 28.0, "colour": (17, 24, 39, 1.0)})
        self.assertAlmostEqual(hint["props"]["lineHeight"], 20.0, places=2)  # 1.4286em of 14px
        self.assertEqual(button["props"]["padding"], (10.0, 16.0, 10.0, 16.0))
        self.assertNotIn("gap", button["props"])  # one child: no gap to show
        self.assertEqual(button["variant"], {"variant": "primary", "icon": False})
        self.assertEqual([e["parent"] for e in els], [None, 0, 0, 0, 3])

    def test_the_facts_read_one_element_a_line_with_its_values(self):
        text = dm.facts_text(dm.elements(NODE)).splitlines()
        self.assertEqual(text[0], "Resume card [FRAME 7:21]: fill #FFFFFF; stroke #E5E7EB 1px; radius 12px; padding 24px; gap 16px; laid out as a column")
        self.assertEqual(text[1], '  Title [TEXT 7:22] "Upload your resume": font Inter; size 18px; weight 600; line height 28px; colour #111827')
        self.assertIn("padding 10px 16px; laid out as a row; variant variant=primary, icon=False", text[3])

    def test_a_stroke_in_framelinks_older_shape_an_image_fill_and_a_spread_gap_are_read_as_they_are(self):
        old = {"nodes": [{"id": "1:1", "name": "Box", "type": "FRAME", "strokes": {"colors": ["rgba(0, 0, 0, 0.5)"], "strokeWeight": "2px"},
                          "fills": [{"type": "IMAGE", "imageRef": "x"}], "layout": {"mode": "row", "gap": "8px", "justifyContent": "space-between"},
                          "children": [{"id": "1:2", "type": "TEXT", "text": "a"}, {"id": "1:3", "type": "TEXT", "text": "b"}]}]}
        self.assertEqual(dm.elements(old)[0]["props"], {"stroke": (0, 0, 0, 0.5), "strokeWeight": (2.0,) * 4})  # no colour to hold an image to, no gap in space-between
        self.assertEqual(dm.show_colour((0, 0, 0, 0.5)), "rgba(0, 0, 0, 0.5)")
        self.assertEqual(dm.sides("16px 24px 8px"), (16.0, 24.0, 8.0, 24.0))

    def test_a_document_as_framelink_sends_it_reads_the_same_once_self_contained(self):
        """Framelink keeps layout, fills, strokes and text styles in globalVars and names them on the node; task_docs inlines them first."""
        raw = {"nodes": [{"id": "7:21", "name": "Card", "type": "FRAME", "fills": "fill_A1", "strokes": "stroke_B2", "layout": "layout_C3", "borderRadius": "12px",
                          "children": [{"id": "7:22", "name": "Title", "type": "TEXT", "text": "Hi", "textStyle": "style_D4", "fills": "fill_E5"}]}],
               "globalVars": {"styles": {"fill_A1": ["#FFFFFF"], "stroke_B2": {"colors": ["#E5E7EB"], "strokeWeight": "1px"}, "fill_E5": ["#111827"],
                                         "layout_C3": {"mode": "column", "padding": "24px 16px"}, "style_D4": {"fontFamily": "Inter", "fontSize": 18, "fontWeight": 600, "lineHeight": "1.5em"}}}}
        dm.fc_figmanodes.self_contained([raw])
        card, title = dm.elements(raw)
        self.assertEqual(card["props"], {"fill": (255, 255, 255, 1.0), "stroke": (229, 231, 235, 1.0), "strokeWeight": (1.0,) * 4, "radius": (12.0,) * 4, "padding": (24.0, 16.0, 24.0, 16.0)})
        self.assertEqual(title["props"], {"font": "Inter", "size": 18.0, "weight": 600, "lineHeight": 27.0, "colour": (17, 24, 39, 1.0)})

    def test_a_criterion_with_a_hex_or_a_px_value_is_a_measured_one(self):
        for text, measured in (("The card has a #FFFFFF fill and a 1px border", True), ("Padding is 24 px", True), ("Empty shows 'Upload your resume'", False),
                               ("Shows 5 items", False)):
            with self.subTest(text):
                self.assertEqual(bool(dm.MEASURED.search(text)), measured)


class Compare(unittest.TestCase):
    def test_a_build_with_the_nodes_values_matches_every_property(self):
        rows, stats = dm.compare(dm.elements(NODE), AS_DESIGNED)
        self.assertEqual(rows, [])
        self.assertEqual((stats["matched"], stats["compared"], stats["texts"]), (26, 26, [3, 3]))

    def test_the_bare_card_of_496_fails_on_its_wrapper_its_type_and_its_button(self):
        """#496: a bare div, default type and an outline button passed verify; measured, each is a row."""
        bare = page({}, {"fs": 24, "fw": 700}, {}, {"bw": [1, 1, 1, 1], "bc": [37, 99, 235, 1], "color": [37, 99, 235, 1], "fs": 13.33, "pad": [1, 6, 1, 6]})
        rows, stats = dm.compare(dm.elements(NODE), bare)
        got = {(r["element"].split(" [")[0], r["property"]): (r["design"], r["built"]) for r in rows}
        self.assertEqual(got[("Resume card", "fill")], ("#FFFFFF", "none"))
        self.assertEqual(got[("Resume card", "stroke")], ("#E5E7EB 1px", "none"))
        self.assertEqual(got[("Resume card", "padding")], ("24px", "0px"))
        self.assertEqual(got[("Button", "fill")], ("#2563EB", "none"))  # the outline button: its border is not the design's fill
        self.assertEqual(got[("Title", "weight")], ("600", "700"))
        self.assertEqual(got[("Title", "line height")], ("28px", "normal"))
        self.assertTrue(all(r["at"] for r in rows))
        self.assertEqual(stats["texts"], [3, 3])
        self.assertLess(stats["matched"], 10)

    def test_a_text_that_is_not_on_the_page_is_a_row_and_its_type_is_not_guessed(self):
        gone = json.loads(json.dumps(AS_DESIGNED))
        gone["els"][3]["own"] = gone["els"][3]["text"] = "Something else"
        rows, stats = dm.compare(dm.elements(NODE), gone)
        self.assertEqual([(r["element"], r["property"], r["built"]) for r in rows], [("Hint [TEXT 7:23]", "text", "not on the page")])
        self.assertEqual(stats["texts"], [2, 3])

    def test_one_built_element_stands_for_a_frame_and_the_wrapper_frame_around_it(self):
        """A design nests frames a build flattens: the card's fill and its content frame's padding on one div are no mismatch."""
        nested = {"nodes": [{"id": "1:1", "name": "Card", "type": "FRAME", "fills": ["#FFFFFF"], "children": [
            {"id": "1:2", "name": "Content", "type": "FRAME", "layout": {"mode": "column", "padding": "24px"}, "children": [
                {"id": "1:3", "name": "Title", "type": "TEXT", "text": "Hello"}]}]}]}
        dom = {"url": "u", "els": [built(-1, "body", text="Hello"), built(0, "div", text="Hello", bg=[255, 255, 255, 1], pad=[24, 24, 24, 24]), built(1, "h2", "Hello")]}
        self.assertEqual(dm.compare(dm.elements(nested), dom)[0], [])

    def test_a_ring_or_an_outline_of_the_designs_colour_is_its_stroke_and_margins_are_its_gap(self):
        ringed = json.loads(json.dumps(AS_DESIGNED))
        ringed["els"][1].update(bw=[0, 0, 0, 0], bc=None, rings=[[1, [229, 231, 235, 1]]])
        self.assertEqual(dm.compare(dm.elements(NODE), ringed)[0], [])
        tight = json.loads(json.dumps(AS_DESIGNED))
        tight["els"][3]["rect"] = [65, 93, 370, 20]  # the hint right under the title: no gap, then 32px to the button
        rows = dm.compare(dm.elements(NODE), tight)[0]
        self.assertEqual([(r["property"], r["design"], r["built"]) for r in rows], [("gap", "16px", "0px, 32px")])

    def test_a_frame_with_no_text_under_it_is_named_as_not_measured(self):
        icon = {"nodes": [{"id": "1:1", "name": "Row", "type": "FRAME", "children": [
            {"id": "1:2", "name": "Icon box", "type": "FRAME", "fills": ["#FF0000"]}, {"id": "1:3", "name": "T", "type": "TEXT", "text": "Hello"}]}]}
        dom = {"url": "u", "els": [built(-1, "body", text="Hello"), built(0, "span", "Hello")]}
        rows, stats = dm.compare(dm.elements(icon), dom)
        self.assertEqual((rows, stats["unmeasured"]), ([], ["Icon box [FRAME 1:2]"]))


class TaskDocs(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        self.proj.mkdir(parents=True)
        (self.proj / "linear.json").write_text(json.dumps({"figma": "StoredKey1234"}))
        self.meta = {"id": "012", "figma_nodes": ["7-21"]}

    def test_the_nodes_run_fetched_are_kept_and_verify_reads_them_without_a_fetch(self):
        with mock.patch.object(dm.fc_framelink, "framelink_fetch", return_value=json.dumps(NODE)) as fetch:
            first = dm.task_docs(self.proj, self.meta, fresh=True)
            again = dm.task_docs(self.proj, self.meta)
            dm.task_docs(self.proj, self.meta, fresh=True)
        self.assertEqual(fetch.call_count, 2)  # the kept copy answered the read in between
        self.assertEqual(fetch.call_args_list[0], mock.call("StoredKey1234", node="7:21"))
        self.assertEqual(first[0][0], "7-21")
        self.assertEqual(again[0][1]["nodes"][0]["id"], "7:21")
        self.assertTrue(dm.node_path(self.proj, "StoredKey1234", "7-21").exists())

    def test_a_task_built_before_the_copy_was_kept_is_fetched_once(self):
        with mock.patch.object(dm.fc_framelink, "framelink_fetch", return_value=json.dumps(NODE)) as fetch:
            dm.task_docs(self.proj, self.meta)
            dm.task_docs(self.proj, self.meta)
        self.assertEqual(fetch.call_count, 1)

    def test_no_file_and_a_failed_fetch_say_which(self):
        (self.proj / "linear.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "figma_nodes needs a Figma file"):
            dm.task_docs(self.proj, self.meta)
        with mock.patch.object(dm.fc_framelink, "framelink_fetch", side_effect=RuntimeError("not found")), self.assertRaisesRegex(RuntimeError, "TaskKey123456 is the task's figma: line: not found"):
            dm.task_docs(self.proj, {**self.meta, "figma": "TaskKey123456"})


class Match(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "p"
        path = dm.node_path(self.proj, "FileKey123456", "7-21")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(NODE))
        self.meta = {"id": "012", "figma_nodes": ["7-21"], "figma": "FileKey123456"}

    def match(self, doms, port="9333", **kw):
        a = SimpleNamespace(**{"only": "012", "node": None, "figma": None, "page": None, "root": None, **kw})
        out, code = io.StringIO(), 0
        with mock.patch.dict(os.environ, {"BP_PORT": port} if port else {}), mock.patch.object(dm.fc_tasks, "load_tasks", return_value=[self.meta]), \
                mock.patch.object(dm, "pages", return_value=[str(i) for i in range(len(doms))]), mock.patch.object(dm, "measure", side_effect=lambda p, root=None: doms[int(p)]), \
                contextlib.redirect_stdout(out):
            if not port:
                os.environ.pop("BP_PORT", None)
            try:
                dm.figma_match(a, self.proj)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_a_built_page_that_matches_passes_and_says_what_it_did_not_compare(self):
        code, out = self.match([AS_DESIGNED])
        self.assertEqual(code, 0)
        self.assertIn("figma match node 7-21 on http://x/resume: ok  26 of 26 properties match", out)
        self.assertIn("sizes, positions, effects, images and icons are not compared", out)

    def test_a_mismatch_fails_with_the_table_and_keeps_the_whole_of_it(self):
        code, out = self.match([page({}, {}, {}, {})])
        self.assertEqual(code, 1)
        self.assertRegex(out, r"Resume card \[FRAME 7:21\]\s+fill\s+#FFFFFF\s+none\s+<div>")
        self.assertIn("... and ", out)  # the tail an e2e case keeps holds the count and the file, not a table cut at its head
        self.assertRegex(out, r"FAIL  \d+ of 26 properties match, \d+ differ")
        kept = (self.proj / "results" / "design" / "012.match.txt").read_text()
        self.assertGreater(len(kept.splitlines()), dm.ROWS_SHOWN + 2)
        self.assertNotIn("... and ", kept)

    def test_a_page_without_the_nodes_words_is_the_feature_missing_as_prove_reads_it(self):
        code, out = self.match([{"url": "http://x/", "els": [built(-1, "body", text="Coming soon"), built(0, "h1", "Coming soon")]}])
        self.assertEqual(code, 1)
        self.assertRegex(out, r"(?m)^ASSERT FAILED: 0 of the 3 texts of node 7-21 are on the page http://x/")

    def test_of_several_open_pages_the_one_that_shows_the_node_is_measured(self):
        code, out = self.match([{"url": "http://x/other", "els": [built(-1, "body", text="Other")]}, AS_DESIGNED])
        self.assertEqual(code, 0)
        self.assertIn("on http://x/resume: ok", out)

    def test_it_needs_a_browser_a_task_with_nodes_and_flags_of_its_own(self):
        self.assertIn("set BP_PORT", self.match([AS_DESIGNED], port="")[0])
        self.meta = {"id": "012", "figma_nodes": []}
        self.assertIn("task 012 has no figma_nodes: line", self.match([AS_DESIGNED])[0])
        for flag in ("page", "root"):
            with self.subTest(flag), self.assertRaises(SystemExit) as e:
                fc.fc_figma.refuse_unused_flags(SimpleNamespace(action="check", **{flag: "x"}))
            self.assertIn(f"figma check does not use --{flag}; it is for figma match", str(e.exception.code))
        fc.fc_figma.refuse_unused_flags(SimpleNamespace(action="match", only="012", node="7-21", page="x", root="#app", figma="k"))


def chromium():
    """Playwright's newest Chromium in the account's real cache (a test's HOME is a temp one), or None."""
    cache = Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library" / "Caches" / "ms-playwright"
    builds = sorted((d for d in cache.glob("chromium-*") if d.name[9:].isdigit()), key=lambda d: int(d.name[9:]), reverse=True) if cache.exists() else []
    for b in builds:
        for exe in b.glob("chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"):
            return str(exe)
    return os.environ.get("BP_CHROME")


@unittest.skipUnless(chromium(), "Playwright Chromium not installed")
class Browser(unittest.TestCase):
    """The measuring script against a real page: a colour in oklch, a rem, margins for a gap, a transparent outline button."""
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        (cls.dir / "bare.html").write_text(BARE)
        (cls.dir / "styled.html").write_text(STYLED)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.env = {**os.environ, "BP_PORT": str(s.getsockname()[1]), "BP_CHROME": chromium()}
        cls.bp = lambda args, timeout=60: subprocess.run(["node", str(ROOT / "bp" / "bin" / "bp.mjs"), *args], capture_output=True, text=True, timeout=timeout, env=cls.env)
        up = cls.bp(["launch", f"file://{cls.dir}/bare.html", "--headless", "--profile", str(cls.dir / "profile")])
        if up.returncode:
            raise unittest.SkipTest(f"bp could not launch Chromium: {(up.stderr or up.stdout)[-200:]}")
        cls.bp(["open", f"file://{cls.dir}/styled.html", "--wait", "1500"])

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["pkill", "-f", f"remote-debugging-port={cls.env['BP_PORT']}"], capture_output=True)

    def measured(self, page):
        run = lambda args, timeout=60: type(self).bp(args, timeout).stdout
        with mock.patch.object(dm, "bp", side_effect=run):
            return dm.compare(dm.elements(NODE), dm.measure(page))

    def test_the_styled_page_matches_and_the_bare_page_does_not(self):
        rows, stats = self.measured("styled.html")
        self.assertEqual((rows, stats["matched"], stats["compared"]), ([], 26, 26))
        rows, stats = self.measured("bare.html")
        got = {(r["element"].split(" [")[0], r["property"]): r["built"] for r in rows}
        self.assertEqual((got[("Resume card", "fill")], got[("Resume card", "padding")], got[("Button", "fill")]), ("none", "0px", "none"))
        self.assertEqual(stats["texts"], [3, 3])
        self.assertGreater(len(rows), 15)

    def test_a_root_that_is_not_on_the_page_is_an_error_and_not_an_empty_match(self):
        run = lambda args, timeout=60: type(self).bp(args, timeout).stdout
        with mock.patch.object(dm, "bp", side_effect=run), self.assertRaisesRegex(RuntimeError, "no element matches #nope"):
            dm.measure("styled.html", "#nope")


if __name__ == "__main__":
    unittest.main()
