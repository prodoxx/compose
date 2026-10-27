import hermetic  # first: no test sees the real home
import argparse, base64, http.server, importlib.machinery, io, importlib.util, json, os, tempfile, threading, unittest
from pathlib import Path
from unittest import mock
import sys
os.environ["HERDR_ENV"] = ""

_spec = importlib.util.spec_from_loader("fc", importlib.machinery.SourceFileLoader("fc", str(Path(__file__).resolve().parents[1] / "bin" / "compose.py")))
fc = importlib.util.module_from_spec(_spec)
for _m in [m for m in sys.modules if m.startswith("fc_") and m not in ("fc_cost", "fc_mcp", "fc_sessions")]: del sys.modules[_m]
_spec.loader.exec_module(fc)


class Frame0(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.wt = Path(tempfile.mkdtemp()).resolve()
        self.proj = self.wt / ".compose" / "shop"
        self.proj.mkdir(parents=True)

    def test_port_is_stable_per_project_and_in_range(self):
        shop = Path("/compose-test/a/.compose/shop")
        self.assertEqual(fc.fc_frame0.frame0_port(shop), fc.fc_frame0.frame0_port(shop))
        self.assertIn(fc.fc_frame0.frame0_port(shop), fc.fc_frame0.FRAME0_PORTS)
        self.assertNotEqual(fc.fc_frame0.frame0_port(shop), fc.fc_frame0.frame0_port(Path("/compose-test/a/.compose/cart")))

    def test_up_launches_the_app_and_registers_no_mcp_server(self):
        app = self.wt / "Frame0.app"
        app.mkdir()
        with mock.patch.object(fc.fc_frame0, "FRAME0_APP", app), mock.patch.object(fc.fc_frame0, "frame0_pid", return_value=None), \
                mock.patch.object(fc.fc_core, "port_listening", side_effect=[False, True]), mock.patch.object(fc.fc_core, "sh") as sh, mock.patch("builtins.print"):
            self.assertEqual(fc.fc_frame0.frame0_up(self.proj), (fc.fc_frame0.frame0_port(self.proj), True))
        self.assertEqual([c.args[0][:2] for c in sh.call_args_list], [["open", "-n"]])  # no `claude mcp add`: a registered server starts with every session

    def test_pid_is_the_main_process_of_this_projects_instance(self):
        app = fc.fc_frame0.FRAME0_APP / "Contents" / "MacOS" / "Frame0"
        flag = f"--user-data-dir={self.proj / 'frame0'}"
        ps = "\n".join([f"  101 {app} --user-data-dir=/elsewhere/frame0",
                        f"  202 {fc.fc_frame0.FRAME0_APP}/Contents/Frameworks/Frame0 Helper.app/Contents/MacOS/Frame0 Helper --type=gpu {flag}",
                        f"  303 {app} {flag}"])
        with mock.patch.object(fc.fc_core, "sh", return_value=mock.Mock(stdout=ps)):
            self.assertEqual(fc.fc_frame0.frame0_pid(self.proj), 303)
        with mock.patch.object(fc.fc_core, "sh", return_value=mock.Mock(stdout=ps.rsplit("\n", 1)[0])):
            self.assertIsNone(fc.fc_frame0.frame0_pid(self.proj))

    def test_save_and_down_do_nothing_without_an_instance(self):
        with mock.patch.object(fc.fc_frame0, "frame0_pid", return_value=None), mock.patch.object(fc.fc_frame0, "frame0_command") as cmd:
            self.assertFalse(fc.fc_frame0.frame0_save(self.proj))
            self.assertFalse(fc.fc_frame0.frame0_down(self.proj))
        cmd.assert_not_called()

    def test_an_app_that_stays_silent_or_is_gone_says_what_to_check(self):
        with mock.patch.object(fc.fc_frame0.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(RuntimeError, "dialog is probably open"):
                fc.fc_frame0.frame0_command(58411, "file:save")
        with mock.patch.object(fc.fc_frame0.urllib.request, "urlopen", side_effect=fc.fc_frame0.urllib.error.URLError(ConnectionRefusedError())):
            with self.assertRaisesRegex(RuntimeError, "not listening on port 58411"):
                fc.fc_frame0.frame0_command(58411, "file:save")


class DrawApp(http.server.BaseHTTPRequestHandler):
    """Frame0's API for a draw: hands out ids, keeps every command it ran, and fails the command named in `fail`."""
    calls, tokens, fail = [], 0, None

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/request_api_token":
            DrawApp.tokens += 1
            out = {"success": True, "data": "tok"}
        else:
            cmd, args = body["command"], body["args"]
            DrawApp.calls.append((cmd, args))
            if cmd == DrawApp.fail:
                out = {"success": False, "error": "no such icon"}
            elif cmd == "page:add":
                out = {"success": True, "data": {"id": "page1", "name": args["pageProps"]["name"]}}
            elif cmd == "shape:get-shape":
                out = {"success": True, "data": {"id": args["shapeId"], "left": 16, "top": 40.4, "width": 120.6, "height": 24}}
            elif cmd == "edit:duplicate":
                out = {"success": True, "data": ["dup1"]}
            else:
                out = {"success": True, "data": f"s{len(DrawApp.calls)}"}
        data = json.dumps(out).encode()
        self.send_response(200), self.send_header("Content-Length", str(len(data))), self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Frame0Draw(unittest.TestCase):
    def setUp(self):
        DrawApp.calls, DrawApp.tokens, DrawApp.fail = [], 0, None
        self.app = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DrawApp)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.shutdown), self.addCleanup(self.app.server_close)
        self.proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
        self.proj.mkdir(parents=True)

    def draw(self, ops, running=True):
        with mock.patch.object(fc.fc_frame0, "frame0_pid", return_value=4242 if running else None), \
                mock.patch.object(fc.fc_frame0, "frame0_port", return_value=self.app.server_address[1]):
            return fc.fc_frame0.frame0_draw(self.proj, ops)

    def sent(self):
        return [c for c in DrawApp.calls if c[0] != "shape:get-shape"]

    def test_runs_the_tools_as_the_mcp_server_does_on_one_token_and_saves(self):
        lines = self.draw([
            {"tool": "add_page", "as": "p", "name": "Checkout"},
            {"tool": "create_frame", "as": "f", "frameType": "browser", "name": "Cart"},
            {"tool": "create_rectangle", "name": "card", "parentId": "$f", "left": 0, "top": 0, "width": 80, "height": 40},
            {"tool": "create_text", "as": "t", "name": "title", "parentId": "$f", "left": 16, "top": 40, "text": "Cart", "type": "paragraph"},
            {"tool": "move_shape", "shapeId": "$t", "dx": 4, "dy": 0},
            {"tool": "duplicate_shape", "shapeId": "$t", "dy": 30},
            {"tool": "set_link", "shapeId": "$t", "linkType": "page", "pageId": "$p"},
        ])
        sent = self.sent()
        self.assertEqual(DrawApp.tokens, 1)
        self.assertEqual([c[0] for c in sent], ["page:add", "shape:create-shape-from-library-by-query", "view:fit-to-screen", "shape:create-shape",
                                                "shape:create-shape", "shape:move", "edit:duplicate", "shape:set-link", "file:save"])
        frame = sent[1][1]
        self.assertEqual(frame, {"query": "Browser&@Frame", "shapeProps": {"name": "Cart", "left": 0, "top": -76, "width": 800, "height": 676,
                                                                           "fillColor": "#ffffff"}, "convertColors": True})
        rect = sent[3][1]
        self.assertEqual(rect["parentId"], "s2")  # $f: the frame's id
        self.assertEqual(rect["shapeProps"], {"name": "card", "left": 0, "top": 0, "width": 80, "height": 40, "fillColor": "#ffffff",
                                              "strokeColor": "#000000", "corners": [0, 0, 0, 0]})
        text = sent[4][1]
        self.assertEqual((text["type"], text["shapeProps"]["wordWrap"], text["shapeProps"]["fontColor"]), ("Text", True, "#000000"))
        self.assertNotIn("width", text["shapeProps"])  # unset stays unset, as the server trims it
        tid = DrawApp.calls[[c[0] for c in DrawApp.calls].index("shape:move")][1]["shapeId"]
        self.assertEqual(sent[7][1], {"shapeId": tid, "linkProps": {"linkType": "page", "pageId": "page1"}})
        self.assertEqual(sent[8], ("file:save", {"allowSaveAs": False}))
        self.assertEqual(lines[0], "  1 p add_page page1")
        self.assertEqual(lines[3], f"  4 t create_text {tid} left=16 top=40 width=121 height=24")
        self.assertEqual(lines[5], "  6 - duplicate_shape dup1 left=16 top=40 width=121 height=24")
        self.assertFalse([c for c in DrawApp.calls if None in c[1].values()])  # the app refuses null where the server sends nothing
        self.assertEqual(sent[6][1], {"shapeIdArray": [tid], "dy": 30})

    def test_a_bad_list_draws_nothing_and_names_every_problem(self):
        with self.assertRaises(RuntimeError) as e:
            self.draw([{"tool": "create_txt", "name": "x"},
                       {"tool": "create_text", "name": "x", "left": 0, "txt": "hi"},
                       {"tool": "move_shape", "shapeId": "$later", "dx": 1, "dy": 1},
                       {"tool": "create_text", "as": "later", "name": "y", "left": 0, "top": 0, "text": "y"}])
        msg = str(e.exception)
        self.assertIn("nothing drawn", msg)
        self.assertIn("op 1: tool is one of add_page", msg)
        self.assertIn("op 2 (create_text): missing text, top", msg)
        self.assertIn("op 2 (create_text): unknown txt", msg)
        self.assertIn("op 3 (move_shape): $later names no earlier op's `as`", msg)
        self.assertEqual((DrawApp.tokens, DrawApp.calls), (0, []))
        for ops in ([], {"tool": "add_page"}):
            with self.subTest(ops=ops), self.assertRaisesRegex(RuntimeError, "JSON array of ops"):
                self.draw(ops)

    def test_a_dollar_in_text_is_text(self):
        self.draw([{"tool": "create_text", "as": "price", "name": "price", "left": 0, "top": 0, "text": "$price"}])
        self.assertEqual(self.sent()[0][1]["shapeProps"]["text"], "$price")

    def test_a_failing_op_names_itself_keeps_what_was_drawn_and_saves(self):
        DrawApp.fail = "shape:create-icon"
        with self.assertRaises(RuntimeError) as e:
            self.draw([{"tool": "create_text", "name": "a", "left": 0, "top": 0, "text": "a"},
                       {"tool": "create_icon", "name": "nope", "left": 0, "top": 0, "size": "small"},
                       {"tool": "create_text", "name": "b", "left": 0, "top": 0, "text": "b"}])
        msg = str(e.exception).splitlines()
        self.assertTrue(msg[0].startswith("  1 - create_text s1"))
        self.assertEqual(msg[1], "op 2 (create_icon) failed: frame0 /execute_command: no such icon")
        self.assertEqual(msg[2], "ops 1-1 are drawn and saved; send op 2 on, with the ids above in place of its $names")
        self.assertEqual([c[0] for c in self.sent()], ["shape:create-shape", "shape:create-icon", "file:save"])

    def test_a_stopped_app_is_named(self):
        with self.assertRaisesRegex(RuntimeError, "shop's Frame0 is not running; `frame0 up` starts it"):
            self.draw([{"tool": "add_page", "name": "x"}], running=False)

    def test_the_command_reads_the_list_from_a_file_or_stdin(self):
        ops = [{"tool": "add_page", "name": "x"}]
        f = self.proj / "ops.json"
        f.write_text(json.dumps(ops))
        for file, stdin in ((str(f), ""), (None, json.dumps(ops)), ("-", json.dumps(ops))):
            with self.subTest(file=file), mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), \
                    mock.patch.object(fc.fc_frame0, "frame0_draw", return_value=["  1 - add_page page1"]) as draw, \
                    mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch("builtins.print") as out:
                fc.fc_frame0.cmd_frame0(argparse.Namespace(action="draw", file=file))
            self.assertEqual(draw.call_args.args[1], ops)
            self.assertEqual(out.call_args.args[0], f"drew 1 ops, saved {self.proj / 'design.f0'}")
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch("sys.stdin", io.StringIO("not json")):
            with self.assertRaisesRegex(SystemExit, "the draw list is not JSON"):
                fc.fc_frame0.cmd_frame0(argparse.Namespace(action="draw", file=None))

    def test_a_page_added_after_another_lands_right_behind_it(self):
        self.draw([{"tool": "add_page", "name": "05.4 New", "after": "pg5"}, {"tool": "add_page", "name": "Plain"}])
        self.assertEqual([c[0] for c in self.sent()[:3]], ["page:set-current-page", "page:add", "page:add"])
        self.assertEqual(self.sent()[0][1], {"pageId": "pg5"})

    def test_after_is_a_page_id_and_a_name_made_earlier_stands_for_its_id(self):
        self.draw([{"tool": "add_page", "as": "p", "name": "05.4 New"}, {"tool": "add_page", "name": "05.5 Next", "after": "$p"}])
        self.assertEqual(self.sent()[1], ("page:set-current-page", {"pageId": "page1"}))


def f0_shape(sid, name="box", reference=None, children=None):
    return {"id": sid, "type": "Text", "name": name, **({"reference": reference} if reference else {}), **({"children": children} if children else {})}


def f0_page(pid, name, shapes=1, links=()):
    kids = [f0_shape(f"{pid}-{i}") for i in range(shapes)] + [f0_shape(f"{pid}-link{i}", "btn", target) for i, target in enumerate(links)]
    return {"id": pid, "type": "Page", "parent": "doc", "name": name, "enable": False, "pageOrigin": [0, 0], "children": kids}


def f0_doc(*pages):
    return {"id": "doc", "type": "Doc", "parent": None, "children": list(pages)}


class Frame0Pages(unittest.TestCase):
    """The planning half: what a drawing's pages, order and links say, and the ops a command makes of them."""

    def test_pages_count_shapes_at_every_depth_and_the_table_can_show_only_the_empty(self):
        deep = {**f0_page("b", "B", 0), "children": [f0_shape("f", children=[f0_shape("g"), f0_shape("h")])]}
        pages = fc.fc_f0pages.frame0_pages(f0_doc(f0_page("a", "A"), deep, f0_page("c", "C", 0)))
        self.assertEqual([(p["n"], p["shapes"]) for p in pages], [(1, 1), (2, 3), (3, 0)])
        self.assertEqual(len(fc.fc_f0pages.frame0_table(pages)), 4)
        header, row = fc.fc_f0pages.frame0_table(pages, only_empty=True)
        self.assertIn("shapes", header)
        self.assertRegex(row, r"^\s+3 c +empty  C$")

    def test_a_page_is_named_by_id_name_or_the_start_of_its_name_and_never_guessed(self):
        doc = f0_doc(f0_page("a", "05 Today - flow"), f0_page("b", "05.1 Today"), f0_page("c", "06 Detail"))
        ref = lambda r: fc.fc_f0pages.frame0_page_ref(doc, r)["id"]
        self.assertEqual((ref("b"), ref("05.1 Today"), ref("05 today - flow"), ref("06")), ("b", "b", "a", "c"))
        with self.assertRaisesRegex(ValueError, "'05' names 2 pages"):
            ref("05")
        with self.assertRaisesRegex(ValueError, "no page 'nope'"):
            ref("nope")

    def test_a_reference_to_a_page_is_a_link_and_one_to_a_shape_is_not(self):
        doc = f0_doc(f0_page("a", "A", links=["b", "gone"]), f0_page("b", "B", 1, links=["a-0"]))
        self.assertEqual([(link["on"], link["target"], link["ok"]) for link in fc.fc_f0pages.frame0_links(doc)], [("a", "b", True), ("a", "gone", False)])

    def test_the_lint_names_empty_pages_pages_out_of_flow_order_and_links_to_gone_pages(self):
        doc = f0_doc(f0_page("p0", "00 Read me"), f0_page("p1", "01.1 Welcome"), f0_page("pa", "A First run - journey"), f0_page("p4a", "04.10 Offer"),
                     f0_page("p41", "04.7 Restore"), f0_page("p42", "04.8 In"), f0_page("p5", "05.1 Today", links=["gone", "gone"]),
                     f0_page("e", "06 Empty - flow", 0), f0_page("p32", "03.2 Added late"))
        problems = fc.fc_f0pages.frame0_lint(doc)
        self.assertEqual(len(problems), 4, problems)
        self.assertEqual(problems[0], "1 empty pages: 8 '06 Empty - flow'; draw them or delete them with `compose frame0 delete --empty`")
        self.assertEqual(problems[1], "page 4 '04.10 Offer' is out of flow order; it belongs behind '04.8 In': `compose frame0 move --page p4a --after p42`")
        self.assertEqual(problems[2], "page 9 '03.2 Added late' is out of flow order; it belongs behind '01.1 Welcome': `compose frame0 move --page p32 --after p1`")
        self.assertRegex(problems[3], r"^2 links to a page that is gone \(gone\), on '05.1 Today': `compose frame0 links --retarget gone=<page>`")

    def test_a_drawing_in_order_with_links_that_land_is_clean(self):
        doc = f0_doc(f0_page("a", "01.1 A", links=["c"]), f0_page("b", "A first run - journey"), f0_page("c", "01.2b B"), f0_page("d", "01.2f C"), f0_page("e", "02 D - flow"))
        self.assertEqual(fc.fc_f0pages.frame0_lint(doc), [])

    def test_a_page_at_the_front_that_belongs_nowhere_earlier_is_told_to_go_first(self):
        doc = f0_doc(f0_page("late", "09.1 Z"), f0_page("a", "01.1 A"), f0_page("b", "01.2 B"), f0_page("c", "01.3 C"))
        self.assertEqual(fc.fc_f0pages.frame0_lint(doc), ["page 1 '09.1 Z' is out of flow order; it belongs behind '01.3 C': `compose frame0 move --page late --after c`"])
        doc = f0_doc(f0_page("a", "05.1 A"), f0_page("b", "05.2 B"), f0_page("first", "01.1 F"))
        self.assertIn("it belongs first: `compose frame0 move --page first --first`", fc.fc_f0pages.frame0_lint(doc)[0])

    def test_a_page_added_at_the_end_is_one_finding_not_one_for_every_page_it_jumped(self):
        pages = [f0_page(f"p{n}", f"{n:02d}.1 Flow {n}") for n in range(1, 9)] + [f0_page("new", "03.2 Added")]
        found = fc.fc_f0pages.frame0_misplaced(fc.fc_f0pages.frame0_pages(f0_doc(*pages)))
        self.assertEqual([(p["name"], behind["name"]) for p, behind in found], [("03.2 Added", "03.1 Flow 3")])

    def test_long_lists_are_cut_to_what_fits_on_a_screen(self):
        pages = [f0_page("keep", "01.1 A")] + [f0_page(f"e{n}", f"13.{n} skeleton", 0) for n in range(25)]
        (line,) = fc.fc_f0pages.frame0_lint(f0_doc(*pages))
        self.assertTrue(line.startswith("25 empty pages: 2 '13.0 skeleton'"))
        self.assertIn(", and 5 more;", line)
        gone = [f0_page(f"p{n}", f"0{n % 9 + 1} P{n}", links=[f"g{n}"]) for n in range(23)]
        out = fc.fc_f0pages.frame0_dangling(f0_doc(*gone))
        self.assertEqual((len(out), out[-1]), (21, "and links to 3 more pages that are gone"))

    def test_a_move_gives_each_page_that_changes_place_one_op_in_the_order_given(self):
        doc = f0_doc(*(f0_page(c, c.upper()) for c in "abcdefg"))
        plan = lambda refs, after: fc.fc_f0pages.frame0_plan_move(doc, refs, after)
        self.assertEqual(plan(["g"], "b"), [("move", "g", 2)])
        self.assertEqual(plan(["f", "g"], None), [("move", "f", 0), ("move", "g", 1)])
        self.assertEqual(plan(["c", "b"], "d"), [("move", "c", 3), ("move", "b", 3)])  # a b c d ... -> a b d c ... -> a d c b ...
        self.assertEqual(plan(["b", "b"], "a"), [])
        with self.assertRaisesRegex(ValueError, "--after names a page that is being moved"):
            plan(["a", "b"], "b")

    def test_delete_clears_links_that_pointed_at_the_pages_and_keeps_one_page(self):
        doc = f0_doc(f0_page("a", "A", links=["b"]), f0_page("b", "B", 0), f0_page("c", "C", 0))
        self.assertEqual(fc.fc_f0pages.frame0_plan_delete(doc, [], True), [("unlink", "a-link0"), ("delete", "b"), ("delete", "c")])
        self.assertEqual(fc.fc_f0pages.frame0_plan_delete(doc, ["a"], False), [("delete", "a")])
        self.assertEqual(fc.fc_f0pages.frame0_plan_delete(doc, ["a", "b"], False), [("delete", "a"), ("delete", "b")])  # a's own link goes with a
        with self.assertRaisesRegex(ValueError, "keeps at least one page"):
            fc.fc_f0pages.frame0_plan_delete(doc, ["a", "b", "c"], False)

    def test_links_are_pointed_at_another_page_or_dropped(self):
        doc = f0_doc(f0_page("a", "A", links=["gone", "b", "gone2"]), f0_page("b", "B"))
        plan = lambda retarget, drop=False: fc.fc_f0pages.frame0_plan_links(doc, retarget, drop)
        self.assertEqual(plan(["gone=B"]), [("relink", "a-link0", "b")])
        self.assertEqual(plan(["b=a"]), [("relink", "a-link1", "a")])
        self.assertEqual(plan(["gone=B"], True), [("relink", "a-link0", "b"), ("unlink", "a-link2")])
        self.assertEqual(plan([], True), [("unlink", "a-link0"), ("unlink", "a-link2")])
        for bad, msg in ((["B"], "is <old page or id>=<new page>"), (["nothing=B"], "no link points at nothing"), (["gone=nowhere"], "no page 'nowhere'")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, msg):
                plan(bad)

def f0_project(*pages):
    proj = Path(tempfile.mkdtemp()) / ".compose" / "shop"
    proj.mkdir(parents=True)
    (proj / "design.f0").write_text(json.dumps(f0_doc(*pages), separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return proj


def f0_pages_of(proj):
    return json.loads((proj / "design.f0").read_text(encoding="utf-8"))["children"]


EDIT_PAGES = (lambda: (f0_page("a", "01.1 A", links=["gone"]), f0_page("b", "01.2 B", 0), f0_page("c", "02.1 C", links=["b"]), f0_page("d", "01.3 D")))


class Frame0Edit(unittest.TestCase):
    """A change to a closed file: backed up, written whole, and nothing left behind."""

    def setUp(self):
        self.proj = f0_project(*EDIT_PAGES())
        self.design = self.proj / "design.f0"

    def edit(self, plan, **kw):
        with mock.patch.object(fc.fc_frame0, "frame0_pid", return_value=None):
            return fc.fc_f0pages.frame0_edit(self.proj, plan, **kw)

    def test_a_closed_file_is_edited_in_place_after_a_backup_and_keeps_its_format(self):
        before = self.design.read_text(encoding="utf-8")
        lines = self.edit(lambda doc: fc.fc_f0pages.frame0_plan_move(doc, ["d"], "a") + fc.fc_f0pages.frame0_plan_delete(doc, ["b"], False))
        pages = f0_pages_of(self.proj)
        self.assertEqual([p["name"] for p in pages], ["01.1 A", "01.3 D", "02.1 C"])
        self.assertNotIn("reference", pages[2]["children"][1])  # C's link to the deleted B is cleared
        self.assertEqual(pages[0]["children"][1]["reference"], "gone")  # the rest is as it was
        (bak,) = self.proj.glob("design.f0.bak-*")
        self.assertEqual(bak.read_text(encoding="utf-8"), before)
        self.assertEqual(lines[-1], f"3 changes, saved {self.design}; the file before is {bak}")
        self.assertEqual(self.design.read_text(encoding="utf-8"), json.dumps(json.loads(self.design.read_text(encoding="utf-8")), separators=(",", ":"), ensure_ascii=False))
        self.assertEqual(list(self.proj.glob(".design.f0.*.tmp")), [])

    def test_a_dry_run_and_a_plan_with_nothing_to_do_write_nothing(self):
        before = self.design.read_text(encoding="utf-8")
        lines = self.edit(lambda doc: fc.fc_f0pages.frame0_plan_delete(doc, ["b"], False), dry_run=True)
        self.assertEqual(lines, ["clear the link of 'btn' on '02.1 C' (it pointed at '01.2 B')", "delete '01.2 B' (0 shapes)", "dry run: nothing changed"])
        self.assertEqual(self.edit(lambda doc: []), ["nothing to change"])
        self.assertEqual((self.design.read_text(encoding="utf-8"), list(self.proj.glob("design.f0.bak-*"))), (before, []))

    def test_a_wrong_command_changes_nothing_and_says_why(self):
        before = self.design.read_text(encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "no page 'x'"):
            self.edit(lambda doc: fc.fc_f0pages.frame0_plan_delete(doc, ["x"], False))
        self.assertEqual((self.design.read_text(encoding="utf-8"), list(self.proj.glob("design.f0.bak-*"))), (before, []))

    def test_only_the_newest_backups_stay(self):
        for _ in range(fc.fc_f0pages.FRAME0_BACKUPS + 3):
            self.edit(lambda doc: [("relink", "c-link0", "a")])
        self.assertEqual(len(list(self.proj.glob("design.f0.bak-*"))), fc.fc_f0pages.FRAME0_BACKUPS)

    def test_an_unreadable_file_says_so(self):
        self.design.write_text("{nope")
        with self.assertRaisesRegex(RuntimeError, "design.f0 is not JSON"):
            self.edit(lambda doc: [])
        self.design.unlink()
        with self.assertRaisesRegex(RuntimeError, "cannot read .*design.f0"):
            self.edit(lambda doc: [])


class LiveApp(http.server.BaseHTTPRequestHandler):
    """Frame0's API holding a drawing: page:move, page:delete and shape:set-link change it, file:save writes it to the file."""
    doc, file, ignore_moves, fail, commands = None, None, False, None, []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        pages, out = LiveApp.doc["children"], {"success": True, "data": "tok"}
        if self.path != "/request_api_token":
            cmd, args, out["data"] = body["command"], body["args"], None
            LiveApp.commands.append(cmd)
            if cmd == LiveApp.fail:
                out = {"success": False, "error": "boom"}
            elif cmd == "page:move" and not LiveApp.ignore_moves:
                page = next(p for p in pages if p["id"] == args["pageId"])
                pages.remove(page)
                pages.insert(args["position"], page)  # the page's final place
            elif cmd == "page:delete":
                pages.remove(next(p for p in pages if p["id"] == args["pageId"]))
            elif cmd == "shape:set-link":
                shape = next(s for p in pages for s in fc.fc_f0pages.frame0_shapes(p) if s["id"] == args["shapeId"])
                shape.pop("reference", None)
                if args["linkProps"]["linkType"] == "page":
                    shape["reference"] = args["linkProps"]["pageId"]
            elif cmd == "file:save":
                Path(LiveApp.file).write_text(json.dumps(LiveApp.doc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        data = json.dumps(out).encode()
        self.send_response(200), self.send_header("Content-Length", str(len(data))), self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Frame0EditRunning(unittest.TestCase):
    """A change to a drawing the app holds open: over its API, with no restart, and read back from the file it saves."""

    def setUp(self):
        self.proj = f0_project(*EDIT_PAGES())
        self.design = self.proj / "design.f0"
        LiveApp.doc, LiveApp.file, LiveApp.ignore_moves, LiveApp.fail, LiveApp.commands = json.loads(self.design.read_text(encoding="utf-8")), str(self.design), False, None, []
        self.app = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LiveApp)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.shutdown), self.addCleanup(self.app.server_close)

    def edit(self, plan, **kw):
        with mock.patch.object(fc.fc_frame0, "frame0_pid", return_value=4242), mock.patch.object(fc.fc_frame0, "frame0_port", return_value=self.app.server_address[1]), \
                mock.patch.object(fc.fc_frame0.os, "kill") as kill:
            lines = fc.fc_f0pages.frame0_edit(self.proj, plan, **kw)
        kill.assert_not_called()  # the app stays up
        return lines

    def test_a_running_app_is_edited_over_its_api_and_never_stopped(self):
        lines = self.edit(lambda doc: fc.fc_f0pages.frame0_plan_move(doc, ["d"], "a") + fc.fc_f0pages.frame0_plan_delete(doc, ["b"], False))
        self.assertEqual(LiveApp.commands, ["file:save", "page:move", "shape:set-link", "page:delete", "file:save"])  # save first so the plan reads what is on screen, save last
        pages = f0_pages_of(self.proj)
        self.assertEqual([p["name"] for p in pages], ["01.1 A", "01.3 D", "02.1 C"])
        self.assertNotIn("reference", pages[2]["children"][1])
        (bak,) = self.proj.glob("design.f0.bak-*")
        self.assertEqual(lines[-1], f"3 changes, saved {self.design}; the file before is {bak}")

    def test_links_are_set_and_cleared_over_the_api(self):
        self.edit(lambda doc: fc.fc_f0pages.frame0_plan_links(doc, ["gone=02.1 C"], False) + [("unlink", "c-link0")])
        pages = f0_pages_of(self.proj)
        self.assertEqual(("reference" in pages[0]["children"][1], pages[0]["children"][1]["reference"] if "reference" in pages[0]["children"][1] else None), (True, "c"))
        self.assertNotIn("reference", pages[2]["children"][1])

    def test_an_app_that_ends_with_another_drawing_than_planned_is_reported_with_its_backup(self):
        LiveApp.ignore_moves = True
        with self.assertRaisesRegex(RuntimeError, r"Frame0 saved a drawing that is not what the plan makes; the file before is .*design\.f0\.bak-"):
            self.edit(lambda doc: fc.fc_f0pages.frame0_plan_move(doc, ["d"], "a"))

    def test_a_failing_op_names_itself_and_the_drawing_is_saved_anyway(self):
        LiveApp.fail = "page:delete"
        with self.assertRaisesRegex(RuntimeError, r"op 2 of 2 \(delete\) failed: frame0 /execute_command: boom"):
            self.edit(lambda doc: fc.fc_f0pages.frame0_plan_delete(doc, ["b"], False))
        self.assertEqual(LiveApp.commands[-1], "file:save")
        self.assertEqual(len(list(self.proj.glob("design.f0.bak-*"))), 1)


class Frame0Command(unittest.TestCase):
    def setUp(self):
        self.proj = f0_project(f0_page("a", "01.1 A"), f0_page("b", "01.2 B", 0), f0_page("c", "03.1 C", links=["gone"]), f0_page("d", "02.1 D"))

    def run_cmd(self, action, **kw):
        """What the command prints and, when it exits, its message."""
        ns = argparse.Namespace(**{"action": action, "file": None, "page": None, "after": None, "first": False, "empty": False, "retarget": None,
                                   "drop_dangling": False, "dry_run": False, **kw})
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_frame0, "frame0_pid", return_value=None), mock.patch("sys.stdout", out):
            try:
                fc.fc_f0pages.cmd_frame0_pages(ns)
            except SystemExit as e:
                return out.getvalue() + str(e.code)
        return out.getvalue()

    def test_the_designers_loop_lint_then_fix_what_it_names_then_lint_clean(self):
        first = self.run_cmd("lint")
        for part in ("frame0 lint: fix these before asking for a review", "1 empty pages", "'02.1 D' is out of flow order", "1 links to a page that is gone"):
            self.assertIn(part, first)
        self.assertIn("4 pages, 1 empty", self.run_cmd("pages"))
        self.assertEqual(self.run_cmd("pages", empty=True).splitlines()[1:], [f"   2 {'b':<21}  empty  01.2 B", "4 pages, 1 empty"])
        self.assertTrue(self.run_cmd("links").startswith("1 links to pages, 1 to a page that is gone"))
        self.assertIn("delete '01.2 B' (0 shapes)\n1 changes, saved", self.run_cmd("delete", empty=True))
        self.assertIn("move '02.1 D' to position 2\n1 changes, saved", self.run_cmd("move", page=["02.1 D"], after="a"))
        self.assertIn("clear the link of 'btn' on '03.1 C' (it pointed at a page that is gone)\n1 changes, saved", self.run_cmd("links", drop_dangling=True))
        self.assertEqual(self.run_cmd("lint"), "frame0 lint: 3 pages, none empty, in flow order, every link lands on a page\n")
        self.assertEqual([p["name"] for p in f0_pages_of(self.proj)], ["01.1 A", "02.1 D", "03.1 C"])

    def test_a_command_with_missing_arguments_or_an_unknown_page_says_what_it_needs(self):
        before = (self.proj / "design.f0").read_text(encoding="utf-8")
        self.assertIn("frame0 move: --page <page> (repeatable) and one of --after <page> or --first", self.run_cmd("move", page=["a"]))
        self.assertIn("frame0 move: --page <page> (repeatable) and one of --after <page> or --first", self.run_cmd("move", page=["a"], after="b", first=True))
        self.assertIn("frame0 delete: --page <page> (repeatable) or --empty", self.run_cmd("delete"))
        self.assertIn("frame0 move: no page 'zz'", self.run_cmd("move", page=["zz"], first=True))
        self.assertIn("frame0 links: no link points at nothing", self.run_cmd("links", retarget=["nothing=a"]))
        self.assertEqual((self.proj / "design.f0").read_text(encoding="utf-8"), before)

    def test_a_dry_run_shows_the_changes_and_writes_nothing(self):
        before = (self.proj / "design.f0").read_text(encoding="utf-8")
        self.assertEqual(self.run_cmd("delete", empty=True, dry_run=True), "delete '01.2 B' (0 shapes)\ndry run: nothing changed\n")
        self.assertEqual((self.proj / "design.f0").read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.proj.glob("design.f0.bak-*")), [])

    def test_a_long_change_prints_the_first_30_and_counts_the_rest(self):
        self.proj = f0_project(f0_page("keep", "01.1 A"), *(f0_page(f"e{n}", f"13.{n} skeleton", 0) for n in range(40)))
        lines = self.run_cmd("delete", empty=True).splitlines()
        self.assertEqual(len(lines), 32)
        self.assertEqual(lines[30], "... and 10 more")
        self.assertTrue(lines[31].startswith("40 changes, saved "))
        self.assertEqual([p["name"] for p in f0_pages_of(self.proj)], ["01.1 A"])

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n0123456789").decode()


class ExportApp(http.server.BaseHTTPRequestHandler):
    """Frame0's API for an export: answers `file:export-image` with a PNG (bare base64, or a data URI when `uri`), and fails the page in `fail`."""
    calls, uri, fail = [], False, None

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        out = {"success": True, "data": "tok"}
        if self.path != "/request_api_token":
            ExportApp.calls.append((body["command"], body["args"]))
            if body["args"].get("pageId") == ExportApp.fail:
                out = {"success": True, "data": ""}
            else:
                out["data"] = ("data:image/png;base64," if ExportApp.uri else "") + PNG
        data = json.dumps(out).encode()
        self.send_response(200), self.send_header("Content-Length", str(len(data))), self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Frame0Export(unittest.TestCase):
    """`frame0 export` is the one thing drawing needed the MCP server for: the running app renders a page to a PNG file the session reads or puts on a card."""

    def setUp(self):
        ExportApp.calls, ExportApp.uri, ExportApp.fail = [], False, None
        self.proj = f0_project(f0_page("a", "01.1 Cart / empty"), f0_page("b", "01.2 Pay"))
        self.app = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ExportApp)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.shutdown), self.addCleanup(self.app.server_close)

    def export(self, refs, out=None, pid=4242):
        with mock.patch.object(fc.fc_frame0, "frame0_pid", return_value=pid), mock.patch.object(fc.fc_frame0, "frame0_port", return_value=self.app.server_address[1]):
            return fc.fc_f0pages.frame0_export(self.proj, refs, out or self.proj / "exports")

    def test_each_page_becomes_a_png_named_after_it(self):
        done = self.export(["01.1", "b"])
        self.assertEqual([(p["id"], path.name) for p, path in done], [("a", "01.1-Cart-empty.png"), ("b", "01.2-Pay.png")])
        self.assertEqual([path.read_bytes()[:8] for _, path in done], [b"\x89PNG\r\n\x1a\n"] * 2)
        self.assertEqual(ExportApp.calls, [("file:save", {"allowSaveAs": False})]  # saved first, so the pages are what is on screen
                         + [("file:export-image", {"pageId": pid, "format": "image/png", "fillBackground": True}) for pid in ("a", "b")])  # what export_page_as_image sent

    def test_a_data_uri_answer_is_decoded_too(self):
        ExportApp.uri = True
        ((_, path),) = self.export(["b"], self.proj / "deep" / "er")
        self.assertEqual(path.read_bytes()[:4], b"\x89PNG")
        self.assertEqual(path.parent, self.proj / "deep" / "er")  # the directory is made

    def test_it_says_what_it_needs_and_touches_nothing_without_the_app(self):
        with self.assertRaisesRegex(RuntimeError, "--page <page> \\(repeatable\\) names what to export"):
            self.export([])
        with self.assertRaisesRegex(RuntimeError, "Frame0 is not running; `frame0 up` starts it"):
            self.export(["a"], pid=None)
        with self.assertRaisesRegex(RuntimeError, "no page 'zz'"):
            self.export(["zz"])
        ExportApp.fail = "a"
        with self.assertRaisesRegex(RuntimeError, "page '01.1 Cart / empty': the app returned no image"):
            self.export(["a"])
        self.assertEqual([c for c in ExportApp.calls if c[0] != "file:save"], [("file:export-image", {"pageId": "a", "format": "image/png", "fillBackground": True})])  # no export before the misses
        self.assertEqual(list((self.proj / "exports").glob("*.png")), [])

    def test_the_command_prints_the_files(self):
        out = io.StringIO()
        with mock.patch.object(fc.fc_core, "project_dir", return_value=self.proj), mock.patch.object(fc.fc_frame0, "frame0_pid", return_value=4242), \
                mock.patch.object(fc.fc_frame0, "frame0_port", return_value=self.app.server_address[1]), mock.patch("sys.stdout", out):
            fc.fc_f0pages.cmd_frame0_pages(argparse.Namespace(action="export", page=["b"], out=None, file=None, after=None, first=False, empty=False, retarget=None,
                                                              drop_dangling=False, dry_run=False))
        self.assertEqual(out.getvalue(), f"{self.proj / 'exports' / '01.2-Pay.png'} 18 bytes  01.2 Pay\n")


if __name__ == "__main__":
    unittest.main()
