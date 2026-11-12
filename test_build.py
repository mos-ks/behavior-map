# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
"""Tests for build.py. Run from the behavior-map folder: python -m unittest test_build -v"""
import os
import tempfile
import unittest

# the tests run on the starter map, never on your own
os.environ["BEHAVIOR_MAP_DIR"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "starter")

import build

RULE = """---
id: git-no-ai-attribution          # category prefix + short slug
title: No AI attribution in commits
category: git
scope: always
strength: hard
enabled: true
times_seen: 16
sources: [A, B, C]
last_seen: 2026-11-08
needs_owner: false
short: "Never add AI attribution: no trailer, no byline."
chat: respond
---

**Rule.** Never add AI attribution to commits.
It applies to pull requests as well.

**Why.** The work goes out under the owner's name.

**Applies / does not apply.** Solo repos and team repos alike.

**Earlier.** Nothing different.

**Example**
- Before: Co-Authored-By: a model
- After: (no trailer)

**In the owner's words**
- 2026-10-27, shop: "stop asking me to say "push", push man"
- undated, blog: "dont use attribution"
"""


def rule_text(rid, category="code", scope="coding", enabled=True, short=None, body="Do the thing.", extra=""):
    lines = ["---", "id: " + rid, "title: " + rid, "category: " + category, "scope: " + scope, "strength: hard",
             "enabled: " + ("true" if enabled else "false"), "times_seen: 3", "sources: [A]", "last_seen: null", "needs_owner: false"]
    if short:
        lines.append('short: "%s"' % short)
    if extra:
        lines.append(extra)
    lines += ["---", "", "**Rule.** " + body, ""]
    return "\n".join(lines)


CORE_CATEGORIES = ["git", "autonomy", "communication", "code", "ops-machines", "product-thinking"]


class TempMap(unittest.TestCase):
    """A small map in a temp folder: one core rule per heading plus a few others."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.core_ids = []
        for cat in CORE_CATEGORIES:
            rid = cat + "-core-rule"
            self.core_ids.append(rid)
            self.write_rule(cat, "core-rule.md", rule_text(rid, cat, "always", short="Short line for %s." % cat))
        self.write_rule("code", "coding-only.md", rule_text("code-coding-only", "code", "coding"))
        self.write_rule("ui-design", "ui-only.md", rule_text("ui-design-ui-only", "ui-design", "ui"))
        self.write_rule("code", "off.md", rule_text("code-off", "code", "coding", enabled=False, body="DISABLED RULE TEXT."))
        self.write_rule("_project/demo", "demo-only.md", rule_text("code-demo-only", "code", "project:demo", body="PROJECT RULE TEXT."))
        self.write_profile("core", [], self.core_ids)
        self.write_profile("coding", ["always", "coding"], [])

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        full = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def write_rule(self, folder, name, text):
        self.write("rules/%s/%s" % (folder, name), text)

    def write_profile(self, name, scopes, ids, exclude=()):
        lines = ["name: " + name, "description: Profile " + name, "include_scopes: [" + ", ".join(scopes) + "]", "include_ids:"]
        lines += ["  - " + i for i in ids]
        lines += ["exclude_ids: [" + ", ".join(exclude) + "]"]
        self.write("profiles/%s.yaml" % name, "\n".join(lines) + "\n")

    def run_build(self, write=False):
        errors, outputs, _rules = build.run(self.root, write=write)
        return errors, outputs


class ParseTests(unittest.TestCase):
    def test_frontmatter_types_and_comment(self):
        r = build.parse_rule(RULE, "rules/git/no-ai-attribution.md")
        self.assertEqual(r["id"], "git-no-ai-attribution")
        self.assertEqual(r["title"], "No AI attribution in commits")
        self.assertIs(r["enabled"], True)
        self.assertIs(r["needs_owner"], False)
        self.assertEqual(r["times_seen"], 16)
        self.assertEqual(r["sources"], ["A", "B", "C"])
        self.assertEqual(r["last_seen"], "2026-11-08")
        self.assertEqual(r["short"], "Never add AI attribution: no trailer, no byline.")
        self.assertEqual(r["chat"], "respond")

    def test_body_fields(self):
        r = build.parse_rule(RULE)
        self.assertEqual(r["rule"], "Never add AI attribution to commits. It applies to pull requests as well.")
        self.assertEqual(r["why"], "The work goes out under the owner's name.")
        self.assertEqual(r["boundary"], "Solo repos and team repos alike.")
        self.assertEqual(r["earlier"], "Nothing different.")
        self.assertEqual(r["example"], {"before": "Co-Authored-By: a model", "after": "(no trailer)"})
        self.assertEqual(len(r["quotes"]), 2)
        self.assertEqual(r["quotes"][0], {"date": "2026-10-27", "project": "shop", "quote": 'stop asking me to say "push", push man'})
        self.assertIsNone(r["quotes"][1]["date"])

    def test_null_and_missing_optional_fields(self):
        r = build.parse_rule(rule_text("code-x"))
        self.assertIsNone(r["last_seen"])
        self.assertIsNone(r["why"])
        self.assertEqual(r["quotes"], [])

    def test_no_frontmatter_is_an_error(self):
        with self.assertRaises(build.BuildError):
            build.parse_rule("**Rule.** No header here.\n", "rules/code/bad.md")

    def test_render_round_trip(self):
        r = build.parse_rule(RULE, "p")
        again = build.parse_rule(build.render_rule(r), "p")
        self.assertEqual(r, again)

    def test_profile_yaml_block_and_inline_lists(self):
        p = build.parse_simple_yaml("name: ui\ndescription: Screens: all of them\ninclude_scopes: [always, ui]\ninclude_ids:\n  - a-b\n  - c-d\nexclude_ids: []\n")
        self.assertEqual(p["include_scopes"], ["always", "ui"])
        self.assertEqual(p["include_ids"], ["a-b", "c-d"])
        self.assertEqual(p["exclude_ids"], [])
        self.assertEqual(p["description"], "Screens: all of them")


class SelectionTests(TempMap):
    def load(self):
        rules, errors = build.load_rules(self.root)
        profiles, perrors = build.load_profiles(self.root)
        self.assertEqual(errors + perrors, [])
        return rules, {p["name"]: p for p in profiles}

    def test_scope_selection(self):
        rules, profiles = self.load()
        ids = [r["id"] for r in build.select(profiles["coding"], rules)]
        self.assertIn("code-coding-only", ids)
        self.assertIn("git-core-rule", ids)
        self.assertNotIn("ui-design-ui-only", ids)

    def test_core_is_explicit_and_keeps_its_order(self):
        rules, profiles = self.load()
        self.assertEqual([r["id"] for r in build.select(profiles["core"], rules)], self.core_ids)

    def test_include_and_exclude_ids(self):
        self.write_profile("coding", ["coding"], ["ui-design-ui-only"], exclude=["code-coding-only"])
        rules, profiles = self.load()
        ids = [r["id"] for r in build.select(profiles["coding"], rules)]
        self.assertEqual(ids, ["ui-design-ui-only"])

    def test_project_rules_are_not_selected_by_scope(self):
        rules, profiles = self.load()
        self.assertNotIn("code-demo-only", [r["id"] for r in build.select(profiles["coding"], rules)])

    def test_unknown_id_in_profile_fails(self):
        self.write_profile("coding", ["coding"], ["code-does-not-exist"])
        errors, _ = self.run_build()
        self.assertTrue(any("code-does-not-exist" in e for e in errors), errors)


class DisabledTests(TempMap):
    def test_disabled_rule_is_skipped_everywhere_but_listed(self):
        self.write_profile("core", [], self.core_ids + ["code-off"])
        errors, out = self.run_build()
        self.assertEqual(errors, [])
        for name, text in out.items():
            if name.startswith("exports/"):
                self.assertNotIn("DISABLED RULE TEXT", text, name)
        self.assertIn("code-off", out["rules.json"])
        self.assertIn("Turned off: 1", out["index.md"])

    def test_project_rule_text_stays_out_of_exports(self):
        errors, out = self.run_build()
        self.assertEqual(errors, [])
        for name, text in out.items():
            if name.startswith("exports/") and not name.startswith("exports/project-"):
                self.assertNotIn("PROJECT RULE TEXT", text, name)
        own = [name for name in out if name.startswith("exports/project-")]
        self.assertEqual(len(own), 1)                              # the project's own file, and only there
        self.assertIn("PROJECT RULE TEXT", out[own[0]])


class LimitTests(TempMap):
    def test_small_map_builds_and_writes(self):
        errors, out = self.run_build(write=True)
        self.assertEqual(errors, [])
        for rel in ("exports/CLAUDE.md", "exports/AGENTS.md", "exports/profile-coding.md",
                    "exports/chatgpt-custom-instructions.txt", "exports/system-prompt-local.md", "index.md", "rules.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.root, *rel.split("/"))), rel)
        self.assertTrue(out["exports/CLAUDE.md"].startswith("<!-- Generated by build.py"))
        self.assertIn("<BEHAVIOR_MAP_DIR>/exports/profile-coding.md", out["exports/CLAUDE.md"])

    def test_core_over_60_lines_fails(self):
        ids = list(self.core_ids)
        for i in range(60):
            rid = "code-extra-%d" % i
            self.write_rule("code", "extra-%d.md" % i, rule_text(rid, "code", "always", short="Extra line %d." % i))
            ids.append(rid)
        self.write_profile("core", [], ids)
        errors, _ = self.run_build()
        self.assertTrue(any("lines" in e and "CLAUDE.md" in e for e in errors), errors)

    def test_chatgpt_block_over_1500_fails_and_writes_nothing(self):
        for i in range(40):
            self.write_rule("communication", "chat-%d.md" % i,
                            rule_text("communication-chat-%d" % i, "communication", "always", short="A fairly long line about how to answer, number %d." % i, extra="chat: respond"))
        errors, _ = self.run_build(write=True)
        self.assertTrue(any("1500" in e for e in errors), errors)
        self.assertFalse(os.path.exists(os.path.join(self.root, "exports")))

    def test_short_over_18_words_fails(self):
        self.write_rule("code", "wordy.md", rule_text("code-wordy", short=" ".join(["word"] * 19)))
        errors, _ = self.run_build()
        self.assertTrue(any("short" in e and "wordy" in e for e in errors), errors)

    def test_core_rule_without_short_fails(self):
        self.write_profile("core", [], self.core_ids + ["code-coding-only"])
        errors, _ = self.run_build()
        self.assertTrue(any("short" in e for e in errors), errors)

    def test_em_dash_in_rule_text_fails(self):
        self.write_rule("code", "dash.md", rule_text("code-dash", body="Do this — then that."))
        errors, _ = self.run_build()
        self.assertTrue(any("dash" in e for e in errors), errors)

    def test_duplicate_id_and_missing_field_fail(self):
        self.write_rule("code", "copy.md", rule_text("code-coding-only"))
        self.write_rule("code", "nofield.md", rule_text("code-nofield").replace("strength: hard\n", ""))
        errors, _ = self.run_build()
        self.assertTrue(any("already used" in e for e in errors), errors)
        self.assertTrue(any("missing field `strength`" in e for e in errors), errors)

    def test_private_details_in_exports_fail(self):
        self.write_rule("code", "leak.md", rule_text("code-leak", body="Mail the result to someone@example.org."))
        errors, _ = self.run_build()
        self.assertTrue(any("email address" in e for e in errors), errors)


class RealMapTests(unittest.TestCase):
    """The starter map must build cleanly and stay inside every limit."""

    @classmethod
    def setUpClass(cls):
        cls.errors, cls.out, cls.rules = build.run(build.ROOT, write=False)

    def test_no_errors(self):
        self.assertEqual(self.errors, [])

    def test_core_file_limits(self):
        for name in ("exports/CLAUDE.md", "exports/AGENTS.md"):
            lines = self.out[name].rstrip("\n").split("\n")
            self.assertLessEqual(len(lines), build.MAX_CORE_LINES)
            headings = [l for l in lines if l.startswith("## ") and l != "## More, by task"]
            self.assertTrue(5 <= len(headings) <= 7, headings)
        self.assertNotIn("claude", self.out["exports/AGENTS.md"].lower())

    def test_core_size(self):
        core = [p for p in build.load_profiles(build.ROOT)[0] if p["name"] == "core"][0]
        self.assertGreaterEqual(len(core["include_ids"]), 10)

    def test_chatgpt_blocks_fit(self):
        about, respond = build.chat_blocks(self.rules, build.load_profiles(build.ROOT)[0])
        self.assertTrue(0 < len(about) <= build.MAX_CHAT_BLOCK)
        self.assertTrue(0 < len(respond) <= build.MAX_CHAT_BLOCK)

    def test_local_prompt_fits(self):
        self.assertLessEqual(len(self.out["exports/system-prompt-local.md"].split()), build.MAX_LOCAL_WORDS)

    def test_every_non_core_profile_has_an_export(self):
        for p in build.load_profiles(build.ROOT)[0]:
            if p["name"] != "core":
                self.assertIn("exports/profile-%s.md" % p["name"], self.out)


class InstallTest(unittest.TestCase):
    def test_install_fills_in_the_folder_and_keeps_foreign_files(self):
        import tempfile
        errors, outputs, _ = build.run(build.ROOT, write=False)
        self.assertEqual(errors, [])
        core = outputs["exports/CLAUDE.md"]
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "sub", "CLAUDE.md")
            self.assertTrue(build.install(core, target).startswith("installed"))
            text = build.read_text(target)
            self.assertNotIn("<BEHAVIOR_MAP_DIR>", text)
            self.assertIn("/exports/profile-coding.md", text)
            self.assertTrue(build.install(core, target).startswith("installed"))   # our own file is replaced
            self.assertEqual(build.read_text(target), text)
            self.assertFalse(os.path.isfile(target + build.BACKUP_SUFFIX))
            self.assertEqual(build.uninstall(target), "removed: %s" % build.folder_text(target))
            self.assertFalse(os.path.isfile(target))

    def test_install_keeps_the_text_a_person_wrote_themselves(self):
        _errors, outputs, _ = build.run(build.ROOT, write=False)
        core = outputs["exports/CLAUDE.md"]
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "CLAUDE.md")
            build.write_text(target, "# My notes\nUse tabs.\n")
            self.assertIn("your own text is kept", build.install(core, target))
            text = build.read_text(target)
            self.assertTrue(text.startswith("# My notes\nUse tabs.\n\n" + build.BLOCK_START))
            self.assertTrue(text.endswith(build.BLOCK_END + "\n"))
            self.assertEqual(build.read_text(target + build.BACKUP_SUFFIX), "# My notes\nUse tabs.\n")
            self.assertTrue(build.installed_from(build.ROOT, target))
            # the person writes below the rules, the rules change: only the marked part is rewritten
            build.write_text(target, text + "\nAnd no semicolons.\n")
            build.write_text(target + build.BACKUP_SUFFIX, "the first copy is never replaced")
            build.install(core.replace("# How to work with the owner", "# How to work with me"), target)
            text = build.read_text(target)
            self.assertIn("# How to work with me", text)
            self.assertNotIn("# How to work with the owner", text)
            self.assertEqual(text.count(build.BLOCK_START), 1)
            self.assertTrue(text.startswith("# My notes\nUse tabs.\n"))
            self.assertTrue(text.endswith("\nAnd no semicolons.\n"))
            self.assertEqual(build.read_text(target + build.BACKUP_SUFFIX), "the first copy is never replaced")
            self.assertIn("your own text is still there", build.uninstall(target))
            self.assertEqual(build.read_text(target), "# My notes\nUse tabs.\n\nAnd no semicolons.\n")
            self.assertTrue(build.uninstall(target).startswith("nothing installed"))

    def test_a_file_from_before_the_marks_is_taken_over_whole(self):
        _errors, outputs, _ = build.run(build.ROOT, write=False)
        core = outputs["exports/CLAUDE.md"]
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "CLAUDE.md")
            build.write_text(target, build.installed_text(core))
            build.install(core, target)
            self.assertTrue(build.read_text(target).startswith(build.BLOCK_START))
            self.assertFalse(os.path.isfile(target + build.BACKUP_SUFFIX))

    def test_every_tool_gets_its_own_file_and_stays_current(self):
        _errors, outputs, _ = build.run(build.ROOT, write=False)
        with tempfile.TemporaryDirectory() as d:
            targets = {tool: (label, rel, os.path.join(d, tool, os.path.basename(target)))
                       for tool, (label, rel, target) in build.INSTALL_TARGETS.items()}
            self.assertEqual(build.refresh_installed(outputs, build.ROOT, targets=targets), [])      # nothing asked, nothing written
            notes = build.refresh_installed(outputs, build.ROOT, also=["codex"], targets=targets)
            self.assertEqual(len(notes), 1)
            agents = build.read_text(targets["codex"][2])
            self.assertNotIn("claude", agents.replace(build.folder_text(build.ROOT), "").lower())
            self.assertFalse(os.path.isfile(targets["claude"][2]))
            changed = dict(outputs, **{"exports/AGENTS.md": outputs["exports/AGENTS.md"].replace("the owner", "the boss")})
            build.refresh_installed(changed, build.ROOT, targets=targets)                     # a later build keeps it current
            self.assertIn("the boss", build.read_text(targets["codex"][2]))
            self.assertFalse(os.path.isfile(targets["opencode"][2]))

    def test_the_tools_named_on_the_command_line(self):
        self.assertEqual(build.tools_named(["--check"], "--install"), [])
        self.assertEqual(build.tools_named(["--install"], "--install"), ["claude"])
        self.assertEqual(build.tools_named(["--install", "opencode"], "--install"), ["opencode"])
        self.assertEqual(build.tools_named(["--uninstall", "all"], "--uninstall"), list(build.INSTALL_TARGETS))
        with self.assertRaises(build.BuildError):
            build.tools_named(["--install", "word"], "--install")

    def test_only_the_folder_it_was_built_from_counts_as_installed(self):
        errors, outputs, _ = build.run(build.ROOT, write=False)
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "CLAUDE.md")
            self.assertFalse(build.installed_from(build.ROOT, target))
            build.install(outputs["exports/CLAUDE.md"], target)
            self.assertTrue(build.installed_from(build.ROOT, target))
            self.assertFalse(build.installed_from(d, target))            # a copy of the map elsewhere

    def test_init_starts_a_map_once_and_it_builds(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "map")
            self.assertIn("started your map", build.init(root))
            self.assertTrue(os.path.isfile(os.path.join(root, "exports", "CLAUDE.md")))
            rule = build.load_rules(root)[0][0]
            self.assertEqual(rule["last_seen"], build.datetime.date.today().isoformat())       # nothing starts out stale
            self.assertEqual(build.run(root, write=False)[0], [])
            build.write_text(os.path.join(root, "rules", "git", "mine.md"), "kept")
            self.assertIn("already a map", build.init(root))
            self.assertEqual(build.read_text(os.path.join(root, "rules", "git", "mine.md")), "kept")

    def test_settings_file_is_optional(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(build.settings(d), {})
            build.write_text(os.path.join(d, "settings.json"), '{"own_skills": "D:/skills"}')
            self.assertEqual(build.settings(d), {"own_skills": "D:/skills"})
            build.write_text(os.path.join(d, "settings.json"), "not json")
            self.assertEqual(build.settings(d), {})

    def test_profile_files_survive_a_read_and_write(self):
        for p in build.load_profiles(build.ROOT)[0]:
            self.assertEqual(build.render_profile(p), build.read_text(build.full_path(build.ROOT, "profiles/%s.yaml" % p["name"])))

    def test_local_prompt_holds_the_pinned_rule(self):
        errors, outputs, _ = build.run(build.ROOT, write=False)
        self.assertIn("needed of the owner", outputs["exports/system-prompt-local.md"])


if __name__ == "__main__":
    unittest.main()
