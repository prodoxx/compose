import hermetic  # first: no test sees the real home
import json
import os
import re
import unittest
from pathlib import Path
os.environ["HERDR_ENV"] = ""

ROOT = Path(__file__).resolve().parents[1]


MOVED = "0h 0a 0e 0b 0g 0d 1 2 3 3b 4 5 6 6a 7 8".split()


def read(rel):
    return (ROOT / rel).read_text()


class CliAndTasksDocs(unittest.TestCase):
    def section(self, text, start, end):
        self.assertIn(start, text)
        body = text.split(start, 1)[1]
        self.assertIn(end, body, f"{end!r} must still follow {start!r}")
        return body.split(end, 1)[0]

    def row(self, section, cmd):
        rows = [l for l in section.splitlines() if l.startswith(f"| `{cmd}`")]
        self.assertEqual(len(rows), 1, f"one row for {cmd} in Teammates and supervision")
        return rows[0]

    def test_cli_rows_name_the_design_commands_and_flags(self):
        sec = self.section(read("docs/cli.md"), "### Teammates and supervision", "### Workspaces")
        for word in ("--figma", "--extra -", "brief"):
            self.assertIn(word, self.row(sec, "designer"))
        for word in ("--only", "--print", "--force", "design-NNN"):
            self.assertIn(word, self.row(sec, "designcrews"))
        figma = self.row(sec, "figma check\\|tree\\|judge\\|publish")
        for word in ("check --figma", "tree --only", "Framelink", "judge --only", "linear.json", "results/NNN.tree.txt", "MET", "NOT_MET", "UNCLEAR", "check --plan", "figma_caps", "publish --library", "publish: ask"):
            self.assertIn(word, figma)
        for cmd in ("crew start\\|list\\|focus\\|stop", "explore", "testcrews", "report", "watch", "teardown", "frame0 up", "guard", "rewake", "skillcheck"):
            self.assertIn(f"| `{cmd}", sec)

    def test_tasks_design_section_shows_block_and_rules(self):
        text = read("docs/tasks.md")
        sec = self.section(text, "Three more rules apply", "## Right-sized")
        self.assertIn("\n## Design tasks\n", sec)
        design = sec.split("## Design tasks", 1)[1]
        self.assertIn("```compose\n", design)
        block = design.split("```compose\n", 1)[1].split("```", 1)[0]
        self.assertRegex(block, r"(?m)^kind: design$")
        self.assertRegex(block, r"(?m)^nodes: .+/.+")
        self.assertRegex(block, r"(?m)^test: compose figma judge --project \S+ --only (\d{3}|NNN)$")
        self.assertNotRegex(block, r"(?m)^files:")
        prose = re.sub(r"(?s)````.*?````|```.*?```", "", design)
        for word in ("page", "results/", "files"):
            self.assertIn(word, prose)
        self.assertNotRegex(prose, r"[.!?] [A-Z`]", "one sentence per line")
        for kept in ("files: extension/snapshot.js", "The `model:` field", "Only an issue with the block", "## Sync"):
            self.assertIn(kept, text)

    def test_pages_have_no_em_dash(self):
        for page in ("docs/cli.md", "docs/tasks.md"):
            self.assertNotIn("\u2014", read(page), page)


class HarnessRoleDocs(unittest.TestCase):
    def setUp(self):
        import sys; sys.path.insert(0, str(ROOT / "bin"))
        import fc_harness
        self.rows = [v for v in fc_harness.HARNESSES.values() if v]
        self.assertTrue(self.rows)
        self.how, self.herdr = read("docs/how-it-works.md"), read("docs/herdr.md")
        self.roles = {l.split("|")[1].strip(): l for l in self.how.split("Eight roles")[1].split("\n\n")[1].splitlines()}

    def line(self, text, word):
        return next((l for l in text.splitlines() if word in l), "")

    def test_role_table_names_each_harness_for_the_lead_and_teammates_and_keeps_the_designer(self):
        for who in ("Team lead", "Planner"):
            row = next(l for k, l in self.roles.items() if k.startswith(who))
            self.assertRegex(row, r"\bpi\b")
            for word in ("Claude Code", *(r["name"] for r in self.rows)):
                self.assertIn(word, row, who)
        self.assertIn("any model id", next(l for k, l in self.roles.items() if "implementer" in k))
        designer = next(l for k, l in self.roles.items() if k.startswith("Designer"))
        self.assertIn("Claude teammate", designer)
        self.assertNotIn(self.rows[0]["name"], designer)

    def test_model_choices_names_codex_ids_and_the_block_and_links_both_pages(self):
        part = self.how.split("## Model choices\n", 1)[1].split("\n## ", 1)[0]
        for word in ("`codex:", "`harnesses.<name>`", "](harnesses.md", "](config.md"):
            self.assertIn(word, part)

    def test_herdr_hint_names_each_harness_and_is_neutral_about_the_session(self):
        hint = self.line(self.herdr, "hint line")
        for word in ("`claude`", "`/compose`", "`codex`", *(f"`{r['sigil']}compose" for r in self.rows)):
            self.assertIn(word, hint)
        self.assertNotIn("the Claude session you started", self.herdr)

    def test_herdr_says_codex_waits_in_the_foreground_and_new_panes_get_the_env_but_no_key_at_sign_in(self):
        self.assertRegex(self.line(self.herdr, "--timeout 240"), r"Codex.*foreground|foreground.*Codex")
        for r in self.rows:
            self.assertIn("PATH", self.line(self.herdr, r["home_env"]), r["home_env"])
        self.assertRegex(self.line(self.herdr, "sign-in screen"), r"(?i)\bno key\b")

    def test_asyncrewake_and_compaction_stay_and_say_they_are_claude_codes(self):
        for word in ("asyncRewake", "compacts at"):
            self.assertIn("Claude Code", self.line(self.herdr, word), word)

    def test_both_pages_link_harnesses_md_without_an_em_dash(self):
        for text in (self.how, self.herdr):
            self.assertIn("](harnesses.md", text)
            self.assertNotIn("\u2014", text)


class HerdrAndWorkspacesDocs(unittest.TestCase):
    def section(self, rel, title):
        return read(rel).split("## " + title + "\n", 1)[1].split("\n## ", 1)[0]

    def test_the_docs_give_the_agent_name_herdr_registers_for_a_long_slug(self):
        import sys; sys.path.insert(0, str(ROOT / "bin"))
        import fc_herdr
        lead = fc_herdr.agent_name("clean-recut-branches", "orchestrator")
        for rel in ("docs/herdr.md", "sections/0h.md"):
            self.assertIn(f"project `clean-recut-branches` has team lead `{lead}`", read(rel).lower())
            self.assertIn("`compose crew list` shows", read(rel))

    def test_roles_table_names_the_design_roles(self):
        rows = {l.split("|")[1].strip(): l for l in self.section("docs/herdr.md", "Roles").splitlines() if l.startswith("|")}
        self.assertIn("tab `designer`, agent `<slug>-designer`", rows.get("designer", ""))
        self.assertIn("Figma", rows.get("designer", ""))
        self.assertIn("Claude only", rows.get("design-NNN", ""))
        self.assertIn("tab `design NNN`", rows.get("design-NNN", ""))
        self.assertIn("screenshot per story", rows.get("design-review", ""))
        self.assertIn("tab `orchestrator`, agent `<slug>-orchestrator`", rows.get("team lead", ""))

    def test_teammate_commands_block_holds_the_design_commands(self):
        code = self.section("docs/herdr.md", "Teammate commands").split("```bash\n", 1)[1].split("```", 1)[0]
        block = [" ".join(l.split()) for l in code.splitlines()]
        for line in (
            "compose figma check --project <slug> --figma <url or key>",
            "compose designer --project <slug> | compose crew start --project <slug> --role designer --title designer --brief -",
            "compose designcrews --project <slug>   # one design-NNN builder per open design task",
            "compose crew start --project <slug> --role orchestrator --title orchestrator --brief - < brief.txt",
            "compose crew list --project <slug>",
        ):
            self.assertIn(" ".join(line.split()), block)

    def test_crew_models_name_the_design_keys(self):
        text = self.section("docs/workspaces.md", "Crew models")
        self.assertIn("`designer`", text)
        self.assertIn("Opus 5.5", text)
        self.assertIn("high", text.split("Opus 5.5", 1)[1].splitlines()[0])
        self.assertIn("`design`", text)
        self.assertIn("design-review", text)
        self.assertIn("first word", text)
        self.assertRegex(text, r"`design-003`[^\n]*`design`")
        self.assertIn("Groundwork and test-writing teammates use `groundwork`.", text)
        self.assertIn("`models.crew` picks each teammate's model by role, then `default`.", text)

    def test_claude_parallel_is_documented_as_a_second_cap_for_claude_sessions(self):
        text = read("docs/workspaces.md")
        part = self.section("docs/workspaces.md", "Parallel")
        self.assertIn('"claude_parallel": 3', text)
        self.assertIn("`claude_parallel` (default 3)", part)
        for command in ("run", "resume", "testcrews"):
            self.assertRegex(part, rf"\| `{command}` \|[^\n]*`claude_parallel`")

    def test_pages_stay_plain_and_one_sentence_per_line(self):
        for rel, title in (("docs/herdr.md", "Roles"), ("docs/workspaces.md", "Crew models")):
            text = read(rel)
            self.assertNotIn("\u2014", text)
            for line in self.section(rel, title).splitlines():
                if not line.startswith("|"):
                    self.assertNotRegex(line, r"[.!?] [A-Z]", line)


class SkillDoc(unittest.TestCase):
    def section(self, start, end="\n### "):
        moved = start.startswith("### ") and start.split()[1].rstrip(".") in MOVED
        text = read("sections/" + start.split()[1].rstrip(".") + ".md") if moved else read("SKILL.md")
        self.assertTrue(start in text, f"no {start!r}")
        body = text[text.index(start) :]
        return body.split(end, 1)[0]

    def test_0d_carries_the_designer_spec(self):
        d = self.section("### 0d. ")
        for phrase in (
            "/compose:designer <ticket>",
            "--figma",
            "designcrews",
            "figma judge --project <slug> --only",
            "kind: design",
            "nodes:",
            "figma:figma-use",
            "design system",
            "Foundations",
            "Components",
            "Hand-off",
            "blocked by",
            "results/NNN.tree.txt",
            "get_design_context",
            "Code Connect",
            "30 variants",
            "models.crew.designer",
            "models.crew.design`",
            "one Framelink image (`download_figma_images`) per story",
            "WRITES ONLY",
            "design_concurrency",
            "figma_caps",
            "compose figma check --project <slug> --plan",
            "compose figma publish --project <slug> --library",
            "publish: ask",
        ):
            self.assertIn(phrase, d)
        self.assertLess(d.index("Foundations"), d.index("Components"))
        self.assertLess(d.index("Components"), d.index("Hand-off"))

    def test_0d_follows_0g_one_sentence_per_line_no_em_dash(self):
        text = read("SKILL.md")
        self.assertLess(text.index("### 0g. "), text.index("### 0d. "))
        d = self.section("### 0d. ")
        self.assertNotIn("\u2014", d)
        for line in d.splitlines():
            if not line.startswith(("|", "#")):
                self.assertNotRegex(line, r"[a-z)`]\. [A-Z]", line)

    def test_who_table_and_0h_name_the_design_crew(self):
        row = next(l for l in read("SKILL.md").splitlines() if l.startswith("| Designer"))
        teammates = next(l for l in self.section("### 0h. ").splitlines() if l.startswith("- **Teammates**"))
        for text in (row, teammates):
            for name in ("design-NNN", "design-review"):
                self.assertIn(name, text)
        self.assertIn("designer", teammates)
        self.assertIn("Linear only", row)
        self.assertIn("product question", row.split(" | ")[-1])

    def test_task_issue_format_names_design_fields(self):
        fmt = self.section("## Task issue format", "\n## Rules")
        self.assertIn("kind: design", fmt)
        self.assertIn("nodes:", fmt)


class DesignerCommand(unittest.TestCase):
    def parts(self):
        text = read("commands/designer.md")
        _, head, body = text.split("---\n", 2)
        return text, dict(line.split(": ", 1) for line in head.splitlines()), body

    def test_frontmatter_matches_the_workspace_command_shape(self):
        _, meta, _ = self.parts()
        self.assertEqual(set(meta), {"description", "argument-hint", "allowed-tools"})
        self.assertEqual(meta["description"].strip('"'), "Start the designer teammate for a ticket: continues design work in Figma from where it stopped")
        self.assertEqual(meta["argument-hint"], '"<Linear url or id> [--figma <file url or key>]"')
        self.assertEqual(meta["allowed-tools"], "Bash(compose designer:*)")

    def test_numbered_steps_name_the_commands_in_order(self):
        _, _, body = self.parts()
        steps = [line for line in body.splitlines() if line[:1].isdigit()]
        self.assertEqual([s.split(".", 1)[0] for s in steps], ["1", "2", "3", "4", "5"])
        self.assertIn("compose skill", steps[0])
        self.assertIn("0j", steps[0])
        self.assertIn("compose projects", steps[1])
        self.assertIn("compose init --project <slug>", steps[1])
        self.assertIn("--figma", steps[2])
        self.assertIn("compose figma check --project <slug> --figma <file>", steps[2])
        pipe = "compose designer --project <slug> | compose crew start --project <slug> --role designer --title designer --brief -"
        self.assertLess(body.index("compose figma check"), body.index(pipe))
        self.assertLess(body.index(pipe), body.index("compose watch --project <slug>"))
        self.assertNotIn("--from designer", body)  # the engineering manager's one watch covers a designer it started: no second watch
        self.assertIn("never start a second watch", body)
        self.assertIn("whatever `--from` says", body)
        self.assertIn('herdr agent prompt <slug>-designer "<answer>"', body)
        self.assertIn("never design in this session", body.lower())

    def test_plain_dashes_and_one_sentence_per_line(self):
        text, _, body = self.parts()
        self.assertNotIn("\u2014", text)
        for line in body.splitlines():
            self.assertNotRegex(line[3:] if line[:1].isdigit() else line, r"\. [A-Z]", line)


class BriefsNameFiles(unittest.TestCase):
    FILES = "SKILL.md, sections/0h.md, sections/0a.md, sections/0e.md, sections/0g.md, sections/4.md, sections/5.md, sections/6.md, sections/6a.md, sections/7.md, sections/8.md and ~/.claude/skills/herdr/SKILL.md, then continue from 0e"
    EM = "The engineering manager's own session is outside the CLI's reach: it runs unrestricted, with every server and skill the product owner has."

    def setUp(self):
        self.text = read("sections/0h.md")
        self.lines = self.text.splitlines()

    def test_team_lead_brief_reads_the_files(self):
        lead = [l for l in self.lines if l.startswith("- **Team lead**")]
        self.assertEqual(len(lead), 1)
        self.assertIn('"read ' + self.FILES + '"', lead[0].replace("\\~", "~"))
        self.assertIn("--role orchestrator --title orchestrator --brief -", lead[0])
        self.assertNotIn("invoke the compose skill", self.text.lower())

    def test_toolsmith_brief_reads_only_skill_md(self):
        block = self.text.split("**When compose itself fails**", 1)[1].split("The team lead reports at every point", 1)[0]
        lines = [l for l in block.splitlines() if "toolsmith" in l and "brief" in l and "SKILL.md" in l]
        self.assertTrue(lines, "a toolsmith brief line names SKILL.md")
        for line in lines:
            self.assertNotIn("sections/", line)
            self.assertNotIn("herdr/SKILL.md", line)
            self.assertNotRegex(line, r"invoke the `?compose`? skill")
        for command in ("compose toolsmith --project <slug> | compose crew start --project <slug> --role toolsmith --work-repo <cli_repo> --brief -",
                        "compose tool-bug file --project <slug> --body", "--approved", "compose tool-bug pr --project <slug> --worktree <path>"):
            self.assertIn(command, block)
        self.assertIn("never edits or commits", block)
        self.assertIn("no `Fixes #n`", block)
        self.assertNotIn("--role toolsmith --cwd", block)  # a session started in the CLI's checkout waits at the trust prompt

    def test_herdr_is_read_as_a_file(self):
        self.assertIn("Then read ~/.claude/skills/herdr/SKILL.md; it carries the safety rules for panes and agents.", self.text.replace("\\~", "~"))
        self.assertNotRegex(self.text.lower(), r"invoke the `?herdr`? skill")

    def test_engineering_manager_sentence_once_after_the_loop(self):
        self.assertEqual(self.text.count(self.EM), 1)
        self.assertIn(self.EM, self.lines, "the sentence sits on its own line")
        loop = next(i for i, l in enumerate(self.lines) if l.startswith("The engineering manager's loop:"))
        change = self.lines.index("**A change of course reaches the product owner through you.**")
        self.assertLess(loop, self.lines.index(self.EM))
        self.assertLess(self.lines.index(self.EM), change)

    def test_no_em_dash(self):
        self.assertNotIn("\u2014", self.text)


class PlanWritingRules(unittest.TestCase):
    """The plan-writing rules are compose's own: no third-party skill file is read, and the plan has a header, a file map, steps, spikes, and a self-review."""
    SKILLS_OFF = "The planner session starts with skills off, so every skill it needs is read as a file."
    RUN = [
        "Read all of it.",
        "Write the plan for an implementer who has not seen the codebase and will not ask: the plan is the set of decisions that implementer cannot make alone, so every such decision is in it, and nothing it can read off the named files is.",
        "The plan is a `## Plan` section in the feature issue's description (`compose linear update <feature> --section \"## Plan\" --file <path>`), never a file in the repo.",
        SKILLS_OFF,
    ]
    SHAPE = [
        "The plan opens with a header: Goal (one or two sentences), Architecture (how the pieces fit, one paragraph), Global constraints (what every task must respect: the simplicity bar, no new dependencies, naming, the test runner), and Review focus (the five inputs or conditions the design implies but no test yet exercises that are most likely to bite a user, most likely first, each one then pinned by a test in the task that owns its code).",
        "Then a file map: every file the feature creates or changes, one line each with its responsibility, before any step.",
        "Then numbered steps, each with the files it touches, the exact signature or export it adds, the test that proves it, and the steps it depends on.",
        "A step holds code only where the implementer could not derive it: an algorithm the spec leaves open, exact copy, a fixed data shape; everywhere else one line of description beats a pasted function body.",
        "An unknown the plan cannot settle (does the library support it, how slow is the query) becomes a spike: a probe task whose deliverable is the answer, written to the issue's Notes, never a code task that guesses.",
        "Before the cut the planner reads its own plan once as the implementer will: every acceptance criterion maps to a step, no step names a file the file map lacks, a type or signature means one thing in every step that uses it, the Review focus names a test or step that covers it, and the plan is no longer than the feature needs.",
    ]

    def test_the_engineering_manager_plays_stories_back_and_records_the_go(self):
        text = read("sections/0h.md")
        self.assertIn("plays it back to the product owner as one chunk of 200 to 300 words ending with what is wrong or missing, never as a list of questions", text)
        self.assertIn('compose decide --project <slug> --new "stories <issue> approved" --quote', text)

    DEEP_DIVE = ("When the planner reports `needs-user \"deep-dive <story>: no tech doc\"`, the engineering manager runs that deep dive with the product owner here (0b), "
                 "records `compose decide --project <slug> --issue <story> --new \"the tech doc on <story> is approved\" --quote \"<their words>\"`, "
                 "then sends `herdr agent prompt <slug>-orchestrator \"shaped <story>\"`.")
    PLAYBACK = ("The proposal sits under the issue's `## Proposed stories`, and the playback names the stories the engineering manager proposes for `deep-dive`; "
                "the planner applies that label when it runs `compose linear create`.")
    AWAY = ("Under away a brainstorm (0b) or a stories playback is never answered by recommendation: it waits for the product owner and is listed in the digest, "
            "and no stories approval is recorded without `--quote`; under yolo the engineering manager answers it as 0b says.")

    def test_0h_routes_a_missing_tech_doc_and_names_the_draft(self):
        lines = read("sections/0h.md").splitlines()
        for line in (self.DEEP_DIVE, self.PLAYBACK, self.AWAY):
            self.assertEqual(lines.count(line), 1, line)
        at = lines.index
        self.assertEqual(at(self.DEEP_DIVE), at(ShapingIsTheManagersBrainstorm.HANDOFF) + 1)
        self.assertLess(at(next(l for l in lines if l.startswith("The team lead forwards a planner's `needs-user \"stories"))), at(self.PLAYBACK))

    def test_a_task_is_sized_by_its_own_test_and_review(self):
        self.assertEqual(read("sections/1.md").count("A task is the smallest unit that carries its own test and is worth its own review: setup, configuration, and docs fold into the task whose deliverable needs them."), 1)

    def test_the_split_rules_put_a_removed_file_under_delete(self):
        self.assertIn("A file the task removes goes under `delete:`, never `files:`", read("sections/2.md"))  # the planner reads sections/2.md, not docs/tasks.md

    def test_no_third_party_skill_is_read(self):
        text = read("sections/1.md")
        self.assertNotRegex(text, r"(?i)superpowers|writing-plans|plugins/cache")
        self.assertEqual(text.count(self.SKILLS_OFF), 1)
        self.assertNotRegex(read("sections/0b.md"), r"(?i)superpowers|brainstorming skill|writing-plans|plugins/cache|builder-check-prompt")

    def test_the_plan_shape_is_stated_line_for_line(self):
        lines = read("sections/1.md").splitlines()
        first = lines.index(self.SHAPE[0])
        self.assertEqual(lines[first:first + len(self.SHAPE)], self.SHAPE)

    def test_setup_has_no_superpowers_row(self):
        import sys; sys.path.insert(0, str(ROOT / "bin"))
        import fc_steps
        self.assertNotIn("superpowers", [r[0] for r in fc_steps.STEPS])
        for doc in ("docs/requirements.md", "docs/new-machine.md"):
            self.assertNotIn("superpowers", read(doc), doc)

    def test_edited_sentences_keep_their_words_and_order(self):
        flat = " ".join(read("sections/1.md").split())
        self.assertIn(" ".join(self.RUN), flat)

    def test_plain_dashes_and_one_sentence_per_line(self):
        text = read("sections/1.md")
        self.assertNotIn("—", text)
        for line in text.splitlines()[1:]:
            if not line.startswith("|"):
                self.assertNotRegex(line, r"[.!?] [A-Z]", line)


class Frame0OptIn(unittest.TestCase):
    FIRST = "The engineering manager shapes the feature with the product owner in conversation, one question at a time, and nothing is planned until the product owner approves the written design."
    OPT_IN = "Nothing of Frame0 runs until a prototype question needs a wireframe: no session has `frame0` tools, and the app starts only when `compose frame0 up --project <slug>` is run, right before the first drawing, never at the start of a session on the chance that a wireframe will be needed."
    ONLY = "The first `compose frame0` command after `up` makes Frame0 ask the product owner to allow \"Frame0 MCP Server\" (the name compose asks with): tell the product owner to click Allow, and retry once they say they did; if the command still fails, say so and wait."

    def setUp(self):
        self.text = read("sections/0b.md")
        self.lines = self.text.splitlines()

    def test_first_sentence_after_heading(self):
        self.assertEqual(self.lines[0], "### 0b. Shape first, always")
        self.assertEqual(self.lines[1], self.FIRST)

    def test_opt_in_sits_right_after_the_prototype_line(self):
        for gone in ("--frame0", "`frame0` MCP server", "export_page_as_image", "has the `frame0` tools"):  # no session carries the server: nothing of Frame0 runs before `up`
            self.assertNotIn(gone, self.text, gone)
        self.assertEqual(self.lines.count(self.OPT_IN), 1)
        proto = [i for i, l in enumerate(self.lines) if l.startswith("A **prototype** question about how a screen or flow should look")]
        self.assertEqual(len(proto), 1)
        self.assertEqual(self.lines[proto[0] + 1], self.OPT_IN)
        self.assertTrue(self.lines[proto[0] + 2].startswith("Each project draws in its own Frame0 app"))

    def test_old_sentence_replaced_in_place(self):
        for old in ("points every teammate's `frame0` tools", "started after this change", "Only a session started with"):
            self.assertFalse(old in self.text, old)
        self.assertEqual(self.lines.count(self.ONLY), 1)
        self.assertEqual(self.lines[self.lines.index(self.ONLY) - 1][:36], "Each project draws in its own Frame0")
        i = self.lines.index(self.ONLY)
        self.assertTrue(self.lines[i - 1].startswith("Each project draws in its own Frame0 app"))
        self.assertTrue(self.lines[i + 1].startswith("Draw each page with one `compose frame0 draw"))

    def test_plain_text_rules(self):
        self.assertNotIn("\u2014", self.text)
        # Older lines (e.g. 2-7 of the numbered steps, "`teardown` saves ...") already hold two sentences; the rule covers the new ones.
        for line in (self.FIRST, self.OPT_IN, self.ONLY):
            self.assertNotRegex(line, r"[.!?] [A-Z]", line)


class LinearCommands(unittest.TestCase):
    LEAD = "You create and edit issues and write judgment comments with `compose linear`"
    PROJECT = "Confirm the file points at a real project before any other Linear call: `compose linear project` must print a project on its team."

    def section0(self):
        text = read("SKILL.md")
        return text.split("\n### 0. ", 1)[1].split("\n### 0i. ", 1)[0].splitlines()

    def test_section_0_names_the_verbs_and_the_required_key(self):
        lines = self.section0()
        lead = [l for l in lines if l.startswith(self.LEAD)]
        self.assertEqual(len(lead), 1)
        for verb in ("get <id> [--body] [--comments]", "list --parent <id> | --search", "create", "update <id>", "comment <id>", "labels", "project", "doc"):
            self.assertIn(verb, lead[0])
        self.assertIn("never a Linear MCP tool", lead[0])
        self.assertIn("The Linear key is required: without it `compose doctor` fails and `compose linear` stops with the line that stores one.", lines)
        self.assertIn("Write bodies come from `--file <path>` or stdin, never from the command line.", lines)
        self.assertIn(self.PROJECT, lines)
        self.assertNotIn("fields", "\n".join(lines), "the compact lines replace the fields rule")

    def test_a_failing_call_is_a_compose_linear_call(self):
        self.assertIn("- a `compose linear` call fails (auth, connection, timeout)", read("SKILL.md"))

    def test_labels_and_the_reconcile_use_the_verbs(self):
        text = read("SKILL.md")
        self.assertIn("create the others once with `compose linear labels --create <name>` if `compose linear labels` does not show them", text)
        self.assertIn("`compose linear list --parent <feature>`, then `--parent` each story", text)

    def test_sync_reads_linear_and_the_mcp_recipe_is_gone(self):
        two = read("sections/2.md")
        self.assertIn("Run `compose sync --repo <wt> --project <slug>`: it reads the feature, its stories, and their tasks from Linear", two)
        self.assertIn("`--stdin` reads the same JSON from stdin instead", two)
        self.assertNotIn("--from-linear", two)
        tasks = read("docs/tasks.md")
        self.assertIn("`compose sync` reads the feature, its stories, and their tasks from Linear", tasks)
        self.assertIn("`compose sync --stdin` takes JSON of this shape instead", tasks)
        self.assertIn("The team lead writes everything else on Linear with `compose linear`.", tasks)
        self.assertNotIn("--from-linear", read("docs/cli.md"))

    def test_manager_session_line_and_the_key_requirement_in_the_docs(self):
        self.assertIn("Compose itself uses no Linear MCP, and this session uses `compose linear` too, never a Linear MCP.", read("sections/0h.md").splitlines())
        self.assertNotIn("connector", read("docs/new-machine.md").split("| Keys |", 1)[1].splitlines()[0])
        requirements = [l for l in read("docs/requirements.md").splitlines() if l.startswith("| Linear |")]
        self.assertEqual(len(requirements), 1)
        self.assertNotIn("connector", requirements[0])

    def test_docs_describe_the_verbs(self):
        cli = [l for l in read("docs/cli.md").splitlines() if l.startswith("| `linear <verb>`")]
        self.assertEqual(len(cli), 1)
        for word in ("get <id>", "--section", "labels [--create <name>]", "doc create", "--any-project",
                     "cancel <id> --file <reason> [--into <id>]", "--add-label", "--drop-label"):
            self.assertIn(word, cli[0])
        self.assertIn("`get`, `update`, `comment`, `cancel`, and `doc update` refuse an issue or document in another project unless `--any-project`", cli[0])
        self.assertIn("`create`, `update`, `comment`, and `cancel` drop the cached reads", cli[0])
        self.assertIn("`cancel` refuses while any compose project of the repo is under away or yolo", cli[0])


class NoLinearMcpToolName(unittest.TestCase):
    NAMES = ("save_issue", "get_issue", "list_issues", "save_comment", "get_project", "list_issue_labels", "create_issue_label",
             "save_document", "mcp__claude_ai_Linear")

    def test_no_shipped_text_names_a_linear_mcp_tool(self):
        files = [ROOT / "SKILL.md", ROOT / "README.md"]
        for rel, pattern in (("sections", "*.md"), ("commands", "*.md"), ("docs", "*.md"), ("bin", "*.py"), ("setup/compose_setup", "*.py")):
            files += sorted((ROOT / rel).glob(pattern))
        self.assertGreater(len(files), 40)
        hits = [f"{f.relative_to(ROOT)}: {n}" for f in files for n in self.NAMES if n in f.read_text()]
        self.assertEqual(hits, [])


    def test_size_and_one_pointer_per_moved_section(self):
        text = read("SKILL.md")
        self.assertLess(len(text.encode()), 30000)
        lines = text.splitlines()
        pointers = [l for l in lines if re.match(r"### \S+\. .*: read sections/\S+\.md$", l)]
        expected = []
        for sid in MOVED:
            title = read(f"sections/{sid}.md").splitlines()[0].split(f"### {sid}. ", 1)[1]
            expected.append(f"### {sid}. {title}: read sections/{sid}.md")
            self.assertEqual(lines.count(expected[-1]), 1, sid)
        self.assertEqual(pointers, expected, "pointer lines keep MOVED order and nothing else points")

    def test_plain_dashes_and_one_sentence_per_line_in_section_0(self):
        text = read("SKILL.md")
        self.assertNotIn("\u2014", text)
        for line in text.split("\n### 0. ", 1)[1].split("\n### 0i. ", 1)[0].splitlines()[1:]:
            if not line.startswith("|"):
                self.assertNotRegex(line, r"[.!?] [A-Z]", line)


class LeadRelaysOnce(unittest.TestCase):
    ONE_LINE = 'needs-user "<issue>: <n> questions <ids>; recommendation <one line>"'

    def setUp(self):
        self.text = read("sections/0h.md").replace("\\~", "~")
        self.lines = self.text.splitlines()
        start = next(i for i, l in enumerate(self.lines) if l.startswith("The engineering manager's loop:"))
        end = next(i for i, l in enumerate(self.lines) if l.startswith("**When compose itself fails**"))
        self.relay = self.lines[start:end]
        self.report = next(i for i, l in enumerate(self.lines) if l.startswith("The team lead reports at every point"))

    def test_lead_forwards_the_one_line_unchanged(self):
        line = next((l for l in self.relay if self.ONE_LINE in l), None)
        self.assertIsNotNone(line, "the relay names the one-line needs-user")
        self.assertIn("architect", line)
        self.assertIn("planner", line)
        self.assertIn("unchanged", line)
        self.assertRegex(line.lower(), r"never re-?sen")

    def test_manager_points_at_the_page_and_records_with_the_quote(self):
        relay = "\n".join(self.relay)
        self.assertRegex(relay, r"engineering manager reads the issue once")
        self.assertIn("asks the product owner those questions one at a time, brainstorming style (0b)", relay)
        self.assertIn("`compose decisions --project <slug> --issue <issue>`", relay)
        self.assertIn('`compose decide --project <slug> --issue <issue> --new "<decision>" --quote "<their words>" --closes <id>`', relay)
        self.assertIn('`--drop <id> --quote "<their words>"`', relay)
        self.assertIn("by question id", relay)
        self.assertNotIn("<card id>", relay)
        self.assertNotRegex(relay, r"\bcards?\b")

    def test_the_relay_names_the_issue_of_the_report(self):
        relay = "\n".join(self.relay)
        self.assertIn("`compose decisions --project <slug> --issue <issue>`", relay)
        self.assertIn('`compose decide --project <slug> --issue <issue> --new "<decision>" --quote "<their words>" --closes <id>`', relay)
        for line in self.relay:
            if "compose decisions --project" in line or "--closes <id>" in line:
                self.assertIn("--issue <issue>", line, line)

    def test_accept_all_strikes_what_a_change_of_course_question_replaces(self):
        text = "\n".join(self.lines)
        self.assertIn("A change-of-course question accepted this way strikes the decision it replaces, `~~<old line>~~ (C<n>)`, unless its recommendation keeps it.", text)
        self.assertIn('The older `"C1-2: B" --change C1` answer to a lettered change question strikes its `Supersedes:` line the same way.', text)

    def test_the_designer_reports_with_the_one_line(self):
        line = next(l for l in read("sections/0d.md").splitlines() if l.startswith("Every open design decision goes up"))
        self.assertIn(f"`compose report --project <slug> --as <slug>-designer {self.ONE_LINE}`", line)

    def test_an_approval_waits_for_the_open_questions(self):
        relay = "\n".join(self.relay)
        self.assertIn("`compose decide` refuses an approval while the issue has open questions", relay)

    def test_manager_refuses_a_paraphrase_and_answers_on_a_rhythm(self):
        relay = "\n".join(self.relay)
        self.assertIn("`compose decide` refuses to record without `--quote` unless away or yolo is on", relay)
        self.assertIn("Take decisions in batches", relay)
        self.assertIn("every 30 minutes or so", relay)
        self.assertIn('"here\'s where we are" line per project', relay)

    def test_lead_report_carries_the_line_not_the_cards(self):
        line = self.lines[self.report]
        self.assertNotIn("carrying the architect's open cards", line)
        self.assertIn("needs-user", line)
        self.assertIn("not the questions in full", line)

    def test_lead_saves_linear_in_few_patches_near_its_reporting_rule(self):
        hits = [i for i, l in enumerate(self.lines) if "compose linear update --section" in l]
        self.assertEqual(len(hits), 1, "one compose linear update --section sentence")
        line = self.lines[hits[0]]
        self.assertLessEqual(abs(hits[0] - self.report), 2, "near the reporting rule")
        for words in ("team lead drafts", "Decisions", "Plan", "one per section", "task issues"):
            self.assertIn(words, line)

    def test_new_lines_are_plain_and_the_older_sentences_stay(self):
        new = [l for l in self.lines if self.ONE_LINE in l or "compose linear update --section" in l or "--closes <id>" in l]
        self.assertTrue(new)
        for line in new:
            self.assertNotRegex(line, r"[.!?] [A-Z]", line)
        self.assertNotIn("—", self.text)
        self.assertIn(BriefsNameFiles.EM, self.lines)
        self.assertIn('"read ' + BriefsNameFiles.FILES + '"', self.text)
        self.assertIn("Then read ~/.claude/skills/herdr/SKILL.md;", self.text)


class BrainstormShape(unittest.TestCase):
    ONCE = [
        "Each message ends with one question: a single sentence, with no list of example answers after it.",
        "Open questions come first; a short menu of options only when the product owner is stuck, and a concrete proposal when they say \"just guess\" or \"what do you think?\".",
        "As soon as one part is understood, describe it back in 200 to 300 words, mark every guess as a guess, and ask what is wrong or missing.",
        "Before any question, look for its answer in Linear first:",
        "Every decision the product owner makes is recorded at once with their words: `compose decide --project <slug> --new \"<decision>\" --quote \"<their words>\"` (`--issue <id>` when it is not the project's feature).",
        "The design is written to the feature issue's description: Intent, Goals, Non-goals, Anti-goals, Constraints, Decided by the product owner, My calls, and Left to the builder.",
        "Before the product owner sees the design, a builder check reads it cold: `compose buildercheck --project <slug> --issue <id> | compose crew start --project <slug> --role builder-check --title \"builder check\" --brief -`.",
        "Sort what the builder check asks into three piles: answered already (put the answer in the design), minor (settle it and mark it as your call), and theirs (only the product owner can answer it, and the answer changes what gets built).",
        "Bring the theirs pile in one message, most important first; that is the only round of questions the check produces.",
        "Under away, a brainstorm waits for the product owner and is listed in the digest; under yolo, the engineering manager takes its own recommendation per question, tagged unconfirmed.",
        "The planner (step 1) starts only after the product owner approves the design.",
        "Before the product owner approves the design, a changed decision is recorded with `compose decide --project <slug> --new \"<decision>\" --replaces D<n> --quote \"<their words>\"`, which strikes the old line and adds the new D id.",
        "After approval, a decision changes only through a change of course (step 8); `--replaces` refuses once stories exist.",
        "A story or epic that needs a technical deep dive carries the Linear label `deep-dive`; propose it when a story's how is not obvious, and the product owner may add or remove it.",
        "A deep dive is a brainstorm on the technical side, and its artifact is a Linear document titled `<issue id>: tech doc`, linked from the issue under `## Tech doc`, read by a builder check like any design.",
        "Without the label, a story goes straight to the planner.",
        "Think wide privately: before proposing anything, come up with several approaches and drop the weak ones, including any that fight what they said about why.",
        "Before writing the design, bring the two or three approaches that survived, each with what it changes and costs, and the one you recommend with why, as one question; the choice goes under Decided by the product owner, or under My calls when they hand it to you, and a change small enough to approve in chat skips this.",
        "Outside herdr, give the printed brief to a fresh harness subagent with no conversation context and read its one report.",
        "The product owner approves the tech doc, and that is recorded the same way: `compose decide --project <slug> --issue <id> --new \"the tech doc on <id> is approved\" --quote \"<their words>\"`.",
        "When the issue carries the `needs-shaping` label, drop it in the same step: `compose linear update <id> --drop-label needs-shaping`.",
    ]
    LOOKUP = [
        "1. the feature issue's Decisions so far and Notes, and the description and comments of the feature, its epic, and the story at hand",
        "2. a `compose linear list --search \"<terms>\"` search, scoped to the project in `.compose/linear.json`, for the question's key terms, since another epic or feature may have decided it",
    ]
    GONE = ("card set", "fog", "Destination", "lavish-axi poll", "accept every recommendation")

    def setUp(self):
        self.text = read("sections/0b.md")
        self.lines = self.text.splitlines()

    def test_heading_and_first_line(self):
        self.assertEqual(self.lines[0], "### 0b. Shape first, always")
        self.assertEqual(self.lines[1], Frame0OptIn.FIRST)

    def test_each_rule_is_stated_once(self):
        for line in self.ONCE:
            self.assertEqual(self.lines.count(line), 1, line)

    def test_the_linear_lookup_list_follows_its_sentence(self):
        i = self.lines.index(self.ONCE[3])
        self.assertEqual(self.lines[i + 1:i + 3], self.LOOKUP)

    def test_the_rules_keep_the_brainstorm_order(self):
        at = lambda s: self.lines.index(s)
        order = [at(self.ONCE[0]), at(self.ONCE[2]), at(self.ONCE[5]), at(self.ONCE[6]), at(self.ONCE[7]), at(self.ONCE[8]), at(self.ONCE[10])]
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.lines[-1] if self.lines[-1] else self.lines[-2], self.ONCE[10], "the hand-off to step 1 closes the section")

    def test_new_lines_sit_in_their_paragraphs(self):
        at = self.lines.index
        self.assertLess(at("**How it gets made.**"), at(self.ONCE[17]))
        self.assertLess(at(self.ONCE[17]), at("**Play it back.**"))
        self.assertEqual(self.lines.index(self.ONCE[-1]), next(n for n, x in enumerate(self.lines) if "the design on <id> is approved" in x) + 1)
        self.assertLess(at(self.ONCE[6]), at(self.ONCE[18]))
        self.assertLess(at(self.ONCE[18]), at("Hand nothing over until it is back."))
        self.assertLess(at(self.ONCE[14]), at(self.ONCE[19]))
        self.assertLess(at(self.ONCE[19]), at(self.ONCE[15]))
        self.assertEqual(self.text.count("(`--issue <id>` when it is not the project's feature)"), 1)

    CLOSES = "An answer to a question a teammate wrote is recorded the same way, with `--closes <id>` for each question it settles: one rule for every decision."

    def test_answers_to_teammates_questions_follow_the_one_recording_rule(self):
        self.assertEqual(self.lines.count(self.CLOSES), 1)
        self.assertEqual(self.lines.index(self.CLOSES), self.lines.index(self.ONCE[next(i for i, l in enumerate(self.ONCE) if l.startswith("Every decision the product owner makes"))]) + 1)

    def test_the_card_flow_is_gone(self):
        for old in self.GONE:
            self.assertNotIn(old, self.text, old)
        for old in ("compose architect", "--accept-all", "queuePrompt", "data-lavish-question", "Not yet specified"):
            self.assertNotIn(old, self.text, old)

    def test_frame0_rules_stay_for_what_the_product_owner_will_look_at(self):
        for line in (Frame0OptIn.OPT_IN, Frame0OptIn.ONLY):
            self.assertEqual(self.lines.count(line), 1, line)
        proto = next(i for i, l in enumerate(self.lines) if l.startswith("A **prototype** question about how a screen or flow should look"))
        self.assertLess(self.lines.index(self.ONCE[0]), proto)
        self.assertLess(proto, self.lines.index(self.ONCE[10]))

    def test_plain_dashes_and_one_sentence_per_line(self):
        self.assertNotIn("—", self.text)
        for line in self.lines[1:]:
            if not line.startswith("|"):
                self.assertNotRegex(line, r"[.!?] [A-Z]", line)


class PlannerSavesInPatches(unittest.TestCase):
    AFTER = PlanWritingRules.SKILLS_OFF

    def rule(self):
        text = read("sections/1.md")
        self.assertIn(self.AFTER + "\n", text)
        return " ".join(text.split(self.AFTER, 1)[1].split()).lower()

    def test_drafts_the_whole_plan_then_saves_in_few_patches_one_per_section(self):
        rule = self.rule()
        self.assertRegex(rule, r"drafts? the whole plan")
        self.assertIn("decisions", rule)
        self.assertRegex(rule, r"before (it )?sav")
        self.assertRegex(rule, r"few `compose linear update --section` writes")
        self.assertIn("one per section", rule)

    def test_task_detail_goes_on_task_issues_and_no_reread(self):
        rule = self.rule()
        self.assertIn("task issues", rule)
        self.assertRegex(rule, r"never re-?reads? an issue it just wrote")

    def test_no_reread_because_an_update_prints_only_its_id(self):
        self.assertIn("`compose linear update` prints only `updated <id>`", self.rule())

    def test_rule_is_stated_once_and_earlier_text_stays(self):
        text = read("sections/1.md")
        self.assertEqual(text.count("prints only `updated <id>`"), 1)
        flat = " ".join(text.split())
        self.assertIn(" ".join(PlanWritingRules.RUN[:3]), flat)
        self.assertIn(PlanWritingRules.SKILLS_OFF, flat)

    def test_plain_dashes_and_one_sentence_per_line(self):
        text = read("sections/1.md")
        self.assertNotIn("\u2014", text)
        for line in text.splitlines()[1:]:
            if not line.startswith("|"):
                self.assertNotRegex(line, r"[.!?] [A-Z]", line)


class OneWatchCoversTheManagersOwnTeammates(unittest.TestCase):
    """A planner or designer the engineering manager starts itself reports to it, and its one watch reports them: no second watch."""

    def test_planner_command_says_the_one_watch_covers_it(self):
        body = read("commands/planner.md").split("---\n", 2)[2]
        self.assertNotIn("--from planner", body)
        self.assertIn("compose watch --project <slug>", body)
        self.assertIn("Never start a second watch", body)
        self.assertIn("`--from orchestrator` watch", body)
        for line in body.splitlines():
            self.assertNotRegex(line, r"\. [A-Z]", line)

    def test_planner_command_shapes_an_unshaped_ticket_here_first(self):
        text = read("commands/planner.md")
        self.assertEqual(text.splitlines().count("Accept an issue id or URL, or a project link; a loose idea becomes a new feature issue first (0a), then is shaped like any unshaped ticket."), 1)
        self.assertNotIn("A loose idea becomes", text)
        self.assertEqual(text.splitlines().count(
            "For `stories <issue>`, play the proposal back as one chunk, record `compose decide --project <slug> --new \"stories <issue> approved\" --quote \"<their words>\"`, "
            "then `herdr agent prompt <slug>-planner \"stories approved: create them\"`."), 1)
        self.assertIn("When the ticket has no approved design (a feature or epic), or is a story labeled `deep-dive` with no tech doc, brainstorm it here first as sections/0b.md says, then start the planner.", text)
        self.assertNotIn("Refuse a loose idea", text)
        self.assertIn("In this session, never plan, write stories, or cut tasks.", text)
        for tool in ("Bash(compose decide:*)", "Bash(compose buildercheck:*)", "Bash(compose linear:*)"):
            self.assertIn(tool, text.split("---\n")[1])

    def test_0h_says_the_watch_passes_teammates_the_manager_started(self):
        lines = read("sections/0h.md").splitlines()
        loop = next(i for i, l in enumerate(lines) if l.startswith("The engineering manager's loop:"))
        line = lines[loop + 1]
        self.assertTrue(line.startswith("That one watch also reports every teammate the engineering manager started itself"), line)
        for word in ("`reports_to`", "results/crew/<agent>.json", "`COMPOSE_ROLE`", "whatever `--from` says", "`needs-user` and `blocked`", "Never start a second watch"):
            self.assertIn(word, line)
        self.assertNotIn("—", line)


class NoGraftWord(unittest.TestCase):
    """Every doc names trail: no standalone word graft is left (`graft/`, `GRAFT_NO_*`, and `mcp__graft__` still pass)."""
    GRAFT = re.compile(r"(?<![\w/.@-])graft(?![\w/-])")

    def assert_no_graft(self, *rels):
        for rel in rels:
            for old in ("@nanonets/graft", "brew install graft"):
                self.assertNotIn(old, read(rel), rel)
            for n, line in enumerate(read(rel).splitlines(), 1):
                self.assertNotRegex(line, self.GRAFT, f"{rel}:{n}")

    def test_skill_and_0a(self):
        self.assert_no_graft("SKILL.md", "sections/0a.md")
        skill, zero_a = read("SKILL.md"), read("sections/0a.md")
        self.assertIn("evidence gathered in this session: trail output with file:line, a command", skill)
        rule = [l for l in skill.splitlines() if l.startswith("- Build a trail graph in the repo (`trail build .`")]
        self.assertEqual(len(rule), 1, "one graph rule")
        for cmd in ("`trail ask`", "`trail callers`", "Without trail"):
            self.assertIn(cmd, rule[0])
        # The old map folder is still ignored beside .trail/ and .compose/.
        self.assertIn("`compose init` ignores `.compose/`, `graft/`, and `.trail/` in every checkout", skill)
        self.assertIn("ignores the tool's files: `graft/`, `.trail/` (where trail", zero_a)
        self.assertRegex(zero_a, r"`\.trail/` \(where trail (0\.23 )?keeps the graph\), and `\.compose/`")
        # trail writes no root .ignore, so the doc must not claim one.
        self.assertNotIn("trail's `.ignore`", zero_a)
        self.assertIn("sets trail up in the worktree: wired for Claude Code (`trail init --no-agents --no-global`", zero_a)
        self.assertIn("with every file trail writes kept out of git through `info/exclude`", zero_a)
        self.assertIn("Without trail installed it prints the install command; `compose trail --project <slug>`", zero_a)

    def test_0d_and_0e(self):
        self.assert_no_graft("sections/0d.md", "sections/0e.md")
        design, explore = read("sections/0d.md"), read("sections/0e.md")
        self.assertIn(
            'through trail: `trail ask "design tokens theme css variables" --source`, `trail grep "--color-"`.',
            design,
        )
        self.assertIn("in the repo (`trail grep` for token files, `tailwind.config`, `tokens.json`)", design)
        self.assertIn("It maps the code through trail only, in under 3 minutes,", explore)
        self.assertIn("check it in the code first (`trail ask`, `trail callers`), and if the code cannot settle it", explore)

    def test_4_and_readme(self):
        self.assert_no_graft("sections/4.md", "README.md")
        four, readme = read("sections/4.md").splitlines(), read("README.md").splitlines()
        self.assertIn(
            "Each clone, and the worktree verify runs in, borrows the main checkout's git-ignored installs"
            " (`node_modules` at any depth, `vendor`, `.venv`) as symlinks that never show in `git status`,"
            " and trail never edits `.gitignore` there. So install dependencies once in the project worktree"
            " before `run`; do not patch test commands to link them.",
            four,
        )
        self.assertIn(
            "   An explore teammate maps your codebase through trail,"
            " so no question, ticket, or plan rests on a guess about the code.",
            readme,
        )
        # The trail rename touches only the tool word: the unrelated "trail in the ticket" line stays as is.
        self.assertIn(
            "- Log each steer as a comment on the task's Linear issue, one line,"
            " so the trail is in the ticket and not only in a terminal.",
            four,
        )

    def test_cli_and_requirements(self):
        self.assert_no_graft("docs/cli.md", "docs/requirements.md")
        cli, reqs = read("docs/cli.md").splitlines(), read("docs/requirements.md").splitlines()
        self.assertIn("| `trail` | Sets trail up again in the project's worktree |", cli)
        doctor = [l for l in cli if l.startswith("| `doctor` |")]
        self.assertEqual(len(doctor), 1, "one doctor row")
        self.assertIn("designer extras not set up", doctor[0])
        self.assertIn("| trail | `file:line` context packs for implementers | `npm i -g @trailhq/trail-code` |", reqs)
        self.assertIn("| `COMPOSE_NO_TRAIL=1` | skip trail |", reqs)
        self.assertIn("| `COMPOSE_TEST_SLOTS=<n>` | how many full-suite `compose timed` commands run at once on this machine (default 1); the next waits for a slot and says so. `COMPOSE_QUICK_SLOTS=<n>` is the same for every other command (`--help`, `uv sync`, or a test run with a `-k`/`-run`/`--grep` filter or one test file or module), in lanes apart from the suites (default 2). `COMPOSE_TEST_JOBS=<n>` is how many test files `python3 -m tests.run` runs side by side (default: a quarter of the cores; `-j` overrides it) |", reqs)
        timed = [l for l in cli if l.startswith("| `timed <seconds> -- <command>` |")]
        self.assertEqual(len(timed), 1, "one timed row")
        for part in ("exit 124", "scratch", "COMPOSE_TEST_SLOTS", "COMPOSE_QUICK_SLOTS", "SIGTERM"):
            self.assertIn(part, timed[0])
        # Only the tool word changes: the neighbouring env row stays, and the old graft env row is gone.
        self.assertIn("| `COMPOSE_NO_TREEHOUSE=1` | use plain git worktrees even with treehouse installed |", reqs)
        self.assertFalse([l for l in reqs if "COMPOSE_NO_GRAFT" in l], "COMPOSE_NO_GRAFT")

    def test_compose_no_policies_is_in_the_env_table_and_the_rows_that_stop_on_policies(self):
        cli, reqs = read("docs/cli.md").splitlines(), read("docs/requirements.md")
        self.assertIn("| `COMPOSE_NO_POLICIES=1` |", reqs)
        for cmd in ("route", "verify", "quick <id>..."):
            row = [l for l in cli if l.startswith(f"| `{cmd}` |")]
            self.assertEqual(len(row), 1, cmd)
            self.assertIn("COMPOSE_NO_POLICIES=1" if cmd != "quick <id>..." else "one `quick: ...` line", row[0])

    def test_how_it_works_and_new_machine(self):
        self.assert_no_graft("docs/how-it-works.md", "docs/new-machine.md")
        how, machine = read("docs/how-it-works.md").splitlines(), read("docs/new-machine.md").splitlines()
        self.assertIn(
            "In a repo that already has code, an explore teammate maps it through trail first"
            " and adds a short codebase brief to the feature.",
            how,
        )
        self.assertIn(
            "1. It reads before it writes: the ticket and its parents with their Decisions so far, the Frame0 wireframes,"
            " the repo's tokens and components through trail, `DESIGN.md`, and what the Figma file already has.",
            how,
        )
        # The folder names stay: init still ignores the old map folder beside .trail/.
        self.assertIn("  `init` git-ignores it, `graft/`, and `.trail/` first.", how)
        # The step table holds the trail and treehouse install row.
        self.assertIn("| trail, treehouse | default | installs `@trailhq/trail-code` and treehouse | no |", machine)


class StackBaseDocs(unittest.TestCase):
    def between(self, rel, first, last):
        # The bullet that starts with `first`, with its continuation lines, up to the line starting with `last`.
        lines = read(rel).splitlines()
        at = next(i for i, l in enumerate(lines) if l.startswith(first))
        end = next(i for i in range(at + 1, len(lines)) if lines[i].startswith(last))
        self.assertNotIn("\u2014", "".join(lines[at:end]))
        return lines[at:end]

    def project_lines(self):
        # Lines 14-15 as edited, held between the untouched bullets around them.
        lines = self.between("sections/0a.md", "- From then on everything", "- Never mix two features")[1:]
        at = next(i for i, l in enumerate(lines) if "init --after <other-slug>" in l)
        self.assertGreater(at, 0, "the trunk line still comes before the --after line")
        return "\n".join(lines[:at]), "\n".join(lines[at:])

    def test_the_stack_sits_on_the_review_base_and_merges_all_or_nothing(self):
        lines = read("sections/0a.md").splitlines()
        for old in ("PRs target it", "final PR"):
            self.assertFalse([l for l in lines if old in l], old)
        trunk, _ = self.project_lines()
        for word in ("`review_base`", "default branch", "`gh stack merge`", "--branch <new>`", "renames its git branch"):
            self.assertIn(word, trunk)
        self.assertRegex(trunk, r"all.or.nothing")

    def test_an_after_project_stacks_on_that_projects_branch(self):
        _, after = self.project_lines()
        for word in ("`review_base`", "`(after <other-slug>)`", "`clean` counts a merge into either."):
            self.assertIn(word, after)
        self.assertRegex(after, r"layers\b[^.]*\b(stack|sit)[^.]*\b(that|the other) project's branch")
        self.assertNotIn("until that one merges", after)

    def test_the_rewritten_project_lines_hold_one_sentence_each(self):
        for line in "\n".join(self.project_lines()).splitlines():
            self.assertNotRegex(line, r"[a-z)`]\. [A-Z`]", line)

    def test_each_epic_feature_has_no_final_pr(self):
        line = " ".join(self.between("SKILL.md", "- **epic**:", "  - **Fog**"))
        self.assertNotIn("final PR", line)
        self.assertTrue(line.startswith("- **epic**: the epic is taken through to done as a unit, but it is never one project:"))
        for word in ("each feature is its own project (0a)", "worktree, team lead,", "stack", "whether its features are known:"):
            self.assertIn(word, line)


class HowItWorksStackBase(unittest.TestCase):
    def setUp(self):
        self.lines = read("docs/how-it-works.md").splitlines()
        self.assertFalse([l for l in self.lines if "\u2014" in l], "em dash")

    def box(self):
        # The flow box: from the line above its top border (`compose run│`) through the bottom border.
        run = next(i for i, l in enumerate(self.lines) if "compose run" in l)
        top = next(i for i in range(run, len(self.lines)) if "┌" in self.lines[i])
        end = next(i for i in range(top, len(self.lines)) if self.lines[i].startswith("  └"))
        return self.lines[top - 1], self.lines[top : end + 1]

    def test_the_box_commits_on_the_project_branch_name(self):
        _, box = self.box()
        self.assertFalse([l for l in box if "fast/" in l], "a fast/ branch in the box")
        self.assertEqual(len([l for l in box if re.fullmatch(r"  │ +format, commit on <project branch>-NNN +│", l)]), 1)

    def test_every_box_line_has_the_same_width_and_right_border(self):
        above, box = self.box()
        self.assertEqual({len(l) for l in box}, {len(box[1])}, [len(l) for l in box])
        self.assertEqual([l[-1] for l in box], ["┐"] + ["│"] * (len(box) - 2) + ["┘"])
        # The run arrow still drops into the box under the `compose run│` line above it.
        self.assertEqual(box[0].index("▼"), above.rindex("│"))
        line = r"  │        yes ─▶ implement \(edits only\) ─▶ CLI runs test, protected untouched, ≤350 lines +│"
        self.assertEqual(len([l for l in box if re.fullmatch(line, l)]), 1)

    def test_the_stack_sits_on_the_review_base_and_merges_all_or_nothing(self):
        self.assertFalse([l for l in self.lines if "trunk is the project branch" in l or "one final PR" in l])
        # The bullet just before the untouched `init --after` bullet, with any continuation lines.
        after = self.lines.index("- `init --after <other>` branches from another project's branch when the feature needs its code.")
        at = max(i for i in range(after) if self.lines[i].startswith("- "))
        self.assertEqual(self.lines[at - 1], "  `init` git-ignores it, `graft/`, and `.trail/` first.")
        line = " ".join(self.lines[at:after])
        for word in ("main", "`review_base`", "`gh stack merge`", "no final PR"):
            self.assertIn(word, line)
        self.assertRegex(line, r"all.or.nothing")
        self.assertRegex(line, r"\b(stack|sit)s?\b[^.]*\breview base\b")


class BranchNamesInSections(unittest.TestCase):
    # Each edit: (file, start of the untouched line above, the edited lines in full, start of the untouched line below).
    EDITS = [
        ("sections/3.md", "When it reports `stubs ready`, run `compose testcrews", [
            "Each test-writing teammate works in its own worktree on its own branch (`<project branch>-tests-NNN`, off the stubs commit), so no teammate can amend or reset another's commit.",
        ], "Tasks that share a protected test file get their writers one after another:"),
        ("sections/3.md", "Each test teammate reports `working \"test ready: <commit>; holdouts: <cases>\"` once", [
            "Review each test on the project branch as its `done` arrives (`git log -p <stubs commit>..HEAD -- <its protected path>`), while the other writers go on.",
            "A test that does not test what its issue says is replaced, never hand-edited: fix the issue if the brief was the gap, then `compose testcrews --project <slug> --only NNN --redo`, which starts a fresh writer off the project branch told to rewrite it.",
        ], "A test that fails on the stubs because it needs code another task owns"),
        ("sections/5.md", "You are the senior engineer over the fast models.", [
            "`run` ends with a `NNN triage clean|flag: <reason>` line per passed task: a fast model checked each diff against its brief and the simplicity bar, all at once (`compose triage` repeats it). Read flagged layers in full. For a clean layer check only its diffstat (`git diff --stat <base>..<project branch>-NNN`): when every changed file is in the task's `files:`, accept it without reading the diff. Reading every diff of an all-clean run is the time triage exists to save. A flag is a pointer, never a verdict.",
            "Read `.compose/<project>/results/NNN.json` and the layer's own diff: `git diff <base>..<project branch>-NNN`, where `base` is in the result (`HEAD` or the lower task's branch). `gh stack view --json` from a layer branch shows the chain.",
        ], "Passing is not done."),
    ]

    def test_the_edited_lines_name_the_project_branch_between_untouched_neighbours(self):
        for rel, above, edited, below in self.EDITS:
            lines = read(rel).splitlines()
            at = [i for i, l in enumerate(lines) if l.startswith(above)]
            self.assertEqual(len(at), 1, (rel, above))
            end = at[0] + 1 + len(edited)
            self.assertEqual(lines[at[0] + 1 : end], edited, rel)
            self.assertTrue(lines[end].startswith(below), (rel, lines[end]))

    def test_no_fast_branch_or_em_dash_is_left_in_either_file(self):
        for rel in ("sections/3.md", "sections/5.md"):
            self.assertFalse([l for l in read(rel).splitlines() if "fast/" in l or "—" in l], rel)

    def test_each_new_name_appears_only_where_the_old_one_was(self):
        three, five = read("sections/3.md"), read("sections/5.md")
        self.assertEqual(three.count("<project branch>-tests-NNN"), 1)  # the review reads the project branch: compose collected the test
        self.assertEqual(three.count("<project branch>-NNN"), 0, "the test writer's branch keeps its tests- part")
        self.assertEqual(five.count("<project branch>-NNN"), 2)
        self.assertEqual(five.count("<project branch>"), 2)
        # The results path and the herdr teammate name are not branches and keep their placeholders.
        self.assertEqual(five.count(".compose/<project>/results/NNN.json"), 1)
        self.assertEqual(three.count("herdr agent prompt <slug>-tests-NNN"), 0, "corrections go through testcrews --redo, not a prompt")


class StackDocs(unittest.TestCase):
    def setUp(self):
        # sections/4.md lines 8-11 plus the new cap line, between the untouched trunk and lockfile lines.
        lines = read("sections/4.md").splitlines()
        at = [i for i, l in enumerate(lines) if l.startswith("The trunk is the branch the user is on")]
        end = [i for i, l in enumerate(lines) if l.startswith("Lockfiles (`package-lock.json`")]
        self.assertEqual((len(at), len(end)), (1, 1))
        self.region = lines[at[0] + 1 : end[0]]

    def test_the_edited_lines_hold_one_sentence_each_and_no_em_dash(self):
        self.assertFalse([l for l in self.region if "\u2014" in l or re.search(r"[a-z)`]\. [A-Z`]", l)])

    def one(self, word):
        found = [l for l in self.region if word in l]
        self.assertEqual(len(found), 1, (word, found))
        return found[0]

    def test_the_chain_and_naming_line_default_to_project_branch_layers(self):
        chain = self.one("(main) <- ")
        self.assertIn("`(main) <- <project branch>-layer-01 <- <project branch>-layer-02 <- ...`", chain)
        self.assertNotIn("fast/", chain)
        naming = self.one("`task_prefix`")
        for word in ("`<project branch>-NNN`", "-tests-NNN`", "-layer-NN`", "every project"):
            self.assertIn(word, naming)
        # The old default, tied to a stored branch, is gone.
        self.assertFalse([l for l in self.region if "else `fast/" in l or "stored `branch`" in l])

    def test_the_naming_line_keeps_the_legacy_rule_with_both_prefixes(self):
        naming = self.one("`task_prefix`")
        for word in ("`fast/<project>/`", "`layer_prefix`", "verify.json", "`clean`"):
            self.assertIn(word, naming)
        self.assertLess(naming.index("<project branch>-NNN"), naming.index("fast/<project>/"), "new names first, legacy after")

    def test_the_cap_line_follows_the_grouping_line(self):
        cap = self.one("`max_stack_prs`")
        at = self.region.index(cap)
        self.assertTrue(self.region[at - 1].startswith("Consecutive tasks, in stack order, are grouped into one layer"))
        self.assertEqual(at, len(self.region) - 1, "the cap line sits just above the lockfile line")
        self.assertIn("Tasks stay tiny for implementers; the layers are sized for the reviewer.", self.region)
        for word in ("~/.compose/config.json", "verify.json", "adjacent", "fold", "same kind first"):
            self.assertIn(word, cap)
        self.assertRegex(cap, r"\bdefault[^.;]*\b10\b")

    def test_only_the_cli_stack_row_names_the_cap(self):
        cli = read("docs/cli.md").splitlines()
        rows = [l for l in cli if "max_stack_prs" in l]
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0].startswith("| `stack` | Cuts passed task branches into review layers (`review_layer_lines`"))
        self.assertIn("`--submit` pushes and opens one draft PR per layer", rows[0])
        at = cli.index(rows[0])
        self.assertTrue(cli[at - 1].startswith("| `verify` | Runs the local pipeline."))
        self.assertEqual(cli[at + 1], "| `top` | Checks out the stack top for manual testing. `--detach` releases it |")


class PrTextDocs(unittest.TestCase):
    def setUp(self):
        # sections/4.md's Lockfiles line and its closing `Nothing is pushed.` lines, held by their untouched neighbours.
        self.lines = read("sections/4.md").splitlines()
        at = self.index("Lockfiles (`package-lock.json`, `bun.lock`, `yarn.lock`, `pnpm-lock.yaml`, `go.sum`, any `*.lock`) are not counted, in a layer or in a task's 350 lines; ")
        self.lockfiles = self.lines[at : self.index("The project branch is not a PR: its groundwork commits")]
        self.pushed = self.lines[self.index("The CLI keeps Linear's state table true on the full path") + 1 :]
        self.text = "\n".join(self.pushed)

    def index(self, start):
        found = [i for i, l in enumerate(self.lines) if l.startswith(start)]
        self.assertEqual(len(found), 1, start)
        return found[0]

    def test_the_size_stays_on_the_layer_line_out_of_the_pr_body(self):
        line = " ".join(self.lockfiles)
        self.assertIn("each `layer` line names them and their size", line)
        self.assertIn("never in the PR body", line)
        self.assertNotIn("## What changed", line)

    def test_each_pr_is_titled_from_its_commits(self):
        self.assertTrue(self.pushed[0].startswith("Nothing is pushed."), self.pushed[0])
        self.assertRegex(self.text, r"\btitles?\b[^.]*\bcommits\b")
        for word in ("subject", "`and N more tasks`", "`Groundwork for <feature title>`", "72 characters", "groundwork", "Jira", "`(i/n)`", "feature's title for a one-PR stack", "one whole story", "more than one PR"):
            self.assertIn(word, self.text)

    def test_the_body_has_four_sections_in_order_from_issues_and_results_with_no_model(self):
        at = [self.text.find(h) for h in ("## Why", "## What changed", "## How it was tested", "## Tickets")]
        self.assertNotIn(-1, at)
        self.assertEqual(at, sorted(at))
        self.assertRegex(self.text, r"task issues?\b[^.]*\bresults\b")
        self.assertRegex(self.text, r"\b(no|without( a| any)?|never( calls)?( a| any)?)\s+model\b")
        self.assertFalse([l for l in self.lines if "size and generated files into the PR body" in l])
        for said in ("one line per task", "test commits folded in", "`compose verify passed`", "no attempt or round counts"):
            self.assertIn(said, self.text)

    def test_the_team_lead_writes_each_layers_title_why_and_what_changed_after_verify_and_before_the_preview(self):
        self.assertRegex(self.text, r"After verify passes and before you show the product owner the preview, write each layer's PR text")
        for said in ("`[written: .compose/<slug>/pr/<name>.md]`", "`[generated: write ", "never applied", "`# <title>`", "a reviewer who has not read the tickets",
                     "not one per commit", "is refused on its `pr` line with the reason", "never restate commit subjects", "ticket ids", "layer or groundwork"):
            self.assertIn(said, self.text)
        example = self.text.split("```markdown\n", 1)[1].split("```", 1)[0].splitlines()
        self.assertEqual([l for l in example if l.startswith("#")], ["# Keep the toggle's state across page reloads", "## Why", "## What changed"])
        self.assertIn("write each layer's PR text and show the `compose stack` preview", read("sections/6.md"))

    def test_the_submit_check_and_push_rule_stay_and_house_rules_hold(self):
        for kept in ("`gh stack submit --auto`", "`submit PR #N on <base>`", "`NO PR for <branch>`"):
            self.assertIn(kept, self.text)
        self.assertIn("Run it only when the user has explicitly asked for that push.", self.pushed)
        for l in self.lockfiles + self.pushed:
            self.assertNotIn("—", l)
            self.assertNotRegex(l, r"[a-z)`]\. [A-Z`]", l)


class ConfigKeysDocs(unittest.TestCase):
    def setUp(self):
        self.text = read("docs/workspaces.md")

    def section(self, title):
        self.assertEqual(self.text.count("\n## " + title + "\n"), 1, title)
        return self.text.split("\n## " + title + "\n", 1)[1].split("\n## ", 1)[0]

    def lines(self, *words):
        # The new section's lines holding every word, joined; at least one must.
        found = [l for l in self.section("Branch names and stack size").splitlines() if all(w in l for w in words)]
        self.assertTrue(found, words)
        return "\n".join(found)

    def test_the_example_config_parses_with_both_keys_at_their_defaults(self):
        cfg = json.loads(self.section("Config").split("```json\n", 1)[1].split("```", 1)[0])
        self.assertEqual((cfg.get("branch_prefix"), cfg.get("max_stack_prs")), ("compose/", 10))
        for name, ws in cfg.get("workspaces", {}).items():
            self.assertFalse({"branch_prefix", "max_stack_prs"} & set(ws), name)

    def test_the_section_sits_right_after_parallel_and_right_before_linear_key(self):
        self.section("Branch names and stack size")
        headings = re.findall(r"(?m)^## (.+)$", self.text)
        at = headings.index("Branch names and stack size")
        self.assertEqual(headings[at - 1 : at + 2], ["Parallel", "Branch names and stack size", "Linear key"])

    def test_branch_prefix_default_empty_value_and_init_record(self):
        self.assertRegex(self.lines("`branch_prefix`", "default"), r"default[^.;]*`compose/`")
        self.assertRegex(self.lines("slug"), r"empty|`\"\"`")
        self.assertRegex(self.lines("`init`", "verify.json", "`branch`"), r"\bonce\b")

    def test_the_three_branch_shapes(self):
        shapes = self.lines("-tests-NNN`", "-layer-NN`")
        self.assertRegex(shapes, r"(?<!tests)-NNN`")

    def test_max_stack_prs_default_override_and_fold(self):
        self.assertRegex(self.lines("`max_stack_prs`", "default"), r"default[^.;]*\b10\b")
        self.assertRegex(self.lines("verify.json"), r"overrid")
        self.assertRegex(self.lines("fold"), r"adjacent")

    def test_both_keys_can_be_set_per_workspace(self):
        self.assertRegex(self.lines("workspace"), r"top.level[^.]*workspace")

    def test_one_sentence_per_line_and_house_rules(self):
        for l in self.section("Branch names and stack size").splitlines():
            self.assertNotRegex(l, r"[a-z)`]\. [A-Z`]", l)
        self.assertNotIn("\u2014", self.text)
        self.assertNotRegex(self.text, r"\bgraft\b")


class ReadinessDocs(unittest.TestCase):
    def test_readiness_row_config_row_and_0b_paragraph(self):
        rows = [l for l in read("docs/cli.md").splitlines() if l.startswith("| `readiness`")]
        self.assertEqual(len(rows), 1)
        self.assertIn("A story labeled `needs-shaping` prints `not ready: needs shaping`, with no judge call and no open questions written.", rows[0])
        for word in ("Jev", "deep-dive", "models.readiness", "## Open questions", "(readiness check, <date>)", "--refresh", "--dry-run", "--json", ".compose/readiness.json"):
            self.assertIn(word, rows[0])
        cfg = [l for l in read("docs/config.md").splitlines() if l.startswith("| `models.readiness`")]
        self.assertEqual(len(cfg), 1)
        for word in ("claude:claude-opus-5-5", "high"):
            self.assertIn(word, cfg[0])
        text = read("sections/0b.md")
        for line in (
            "A story the readiness check judged not ready carries its questions under `## Open questions`, each tagged `(readiness check, <date>)`.",
            "Planning that story starts from those questions, asked one at a time like any other, and each answer goes into the story itself, never only into the conversation.",
            "Remove a question's line once its answer is on the story; the next check judges the story again.",
        ):
            self.assertEqual(text.count(line + "\n"), 1, line)


class ReadyDocs(unittest.TestCase):
    TASKS = ["sync", "linear <verb>", "tasks", "route", "quick <id>...", "run", "done", "triage", "resume", "reflect", "hold", "change start\\|done\\|undo", "spec-check", "ready", "delivery", "prd", "factory", "readiness", "bench"]

    def tasks_rows(self):
        sec = read("docs/cli.md").split("### Tasks\n", 1)[1].split("### Verify and stack", 1)[0]
        return [l for l in sec.splitlines() if l.startswith("| `")]

    def skill_0i(self):
        text = read("SKILL.md")
        body = text[text.index("### 0i. The levels: project, epic, feature, story, task") :].split("\n### ", 1)[0]
        return [l for l in body.splitlines()[1:] if not l.startswith("|")]

    def test_ready_row_follows_spec_check_and_the_other_rows_stay(self):
        rows = self.tasks_rows()
        self.assertEqual([r.split("`")[1] for r in rows], self.TASKS)
        self.assertEqual(len([l for l in read("docs/cli.md").splitlines() if l.startswith("| `ready`")]), 1)

    def test_ready_row_states_the_rule_and_its_outputs(self):
        rows = [r for r in self.tasks_rows() if r.startswith("| `ready`")]
        self.assertEqual(len(rows), 1, "one ready row in Tasks")
        row = rows[0]
        for word in ("`Ready`", "story", "feature", "epic", "Todo", "Backlog", "blocker", "Done", "Canceled", "Design", "In Review"):
            self.assertIn(word, row)
        for word in ("can start now:", "results/ready.json", "compose linear labels --create Ready", "## Open questions"):
            self.assertIn(word, row)
        self.assertNotIn("\u2014", row)

    def test_skill_0i_labels_sentence_names_ready(self):
        lines = [l for l in self.skill_0i() if all(f"`{w}`" in l for w in ("Epic", "Story", "Task"))]
        self.assertEqual(len(lines), 1)
        self.assertIn("`Ready`", lines[0])
        self.assertIn("compose linear labels --create", lines[0])

    def test_skill_0i_says_what_ready_means_and_who_sets_it(self):
        lines = [l for l in self.skill_0i() if "`compose ready`" in l]
        self.assertEqual(len(lines), 1, "one sentence names compose ready in 0i")
        for word in ("`Ready`", "story", "feature", "can start now"):
            self.assertIn(word, lines[0])
        self.assertNotIn("`Epic`", lines[0])
        self.assertNotRegex(lines[0], r"[.!?] [A-Z`]", "one sentence per line")
        self.assertNotIn("\u2014", lines[0])


class ReadyCheckpoints(unittest.TestCase):
    def skill_0s(self, body=False):
        text = read("SKILL.md")
        sec = text[text.index("### 0s. Every level's state, kept true") :].split("\n### ", 1)[0]
        return sec if body else [l for l in sec.splitlines()[1:] if not l.startswith("|")]

    def row(self, cmd):
        rows = [l for l in read("docs/cli.md").splitlines() if l.startswith(f"| `{cmd}`")]
        self.assertEqual(len(rows), 1, f"one {cmd} row")
        return rows[0]

    def one_line(self, lines, pred, what):
        found = [l for l in lines if pred(l)]
        self.assertEqual(len(found), 1, what)
        self.assertNotRegex(found[0], r"[.!?] [A-Z`]", "one sentence per line")
        self.assertNotIn("\u2014", found[0])
        return found[0]

    def test_0s_tells_the_lead_and_designer_to_run_ready_at_each_checkpoint(self):
        line = self.one_line(self.skill_0s(), lambda l: "compose ready --project <slug>" in l, "one 0s sentence runs compose ready")
        for word in ("checkpoint", "team lead", "designer"):
            self.assertIn(word, line)

    def test_0s_says_which_commands_refresh_the_marks(self):
        line = self.one_line(self.skill_0s(), lambda l: "`Ready`" in l and "refresh" in l, "one 0s sentence on the CLI's refreshes")
        self.assertNotIn("compose ready --project", line)
        for word in (r"roll-?\s?ups?", r"\bsync\b", r"\brun\b", r"\bverify\b.*\bPASS\b", r"\bclean\b"):
            self.assertRegex(line, word)

    def test_0s_keeps_its_checkpoint_reconcile_and_the_sentences_stay_in_0s(self):
        lines = self.skill_0s()
        self.assertTrue(any(l.startswith("Checkpoints, each followed by a reconcile of the whole tree") for l in lines))
        outside = read("SKILL.md").replace(self.skill_0s(body=True), "")
        self.assertNotIn("compose ready --project <slug>", outside)

    def test_cli_ready_row_names_the_commands_that_refresh_too(self):
        row = self.row("ready")
        for word in ("sync", "run", "verify", "clean"):
            self.assertRegex(row, rf"`(compose )?{word}`")

    def test_cli_init_row_names_the_herdr_tabs_and_how_to_skip_them(self):
        row = self.row("init")
        for word in ("`<slug> shell`", "`COMPOSE_NO_HERDR=1`"):
            self.assertIn(word, row)
        self.assertNotIn("`<slug> status`", row)
        self.assertNotIn("\u2014", row)

    def test_cli_sync_and_clean_rows_name_ready_and_keep_what_they_said(self):
        tasks = read("docs/cli.md").split("### Tasks\n", 1)[1].split("### Verify and stack", 1)[0]
        projects = read("docs/cli.md").split("### Projects\n", 1)[1].split("### Tasks\n", 1)[0]
        for cmd, sec, kept in (("sync", tasks, "--stdin"), ("clean", projects, "--delete-unmerged")):
            row = self.row(cmd)
            self.assertIn(row, sec)
            self.assertRegex(row, r"\bReady\b")
            self.assertIn(kept, row)
            self.assertNotIn("\u2014", row)


class TellWhatCanStart(unittest.TestCase):
    CAN_START = "can start now: NEB-x <title>, ..."

    def one_sentence(self, line):
        self.assertIn("event ready=", line)
        self.assertIn(self.CAN_START, line)
        self.assertIn("product owner", line)
        self.assertNotRegex(line, r"[.!?] [A-Z`]", "one sentence per line")
        self.assertNotIn("\u2014", line)

    def test_manager_tells_the_product_owner_unprompted_in_one_line(self):
        lines = read("sections/0h.md").splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("The engineering manager supervises"))
        end = next(i for i in range(start, len(lines)) if not lines[i].strip())
        para = lines[start:end]
        found = [l for l in para if "event ready=" in l]
        self.assertEqual(len(found), 1, "one sentence on event ready= in the supervises paragraph")
        self.assertEqual([l for l in lines if "event ready=" in l], found, "and nowhere else in 0h")
        line = found[0]
        self.one_sentence(line)
        self.assertIn("event ready=<ids>", line)
        self.assertIn("unprompted", line)
        self.assertIn("one line", line)
        self.assertRegex(line, r"\b(no|not|never|without)\b[^.]*desktop notification")
        self.assertTrue(para[0].startswith("The engineering manager supervises, it does not only relay."))
        self.assertTrue(any(l.startswith("On a stall, read the team lead's pane") for l in para))

    def test_designer_starter_passes_ready_on_the_same_way(self):
        lines = read("sections/0d.md").splitlines()
        i = next(i for i, l in enumerate(lines) if l.startswith("The starter only relays"))
        self.assertIn("compose watch --project <slug> --from designer", lines[i])
        self.assertNotIn("event ready=", lines[i])
        line = lines[i + 1]
        self.one_sentence(line)
        self.assertIn("starter", line)
        self.assertIn("same way", line)
        self.assertEqual([l for l in lines if "event ready=" in l], [line], "one sentence in 0d")
        self.assertTrue(lines[i + 2].startswith("It is never inline"))


class ReadyEventDocs(unittest.TestCase):
    PROJECTS = ["setup", "update", "doctor", "init", "projects", "status", "clean", "trail", "reconcile"]

    def rows(self, start, end):
        sec = read("docs/cli.md").split(start + "\n", 1)[1].split(end, 1)[0]
        return [l for l in sec.splitlines() if l.startswith("| `")]

    def row(self, rows, cmd):
        found = [l for l in rows if l.startswith(f"| `{cmd}`")]
        self.assertEqual(len(found), 1, f"one {cmd} row in its section")
        self.assertEqual(len([l for l in read("docs/cli.md").splitlines() if l.startswith(f"| `{cmd}`")]), 1)
        self.assertNotIn("\u2014", found[0])
        return found[0]

    def projects_rows(self):
        return self.rows("### Projects", "### Tasks")

    def test_watch_row_names_the_ready_event_and_only_new_issues(self):
        row = self.row(self.rows("### Teammates and supervision", "### Workspaces"), "watch")
        self.assertIn("event ready=<ids>", row)
        self.assertRegex(row, r"\bonly\b[^|]*\bjust became ready\b")
        for kept in ("Blocks until something happens", "--now"):
            self.assertIn(kept, row)

    def test_status_row_shows_the_can_start_now_line_from_the_ready_list(self):
        row = self.row(self.projects_rows(), "status")
        self.assertIn("can start now:", row)
        self.assertIn("results/ready.json", row)
        self.assertNotIn("--watch", row)

    def test_projects_row_ends_each_line_with_can_start_now(self):
        row = self.row(self.projects_rows(), "projects")
        self.assertRegex(row, r"\bend(s|ing)? with\b[^|]*can start now:")
        self.assertIn("done/total", row)

    def test_only_existing_rows_change(self):
        self.assertEqual([r.split("`")[1] for r in self.projects_rows()], self.PROJECTS)
        sup = self.rows("### Teammates and supervision", "### Workspaces")
        self.assertFalse([r for r in sup if "ready" in r.split("|")[1]], "no new ready row in supervision")


class LinkFeatures(unittest.TestCase):
    EPIC = "- Epic: one planner per feature, in parallel."
    LINK = "Link a feature blocked by another feature only when it needs that feature's code."

    def bullet(self):
        lines = read("sections/0a.md").splitlines()
        i = next(i for i, l in enumerate(lines) if l.startswith("- A project made with `init --after <other-slug>`"))
        end = next(j for j in range(i + 1, len(lines)) if not lines[j].startswith("  "))
        return lines, lines[i:end]

    def one(self, lines, bullet, *words):
        found = [l for l in bullet if all(w in l for w in words)]
        self.assertEqual(len(found), 1, f"one line in the --after bullet with {words}")
        self.assertEqual([l for l in lines if all(w in l for w in words)], found, "and nowhere else in 0a")
        self.assertNotRegex(found[0], r"[.!?] [A-Z`]", "one sentence per line")
        return found[0]

    def test_after_bullet_says_sync_links_the_features(self):
        lines, bullet = self.bullet()
        line = self.one(lines, bullet, "`sync`", "blocked by")
        self.assertRegex(line, r"`sync` links\b")
        self.assertEqual(line.count("feature issue"), 2)
        self.assertIn("that project's feature issue", line)

    def test_after_bullet_links_only_on_a_code_need(self):
        lines, bullet = self.bullet()
        line = self.one(lines, bullet, "only when", "needs that feature's code")
        self.assertIn("blocked by", line)
        for verb in ("calls", "imports", "edits"):
            self.assertIn(verb, line)
        self.assertIn("what that feature builds", line)
        self.assertNotIn("`sync`", line)

    def test_after_bullet_keeps_its_lines(self):
        _, bullet = self.bullet()
        self.assertIn("records it as `review_base` in its `verify.json`", bullet[0])
        for kept in ("Its layers stack on that project's branch", "`clean` counts a merge into either."):
            self.assertTrue(any(kept in l for l in bullet[1:]), kept)
        self.assertFalse([l for l in bullet if "\u2014" in l])

    def test_planner_epic_line_ends_with_the_link_rule(self):
        brief = read("bin/fc_planner_brief.py").split('PLANNER_BRIEF = """', 1)[1].split('"""', 1)[0]
        lines = [l.strip() for l in brief.splitlines()]
        epic = [i for i, l in enumerate(lines) if l.startswith(self.EPIC)]
        self.assertEqual(len(epic), 1)
        line = lines[epic[0]]
        self.assertTrue(line.endswith(" " + self.LINK), line)
        self.assertIn("An epic whose features cannot be named yet goes back to shaping (0b): report needs-user and stop.", line)
        self.assertEqual(brief.count(self.LINK), 1)
        self.assertTrue(lines[epic[0] + 1].startswith("- Feature: "))


class DesignWaitsDocs(unittest.TestCase):
    DESIGNER = "The designer syncs (step 2), then starts every builder at once"

    def test_designcrews_row_starts_only_unblocked_tasks_and_prints_the_rest(self):
        cli = read("docs/cli.md")
        sec = cli.split("### Teammates and supervision\n", 1)[1].split("### Workspaces", 1)[0]
        rows = [l for l in sec.splitlines() if l.startswith("| `designcrews`")]
        self.assertEqual(len(rows), 1, "one designcrews row in Teammates and supervision")
        self.assertEqual(len([l for l in cli.splitlines() if l.startswith("| `designcrews`")]), 1)
        row = rows[0]
        self.assertRegex(row, r"\bonly\b[^|]*\bblock(ers?|ed by)\b[^|]*In Review or Done")
        self.assertIn("<id> waits on <ids>", row)
        self.assertEqual(row.count("waits on"), 1)
        for kept in ("design-NNN", "--only", "--max N", "--print", "--force", "design_concurrency"):
            self.assertIn(kept, row)
        self.assertNotIn("—", row)
        self.assertEqual(row.count(" | "), 1, "still two cells")

    def test_0d_says_a_design_task_waits_right_after_the_designer_line(self):
        lines = read("sections/0d.md").splitlines()
        i = next(i for i, l in enumerate(lines) if l.startswith(self.DESIGNER))
        self.assertIn("compose designcrews --project <slug>", lines[i])
        self.assertNotIn("In Review or Done", lines[i])
        line = lines[i + 1]
        for word in ("design task", "every task", "blocked by", "In Review or Done"):
            self.assertIn(word, line)
        self.assertRegex(line, r"\bwaits? until\b")
        self.assertNotRegex(line, r"[.!?] [A-Z`]", "one sentence per line")
        self.assertTrue(line.endswith("."))
        self.assertNotIn("—", line)
        self.assertEqual([l for l in lines if "In Review or Done" in l], [line], "one such sentence in 0d")
        self.assertTrue(lines[i + 2].startswith("`--only` picks tasks"))


SETUP_STEPS = [
    "2. Answer `compose setup`.",
    "   On a terminal it asks one question at a time: Quick start or Custom, a yes or no for each missing step under Custom, then each key with hidden input.",
    "   With `--yes`, or when there is no terminal, it runs the same steps with every default, never prompts, and ends by listing what is still missing.",
    "   It installs the tools `compose` needs, links the CLI and the skill, and saves your keys in `~/.compose/auth.json` (mode 600).",
    "   It sets up herdr with the notification settings `compose` needs, declares pi's OpenRouter provider, writes the default model choices, and ends with a model probe.",
]
SETUP_ROW = "| `compose setup [--yes]` | Installs and configures everything `compose` needs, one question at a time on a terminal; `--yes`, or no terminal, takes every default |"
DEVELOPMENT = [
    "## Development",
    "",
    "Run the CLI tests with `python3 -m tests.run test_x.py ...` for the files you changed, or `python3 -m tests.run --all` for every file (a verify or run check needs no flag), which runs the test files in parallel (`-j N`, else `COMPOSE_TEST_JOBS`, else a quarter of the cores) and prints unittest's summary; `python3 -m unittest discover -s tests` runs the same tests serially.",
    "Type-check the CLI with `npx -y pyright@1.1.414 --pythonpath /usr/bin/python3` (strict; the error count is reported, nothing is gated on it).",
    "Run the jev tests with `cd jev && uv run pytest`, and the bp tests with `cd bp && npm test`.",
    "The CLI core is stdlib; `jev/` is a uv project, and `bp/` is one Node file with no dependencies.",
    "",
    "## Credits",
]
CLI_HEAD = "| `setup` | Installs and configures everything `compose` needs ([new-machine.md](new-machine.md)). On a terminal it asks one question at a time (Quick start or Custom, a yes or no per missing step under Custom, each key with hidden input); `--yes`, or no terminal, runs the same steps with every default and never prompts."
CLI_RULES = " It shows the Claude Code permission rules `compose` needs (`gh pr view`, `compose land`, moving a `compose/*` branch) and writes them to `~/.claude/settings.json` only on your yes; `--yes` and a dry run never write them, and rules that stay asking (`git push`, force-push, `gh pr merge`, `clean --delete-unmerged`, `clean --abandon`, `crew stop --force`) are never added."
CLI_EXITS = " Without `--yes` it also exits 1 when a required key is missing or the model probe fails. With `--yes` it exits 0 and ends with `compose is installed but not ready:` when a required step is missing |"


class HarnessHomePage(unittest.TestCase):
    def setUp(self):
        import sys; sys.path.insert(0, str(ROOT / "bin"))
        import fc_harness
        self.reserved, self.keys = fc_harness.RESERVED, [k for k, v in fc_harness.HARNESSES.items() if v]

    def test_page_names_every_reserved_name_and_row_key(self):
        page = read("docs/harnesses.md")
        self.assertTrue(self.reserved and self.keys)
        for key in (*self.reserved, *self.keys):
            self.assertIn(f"`{key}", page, f"{key!r} not documented")

    def test_page_covers_the_prefixes_roles_and_what_is_not_supported(self):
        page = read("docs/harnesses.md")
        for word in ("`claude:`", "`pi:`", "OpenCode", "codex exec", "$compose", "config.md", "NEB-1490", "NEB-1524"):
            self.assertIn(word, page, word)

    def test_readme_links_the_page_and_names_codex_in_the_quick_start(self):
        readme = read("README.md")
        self.assertRegex(readme, r"(?m)^\| \[harnesses\.md\]\(docs/harnesses\.md\) \|")
        quick = readme.split("## Quick start")[1].split("\n## ")[0]
        self.assertIn("`codex`", quick)
        self.assertIn("`$compose", quick)
        self.assertIn("`/compose", quick)

    def test_no_em_dash_and_no_claude_only_test_reader(self):
        for rel in ("README.md", "docs/harnesses.md"):
            self.assertNotIn("\u2014", read(rel), rel)
            self.assertNotRegex(read(rel), r"(?i)test_reader[^\n]*must be (a )?`?claude:", rel)


class SetupDocs(unittest.TestCase):
    def assertBlock(self, lines, block):
        starts = [i for i, l in enumerate(lines) if l == block[0]]
        self.assertEqual(len(starts), 1, f"one line {block[0]!r}")
        self.assertEqual(lines[starts[0]:starts[0] + len(block)], block)

    def test_readme_setup_steps_in_place(self):
        self.assertBlock(read("README.md").splitlines(), SETUP_STEPS)

    def test_readme_setup_row_in_place(self):
        lines = read("README.md").splitlines()
        self.assertBlock(lines, ["| Command | What it does |", "|---|---|", SETUP_ROW])
        self.assertEqual([l for l in lines if l.startswith("| `compose setup [--yes]`")], [SETUP_ROW])
        self.assertIn("| `compose update` | Pulls this checkout (`--ff-only`), relinks what `compose` owns, and runs `doctor` |", lines)

    def test_readme_development_drops_the_setup_app(self):
        self.assertBlock(read("README.md").splitlines(), DEVELOPMENT)

    def test_cli_setup_row_keeps_the_rest(self):
        rows = [l for l in read("docs/cli.md").splitlines() if l.startswith("| `setup` |")]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith(CLI_HEAD), rows[0])
        self.assertIn(CLI_RULES, rows[0])
        self.assertTrue(rows[0].endswith(CLI_EXITS), rows[0])

    def test_old_words_are_gone(self):
        for rel in ("README.md", "docs/cli.md"):
            for word in ("Textual", "guided", "plain path", "setup/tests"):
                self.assertNotIn(word, read(rel), f"{word!r} in {rel}")


MACHINE = {
    17: "",
    18: "On a terminal, `compose setup` asks one question at a time: Quick start takes every default, Custom asks a yes or no for each missing step.",
    19: "With `--yes`, or without a terminal, it runs the same steps with every default and never prompts.",
    20: "A key you did not enter is skipped, and the ending lists it with the command that sets it later.",
    45: "",
    46: "With `--yes`, setup ends with `compose is ready`, or with `compose is installed but not ready:` and one line per missing required step.",
    50: "",
    51: "A machine without a herdr config gets the config the maintainer uses, from `bin/herdr.toml` in the checkout.",
    52: "A machine with one keeps all of it, and setup sets only the two lines `compose` needs, so the only desktop alert is the one `compose` sends itself:",
}
REQUIREMENTS = {
    105: "| `~/.pi/agent/` | pi's `models.json` (setup declares `openrouter-fast`), its own `auth.json`, and the herdr extension |",
    106: "| `~/.config/herdr/config.toml` | herdr's config: copied from `bin/herdr.toml` when absent, else only the two notification lines are set |",
    107: "",
}


class MachineDocs(unittest.TestCase):
    def assertLines(self, rel, want):
        lines = read(rel).splitlines()
        for n, line in want.items():
            if line:
                self.assertIn(line, lines, f"{rel}:{n}")

    def test_new_machine_lines_in_place(self):
        self.assertLines("docs/new-machine.md", MACHINE)

    def test_requirements_line_in_place(self):
        self.assertLines("docs/requirements.md", REQUIREMENTS)

    def test_old_words_are_gone(self):
        for rel in ("docs/new-machine.md", "docs/requirements.md"):
            for word in ("guided", "plain path", "setup/herdr.toml"):
                self.assertFalse(word in read(rel), f"{word!r} in {rel}")

    def test_each_new_line_once(self):
        text = read("docs/new-machine.md")
        for n in (18, 19, 46, 51):
            self.assertEqual(text.count(MACHINE[n]), 1, MACHINE[n])
        self.assertEqual(read("docs/requirements.md").count("bin/herdr.toml"), 1)


class HarnessInstallDocs(unittest.TestCase):
    def setUp(self):
        import sys; sys.path.insert(0, str(ROOT / "bin"))
        import fc_harness, fc_steps
        self.table = [fc_harness.HARNESSES[k] for k in fc_harness.rows()]
        self.names = [[s[0] for s in fc_steps.STEPS if s[0].startswith(h["name"] + " ")] for h in self.table]
        self.machine, self.reqs = read("docs/new-machine.md"), read("docs/requirements.md")

    def section(self, text, title):
        return text.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]

    def test_new_machine_names_every_row_each_harness_makes_in_steps_order(self):
        table = self.section(self.machine, "What setup does")
        self.assertTrue(self.names and all(self.names))
        for names in self.names:
            for name in names:
                self.assertTrue([l for l in table.splitlines() if l.startswith("|") and name in l], name)
            self.assertEqual([table.index(n) for n in names], sorted(table.index(n) for n in names), names)
        self.assertIn("next: run codex in a pane and type $compose", self.machine)

    def test_requirements_names_each_home_env_a_codex_row_and_claude_as_optional(self):
        env, files = self.section(self.reqs, "Environment variables"), self.section(self.reqs, "Files outside the repo")
        for h in self.table:
            self.assertIn(h["home_env"], env, h["home_env"])
            self.assertIn(h["home_env"], files, h["home_env"])
        self.assertIn("COMPOSE_HARNESS", env)
        self.assertRegex(self.section(self.reqs, "On by default"), r"(?m)^\| Codex.*`codex login`")
        self.assertTrue([l for l in self.reqs.splitlines() if "Claude Code" in l and "optional" in l and "another harness" in l])

    def test_both_pages_link_harnesses_and_have_no_em_dash(self):
        for text in (self.machine, self.reqs):
            self.assertIn("](harnesses.md", text)
            self.assertNotIn("\u2014", text)


class HarnessCliRows(unittest.TestCase):
    def setUp(self):
        import sys; sys.path.insert(0, str(ROOT / "bin"))
        import fc_harness
        self.names = [fc_harness.HARNESSES[k]["name"] for k in fc_harness.rows()]
        cli = read("docs/cli.md").splitlines()
        self.setup, self.doctor, self.crew = ([l for l in cli if l.startswith(p)][0] for p in ("| `setup` |", "| `doctor` |", "| `crew start"))

    def test_setup_row_exits_1_only_without_any_harness_and_says_what_codex_gets(self):
        self.assertTrue(self.names)
        self.assertNotRegex(self.setup, r"exits 1 when Claude Code is missing\.")
        self.assertRegex(self.setup, r"exits 1 when Claude Code is missing and no other harness")
        for word in (*self.names, "skill", "command", "hooks", "trust"):
            self.assertIn(word, self.setup)

    def test_doctor_row_names_the_claude_only_rows_as_optional_beside_another_harness(self):
        for name in self.names:
            self.assertIn(f"optional, {name} is set up", self.doctor)
        for row in ("herdr skill", "compose skill link"):
            self.assertIn(row, self.doctor)

    def test_crew_row_names_each_harness_the_flag_the_env_the_sign_in_check_and_the_page(self):
        for word in (*self.names, "pi", "--harness", "COMPOSE_HARNESS", "harnesses.<name>", "codex login status", "sign in"):
            self.assertIn(word, self.crew)
        self.assertIn("](harnesses.md", self.crew)

    def test_the_three_rows_have_no_em_dash(self):
        for row in (self.setup, self.doctor, self.crew):
            self.assertNotIn("—", row)


DRY_ROW = "| `compose setup --dry-run` | Walks the same setup and changes nothing: every install, write, and key save prints as a `would` line, and the last line counts them |"
DRY_SENTENCE = " With `--dry-run` it asks the same questions and runs the same checks but changes nothing on the machine, prints each change as a `would` line, and ends with `dry run: nothing changed; N changes listed above`."
UPDATE_ROW = "| `compose update` | Pulls this checkout (`--ff-only`), relinks what `compose` owns, and runs `doctor` |"


class DryRunDocs(unittest.TestCase):
    def test_readme_row_right_after_the_setup_row(self):
        lines = read("README.md").splitlines()
        self.assertEqual(lines.count(SETUP_ROW), 1)
        i = lines.index(SETUP_ROW)
        self.assertEqual(lines[i:i + 3], [SETUP_ROW, DRY_ROW, UPDATE_ROW])

    def test_cli_sentence_right_after_the_defaults_sentence(self):
        rows = [l for l in read("docs/cli.md").splitlines() if l.startswith("| `setup` |")]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith(CLI_HEAD + DRY_SENTENCE + CLI_RULES), rows[0])
        self.assertTrue(rows[0].endswith(CLI_EXITS), rows[0])

    def test_each_file_names_the_flag_once(self):
        self.assertEqual(read("README.md").count("--dry-run"), 1)
        self.assertEqual(sum(l.count("--dry-run") for l in read("docs/cli.md").splitlines() if l.startswith("| `setup` |")), 1)  # `clean --abandon` has its own


FEATURE_HEAD = "- **feature**: if it has no approved design, shape it (step 0b). When it has no stories, the planner teammate proposes them as a `needs-user` list "
FEATURE_TAIL = " and creates them on the product owner's go; then it writes the plan (step 1) and splits (step 2) under each story."
LEAN_PAREN = "(one issue per story, each the thinnest slice that works end to end and unblocks the next, at most 5 criteria, in the order they should ship)"
PLANNING_MISS = "A second wave for polish or a toolchain fix the first wave could have seen is a planning miss."
LEAN_LINES = [
    "Stories are lean: each is the thinnest slice that works end to end and unblocks the next story, with at most 5 criteria.",
    'Split cues in a story: "all", "every", several When/Then pairs, or several outside systems.',
    "When no split fits, a time-boxed probe answers the unknown first, then the split.",
]


class LeanStoryRule(unittest.TestCase):
    def test_feature_row_holds_the_lean_parenthesis_and_nothing_else_changes(self):
        rows = [l for l in read("SKILL.md").splitlines() if l.startswith("- **feature**:")]
        self.assertEqual(rows, [FEATURE_HEAD + LEAN_PAREN + FEATURE_TAIL])
        self.assertNotIn("one issue per user-visible slice", rows[0])

    def test_story_row_of_the_table_keeps_its_slice_wording(self):
        rows = [l for l in read("SKILL.md").splitlines() if l.startswith("| **story** |")]
        self.assertEqual(len(rows), 1)
        self.assertIn("its description is one user-visible slice with acceptance criteria", rows[0])

    def test_three_lines_right_after_the_planning_miss_line_once_each(self):
        lines = read("sections/1.md").splitlines()
        self.assertEqual(lines.count(PLANNING_MISS), 1)
        i = lines.index(PLANNING_MISS)
        self.assertEqual(lines[i + 1:i + 4], LEAN_LINES)
        self.assertTrue(lines[i + 4].startswith("The plan also states the simplicity bar"), "the three lines are added, not swapped in")
        for line in LEAN_LINES:
            self.assertEqual(lines.count(line), 1, line)

    def test_skill_stays_under_the_size_cap(self):
        self.assertLess(len(read("SKILL.md").encode()), 30000)

    def test_no_outside_source_named_in_the_skill(self):
        for path in [ROOT / "SKILL.md"] + sorted((ROOT / "sections").glob("*.md")):
            for word in ("Product-Manager-Skills", "deanpeters"):
                self.assertNotIn(word, path.read_text(), f"{word!r} in {path.name}")


class LeanDesignStories(unittest.TestCase):
    LEAN = "Design stories are lean: one flow's lowfi or one screen's hifi each, cut just ahead of the code story it unblocks, walking skeleton flow first, lowfi then hifi inside each flow."
    OLD = "| **epic** | Check for a design system (below). None: write the design-system epic with its features and stories, send its open decisions up as open questions, build Foundations once they are answered. One exists: continue at the first story not Done under it or under the epic's features."

    def epic_row(self):
        lines = read("sections/0d.md").splitlines()
        rows = [i for i, l in enumerate(lines) if l.startswith("| **epic** |")]
        self.assertEqual(len(rows), 1, "one epic row in 0d")
        return lines, rows[0]

    def test_epic_row_ends_its_last_cell_with_the_lean_sentence(self):
        lines, i = self.epic_row()
        row = lines[i]
        self.assertTrue(row.endswith(self.LEAN + " |"), row)
        self.assertTrue(row.startswith(self.OLD), "the cell keeps its old text before the new sentence")
        self.assertRegex(row[len(self.OLD):-len(self.LEAN) - 2], r"^ +$")

    def test_row_stays_one_line_with_two_cells(self):
        lines, i = self.epic_row()
        row = lines[i]
        self.assertEqual(row.count("|"), 3)
        self.assertEqual(row.count(" | "), 1)
        self.assertEqual(lines[i - 1], "|---|---|")
        self.assertTrue(lines[i + 1].startswith("| **feature** |"))
        self.assertNotIn("—", row)

    def test_sentence_appears_once_in_0d(self):
        text = read("sections/0d.md")
        self.assertEqual(text.count(self.LEAN), 1)
        self.assertEqual(text.count("Design stories are lean"), 1)


PLAN_LINES = [
    "**The delivery plan.**",
    "The planner rewrites its judgment sections (Outside waits, How this maps to compose, Priority when something has to give) at each re-plan, with `compose linear doc update <id> --section \"<heading>\" --file <path>`.",
    "Priority is the ranked list under Priority when something has to give; Linear's priority field stays unused.",
    "The team lead and the engineering manager never edit the delivery plan by hand: the CLI rewrites its data sections and the planner its judgment sections.",
    "A change to the order across features goes through the product delivery plan (sections/0p.md).",
]
REPLAN = [
    "**Re-plan.**",
    "1. If no change is open: sync, then `compose change start --why \"<one line>\"`.",
    "2. The planner teammate edits Linear: `## Plan`; it cancels the tasks that rest on the old line (a canceled task loses its `compose` block), edits the briefs that change, and adds new tasks with the next ids, and `compose testcrews --only <new and changed ids> --redo` for their tests: `--redo` rewrites the test of a passed task whose brief changed.",
    "3. The 3b gate applies unchanged: a list that matches what the product owner just settled dispatches at once, and anything past it goes back up.",
    "4. Settle each task `compose status` shows paused: `compose change keep|part|discard <id> [--files a,b]`; keep when its work still fits the new brief, part for the files that still do, discard when what it rested on is gone.",
    "5. Sync, then `compose change done`: it comments the change on the feature, and refuses while a paused task is unsettled.",
    "6. `compose hold --release <ids>`, then `compose run --only <new and redone ids> --redo` for the layers built on the old line, adding the paused tasks you kept, part kept or discarded when the run has ended (a live run restarts them on release).",
]


class PlanOwnership(unittest.TestCase):
    def setUp(self):
        self.lines = read("sections/8.md").splitlines()

    def test_the_plan_lines_and_a_blank_right_before_undo(self):
        i = self.lines.index("**Undo and update.**")
        self.assertEqual(self.lines[i - len(PLAN_LINES) - 1:i], PLAN_LINES + [""])

    def test_each_line_written_once(self):
        for line in PLAN_LINES:
            self.assertEqual(self.lines.count(line), 1, line)

    def test_replan_list_unchanged_and_nothing_else_between(self):
        self.assertEqual(self.lines.count(REPLAN[0]), 1)
        i = self.lines.index(REPLAN[0])
        self.assertEqual(self.lines[i:i + 7], REPLAN)
        self.assertEqual(self.lines.count(PLAN_LINES[0]), 1, "no delivery plan block")
        j = self.lines.index(PLAN_LINES[0])
        self.assertGreater(j, i + 6)
        self.assertEqual([l for l in self.lines[i + 7:j] if l.strip()], [])

    def test_hold_pauses_running_work(self):
        self.assertIn("1. Hold first: `compose hold --project <slug> --rests-on <decision ids, issue ids, or reflect> --reason \"<one line>\"` holds every task that rests on the change, and pauses the running ones within seconds with their work kept.", self.lines)
        self.assertIn("   Hold before you confirm: a pause costs nothing, and a release with no change keeps all the work.", self.lines)
        rows = {l.split("|")[1].strip(): l for l in read("docs/cli.md").splitlines() if l.startswith("| `")}
        hold = rows["`hold`"]
        self.assertIn("--rests-on", hold)
        self.assertIn("one already running pauses within seconds and keeps its work on a ref", hold)
        self.assertIn("keep\\|part\\|discard", rows["`change start\\|done\\|undo`"] if "`change start\\|done\\|undo`" in rows else next(l for k, l in rows.items() if k.startswith("`change")))

    def test_house_rules(self):
        self.assertNotIn("\u2014", read("sections/8.md"))
        for line in PLAN_LINES:
            self.assertNotRegex(line, r"[.!?] [A-Z]")


class ManagerProseNamesPostures(unittest.TestCase):
    AWAY = "**Away: the product owner is away** (`/compose:away [--push] [words]`, `compose away`; modeled on firstmate's away mode)."
    BIAS = "Bias toward exit: when unsure whether a message is the return, it is."
    YOLO = [
        "**Yolo: the engineering manager answers until an explicit off** (`/compose:yolo [--push] [words]`, `compose yolo`).",
        "Yolo follows every Away rule above, with the `yolo:` prefix, the `(yolo recommendation, unconfirmed, <date>)` tag, and `compose yolo` in place of `compose away`; only the exit differs.",
        "Only an explicit off ends it: the message `yolo:off` (or `yolo off`), `/compose:yolo off`, or `compose yolo off --project <slug>`; then run `compose yolo off --project <slug>` (every project of an epic), relay the digest, and ask its \"To confirm\" decisions in one ask.",
        "Every other message is a steer: act on it at once (a change of plans goes to the team lead; an answer to an open question is recorded with `compose decide --issue <issue> --new \"<decision>\" --quote \"<their words>\" --closes <id>`, and a word on a tagged line with its bare id, which rewrites it as theirs; each wakes the team lead), reply with what changed, and yolo stays on; \"To confirm\" waits for the off.",
        "`compose away digest --project <slug>` or `compose yolo digest --project <slug>` prints the digest and leaves the posture on.",
    ]
    NEVER = [
        "The never-set still stops and lands in \"Waiting on you\": destructive, irreversible, and security-sensitive actions are never pre-authorizable whatever the words say.",
        "That covers anything that costs money beyond the configured models (a model or provider switch, keys), the payment halt, merging a toolsmith fix into compose itself, force-push, `clean --delete-unmerged`, `clean --abandon`, `crew stop --force`, attended prompts (Frame0 Allow, sign-ins), and anything the product owner's CLAUDE.md forbids.",
    ]

    def setUp(self):
        self.lines = read("sections/0h.md").splitlines()

    def test_away_paragraph_is_the_old_paragraph_for_away(self):
        self.assertEqual(self.lines.count(self.AWAY), 1)
        self.assertEqual(self.lines.count(self.BIAS), 1)
        start, end = self.lines.index(self.AWAY), self.lines.index(self.YOLO[0])
        self.assertLess(self.lines.index(self.BIAS), end)
        away = "\n".join(self.lines[start:end])
        for want in ["compose away off --project <slug>", "away: <question ids>: accept-all", "compose decide --project <slug> --issue <issue> --accept-all",
                     "(away recommendation, unconfirmed, <date>)", "compose away decided --project <slug>",
                     'compose decide --project <slug> "<id>" --quote "<their words>"']:
            self.assertIn(want, away)
        for gone in ["compose yolo", "yolo:", "/compose:yolo", "(yolo recommendation"]:
            self.assertNotIn(gone, away)

    def test_yolo_paragraph_follows_the_away_paragraph_line_by_line(self):
        i = self.lines.index(self.YOLO[0])
        self.assertEqual(self.lines[i:i + 5], self.YOLO)
        self.assertEqual(self.lines.count(self.YOLO[0]), 1)
        self.assertGreater(i, self.lines.index(self.BIAS))
        self.assertEqual(self.lines[i - 1], "")
        self.assertEqual(self.lines[i - 2], self.BIAS)

    def test_never_set_lines_and_no_em_dash(self):
        for line in self.NEVER:
            self.assertEqual(self.lines.count(line), 1)
        self.assertNotIn("—", "\n".join(self.lines))


class SlashCommandsNamePostures(unittest.TestCase):
    HINT = 'argument-hint: "[--push] [words] | --clear-words | digest | off"'
    DESC = {"away": "description: Go away - the engineering manager answers with the recommendation and logs it; your first message ends it",
            "yolo": "description: Yolo - the engineering manager answers with the recommendation and logs it until you say yolo:off; you can still steer"}
    LAST = {"away": "Then continue under sections/0h.md's Away paragraph.",
            "yolo": "Then continue under sections/0h.md's Yolo paragraph."}

    def lines(self, p):
        return read(f"commands/{p}.md").splitlines()

    def test_frontmatter_and_last_line(self):
        for p in ("away", "yolo"):
            with self.subTest(p):
                lines = self.lines(p)
                self.assertEqual(lines[:5], ["---", self.DESC[p], self.HINT, f"allowed-tools: Bash(compose {p}:*)", "---"])
                self.assertEqual(lines[-1], self.LAST[p])

    def test_off_digest_and_otherwise_lines_in_order(self):
        for p in ("away", "yolo"):
            with self.subTest(p):
                lines = self.lines(p)
                off = [i for i, l in enumerate(lines) if l.startswith("If `$ARGUMENTS` is `off`, run ")]
                digest = f"If `$ARGUMENTS` is `digest`, run `compose {p} digest --project <slug> < /dev/null` for each project and show each digest as is; {p} stays on."
                other = f"Otherwise run `compose {p} --project <slug> $ARGUMENTS < /dev/null` for each project and show the announcement as is."
                self.assertEqual(len(off), 1)
                self.assertIn(f"`compose {p} off --project <slug> < /dev/null`", lines[off[0]])
                self.assertEqual(lines.count(digest), 1)
                self.assertEqual(lines.count(other), 1)
                self.assertLess(off[0], lines.index(digest))
                self.assertLess(lines.index(digest), lines.index(other))

    def test_keeps_project_and_words_lines(self):
        for p in ("away", "yolo"):
            with self.subTest(p):
                lines = self.lines(p)
                self.assertIn("The project is the one this session runs; for an epic, every project of the epic.", lines)
                self.assertIn("Do not reword, summarize, or widen the words: they pre-answer a question verbatim, never by analogy.", lines)

    def test_away_never_names_yolo_and_no_em_dash(self):
        self.assertNotIn("compose yolo", read("commands/away.md"))
        for p in ("away", "yolo"):
            self.assertNotIn("—", read(f"commands/{p}.md"))


class SkillNamesPostures(unittest.TestCase):
    EVENTS = ["- `event away=on`: the product owner is away; continue under 0h's Away rules.",
              "- `event yolo=on`: yolo is on until an explicit off; continue under 0h's Yolo rules."]
    PUSH = ("- Never push or submit a stack without the user's explicit permission for that push; `compose away --push` or `compose yolo --push` is that permission for the project's stack, given in advance; neither posture without --push ever pushes. "
            "`compose land --yes` merges on GitHub only when the user said to land; neither posture covers it. `run` registers stacks locally only.")
    AWAY = ("- While the product owner is away (`results/yolo.json`, `event away=on` or `event yolo=on`), decisions follow 0h's Away or Yolo paragraph: "
            "the recommendation, logged; the never-set still waits.")
    TAG = ("An answer that starts `away:` or `yolo:` is the engineering manager's recommendation, not the product owner's: the line ends "
           "`(away recommendation, unconfirmed, <date>)` or `(yolo recommendation, unconfirmed, <date>)`, the one its prefix names, until they confirm it (0h).")

    def test_skill_event_lines_in_order_and_old_one_gone(self):
        lines = read("SKILL.md").splitlines()
        self.assertIn(self.EVENTS[0], lines)
        i = lines.index(self.EVENTS[0])
        self.assertEqual(lines[i:i + 2], self.EVENTS)
        self.assertEqual(lines.count(self.EVENTS[1]), 1)
        self.assertFalse([l for l in lines if "event yolo=on" in l and "the product owner is away;" in l])

    def test_skill_push_and_away_lines_once_and_old_ones_gone(self):
        text = read("SKILL.md")
        lines = text.splitlines()
        for want in (self.PUSH, self.AWAY):
            self.assertEqual(lines.count(want), 1)
        for old in ("yolo without --push never pushes", "yolo never covers it", "(`results/yolo.json`, `event yolo=on`)"):
            self.assertNotIn(old, text)

    def test_0b_names_the_unconfirmed_tag_and_card_prefixes_are_gone(self):
        text = read("sections/0b.md")
        self.assertNotIn(self.TAG, text)  # 0b asks no cards, so no answer starts `away:` or `yolo:` there
        self.assertNotIn("An answer that starts `yolo:` is", text)
        self.assertEqual(text.count("`(yolo recommendation, unconfirmed, <date>)`"), 1)

    def test_no_em_dash(self):
        for rel in ("SKILL.md", "sections/0b.md"):
            self.assertNotIn("—", read(rel))


class UserDocsNamePostures(unittest.TestCase):
    STEP = ["`/compose:yolo [words]` works the same but stays on until you send `yolo:off`, and your other messages steer it without ending it.",
            "`/compose:away digest` or `/compose:yolo digest` prints the digest and leaves it on."]
    ROWS = ["| `compose away --project <name> [--push] [words]\\|--clear-words\\|digest\\|off` | Lets the engineering manager answer for you while you are away, until your first message, and prints a digest when you turn it off |",
            "| `compose yolo --project <name> [--push] [words]\\|--clear-words\\|digest\\|off` | Lets the engineering manager answer for you until you send `yolo:off`, while you still steer, and prints a digest when you turn it off |"]
    WATCH = "`event away=on ... by=<the session that set it>` under away, or `event yolo=on ...` under yolo"

    def cli_row(self, name):
        rows = [l for l in read("docs/cli.md").splitlines() if l.startswith(f"| `{name} [--push] [words]\\|decided\\|--clear-words\\|digest\\|off` |")]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_readme_step_away_lines(self):
        lines = read("README.md").splitlines()
        i = lines.index(self.STEP[0])
        self.assertEqual(lines[i:i + 2], self.STEP)
        self.assertEqual(lines.count(self.STEP[1]), 1)
        before = lines[i - 5:i]
        for k in (0, 3, 4):
            self.assertIn("/compose:away", before[k])
        self.assertNotIn("/compose:yolo", "".join(before))
        self.assertEqual(lines[i - 6], "")

    def test_readme_rows_away_then_yolo(self):
        lines = read("README.md").splitlines()
        i = lines.index(self.ROWS[0])
        self.assertEqual(lines[i:i + 2], self.ROWS)
        self.assertFalse([l for l in lines if l.startswith("| `compose yolo") and "[words]\\|--clear-words\\|off`" in l])

    def test_cli_watch_row(self):
        row = [l for l in read("docs/cli.md").splitlines() if l.startswith("| `watch` |")][0]
        self.assertIn(self.WATCH, row)
        self.assertNotIn("by=<the session that set it>` under yolo.", row)

    def test_cli_rows_away_then_yolo(self):
        lines = read("docs/cli.md").splitlines()
        away, yolo = self.cli_row("away"), self.cli_row("yolo")
        self.assertEqual(lines.index(yolo), lines.index(away) + 1)
        for row, me, other in ((away, "away", "yolo"), (yolo, "yolo", "away")):
            for want in ("`digest` prints the digest and leaves it on", "of the posture that is not on exits 1",
                         f"prints the `{me}: ...` form", f"`(" + f"{me} recommendation, unconfirmed, <date>)`"):
                self.assertIn(want, row)
            self.assertNotIn(f"{other}: ...", row)
            self.assertNotIn(f"({other} recommendation", row)
        self.assertIn("the product owner's first message ends it", away)
        self.assertNotIn("yolo:off", away)
        self.assertIn("stays on until `yolo:off`, `/compose:yolo off`, or `compose yolo off`", yolo)
        self.assertNotIn("first message ends it", yolo)

    def test_no_em_dash(self):
        for rel in ("README.md", "docs/cli.md"):
            self.assertNotIn("—", read(rel))


class MentionsNamePostures(unittest.TestCase):
    LINES = (
        ("sections/0g.md", "An autonomous mode is never a mandate to decide for the user, except away and yolo (0h), "
         "whose decisions are the recommendations, logged for the product owner's return.", "except yolo (0h)"),
        ("docs/herdr.md", "`watch --now` and `status` also say whether away or yolo is on and which session set it, "
         "since one session's `compose away` or `compose yolo` changes how every session on the project answers.",
         "whether yolo is on"),
    )

    def test_each_line_is_replaced_in_place(self):
        for rel, line, old in self.LINES:
            self.assertEqual(read(rel).splitlines().count(line), 1, rel)
            self.assertNotIn(old, read(rel))

    def test_no_em_dash(self):
        for rel, *_ in self.LINES:
            self.assertNotIn("—", read(rel))


class TiersBudgetAndPartialStacks(unittest.TestCase):
    def test_cli_rows_name_the_tier_and_budget_commands_and_the_stack_flags(self):
        cli = read("docs/cli.md")
        tiers = cli.split("### Tiers and budget\n", 1)[1].split("\n## ", 1)[0]
        for word in ("compose tier --project", "no-frontier", "results/tier.json", "compose budget --project", "default 25", "results/budget.json", "needs-user"):
            self.assertIn(word, tiers)
        stack = next(l for l in cli.splitlines() if l.startswith("| `stack`"))
        for word in ("--update", "--force-with-lease", "stack  hold", "nothing to submit", "origin/<base>"):
            self.assertIn(word, stack)
        land = next(l for l in cli.splitlines() if l.startswith("| `land`"))
        for word in ("N of M layer(s) submitted", "land   left"):
            self.assertIn(word, land)

    def test_config_documents_tiers_prices_and_the_errors_for_a_bad_prices(self):
        cfg = read("docs/config.md")
        for word in ("| `models.tiers` |", "| `prices` |", "## Model tiers, prices and the frontier budget", "compose budget --project <slug> frontier <dollars>",
                     "prices must be an object of model id to 2 or 4 numbers", "`+?`", "claude:claude-opus-5-5"):
            self.assertIn(word, cfg)

    def test_tasks_documents_the_tier_key_and_the_sections_name_the_new_commands(self):
        tasks = read("docs/tasks.md")
        self.assertIn("## Model tier\n", tasks)
        self.assertIn("tier must be cheap, standard, frontier or no-frontier", tasks)
        self.assertIn("`model:` line", tasks)
        self.assertIn("--update", read("sections/4.md"))
        self.assertIn("land   left", read("sections/7.md"))
        self.assertIn("compose budget --project <slug> frontier <dollars>", read("sections/0h.md"))

    def test_the_new_lines_have_no_em_dash(self):
        for rel in ("docs/config.md", "sections/4.md", "sections/7.md", "sections/0h.md"):
            self.assertNotIn("\u2014", read(rel), rel)


class OneSuiteAfterARebase(unittest.TestCase):
    def test_the_lead_runs_one_full_suite_on_the_top_and_the_final_verify_never_one_per_layer(self):
        six, four = read("sections/6.md"), read("sections/4.md")
        for text in (six, four):
            self.assertIn("After a rebase or a stack rebuild", text)
            self.assertIn("one full suite on the top of the stack and the final verify, never a ", text)
        self.assertIn("never a per-layer suite", six)
        self.assertIn("never a suite per layer", four)
        self.assertIn("never one per layer", read("docs/verify.md"))

    def test_a_green_check_is_taken_by_tree_and_command_and_fresh_runs_it_again(self):
        for rel in ("sections/4.md", "sections/6.md", "docs/verify.md"):
            text = read(rel)
            self.assertIn("<check> green on tree <sha12> at <time>, not rerun", text, rel)
            self.assertIn("--fresh", text, rel)
        self.assertIn("--fresh", read("docs/cli.md"))
        self.assertIn("not rerun", read("docs/cli.md"))

    def test_the_new_lines_have_no_em_dash_and_the_skill_stays_under_its_cap(self):
        for rel in ("sections/4.md", "sections/6.md", "docs/verify.md", "docs/cli.md"):
            self.assertNotIn("\u2014", read(rel), rel)
        self.assertLess(len(read("SKILL.md").encode()), 30000)


class HarnessPaneAndInstallClaims(unittest.TestCase):
    def test_herdr_sign_in_line_says_compose_stops_and_names_the_pane_and_screen(self):
        lines = [l for l in read("docs/herdr.md").splitlines() if "A pane on a sign-in screen" in l]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("no key", lines[0])
        self.assertNotIn("stops naming", lines[0])
        for want in ("the pane", "the screen"):
            self.assertIn(want, lines[0])

    def test_requirements_codex_row_matches_what_codex_runs_and_what_setup_does(self):
        sec = read("docs/requirements.md").split("\n## On by default\n", 1)[1].split("\n## ", 1)[0]
        rows = [l for l in sec.splitlines() if l.startswith("| Codex")]
        self.assertEqual(len(rows), 1, rows)
        for want in ("team lead", "implementer", "never installs", "never signs in", "`codex login`"):
            self.assertIn(want, rows[0])
        self.assertNotIn("for teammate sessions besides Claude Code", rows[0])

    def test_neither_page_has_an_em_dash(self):
        for rel in ("docs/herdr.md", "docs/requirements.md"):
            self.assertNotIn("\u2014", read(rel), rel)


class HarnessSafetyPage(unittest.TestCase):
    def setUp(self):
        import sys; sys.path.insert(0, str(ROOT / "bin"))
        import fc_harness
        self.rows = [v for v in fc_harness.HARNESSES.values() if v and v.get("login_check")]
        self.page = read("docs/harnesses.md")

    def test_page_names_each_rows_hook_files_home_env_and_sign_in_check(self):
        self.assertTrue(self.rows)
        for row in self.rows:
            check = " ".join([row["exe"], *row["login_check"]])
            for word in (row["home_env"], row["hooks_file"], row["trust_file"], f"`{check}`"):
                self.assertIn(word, self.page, word)

    def test_page_says_what_compose_never_does(self):
        flag = [l for l in self.page.splitlines() if "--dangerously-bypass-hook-trust" in l]
        self.assertTrue(flag and all("never" in l.lower() for l in flag), flag)
        self.assertRegex(self.page, r"(?i)never signs? in")
        self.assertRegex(self.page, r"(?i)never presses? a key")

    def test_page_names_both_fences_and_the_confirmed_departure(self):
        for word in ("workspace-write", "-a never", "--add-dir", "read-only", "Python 3.9", "NEB-1533"):
            self.assertIn(word, self.page, word)
        self.assertRegex(self.page, r"(?is)departure.{0,300}product owner.{0,100}confirm|product owner.{0,100}confirm.{0,300}departure")

    def test_no_em_dash(self):
        self.assertNotIn("—", self.page)


class HarnessModelConfig(unittest.TestCase):
    def setUp(self):
        import sys; sys.path.insert(0, str(ROOT / "bin"))
        import fc_config, fc_harness
        self.fc_config, self.reserved = fc_config, fc_harness.RESERVED
        self.config, self.workspaces = read("docs/config.md"), read("docs/workspaces.md")

    def section(self, title):
        self.assertEqual(self.config.count(f"\n## {title}\n"), 1, title)
        return self.config.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]

    def rows(self, first):
        return [l for l in self.config.splitlines() if l.startswith(f"| {first}")]

    def test_the_harnesses_example_is_valid_and_codex_crew_models_come_from_its_block(self):
        sec = self.section("Harnesses")
        for word in ("harnesses.md", "models.crew.default", "one level deep"):
            self.assertIn(word, sec, word)
        cfg = json.loads(sec.split("```json\n", 1)[1].split("```", 1)[0])
        block = cfg["harnesses"]["codex"]["models"]["crew"]
        self.assertIn("default", block)
        name, merged = self.fc_config.settings(cfg, "codex")
        self.assertEqual(merged["models"]["crew"], block)
        self.assertNotIn("harnesses", merged)

    def test_keys_and_model_ids_tables_cover_harnesses_and_codex(self):
        self.assertTrue(self.rows("`harnesses`"))
        self.assertRegex("".join(self.rows("`models.test_reader`")), r"`claude:`.*`codex:`")
        ids = self.section("Model ids")
        self.assertTrue(self.rows("`codex:<model>`") and self.rows("`codex`"))
        self.assertRegex(self.workspaces, r"(?m)^\| `codex:<model>`")
        self.assertIn("harnesses.md", self.workspaces)
        self.assertNotRegex(ids, r"(?i)must be a `claude:`")

    def test_error_table_has_the_harness_block_and_reserved_name_lines(self):
        errors = self.section("When the config is wrong")
        self.assertTrue(self.reserved)
        reserved = f'providers.<name> is reserved: {", ".join(self.reserved)} name harnesses, not providers'
        for line in ('no "harnesses.<name>" block', 'harnesses.<name>.models.crew has no "default"', reserved):
            self.assertIn(f"`{line}`", errors, line)
        self.assertTrue([l for l in errors.splitlines() if "teammate sessions run in" in l and "OpenCode ids are for implementers only" in l])

    def test_neither_page_has_the_stale_lines_or_an_em_dash(self):
        for rel, text in (("docs/config.md", self.config), ("docs/workspaces.md", self.workspaces)):
            self.assertNotRegex(text, r"(?i)must be a `claude:` model|run in (pi or Claude Code|Claude Code or pi)\b", rel)
            self.assertNotIn("—", text, rel)


class HarnessConfigClaims(unittest.TestCase):
    def setUp(self):
        import sys; sys.path.insert(0, str(ROOT / "bin"))
        import fc_config
        self.fc_config = fc_config
        self.config, self.workspaces = read("docs/config.md"), read("docs/workspaces.md")

    def test_the_test_reader_bills_by_its_harness(self):
        para = self.workspaces.split("`test_reader` reads each failed test run", 1)[1].split("\n\n", 1)[0].splitlines()
        self.assertFalse([l for l in para if re.search(r"(?i)claude code subscription", l) and "`claude:`" not in l], para)
        self.assertTrue([l for l in para if re.search(r"(?i)chatgpt subscription", l) and "`codex:`" in l], para)

    def test_the_codex_example_default_is_bare_codex_or_marked_not_verified(self):
        sec = self.config.split("\n## Harnesses\n", 1)[1].split("\n## ", 1)[0]
        cfg = json.loads(sec.split("```json\n", 1)[1].split("```", 1)[0])
        default = cfg["harnesses"]["codex"]["models"]["crew"]["default"]
        self.assertEqual(self.fc_config.settings(cfg, "codex")[1]["models"]["crew"]["default"], default)
        if default != "codex":
            self.assertTrue([l for l in sec.splitlines() if default in l and "not verified" in l], default)

    def test_neither_page_has_an_em_dash(self):
        for rel, text in (("docs/config.md", self.config), ("docs/workspaces.md", self.workspaces)):
            self.assertNotIn("—", text, rel)


class HarnessSessionOrder(unittest.TestCase):
    def setUp(self):
        import sys; sys.path.insert(0, str(ROOT / "bin"))
        import fc_harness
        self.session_env = fc_harness.HARNESSES["codex"]["session_env"]
        self.page = read("docs/harnesses.md")
        self.lines = self.page.split("\n## Models per harness\n", 1)[1].split("\n## ", 1)[0].splitlines()

    def first(self, word):
        return next(i for i, l in enumerate(self.lines) if word in l)

    def test_the_session_variable_lines_come_in_order_and_codex_names_its_block(self):
        flag, codex, claude = (self.first(w) for w in ("`COMPOSE_HARNESS`", "CODEX_THREAD_ID", "`claude` has no block"))
        self.assertLess(flag, codex)
        self.assertLess(codex, claude)
        self.assertIn("`CLAUDECODE=1`", self.lines[claude])
        self.assertIn("`codex`", self.lines[codex])
        self.assertRegex(self.lines[codex], r"(?i)block")
        self.assertEqual(re.findall(r"`([A-Z][A-Z_]+)`", self.lines[codex]), [self.session_env])

    def test_the_codex_check_comes_before_claudecode_and_says_why(self):
        check = [l for l in self.lines if re.search(r"before `CLAUDECODE`", l)]
        self.assertEqual(len(check), 1, check)
        self.assertRegex(check[0], r"(?i)codex")
        self.assertIn("`CLAUDECODE=1`", check[0])
        self.assertNotRegex("\n".join(self.lines), r"(?m)^Without that it is `claude`")

    def test_how_to_add_a_row_names_the_session_variable_before_the_hooks(self):
        row = [l for l in self.page.splitlines() if "the home variable and its default" in l]
        self.assertEqual(len(row), 1, row)
        self.assertRegex(row[0], r"home variable and its default,.*session variable.*hooks")

    def test_no_em_dash(self):
        self.assertNotIn("—", self.page)


class HarnessDefaultBlockDocs(unittest.TestCase):
    CLAUDE = "With none of them it is `claude` under Claude Code (`CLAUDECODE=1`), and `claude` has no block."
    SIX = [
        "A harness with a built-in default needs no block in `~/.compose/config.json`.",
        "Codex has one, made from the top level: every role on `claude` or a `claude:<id>` model runs on `codex`, Codex's own default model, and a role on any other model keeps it.",
        "A `harnesses.codex` block replaces that default whole.",
        "compose never writes the block or any other part of the config.",
        "A block whose `models.crew` has no `default` stops with a line saying so.",
        "A harness other than `claude` with no built-in default and no block stops with a line naming it, which is `gemini` and `grok` today.",
    ]

    def setUp(self):
        self.page, self.config = read("docs/harnesses.md"), read("docs/config.md")

    def test_models_per_harness_has_the_six_lines_after_the_claude_line(self):
        lines = self.page.split("\n## Models per harness\n", 1)[1].split("\n## ", 1)[0].splitlines()
        i = lines.index(self.CLAUDE)
        self.assertEqual(lines[i + 1:i + 1 + len(self.SIX)], self.SIX)

    def test_config_harnesses_section_says_codex_has_a_default(self):
        sec = self.config.split("\n## Harnesses\n", 1)[1].split("\n## ", 1)[0]
        lines = sec.splitlines()
        self.assertIn("Each block needs `models.crew.default`.", lines)
        i = lines.index("Each block needs `models.crew.default`.")
        self.assertEqual(lines[i + 1], "Claude Code needs no block, and Codex has a built-in one that a `codex` block replaces, see [harnesses.md](<harnesses.md>).")
        self.assertIn("A Codex session reads its crew models from the `codex` block, else from the default made from the top level, which puts every Claude model on bare `codex`, and every other key from the workspace.", lines)
        for word in ("harnesses.md", "models.crew.default", "one level deep"):
            self.assertIn(word, sec)

    def test_error_row_names_gemini_and_grok_as_the_harnesses_needing_a_block(self):
        errors = self.config.split("\n## When the config is wrong\n", 1)[1].split("\n## ", 1)[0]
        rows = [l for l in errors.splitlines() if l.startswith('| `no "harnesses.<name>" block`')]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("Add the block for that harness, which only `gemini` and `grok` need today, see [Harnesses](<#harnesses>).", rows[0])

    def test_no_line_says_a_codex_session_without_a_block_stops(self):
        for rel, text in (("docs/harnesses.md", self.page), ("docs/config.md", self.config)):
            for l in text.splitlines():
                self.assertIsNone(re.search(r"(?i)codex.*\bno\b.*\bblock\b.*\bstops?\b|with no block stops", l), f"{rel}: {l}")

    def test_neither_page_has_an_em_dash(self):
        for rel, text in (("docs/harnesses.md", self.page), ("docs/config.md", self.config)):
            self.assertNotIn("—", text, rel)


class HarnessTierDocs(unittest.TestCase):
    SIX_LAST = "A harness other than `claude` with no built-in default and no block stops with a line naming it, which is `gemini` and `grok` today."
    HARNESS_LINE = "In a Codex session with no block, every `models.tiers` entry on a `claude` or `claude:<id>` model, the default lists included, also runs on `codex` with its effort kept, and a `harnesses.codex` block still replaces that default whole."
    CONFIG_LINE = "A Codex session with no `harnesses.codex` block also puts every entry of these lists on a `claude` or `claude:<id>` model on bare `codex`, with its effort kept, and a `codex` block still replaces that default whole, see [harnesses.md](<harnesses.md>)."

    def setUp(self):
        self.page, self.config = read("docs/harnesses.md"), read("docs/config.md")

    def test_harnesses_line_follows_the_sixth_line_then_a_blank_line(self):
        lines = self.page.split("\n## Models per harness\n", 1)[1].split("\n## ", 1)[0].splitlines()
        i = lines.index(self.SIX_LAST)
        self.assertEqual(lines[i + 1:i + 3], [self.HARNESS_LINE, ""])

    def test_config_line_follows_the_tiers_fence_with_a_blank_line_each_side(self):
        lines = self.config.split("\n## Model tiers, prices and the frontier budget\n", 1)[1].split("\n## ", 1)[0].splitlines()
        j = [i for i, l in enumerate(lines) if l.startswith('"models": { "tiers"')]
        self.assertEqual(len(j), 1, j)
        self.assertEqual(lines[j[0] + 1:j[0] + 5], ["```", "", self.CONFIG_LINE, ""])
        self.assertTrue(lines[j[0] + 5].startswith("`prices` gives the list price"), lines[j[0] + 5])

    def test_each_line_is_one_physical_line_and_appears_once(self):
        self.assertEqual(self.page.splitlines().count(self.HARNESS_LINE), 1)
        self.assertEqual(self.config.splitlines().count(self.CONFIG_LINE), 1)

    def test_neither_page_has_an_em_dash(self):
        for rel, text in (("docs/harnesses.md", self.page), ("docs/config.md", self.config)):
            self.assertNotIn("—", text, rel)


class ShapingIsTheManagersBrainstorm(unittest.TestCase):
    HANDOFF = ("When the team lead reports `needs-user \"shape <issue>: ...\"`, the engineering manager brainstorms that issue with the product owner here (0b), "
               "then sends `herdr agent prompt <slug>-orchestrator \"shaped <issue>\"`.")
    ASK_ONCE = ("Every round-trip to the product owner stalls the project, so shape in one conversation (0b): "
                "start groundwork and the smoke check (0g) at once so toolchain findings join the brainstorm.")

    def test_0h_names_the_handoff_once(self):
        self.assertEqual(read("sections/0h.md").count(self.HANDOFF), 1)
        self.assertIn('`herdr agent prompt <slug>-orchestrator "shaped <id>"`', read("sections/0b.md"))
        self.assertIn("or at once in a new repo", read("sections/0h.md"))
        self.assertIn("on `shaped <issue>` it starts the planner (step 1).", read("sections/0h.md"))
        self.assertIn("0b is the engineering manager's; inside herdr the team lead never shapes, it reports `needs-user` and waits (0h).", read("SKILL.md"))

    def test_skill_shapes_in_one_conversation_not_through_architect_cards(self):
        text = read("SKILL.md")
        for gone in ("ask once, through architect", "as a card in the card format (0b)"):
            self.assertNotIn(gone, text)
        self.assertIn(self.ASK_ONCE, text)

class UserDocsShaping(unittest.TestCase):
    def test_user_docs_describe_shaping_as_a_conversation(self):
        how = read("docs/how-it-works.md")
        for line in (
            "The engineering manager shapes each feature with you in one conversation, one question at a time, and writes the design to the feature issue.",
            "A builder check reads the design before you do.",
        ):
            self.assertIn(line + "\n", how)
            self.assertEqual(how.count(line), 1, line)
        for gone in ("Next, the engineering manager shapes each feature with you.", "Before you see the design, a builder check reads it cold"):
            self.assertNotIn(gone, how)
        rows = read("docs/cli.md").splitlines()
        decide = [l for l in rows if l.startswith("| `decide`")]
        self.assertEqual(len(decide), 1)
        self.assertIn("--new", decide[0])
        self.assertIn("--replaces", decide[0])
        for flag in ("--closes", "--drop", "--accept-all", "no open questions"):
            self.assertIn(flag, decide[0])
        decisions = [l for l in rows if l.startswith("| `decisions`")]
        self.assertIn("open question", decisions[0])
        self.assertIn("`<n> questions <ids>`", next(l for l in rows if l.startswith("| `report`")))
        architect = [l for l in rows if l.startswith("| `architect")]
        self.assertEqual(len(architect), 1)
        self.assertIn("--change", architect[0])
        self.assertEqual(len([l for l in rows if l.startswith("| `buildercheck`")]), 1)
        for rel in ("docs/how-it-works.md", "README.md", "docs/herdr.md", "docs/tasks.md", "docs/requirements.md", "docs/workspaces.md"):
            text = read(rel).lower()
            self.assertNotIn("wayfinder", text, rel)
            self.assertNotIn("card set", text, rel)
        self.assertNotRegex(read("docs/herdr.md"), r"compose architect --project <slug> --extra")


class ReconcileFlow(unittest.TestCase):
    FRONT = ("---\ndescription: Reconcile a messy Linear project - sort every open ticket into keep, cancel, or verify, and apply it on your go\n"
             'argument-hint: "<Linear project link>"\nallowed-tools: Bash(compose:*), Bash(herdr:*), Bash(trail:*)\n---\n')
    LINES = (
        "### 0k. Reconcile an existing project",
        "A reconcile is the one-time cleanup of a messy Linear project: every open ticket ends up kept, canceled, merged into the ticket it joins, or left for the product owner to verify.",
        "It never fixes build order or blockers, and a missing design, PRD, or delivery plan is not its job: say so in the report and leave it to planning, and a missing PRD to the factory, which plans it first.",
        "Research first, in parallel, with one helper per source: the code (the trunk and every unmerged `compose/*` branch), the designs, and Linear (every open ticket from `compose reconcile list`, its body, and the project's documents).",
        "Write what they found to a Linear document titled `<project name>: reconcile research <date>`: what the product is, what is built, designed, and planned, the contradictions, and the tickets that look stale.",
        "Sort with the product owner one question at a time, a group of tickets per question; a ticket a recorded decision already settles is your call, named with that decision.",
        "Write the sheet and its reason files outside the repo, then run `compose reconcile check --file <sheet>` until it passes.",
        'Write the proposal with `compose reconcile show | compose linear doc create --title "<project name>: reconcile <date>"`, give the product owner its link and the counts, and ask for their go.',
        'On their go, run `compose reconcile apply --go "<their words>"`; report every skipped or FAILED line as it is, and run apply again for the failed ones.',
        "Never delete or re-parent a ticket: a ticket that belongs elsewhere goes in the report for the product owner.",
        'Then run `compose reconcile notices` and send each live team lead its lines with `herdr agent prompt <slug>-orchestrator "<message>"`.',
        "The message asks it to stop work on what was canceled and wind down the teammates on it, save what is worth keeping under the main checkout's `.compose/<slug>`, update its plan and Decisions so far, merge nothing, delete no branch, worktree, or ticket, cancel no ticket itself, and report back what it changed and what is worth salvaging.",
        "Under away or yolo, research and the sheet go ahead, and the apply waits for the product owner.",
    )

    def test_command_file(self):
        text = read("commands/reconcile.md")
        self.assertTrue(text.startswith(self.FRONT))
        body = text[len(self.FRONT):].splitlines()
        self.assertIn("You are the engineering manager for `$ARGUMENTS`.", body)
        self.assertIn("Follow sections/0k.md from its first step.", body)

    def test_section_lines_appear_once(self):
        lines = read("sections/0k.md").splitlines()
        self.assertEqual(lines[0], self.LINES[0])
        for want in self.LINES:
            self.assertEqual(lines.count(want), 1, want)

    def test_fix_round_one_wording(self):
        text = read("sections/0k.md")
        self.assertNotIn("is the only thing that writes to Linear", text)
        self.assertIn("Linear is written only through the compose CLI: `compose reconcile`, plus `compose linear doc create` for the two documents.", text.splitlines())
        self.assertIn('File that document with `compose linear doc create --title "<project name>: reconcile research <date>"`.', text.splitlines())
        self.assertIn("A kept ticket can also carry a new `title`, which renames it.", text.splitlines())
        self.assertIn("A `new` entry takes `deep_dive` too.", text.splitlines())

    def test_the_figma_key_is_read_from_each_projects_linear_json(self):
        lines = read("sections/0k.md").splitlines()
        self.assertIn("The designs come from the Figma file in the `figma` key of each project's `.compose/<slug>/linear.json`, or the one the product owner names.", lines)
        self.assertNotIn("`.compose/linear.json`'s `figma`", read("sections/0k.md"))

    def test_plain_dashes_and_one_sentence_per_line(self):
        for rel in ("sections/0k.md", "commands/reconcile.md"):
            for line in read(rel).splitlines():
                self.assertNotIn("\u2014", line)
                if not line.startswith(("#", "---", "description:", "argument-hint:", "allowed-tools:", "- ", "  ")):
                    self.assertNotRegex(line, r"[.!?] [A-Z]", line)


class ReconcileUserDocs(unittest.TestCase):
    def test_user_docs_describe_the_reconcile(self):
        rows = [l for l in read("docs/cli.md").splitlines() if l.startswith("| `reconcile` |")]
        self.assertEqual(len(rows), 1)
        for word in ("list", "check --file", "show", "apply --go", "notices", "~/.compose/reconcile/"):
            self.assertIn(word, rows[0])
        ready = [l for l in read("docs/cli.md").splitlines() if l.startswith("| `ready`")]
        self.assertEqual(len(ready), 1)
        self.assertIn("needs-shaping", ready[0])
        self.assertIn("- `/compose:reconcile <Linear project link>` sorts every open ticket of a messy project into keep, cancel, or verify with you, and changes Linear only on your go.", read("README.md").splitlines())
        how = read("docs/how-it-works.md")
        self.assertIn("## Cleaning up an existing project", how.splitlines())
        self.assertIn("A reconcile never deletes a ticket: every cancel carries its reason as a comment.", how.splitlines())


ZERO_P = [
    "The product delivery plan is one Linear document, `Product delivery plan`, in the repo's Linear project; its Tracks section is the order across every feature, and the track list order is the priority when something has to give.",
    "A track is a line of features built one after another; tracks run in parallel unless a track says what it comes after, and the CLI computes the waves from that.",
    "When there is none, start with `compose delivery seed`: it lists the open features, their own delivery plans, and a guess at the tracks from the blocks relations Linear holds today.",
    "Shape it with the product owner as sections/0b.md shapes anything: one question at a time, the guess played back as a guess, and every track settled in their words.",
    "Write the tracks with `compose delivery tracks --file <path> --quote \"<their words>\"`; it refuses without their words, under away and yolo too.",
    "Put both files under `.compose/delivery/` in the repo, the one place this command writes without asking.",
    "The delivery plan wins: `compose delivery reconcile` fixes the blocks relations between plan features and the Ready labels, and only proposes what would cancel, delete, or re-parent a ticket.",
    "Bring every proposed fix to the product owner with its reason; never cancel, delete, or re-parent a ticket for the plan without their yes.",
    "When a re-plan adds, drops, or reorders features, play the new tracks back and write them again.",
    "Never edit the Waves, Start today, Proposed fixes, or History sections by hand: the CLI writes them.",
]
DELIVERY_ROW = "| `delivery` |"
README_DELIVERY = "- `/compose:delivery [seed | reconcile]` writes or changes the product delivery plan with you: the order across every feature, in parallel tracks."
HOW_LINE = "One product delivery plan in Linear sets the order across features, in parallel tracks; `compose delivery reconcile` makes Linear follow it and proposes anything destructive."


class ProductDeliveryPlan(unittest.TestCase):
    def body(self, rel):
        text = read(rel)
        return text.split("---\n", 2)[2].splitlines() if text.startswith("---\n") else text.splitlines()

    def test_section_0p_holds_each_line_once(self):
        lines = read("sections/0p.md").splitlines()
        self.assertEqual(lines[0], "### 0p. The product delivery plan sets the order across features")
        for line in ZERO_P:
            self.assertEqual(lines.count(line), 1, line)

    def test_section_0p_names_who_runs_it_and_the_writes(self):
        text = read("sections/0p.md")
        for need in ("The engineering manager runs it (outside herdr the product owner's own session), never the team lead.",
                     "Why this order is written with `compose linear doc update <id> --section \"## Why this order\" --file <path>`.",
                     "A reconcile runs after every `tracks` write and before a new feature project starts."):
            self.assertEqual(text.splitlines().count(need), 1, need)

    def test_section_8_ends_its_delivery_block_with_the_pointer(self):
        self.assertEqual(read("sections/8.md").splitlines().count("A change to the order across features goes through the product delivery plan (sections/0p.md)."), 1)

    def test_command_file(self):
        text = read("commands/delivery.md")
        head = text.split("---\n", 2)[1].splitlines()
        self.assertEqual(head, ["description: Write or change the product delivery plan with the product owner - the order across every feature, in parallel tracks",
                                'argument-hint: "[seed | reconcile]"',
                                "allowed-tools: Bash(compose delivery:*), Bash(compose linear:*), Write(.compose/delivery/**)"])  # Write: the --file for tracks and Why this order, and nothing else
        body = self.body("commands/delivery.md")
        for line in ("Follow sections/0p.md.", "In this session, never plan stories or cut tasks."):
            self.assertEqual(body.count(line), 1, line)
        for need in ("compose delivery seed", "compose delivery reconcile --dry-run", "steps 0 and 0j"):
            self.assertIn(need, text)

    def test_cli_row_and_how_it_works(self):
        rows = [l for l in read("docs/cli.md").splitlines() if l.startswith(DELIVERY_ROW)]
        self.assertEqual(len(rows), 1)
        for word in ("`seed`", "`order`", "`reconcile`", "`tracks`", "`--quote`", "`--dry-run`"):
            self.assertIn(word, rows[0])
        self.assertEqual(read("docs/how-it-works.md").splitlines().count(HOW_LINE), 1)
        ready = [l for l in read("docs/cli.md").splitlines() if l.startswith("| `ready` |")][0]
        self.assertIn("a story is not Ready while its feature waits on a feature that is not built", ready)

    def test_readme_lists_the_slash_command(self):
        lines = read("README.md").splitlines()
        self.assertEqual(lines.count(README_DELIVERY), 1)
        self.assertEqual(lines[lines.index(README_DELIVERY) - 1][:17], "- `/compose:desig")

    def test_house_rules(self):
        for rel in ("sections/0p.md", "commands/delivery.md", "sections/8.md", "docs/cli.md", "docs/how-it-works.md"):
            self.assertNotIn("\u2014", read(rel), rel)
        for rel in ("sections/0p.md", "commands/delivery.md"):
            for line in self.body(rel)[1 if rel.startswith("sections") else 0:]:
                self.assertNotRegex(line, r"[.!?] [A-Z]", line)


class PrdFlow(unittest.TestCase):
    """The PRD flow: sections/0q.md, /compose:prd, and the lines pointing at it."""
    def test_section_0r_says_the_flow(self):
        lines = read("sections/0q.md").splitlines()
        self.assertEqual(lines[0], "### 0q. The PRD comes before the delivery plan")
        text = "\n".join(lines)
        for need in ("`compose prd seed`", "one question at a time", "sections/0b.md", "`compose prd check --file .compose/prd/prd.md`", "`nothing important left`", "could not run", "--file .compose/prd/prd.md --quote",
                     "under away and yolo too", "sections/0p.md", "never plans stories", "`## PRD: product requirements`"):
            self.assertIn(need, text)
        for h in ("Problem", "Who it is for", "Goals", "Non-goals", "Anti-goals", "The product at a glance", "Open questions"):
            self.assertIn(f"`## {h}`", text)

    def test_command_file_and_pointers(self):
        head = read("commands/prd.md").split("---\n", 2)[1].splitlines()
        self.assertEqual(head[1], 'argument-hint: "[seed]"')
        self.assertIn("Write(.compose/prd/**)", head[2])
        self.assertNotIn("buildercheck", head[2]); self.assertNotIn("compose crew", head[2])
        self.assertNotIn("buildercheck", read("sections/0q.md"))
        self.assertIn("Follow sections/0q.md.", read("commands/prd.md").splitlines())
        self.assertEqual(len([l for l in read("README.md").splitlines() if l.startswith("- `/compose:prd [seed]`")]), 1)
        self.assertEqual(len([l for l in read("sections/0f.md").splitlines() if l.startswith("On `event factory=prd-missing`")]), 1)
        for rel, need in (("sections/0n.md", "PRD: product requirements"), ("sections/0p.md", "sections/0q.md"), ("sections/0k.md", "to the factory, which plans it first")):
            self.assertIn(need, read(rel), rel)
        self.assertEqual(len([l for l in read("docs/cli.md").splitlines() if l.startswith("| `prd` |")]), 1)

    def test_house_rules(self):
        for rel in ("sections/0q.md", "commands/prd.md"):
            self.assertNotIn("\u2014", read(rel), rel)
            body = read(rel).split("---\n", 2)[2].splitlines() if rel.startswith("commands") else read(rel).splitlines()[1:]
            for line in body:
                self.assertNotRegex(line, r"[.!?] [A-Z]", line)


class FactoryLoop(unittest.TestCase):
    SECTION = [
        "### 0f. The factory builds what is ready and offers what to plan",
        "The engineering manager starts the factory with `compose factory start` inside herdr, then keeps `compose factory watch` running in the background, as 0h runs `compose watch`.",
        "Each turn it makes Linear follow the product delivery plan, then hands ready stories to their features' team leads in delivery plan order, never more at once than the day pace.",
        "Tell the product owner the day pace in one line when the factory starts; `compose factory pace <n>` changes it when they say.",
        "On `event factory=bootstrap`, plan the product delivery plan with the product owner from `compose delivery seed` (sections/0p.md); nothing is built before it exists.",
        "On `event factory=offer`, offer that ticket in one line with its why, and say they may plan another ticket of their choosing or do something else.",
        "On a yes, run `compose factory plan <id>` with the ticket they chose, plan it here as commands/planner.md says, and run `compose factory plan --done` when it is approved or they stop; on a no, run `compose factory decline`.",
        "On `event factory=move-up`, ask the product owner whether to move that ticket up; on yes, write the new tracks with their words (sections/0p.md), and on no, the delivery plan's order stands.",
        "On `event factory=held`, re-plan the paused work with the product owner and settle each held task with `compose change keep|part|discard` (step 8).",
        "`compose factory stop` stops dispatch and offers and lets running work finish; `compose factory stop --hold \"<why>\"` pauses it too.",
        "A team lead the factory starts reads this section too: a `factory: build story ...` prompt means build that story only, and report done when it passes verify.",
    ]
    COMMAND = [
        "Invoke the compose skill, resolve the repo by its steps 0 and 0j, and follow sections/0f.md.",
        "When `$ARGUMENTS` is `stop`, `status`, or `pace <n>`, run `compose factory $ARGUMENTS` and show its output as is.",
        "When `$ARGUMENTS` is `stop --hold <why>`, run `compose factory stop --hold \"<why>\"` with the why quoted as one argument, and show its output as is.",
        "With no arguments, do not ask which project as 0j does for a bare project link: the factory works on the repo's Linear project from step 0, so run `compose factory start` and follow sections/0f.md.",
        "In this session, never write code, stories, or tasks yourself.",
    ]
    HOW = "The factory (`/compose:factory`) runs in the background: it builds every ready story in delivery plan order at a day pace you set, pauses work a plan change rests on, and offers the next thing to plan while it builds, ahead into later waves."

    def body(self, rel):
        text = read(rel)
        return text.split("---\n", 2)[2].splitlines() if text.startswith("---\n") else text.splitlines()

    def test_section_holds_each_line_once(self):
        lines = read("sections/0f.md").splitlines()
        self.assertEqual(lines[0], self.SECTION[0])
        for line in self.SECTION:
            self.assertEqual(lines.count(line), 1, line)
        text = read("sections/0f.md")
        for need in ("dispatched", "proposed", "loop-lost", "compose watch --project", "/compose:reconcile", "sections/0k.md", "yolo", "away",
                     "unless one already runs for that project", "waits for their look and merge", "waits 3 turns"):
            self.assertIn(need, text)
        for event in ("verified", "lead-lost", "dropped", "changed", "judge-failed", "turn-failed", "watching"):
            self.assertEqual(len([l for l in lines if l.startswith(f"On `event factory={event}`")]), 1, event)

    def test_command_file(self):
        text = read("commands/factory.md")
        head = text.split("---\n", 2)[1].splitlines()
        self.assertEqual(head, ["description: Start the factory - it builds ready stories in delivery plan order at a day pace and offers what to plan next while it builds",
                                'argument-hint: "[stop [--now] [--hold <why>] | status | pace <n> | night [--until HH:MM] [--budget <dollars>] [--stop-when <words>] | day | digest]"',
                                "allowed-tools: Bash(compose:*), Bash(herdr:*)"])
        body = self.body("commands/factory.md")
        for line in self.COMMAND:
            self.assertEqual(body.count(line), 1, line)

    def test_cli_row_and_how_it_works(self):
        rows = [l for l in read("docs/cli.md").splitlines() if l.startswith("| `factory` |")]
        self.assertEqual(len(rows), 1)
        for word in ("start", "stop", "--hold", "status", "watch", "pace", "plan", "--done", "decline"):
            self.assertIn(word, rows[0])
        self.assertEqual(read("docs/how-it-works.md").splitlines().count(self.HOW), 1)

    def test_house_rules(self):
        for rel in ("sections/0f.md", "commands/factory.md", "docs/cli.md", "docs/how-it-works.md"):
            self.assertNotIn("\u2014", read(rel), rel)
        for rel in ("sections/0f.md", "commands/factory.md"):
            for line in self.body(rel)[1 if rel.startswith("sections") else 0:]:
                self.assertNotRegex(line, r"[.!?] [A-Z]", line)

NIGHT_0N = [
    "Night starts only when the product owner says so: run `compose factory night` with the end time, budget, and stop condition they give, and show its lines as is.",
    "The product owner's first message back ends it: run `compose factory day`, relay the digest, and ask its \"To confirm\" items in one ask before acting on the message.",
    "At night a team lead's `needs-user` belongs to the night answerer: never answer it yourself, and wait for `event factory=answered`, `waiting`, or `answer-failed`.",
    "On `event factory=waiting` or `event factory=night-ended state=no-credit`, send one `PushNotification` naming what waits and the recommendation.",
    "On `event factory=for-morning`, do nothing: planning waits for the product owner and the digest lists it.",
    "`compose factory stop` at night drains: nothing new starts and running work finishes; a second `compose factory stop` stops at once and keeps the work paused on its ref.",
    "When planning a ticket that has a section in the night prep document, open with its questions and still adapt to every answer (0b).",
    "Night never plans: it never writes stories, deep dives, tracks, or Ready, and never answers its own prepared questions.",
    "Relay any other `event factory=night-ended` and each `event factory=draining` to the product owner in one line; on `event factory=usage-wait`, `usage-resumed`, `woke`, `prepared`, `prep-dropped`, `prep-failed`, or a `judge-failed` on the stop condition, do nothing, because the digest lists them.",
]
NIGHT_FILES = {
    "sections/0f.md": ["Overnight, `compose factory night` switches the factory to night (sections/0n.md)."],
    "commands/factory.md": [
        'argument-hint: "[stop [--now] [--hold <why>] | status | pace <n> | night [--until HH:MM] [--budget <dollars>] [--stop-when <words>] | day | digest]"',
        "When `$ARGUMENTS` is `stop --now`, run `compose factory stop --now` and show its output as is.",
        "When `$ARGUMENTS` starts with `night`, `day`, or `digest`, run `compose factory $ARGUMENTS` with any `--stop-when \"<words>\"` quoted as one argument, show its output as is, and follow sections/0n.md.",
    ],
    "commands/away.md": ["When `compose factory status` says the factory runs for this repo, run `compose factory night` instead (an end time or budget in the words go to `--until` and `--budget`) and follow sections/0n.md; `off` then means `compose factory day`."],
    "docs/how-it-works.md": ["At night (`/compose:factory night`) it keeps building inside the end time and dollar budget you set, answers questions on approved tickets with a one-time frontier answerer, prepares questions for your next planning session, and greets you with one morning digest."],
}


class NightMode(unittest.TestCase):
    def test_section_0n_holds_each_line_once(self):
        lines = read("sections/0n.md").splitlines()
        self.assertEqual(lines[0], "### 0n. Night: the factory builds while the product owner sleeps")
        for line in NIGHT_0N:
            self.assertEqual(lines.count(line), 1, line)

    def test_section_0n_one_sentence_per_line(self):
        for line in read("sections/0n.md").splitlines()[1:]:
            self.assertNotRegex(line, r"[.!?] [A-Z]", line)

    def test_section_0n_says_what_the_code_does(self):
        text = read("sections/0n.md")
        for word in ("one level", "usage wait", "real dollars", "never stops running work", "sections/0h.md", "compose factory digest"):
            self.assertIn(word, text)

    def test_pointer_lines_once(self):
        for rel, lines in NIGHT_FILES.items():
            got = read(rel).splitlines()
            for line in lines:
                self.assertEqual(got.count(line), 1, f"{rel}: {line}")

    def test_every_night_event_named_in_0n_is_one_the_code_says(self):
        text = read("sections/0n.md")
        said = set(re.findall(r"factory=([\w-]+)", "".join(p.read_text() for p in (ROOT / "bin").glob("fc_*.py"))))
        line = next(l for l in text.splitlines() if l.startswith("Relay any other"))
        named = re.findall(r"`(?:event factory=)?([a-z][\w-]*)`", line)
        self.assertEqual(len(named), 9, named)
        self.assertEqual([n for n in named if n not in said], [])

    def test_cli_row_names_night(self):
        rows = [l for l in read("docs/cli.md").splitlines() if l.startswith("| `factory`")]
        self.assertEqual(len(rows), 1)
        for word in ("night", "--until", "--budget", "--stop-when", "day", "digest", "--now"):
            self.assertIn(word, rows[0])

    def test_no_em_dash(self):
        for rel in ("sections/0n.md", "sections/0f.md", "commands/factory.md", "commands/away.md", "docs/cli.md", "docs/how-it-works.md"):
            self.assertNotIn("\u2014", read(rel), rel)



class NoCards(unittest.TestCase):
    """Teammates write open questions, never cards: no skill or doc prose speaks of cards (a `discard` is not one; code spans, such as the
    older `<n> cards <ids>` the report gate still reads, are left out)."""

    def test_no_card_wording_left(self):
        import re
        paths = sorted((ROOT / "sections").glob("*.md")) + sorted((ROOT / "commands").glob("*.md")) + [
            ROOT / "SKILL.md", ROOT / "docs" / "cli.md", ROOT / "docs" / "how-it-works.md", ROOT / "docs" / "herdr.md"]
        for path in paths:
            for n, line in enumerate(path.read_text().splitlines(), 1):
                self.assertNotRegex(re.sub(r"`[^`]*`", "", line), re.compile(r"\bcards?\b", re.I), f"{path.name}:{n}")

if __name__ == "__main__":
    unittest.main()
