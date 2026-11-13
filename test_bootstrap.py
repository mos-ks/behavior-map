# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
"""Tests for bootstrap.py (the build from the chats) and the page's part in it. A scripted stand-in plays the
model, so the tests run offline and cost nothing.

    python -m unittest test_bootstrap -v
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error

# the tests run on the starter map, never on your own
os.environ["BEHAVIOR_MAP_DIR"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "starter")

import bootstrap
import build
import skills
import ui

CHATS = {
    ("shop", "2026-08-03"): ["add a cart page with a list of items and a total",
                             "why is there a co-authored-by line in the commit? dont add that",
                             "use pnpm here, not npm",
                             "release the app: bump the version, build it, tag it and push the tag",
                             "answer first please, then explain"],
    ("shop", "2026-08-19"): ["you ran npm install again. i said pnpm in this repo",
                             "release it again: bump, build, tag, push the tag",
                             "my server is at 10.1.2.3, never write that anywhere"],
    ("blog", "2026-09-02"): ["again a co-author trailer on the commit. never add any ai attribution, anywhere",
                             "first the answer, then the details"],
}


def write_chats(projects, chats=CHATS):
    for (project, day), prompts in chats.items():
        folder = os.path.join(projects, "c--work-" + project)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "%s-%s.jsonl" % (project, day)), "w", encoding="utf-8") as f:
            for i, text in enumerate(prompts):
                f.write(json.dumps({"type": "user", "origin": {"kind": "human"}, "entrypoint": "claude-vscode", "cwd": "C:\\work\\" + project,
                                    "timestamp": "%sT10:%02d:00Z" % (day, i), "sessionId": project + day, "message": {"content": text}}) + "\n")


class FakeModel:
    """Answers the three kinds of question the way a careful model would, from keywords."""

    KINDS = [("co-auth", {"rule": "Never add AI attribution to commits.", "category": "git", "scope": "always", "firm": True}),
             ("pnpm", {"rule": "Use pnpm, never npm.", "category": "code", "scope": "project", "firm": True}),
             ("answer", {"rule": "Give the answer first, then the details.", "category": "communication", "scope": "always", "firm": False}),
             ("my server", {"rule": "Never write the server address 10.1.2.3 anywhere.", "category": "security-privacy", "scope": "always", "firm": True})]
    FINAL = {"git": {"title": "No AI attribution", "short": "Never add AI attribution to commits.", "existing": "git-no-ai-attribution"},
             "code": {"title": "Use pnpm", "short": "Use pnpm, never npm."},
             "communication": {"title": "Answer first", "short": "Answer first, details after.", "existing": "communication-result-first"},
             "security-privacy": {"title": "Keep the address private", "short": "Keep it private."}}

    def __init__(self):
        self.asked = []

    def __call__(self, system, user):
        self.asked.append(system[:30])
        if system == bootstrap.EXTRACT:
            rules, release = [], []
            for line in user.split("\n")[1:]:
                n, _date, _project, text = line.split(" | ", 3)
                for word, finding in self.KINDS:
                    if word in text:
                        rules.append(dict(finding, prompts=[int(n)], why=""))
                if "release" in text:
                    release.append(int(n))
            skills_ = [{"name": "release-app", "what": "Release the app the usual way.", "prompts": release}] if release else []
            return {"rules": rules, "skills": skills_ + [{"name": "made-up", "what": "Not shown.", "prompts": [9999]}]}
        if system.startswith(bootstrap.MERGE.split("%")[0]):
            category = re.search(r'category "([^"]+)"', system).group(1)
            count = len([l for l in user.split("\n\n")[0].split("\n")[1:] if l.strip()])
            first = user.split("\n")[1].split(" | ", 2)[2]
            return {"rules": [dict(self.FINAL[category], **{"from": list(range(count)), "rule": first, "why": "", "earlier": "", "open_question": ""})]}
        if system == bootstrap.MERGE_SKILLS:
            count = len([l for l in user.split("\n")[1:] if l.strip()])
            return {"skills": [{"from": list(range(count)), "name": "release-app", "description": "Releases the app: version, build, tag.",
                                "steps": ["Bump the version.", "Build it.", "Tag it and push the tag."]}]}
        raise AssertionError("unexpected question")


class BuildCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bmap-boot-")
        self.projects = tempfile.mkdtemp(prefix="bmap-chats-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.projects, True)
        for name in ("rules", "profiles"):
            shutil.copytree(os.path.join(build.ROOT, name), os.path.join(self.root, name))
        shutil.copy(os.path.join(build.ROOT, "conflicts.md"), self.root)
        write_chats(self.projects)

    def rules(self):
        return {r["id"]: r for r in build.load_rules(self.root)[0]}

    def run_build(self, model=None):
        model = model or FakeModel()
        found = bootstrap.prepare(self.root, model, self.projects)
        return model, bootstrap.apply(found, self.root)


class BuildTest(BuildCase):
    def test_a_map_is_built_from_the_chats(self):
        _model, summary = self.run_build()
        self.assertEqual(summary["problems"], [])
        rules = self.rules()
        git = rules["git-no-ai-attribution"]                       # the starter rule, rewritten in the owner's words
        self.assertEqual(git["sources"], ["chats"])
        self.assertEqual(git["times_seen"], 2)
        self.assertEqual(git["last_seen"], "2026-09-02")
        self.assertEqual({q["project"] for q in git["quotes"]}, {"shop", "blog"})
        pnpm = rules["code-shop-use-pnpm"]                         # said in one project: a project rule
        self.assertEqual(pnpm["scope"], "project:shop")
        self.assertTrue(pnpm["path"].startswith("rules/_project/shop/"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "exports", "project-shop.md")))
        self.assertNotIn("10.1.2.3", json.dumps(bootstrap.read_prompts(self.root, self.projects)))   # blanked before the model reads it
        self.assertNotIn("security-privacy-keep-the-address-private", rules)          # the model wrote one in anyway
        self.assertEqual(summary["left_out"].get("it named something private"), 1)
        self.assertEqual(summary["rewritten"], 2)
        self.assertEqual(summary["skills"], ["release-app"])
        skill = build.read_text(os.path.join(self.root, "skills", "release-app", "SKILL.md"))
        self.assertIn("1. Bump the version.", skill)
        self.assertIn("release it again", skill)
        self.assertIn("git-no-ai-attribution", build.read_text(os.path.join(self.root, "exports", "CLAUDE.md")) + " ".join(
            build.load_profiles(self.root)[0][0]["include_ids"]))
        self.assertEqual(build.run(self.root, write=False)[0], [])

    def test_a_second_build_does_not_ask_the_same_part_twice(self):
        self.run_build()
        model, _summary = self.run_build()
        self.assertNotIn(bootstrap.EXTRACT[:30], model.asked)

    def test_a_suggested_skill_appears_in_the_library_with_a_switch(self):
        self.run_build()
        home = tempfile.mkdtemp(prefix="bmap-home-")
        self.addCleanup(shutil.rmtree, home, True)
        where = {"home": home, "project_folders": [], "projects": [], "own_skills": "", "suggested": os.path.join(self.root, "skills")}
        entry = next(s for s in skills.inventory(where) if s["key"] == "release-app")
        self.assertEqual((entry["source"], entry["where"], entry["switch"]), ("suggested from your chats", "nowhere", ["install"]))
        skills.switch("release-app", "install", where)
        self.assertTrue(os.path.isfile(os.path.join(home, ".claude", "skills", "release-app", "SKILL.md")))

    def test_without_chats_or_an_answer_nothing_changes(self):
        empty = tempfile.mkdtemp(prefix="bmap-none-")
        self.addCleanup(shutil.rmtree, empty, True)
        before = sorted(self.rules())
        with self.assertRaises(bootstrap.Failed):
            bootstrap.prepare(self.root, FakeModel(), empty)

        def silent(_system, _user):
            raise OSError("connection refused")
        with self.assertRaises(bootstrap.Failed):
            bootstrap.prepare(self.root, silent, self.projects)
        self.assertEqual(sorted(self.rules()), before)

    def test_a_finding_must_name_a_prompt_it_was_shown(self):
        found = bootstrap.clean_find({"rules": [{"rule": "x y z", "category": "git", "prompts": [5]},
                                                {"rule": "a b c", "category": "nonsense", "prompts": [1]},
                                                {"rule": "d e f", "category": "git", "prompts": [1, True]}],
                                      "skills": "not a list"}, {1, 2})
        self.assertEqual([r["prompts"] for r in found["rules"]], [[1]])
        self.assertEqual(found["skills"], [])


class EngineTest(unittest.TestCase):
    def test_claude_code_is_asked_as_a_plain_model(self):
        seen = {}

        def runner(args, **kw):
            seen["args"], seen["input"] = args, kw["input"]
            return subprocess.CompletedProcess(args, 0, json.dumps({"result": 'Here: {"rules": [], "skills": []}', "is_error": False}).encode(), b"")
        self.assertEqual(bootstrap.ask_claude("SYSTEM", "USER", exe="claude", runner=runner), {"rules": [], "skills": []})
        self.assertIn("-p", seen["args"])
        self.assertEqual(seen["args"][seen["args"].index("--tools") + 1], "")
        self.assertTrue(seen["input"].startswith(b"SYSTEM"))

        def failing(args, **kw):
            return subprocess.CompletedProcess(args, 1, json.dumps({"result": "Not logged in", "is_error": True}).encode(), b"")
        with self.assertRaises(OSError):
            bootstrap.ask_claude("S", "U", exe="claude", runner=failing)

    def test_an_address_that_refuses_the_strict_form_is_asked_plainly(self):
        bodies = []

        def call(cfg, path, body, timeout):
            bodies.append(body)
            if "response_format" in body:
                raise urllib.error.HTTPError("u", 400, "bad", {}, None)
            return {"choices": [{"message": {"content": '{"skills": []}'}}]}
        self.assertEqual(bootstrap.ask_api("S", "U", {"model": "m", "base_url": "http://x/v1"}, call), {"skills": []})
        self.assertEqual(len(bodies), 2)


class PageBuildTest(BuildCase):
    def test_the_page_runs_the_build_in_the_background(self):
        original = bootstrap.learn.sessions_dir
        self.addCleanup(setattr, bootstrap.learn, "sessions_dir", original)
        empty = tempfile.mkdtemp(prefix="bmap-empty-")                            # never the real chats
        self.addCleanup(shutil.rmtree, empty, True)
        bootstrap.learn.sessions_dir = lambda: empty
        worker = ui.start_build("api", self.root, ask=FakeModel())
        worker.join(30)
        job = ui.build_status(self.root)["job"]
        self.assertEqual(job["state"], "failed")
        self.assertIn("No chats were found", job.get("error", ""))
        bootstrap.learn.sessions_dir = lambda: self.projects
        worker = ui.start_build("api", self.root, ask=FakeModel())
        worker.join(60)
        status = ui.build_status(self.root)
        self.assertEqual(status["job"]["state"], "done", status["job"].get("error"))
        self.assertEqual(status["last"]["skills"], ["release-app"])
        self.assertFalse(status["starter_only"])
        with self.assertRaises(ui.Refused):
            ui.start_build("api", self.root)                                       # a test copy never reads the real chats

    def test_a_model_address_is_checked_before_it_is_saved(self):
        def call(cfg, path, timeout):
            return {"data": [{"id": "qwen"}]}
        with self.assertRaises(ui.Refused):
            ui.save_engine({"base_url": "localhost", "model": "qwen"}, self.root, call)
        with self.assertRaises(ui.Refused):
            ui.save_engine({"base_url": "http://localhost:8000/v1", "model": "other"}, self.root, call)
        ui.save_engine({"base_url": "http://localhost:8000/v1/", "model": "qwen", "api_key": "k1"}, self.root, call)
        ui.save_engine({"base_url": "http://localhost:8000/v1", "model": "qwen"}, self.root, call)       # empty key: the saved one stays
        saved = json.loads(build.read_text(os.path.join(self.root, "signals", "judge.json")))
        self.assertEqual(saved, {"base_url": "http://localhost:8000/v1", "model": "qwen", "api_key": "k1"})


class ClosingTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(ui.SEEN, tabs=dict(ui.SEEN["tabs"]))
        self.addCleanup(ui.SEEN.update, self.saved)

    def test_the_page_stops_only_when_no_tab_is_left(self):
        now = time.time()
        ui.SEEN.update(last=now, tabs={}, had_tab=False)
        self.assertFalse(ui.no_tab_left(now + 60))                        # started, the browser is still opening
        self.assertTrue(ui.no_tab_left(now + ui.IDLE_MINUTES * 60 + 1))
        ui.SEEN.update(last=now, tabs={"a": now, "b": now}, had_tab=True)
        ui.SEEN["tabs"].pop("a")                                           # one of two tabs closed
        self.assertFalse(ui.no_tab_left(now + 60))
        ui.SEEN["tabs"].pop("b")                                           # the last one closed
        self.assertFalse(ui.no_tab_left(now + 5))                         # a reload comes back in time
        self.assertTrue(ui.no_tab_left(now + ui.BYE_SECONDS + 1))
        ui.SEEN.update(last=now, tabs={"c": now}, had_tab=True)            # a tab the browser threw away without a word
        self.assertTrue(ui.no_tab_left(now + ui.IDLE_MINUTES * 60 + 1))


if __name__ == "__main__":
    unittest.main()
