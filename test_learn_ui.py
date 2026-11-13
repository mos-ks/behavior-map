# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
"""Tests for learn.py (corrections) and ui.py (the page's server). They work on a copy of the map, never on the real one.

    python -m unittest test_learn_ui -v
"""
import datetime
import glob
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

# the tests run on the starter map, never on your own
os.environ["BEHAVIOR_MAP_DIR"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "starter")

import build
import judge
import learn
import skills
import sync
import ui

RULE = "git-no-ai-attribution"
# the chats in these tests were typed today: the scan only reads what is newer than the map
TODAY = datetime.date.today().isoformat()


def day(offset):
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


def copy_map():
    root = tempfile.mkdtemp(prefix="bmap-test-")
    for name in ("rules", "profiles"):
        shutil.copytree(os.path.join(build.ROOT, name), os.path.join(root, name))
    shutil.copy(os.path.join(build.ROOT, "conflicts.md"), root)
    return root


def rule(root, rule_id=RULE):
    return next(r for r in build.load_rules(root)[0] if r["id"] == rule_id)


def transcript(projects, lines, name="s1"):
    folder = os.path.join(projects, "c--proj")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name + ".jsonl"), "a", encoding="utf-8") as f:
        for text, extra in lines:
            row = {"type": "user", "origin": {"kind": "human"}, "entrypoint": "claude-vscode", "cwd": "C:\\work\\demo",
                   "timestamp": TODAY + "T10:00:00Z", "sessionId": name, "message": {"content": text}}
            row.update(extra)
            f.write(json.dumps(row) + "\n")


class MapCase(unittest.TestCase):
    def setUp(self):
        self.root = copy_map()
        self.projects = tempfile.mkdtemp(prefix="bmap-proj-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.projects, True)


class CorrectionTest(unittest.TestCase):
    def test_what_counts_as_a_correction(self):
        for text in ("dont add a co-author line", "i said no emoji in the ui", "no, use the subscription", "why did you ask me again"):
            self.assertTrue(learn.is_correction(text), text)
        for text in ("hör auf mit den emojis", "hoer auf mit den emojis", "hab ich dir doch gesagt, kein co-author",
                     "schon wieder ein neues fenster", "ab jetzt immer auf main pushen", "falsch, nimm das abo",
                     "warum hast du das gelöscht", "nächstes mal erst fragen"):
            self.assertTrue(learn.is_correction(text), text)
        self.assertFalse(learn.is_correction("bau mir bitte den header neu"))
        for text in ("fix the header", "ok", "<task-notification>dont</task-notification>", "/simplify never", "x " * 300 + "never"):
            self.assertFalse(learn.is_correction(text), text[:40])

    def test_private_details_are_taken_out(self):
        text = learn.clean('mail me at someone@example.org from 192.168.0.1 see https://example.org/x "now"')
        for bad in ("someone@", "192.168", "https://", '"'):
            self.assertNotIn(bad, text)

    def test_groups_need_three_on_two_days(self):
        def sig(i, day):
            return learn.new_signal("never open a test window on my screen please", day, "p", "s%d" % i, "scan")
        self.assertEqual(learn.groups([sig(1, "2026-11-12"), sig(2, "2026-11-12"), sig(3, "2026-11-12")]), [])
        found = learn.groups([sig(1, "2026-11-12"), sig(2, "2026-11-13"), sig(3, "2026-11-13")])
        self.assertEqual(found[0]["count"], 3)


class ScanTest(MapCase):
    def test_only_what_a_person_typed_is_read(self):
        transcript(self.projects, [
            ("dont do that again", {}),
            ("dont do that again", {"isMeta": True}),
            ("dont do that again", {"isSidechain": True}),
            ("dont do that again", {"origin": None}),
            ("dont do that again", {"entrypoint": "sdk-cli"}),
        ])
        path = os.path.join(self.projects, "c--proj", "s1.jsonl")
        prompts, end = learn.owner_prompts(path)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["project"], "demo")
        self.assertEqual(learn.owner_prompts(path, end)[0], [])

    def test_a_matching_correction_counts_once_and_marks_the_rule_broken(self):
        before = rule(self.root)["times_seen"]
        said = "dont " + rule(self.root)["rule"]
        transcript(self.projects, [(said, {}), ("fix the header please", {})])
        self.assertEqual(learn.scan(self.root, self.projects), 1)
        self.assertEqual(learn.scan(self.root, self.projects), 0)          # nothing new the second time
        summary, changed = learn.review(self.root)
        self.assertEqual(changed, [RULE])
        learn.review(self.root)                                           # a second review must not count again
        self.assertEqual(rule(self.root)["times_seen"], before + 1)
        self.assertEqual(rule(self.root)["last_seen"], TODAY)
        self.assertEqual(summary["broken"], {RULE: 1})
        sig = learn.load_signals(self.root)[0]
        learn.decide(sig["id"], "dismiss", root=self.root)                # the owner says it was not about this rule
        self.assertEqual(rule(self.root)["times_seen"], before)
        self.assertEqual(learn.review(self.root)[0]["broken"], {})

    def test_an_untouched_correction_leaves_the_inbox_after_a_month(self):
        transcript(self.projects, [("never ever zzqx the wibble again", {})])
        learn.scan(self.root, self.projects)
        self.assertEqual(learn.review(self.root, now=day(7))[0]["counts"]["open"], 1)
        self.assertEqual(learn.review(self.root, now=day(37))[0]["counts"]["expired"], 1)

    def test_all_prompts_are_written_once_with_private_details_blanked(self):
        transcript(self.projects, [("mail it to someone@example.org please", {"timestamp": "2026-03-01T10:00:00Z"}),
                                   ("fix the header", {"timestamp": "2026-01-01T10:00:00Z"}),
                                   ("<command-name>/clear</command-name>", {}), ("not typed by a person", {"origin": None})])
        count, out = learn.all_prompts(self.root, self.projects)
        rows = [json.loads(line) for line in build.read_text(out).split("\n") if line]
        self.assertEqual((count, [r["text"] for r in rows]), (2, ["fix the header", "mail it to [redacted] please"]))
        self.assertEqual(rows[0], {"date": "2026-01-01", "project": "demo", "text": "fix the header"})

    def test_old_chats_are_skipped(self):
        transcript(self.projects, [("dont do that again", {"timestamp": "2026-10-01T10:00:00Z"})])
        self.assertEqual(learn.scan(self.root, self.projects), 0)


class InstallFromThePageTest(MapCase):
    def test_a_tool_is_installed_and_removed(self):
        target = os.path.join(self.projects, "AGENTS.md")
        targets = {"codex": ("Codex", "exports/AGENTS.md", target)}
        with self.assertRaises(ui.Refused):
            ui.install_tool("codex", "install", self.root)             # a copy of the map never touches the real files
        self.assertTrue(ui.install_tool("codex", "install", self.root, targets).startswith("installed"))
        self.assertTrue(build.installed_from(self.root, target))
        self.assertTrue(ui.install_tool("codex", "remove", self.root, targets).startswith("removed"))
        self.assertFalse(os.path.isfile(target))
        with self.assertRaises(ui.Refused):
            ui.install_tool("word", "install", self.root, targets)

    def test_the_files_tab_names_every_tool(self):
        rows = {row["name"]: row for row in ui.state(self.root)["exports"]}
        self.assertEqual([t["tool"] for t in rows["CLAUDE.md"]["tools"]], ["claude"])
        self.assertEqual([t["tool"] for t in rows["AGENTS.md"]["tools"]], ["codex", "opencode"])
        self.assertEqual(rows["system-prompt-local.md"]["tools"], [])
        self.assertFalse(any(row["live"] for row in rows.values()))


class EditTest(MapCase):
    def test_an_edit_is_written_and_built(self):
        ui.save_rule(RULE, {"why": "Because the owner says so.", "strength": "soft"}, self.root)
        r = rule(self.root)
        self.assertEqual((r["why"], r["strength"]), ("Because the owner says so.", "soft"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "exports", "CLAUDE.md")))

    def test_an_edit_that_does_not_build_is_put_back(self):
        path = os.path.join(self.root, *rule(self.root)["path"].split("/"))
        before = build.read_text(path)
        with self.assertRaises(ui.Refused) as caught:
            ui.save_rule(RULE, {"short": "word " * 30}, self.root)
        self.assertIn("short", str(caught.exception))
        self.assertEqual(build.read_text(path), before)
        with self.assertRaises(ui.Refused):
            ui.save_rule(RULE, {"title": "  "}, self.root)

    def test_core_membership(self):
        core = lambda: ui.state(self.root)["profiles"][0]["ids"]
        self.assertIn(RULE, core())
        ui.save_rule(RULE, {"core": False}, self.root)
        self.assertNotIn(RULE, core())
        ui.save_rule(RULE, {"core": True}, self.root)
        self.assertIn(RULE, core())
        no_short = next(r for r in build.load_rules(self.root)[0] if not r.get("short") and r["scope"] == "always")
        with self.assertRaises(ui.Refused):
            ui.save_rule(no_short["id"], {"core": True}, self.root)
        self.assertNotIn(no_short["id"], core())

    def test_switching_a_rule_off_takes_it_out_of_the_exports(self):
        short = rule(self.root)["short"]
        ui.save_rule(RULE, {"enabled": False}, self.root)
        self.assertNotIn(short, build.read_text(os.path.join(self.root, "exports", "CLAUDE.md")))

    def test_a_new_rule_from_a_correction(self):
        sig = learn.note("never zzqx the wibble", root=self.root)
        rid, _snap = ui.new_rule({"category": "code", "title": "No wibble zzqx", "rule": "Leave the wibble alone.", "signals": [sig["id"]]}, self.root)
        r = rule(self.root, rid)
        self.assertEqual(r["quotes"][0]["quote"], "never zzqx the wibble")
        self.assertEqual(learn.load_signals(self.root)[0]["rule"], rid)
        self.assertEqual(learn.review(self.root)[0]["broken"], {})          # where a rule comes from is not a breach of it
        with self.assertRaises(ui.Refused):
            ui.new_rule({"category": "code", "title": "", "rule": ""}, self.root)

    def test_each_generated_file_says_where_it_is_written_and_who_reads_it(self):
        ui.save_rule(RULE, {"why": "x"}, self.root)                       # builds the exports of the copy
        files = {f["name"]: f for f in ui.state(self.root)["exports"]}
        self.assertEqual(set(files), set(os.listdir(os.path.join(self.root, "exports"))))
        self.assertTrue(files["CLAUDE.md"]["file"].endswith("/exports/CLAUDE.md"))
        self.assertFalse(files["CLAUDE.md"]["live"])                      # a copy of the map is never the installed one
        self.assertIn("Not installed", files["CLAUDE.md"]["used"])

    def test_a_project_rule_keeps_its_project(self):
        project_rule = next(r for r in build.load_rules(self.root)[0] if str(r["scope"]).startswith("project:"))
        with self.assertRaises(ui.Refused):
            ui.save_rule(project_rule["id"], {"scope": "always"}, self.root)


class ServerTest(MapCase):
    def setUp(self):
        MapCase.setUp(self)
        self.server, self.address = ui.serve(8930, self.root, token="t0ken")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, headers):
        req = urllib.request.Request(self.address.rstrip("/") + path, headers=headers)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, res.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, ""

    def test_only_its_own_page_gets_an_answer(self):
        self.assertEqual(self.get("/api/state", {"X-Brain-Token": "t0ken"})[0], 200)
        self.assertEqual(self.get("/api/state", {})[0], 403)                                      # no token
        self.assertEqual(self.get("/api/state", {"X-Brain-Token": "t0ken", "Host": "evil.example"})[0], 403)
        status, page = self.get("/", {})
        self.assertEqual(status, 200)
        self.assertIn("t0ken", page)


def skill(folder, name, header):
    os.makedirs(os.path.join(folder, name), exist_ok=True)
    with open(os.path.join(folder, name, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\n" + header + "\n---\n\nBody.\n")


class SkillLibraryTest(MapCase):
    def setUp(self):
        MapCase.setUp(self)
        self.home = tempfile.mkdtemp(prefix="bmap-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        docs = os.path.join(self.home, "Documents")
        skill(os.path.join(self.home, ".claude", "skills"), "polish", "name: polish\ndescription: >\n  Make the page\n  look right.")
        skill(os.path.join(self.home, ".claude", "skills", "synced", "acct"), "pdf", 'name: pdf\ndescription: "Read PDFs."')
        skill(os.path.join(docs, "shop", ".claude", "skills"), "run-shop", "name: run-shop\ndescription: Start the shop.")
        skill(os.path.join(docs, "shop-fix", ".claude", "skills"), "run-shop", "name: run-shop\ndescription: Start the shop.")
        skill(os.path.join(docs, "my-skills"), "walk-the-app", "name: walk-the-app\ndescription: Walk the app.")
        skill(os.path.join(docs, "my-skills"), "polish", "name: polish\ndescription: Make the page look right.")

        self.where = {"home": self.home, "project_folders": [docs], "projects": [], "own_skills": os.path.join(docs, "my-skills")}

    def library(self):
        found = skills.library(self.root, self.where, projects_dir=self.projects)
        return {k["key"]: k for k in found}

    def test_places_come_from_the_settings_and_from_where_the_chats_worked(self):
        elsewhere = os.path.join(self.home, "elsewhere", "tool")
        skill(os.path.join(elsewhere, ".claude", "skills"), "run-tool", "name: run-tool\ndescription: Start the tool.")
        folder = os.path.join(self.projects, "c--tool")
        os.makedirs(folder)
        with open(os.path.join(folder, "s1.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "cwd": elsewhere, "entrypoint": "claude-vscode", "timestamp": TODAY + "T10:00:00Z",
                                "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "run-tool"}}]}}) + "\n")
        build.write_text(os.path.join(self.root, "settings.json"), json.dumps({"own_skills": self.where["own_skills"]}))
        skills.count_use(self.root, self.projects)                         # notes the folder the chat worked in
        where = skills.places(self.root, self.home)
        self.assertEqual(where["own_skills"], self.where["own_skills"])
        self.assertIn(build.folder_text(elsewhere), where["projects"])
        found = {k["key"]: k for k in skills.library(self.root, where, projects_dir=self.projects)}
        self.assertEqual((found["run-tool"]["where"], found["run-tool"]["projects"], found["run-tool"]["used"]), ("projects", ["tool"], 1))
        self.assertEqual(found["walk-the-app"]["where"], "nowhere")

    def test_where_each_skill_is_active(self):
        lib = self.library()
        self.assertEqual(lib["polish"]["where"], "everywhere")
        self.assertEqual(lib["polish"]["source"], "written by you")                 # installed copy of the owner's own skill
        self.assertEqual(lib["polish"]["description"], "Make the page look right.")  # a folded description is one line
        self.assertEqual(lib["anthropic-skills:pdf"]["where"], "everywhere")
        self.assertEqual((lib["run-shop"]["where"], lib["run-shop"]["projects"]), ("projects", ["shop", "shop-fix"]))
        self.assertEqual(lib["walk-the-app"]["where"], "nowhere")

    def test_install_update_remove_touch_only_its_own_copies(self):
        docs, mine = os.path.join(self.home, "Documents"), os.path.join(self.home, ".claude", "skills")
        os.makedirs(os.path.join(docs, "my-skills", "walk-the-app", "evals"))
        self.assertEqual(self.library()["walk-the-app"]["switch"], ["install"])
        self.assertEqual(self.library()["polish"]["switch"], [])               # there already, and not put there by the script
        with self.assertRaises(build.BuildError):
            skills.switch("polish", "remove", self.where)
        with self.assertRaises(build.BuildError):
            skills.switch("anthropic-skills:pdf", "install", self.where)
        self.assertTrue(os.path.isfile(os.path.join(mine, "polish", "SKILL.md")))

        skills.switch("walk-the-app", "install", self.where)
        self.assertTrue(os.path.isfile(os.path.join(mine, "walk-the-app", "SKILL.md")))
        self.assertFalse(os.path.exists(os.path.join(mine, "walk-the-app", "evals")))       # answer keys stay behind
        lib = self.library()
        self.assertEqual((lib["walk-the-app"]["where"], lib["walk-the-app"]["switch"]), ("everywhere", ["remove"]))

        skill(os.path.join(docs, "my-skills"), "walk-the-app", "name: walk-the-app\ndescription: Walk the app twice.")
        self.assertEqual(self.library()["walk-the-app"]["switch"], ["remove", "update"])
        skills.switch("walk-the-app", "update", self.where)
        self.assertIn("twice", build.read_text(os.path.join(mine, "walk-the-app", "SKILL.md")))

        skills.switch("walk-the-app", "remove", self.where)
        self.assertFalse(os.path.exists(os.path.join(mine, "walk-the-app")))
        self.assertEqual(self.library()["walk-the-app"]["where"], "nowhere")

    def test_a_copy_of_the_map_cannot_change_the_real_skills_folder(self):
        with self.assertRaises(ui.Refused):
            ui.switch_skill("walk-the-app", "install", self.root)

    def test_use_is_counted_once_per_load_and_not_for_script_runs(self):
        def load(name, extra=None):
            row = {"type": "assistant", "cwd": "C:\\work\\shop", "timestamp": TODAY + "T10:00:00Z", "entrypoint": "claude-vscode",
                   "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": name}}]}}
            row.update(extra or {})
            return json.dumps(row)
        folder = os.path.join(self.projects, "c--shop")
        os.makedirs(folder)
        with open(os.path.join(folder, "s1.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join([load("polish"), load("polish"), load("polish", {"entrypoint": "sdk-cli"}), load("code-review")]) + "\n")
        lib = self.library()
        self.assertEqual((lib["polish"]["used"], lib["polish"]["used_in"], lib["polish"]["last_used"]), (2, {"shop": 2}, TODAY))
        self.assertEqual(lib["code-review"]["source"], "built in, or removed since")     # used, but not on disk
        self.assertEqual(lib["walk-the-app"]["used"], 0)
        self.assertEqual(self.library()["polish"]["used"], 2)                            # a second read counts nothing twice


class SaveToGitHubTest(unittest.TestCase):
    """Against a throwaway repo with a local folder standing in for GitHub."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bmap-git-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.remote, self.work = os.path.join(self.base, "remote.git"), os.path.join(self.base, "work")
        os.makedirs(self.work)
        sync.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], self.base)
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "T"],
                     ["remote", "add", "origin", self.remote]):
            sync.run(["git"] + args, self.work)
        self.write("a.txt", "one")
        sync.run(["git", "add", "-A"], self.work)
        sync.run(["git", "commit", "-q", "-m", "first"], self.work)
        sync.run(["git", "push", "-q", "-u", "origin", "main"], self.work)

    def write(self, name, text):
        build.write_text(os.path.join(self.work, name), text)

    def pushed(self):
        return sync.run(["git", "log", "--format=%s", "main"], self.remote)[1].split("\n")

    def test_a_change_is_committed_and_pushed_after_a_clean_scan(self):
        self.assertEqual(sync.save(self.work, scan=lambda root: 0), "GitHub is up to date")
        self.write("rules/git/x.md", "two")
        self.assertEqual(sync.save(self.work, scan=lambda root: 0), "pushed to GitHub")
        self.assertTrue(self.pushed()[0].endswith(": 1 rule"), self.pushed())
        self.assertNotIn("Co-Authored", sync.run(["git", "log", "-1", "--format=%B"], self.work)[1])

    def test_nothing_is_pushed_without_a_clean_scan_and_the_next_run_catches_up(self):
        self.write("b.txt", "two")
        self.assertIn("could not run", sync.save(self.work, scan=lambda root: None))
        self.write("c.txt", "three")
        self.assertIn("found 2 possible secrets", sync.save(self.work, scan=lambda root: 2))
        self.assertEqual(self.pushed(), ["first"])
        self.assertEqual(sync.save(self.work, scan=lambda root: 0), "pushed to GitHub")
        self.assertEqual(len(self.pushed()), 3)

    def test_a_folder_that_is_not_its_own_repo_is_left_alone(self):
        inner = os.path.join(self.work, "inner")
        os.makedirs(inner)
        self.assertIn("not a git repo", sync.save(inner, scan=lambda root: 0))


class TwoComputersTest(unittest.TestCase):
    """One map, two clones (say a laptop and a server), a local folder standing in for GitHub."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="bmap-two-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.remote, self.a, self.b = (os.path.join(self.base, n) for n in ("remote.git", "a", "b"))
        sync.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], self.base)
        os.makedirs(self.a)
        build.init(self.a)
        self.git(self.a, ["init", "-q", "-b", "main"])
        self.who(self.a)
        self.git(self.a, ["remote", "add", "origin", self.remote])
        sync.commit(self.a, "first")
        self.git(self.a, ["push", "-q", "-u", "origin", "main"])
        self.git(self.base, ["clone", "-q", self.remote, self.b])
        self.who(self.b)
        self.rel = os.path.relpath(sorted(glob.glob(os.path.join(self.a, "rules", "*", "*.md")))[0], self.a).replace("\\", "/")

    def git(self, cwd, args):
        code, out = sync.run(["git"] + args, cwd)
        self.assertEqual(code, 0, out)
        return out

    def who(self, root):
        self.git(root, ["config", "user.email", "t@example.invalid"])
        self.git(root, ["config", "user.name", "T"])

    def rule(self, root):
        return build.parse_rule(build.read_text(os.path.join(root, self.rel)), self.rel)

    def change(self, root, **fields):
        r = self.rule(root)
        r.update(fields)
        build.write_text(os.path.join(root, self.rel), build.render_rule(r))
        build.run(root)

    def save(self, root):
        return sync.save(root, scan=lambda _root: 0)

    def test_the_other_computer_takes_in_a_change_and_rebuilds(self):
        self.change(self.a, rule="Wording from the laptop, kept as it is.")
        self.assertEqual(self.save(self.a), "pushed to GitHub")
        self.assertIn("took in 1 change", sync.follow(self.b))
        self.assertEqual(self.rule(self.b)["rule"], "Wording from the laptop, kept as it is.")
        self.assertIn("Wording from the laptop", build.read_text(os.path.join(self.b, "rules.json")))
        self.assertEqual(sync.follow(self.b), "nothing new from the other computers")

    def test_different_fields_of_one_rule_merge_and_counts_add_up(self):
        start = self.rule(self.a)
        quote = {"date": "2026-11-12", "project": "server", "quote": "said on the server"}
        self.change(self.a, rule="Wording from the laptop.", times_seen=int(start["times_seen"]) + 1)
        self.change(self.b, times_seen=int(start["times_seen"]) + 2, quotes=start["quotes"] + [quote], last_seen="2026-11-12")
        self.assertEqual(self.save(self.a), "pushed to GitHub")
        self.assertEqual(self.save(self.b), "pushed to GitHub")
        merged = self.rule(self.b)
        self.assertEqual(merged["rule"], "Wording from the laptop.")
        self.assertEqual(int(merged["times_seen"]), int(start["times_seen"]) + 3)
        self.assertIn(quote, merged["quotes"])
        self.assertEqual(build.run(self.b, write=False)[0], [])
        sync.follow(self.a)
        self.assertEqual(self.rule(self.a), merged)
        self.assertEqual(self.git(self.a, ["status", "--porcelain"]), "")

    def test_one_field_edited_differently_on_both_stops_and_names_the_file(self):
        self.change(self.a, rule="The laptop's wording.")
        self.change(self.b, rule="The server's wording.")
        self.assertEqual(self.save(self.a), "pushed to GitHub")
        line = self.save(self.b)
        self.assertIn("not pushed: changed here and on another computer: " + self.rel, line)
        self.assertEqual(self.rule(self.b)["rule"], "The server's wording.")              # nothing lost on either side
        self.assertEqual(self.git(self.b, ["status", "--porcelain"]), "")
        self.assertFalse(sync.rebasing(self.b))

    def test_the_corrections_log_merges_by_correction(self):
        learn.note("dont do that on the laptop again", root=self.a)
        shared = learn.note("never push without asking", root=self.a)
        self.assertEqual(self.save(self.a), "pushed to GitHub")
        sync.follow(self.b)
        learn.decide(shared["id"], "dismiss", root=self.b)
        learn.note("stop opening windows on the server", root=self.b)
        learn.note("i said no new files here", root=self.a)
        self.assertEqual(self.save(self.a), "pushed to GitHub")
        self.assertEqual(self.save(self.b), "pushed to GitHub")
        sigs = {s["text"]: s["status"] for s in learn.load_signals(self.b)}
        self.assertEqual(len(sigs), 4)
        self.assertEqual(sigs["never push without asking"], "dismissed")


class LocalModelTest(MapCase):
    CFG = {"base_url": "http://127.0.0.1:1/v1", "model": "m", "api_key": "k"}

    def test_settings_come_from_the_opencode_file(self):
        path = os.path.join(self.projects, "opencode.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write('''{ // my models
  "provider": {"box": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://box:8000/v1", "apiKey": "secret"},
                       "models": {"coder": {"name": "Coder"}, "coder-think": {"id": "coder"}}}}}''')
        self.assertEqual(judge.settings(self.root, path), {"base_url": "http://box:8000/v1", "api_key": "secret", "model": "coder"})
        self.assertIsNone(judge.settings(self.root, os.path.join(self.projects, "missing.json")))

    def test_short_prompts_without_a_marker_word_wait_for_the_model(self):
        transcript(self.projects, [("please keep the tables as wide as the window", {}), ("dont do that again", {})])
        self.assertEqual(learn.scan(self.root, self.projects, wide=True), 1)            # only the marked one reaches the inbox
        self.assertEqual(sorted(s["status"] for s in learn.load_signals(self.root)), ["open", "waiting"])
        learn.review(self.root, now=day(37))                                        # never judged: dropped after a month
        self.assertEqual([s["status"] for s in learn.load_signals(self.root)], ["expired"])

    def test_the_model_sets_task_talk_aside_places_matches_and_words_new_rules(self):
        talk = learn.note("i dont want another apk, the browser should work", root=self.root)
        match = learn.note("stop putting your name on my commits", root=self.root)
        new = learn.note("never zzqx the wibble", root=self.root)
        offered = {}

        def asker(cfg, text, rules):
            offered[text] = [r["id"] for r in rules]
            if "apk" in text:
                return {"standing": False, "rule": None, "new_rule": ""}
            if "commits" in text:
                return {"standing": True, "rule": RULE, "new_rule": ""}
            return {"standing": True, "rule": None, "new_rule": "Leave the wibble alone."}

        off = judge.sort_signals(self.root, self.CFG, asker, reachable=lambda cfg: False)
        self.assertEqual((off["on"], off["judged"]), (False, 0))
        done = judge.sort_signals(self.root, self.CFG, asker, reachable=lambda cfg: True)
        self.assertEqual((done["on"], done["judged"]), (True, 3))
        self.assertLessEqual(max(len(ids) for ids in offered.values()), judge.CANDIDATES)
        by_id = {s["id"]: s for s in learn.load_signals(self.root)}
        self.assertEqual(by_id[talk["id"]]["status"], "set aside")
        self.assertEqual((by_id[match["id"]]["status"], by_id[match["id"]]["rule"]), ("attached", RULE))
        self.assertEqual((by_id[new["id"]]["status"], by_id[new["id"]]["proposal"]), ("open", "Leave the wibble alone."))
        self.assertEqual(learn.review(self.root)[0]["broken"], {RULE: 1})
        self.assertEqual(judge.sort_signals(self.root, self.CFG, asker, reachable=lambda cfg: True)["judged"], 0)   # nothing is judged twice

    def test_a_model_that_fails_midway_leaves_the_rest_for_next_time(self):
        learn.note("dont do the first thing again", root=self.root)
        learn.note("dont do the second thing again", root=self.root)

        def asker(cfg, text, rules):
            if "second" in text:
                raise OSError("connection reset")
            return {"standing": False, "rule": None, "new_rule": ""}
        result = judge.sort_signals(self.root, self.CFG, asker, reachable=lambda cfg: True)
        self.assertEqual(result["judged"], 1)
        self.assertIn("stopped answering", result["note"])
        self.assertEqual(sorted(s["status"] for s in learn.load_signals(self.root)), ["open", "set aside"])

    def test_asking_a_real_endpoint_and_refusing_a_rule_that_was_not_offered(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        seen = {}

        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body):
                data = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply({"data": [{"id": "m"}]})

            def do_POST(self):
                seen.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))), key=self.headers.get("Authorization"))
                self.reply({"choices": [{"message": {"content": 'Here: {"standing": true, "rule": "%s", "new_rule": ""}' % seen_rule[0]}}]})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cfg = {"base_url": "http://127.0.0.1:%d/v1" % server.server_address[1], "model": "m", "api_key": "k"}
        offered = [rule(self.root)]
        self.assertTrue(judge.available(cfg))
        self.assertFalse(judge.available(dict(cfg, model="other")))
        seen_rule = [RULE]
        self.assertEqual(judge.ask(cfg, "no co-author line", offered), {"standing": True, "rule": RULE, "new_rule": ""})
        self.assertEqual((seen["key"], seen["model"], seen["temperature"]), ("Bearer k", "m", 0))
        seen_rule[0] = "made-up-rule"
        self.assertIsNone(judge.ask(cfg, "no co-author line", offered)["rule"])


if __name__ == "__main__":
    unittest.main()
