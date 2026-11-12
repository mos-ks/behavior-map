#!/usr/bin/env python3
# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
"""The skill library: which skills exist on this computer, where each is active, and how often each was used.

    python skills.py                    # print the library
    python skills.py install <name>     # copy one of your own skills into ~/.claude/skills (active in every project)
    python skills.py remove <name>      # take that copy out again

Where skills are looked for:
  ~/.claude/skills/<name>                  active in every project
  ~/.claude/skills/synced/*/<name>         from the Claude app, active in every project (called anthropic-skills:<name>)
  plugins that are installed and switched on    active in every project (called <plugin>:<name>)
  <project>/.claude/skills/<name>          active in that project only; projects are the folders your chats worked
                                           in, plus what sits directly in ~/Documents, ~/Projects, ~/code and the like
  the folder named as "own_skills" in the map's settings.json: skills you write; active nowhere until installed
  <map>/skills/<name>                      skills suggested by bootstrap.py from tasks you asked for again and again;
                                           active nowhere until installed

settings.json in the map folder is optional: {"project_folders": ["D:/work"], "own_skills": "D:/my-skills"}.

Use is counted from the Claude Code transcripts: every time an assistant loaded a skill, with the project it was
working in. Test runs started by a script (`claude -p`) do not count. A skill that was used but is not on disk
ships with Claude Code or was removed since.

Install and remove touch only what this script put there: an installed copy carries a marker file, and a folder
without the marker is never replaced or deleted. The copy leaves out the tests and their answer keys.

Python 3.9+, standard library only.
"""
import glob
import json
import os
import re
import shutil
import sys

import build
import learn

HOME = os.path.expanduser("~")
SKILL_CALL = (b'"name":"Skill"', b'"name": "Skill"')
MARKER = ".installed-by-behaviour-map"       # in an installed copy: this script put the folder there and may remove it
LEFT_OUT = shutil.ignore_patterns(".git", "evals", "tests", "__pycache__", "*.pyc", "node_modules")
RULES_PER_SKILL, RULE_FLOOR = 3, 0.12      # a skill lists the rules closest to its description above this likeness


def describe(path):
    """(name, description) from the header of a SKILL.md; the folder name when the header has none."""
    name, lines = os.path.basename(os.path.dirname(path)), []
    try:
        m = re.match(r"^---\r?\n(.*?)\r?\n---", build.read_text(path), re.S)
    except (OSError, UnicodeDecodeError):
        m = None
    if not m:
        return name, ""
    collecting = False
    for line in m.group(1).split("\n"):
        key = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if key:
            collecting = key.group(1) == "description"
            value = key.group(2).strip()
            if key.group(1) == "name" and value:
                name = value.strip("\"'")
            elif collecting and value not in ("", ">", "|", ">-", "|-"):
                lines.append(value.strip("\"'"))
        elif collecting and line.strip():
            lines.append(line.strip())
    return name, learn.one_line(" ".join(lines))


def skill_files(folder):
    return sorted(glob.glob(os.path.join(folder, "*", "SKILL.md")))


def entry(key, path, source, where, projects=()):
    name, description = describe(path)
    return {"key": key or name, "name": name, "description": description, "source": source, "where": where,
            "projects": list(projects), "path": build.folder_text(os.path.dirname(path)), "switch": []}


def enabled_plugins(home):
    """{plugin name: install folder} for plugins that are installed and switched on."""
    try:
        installed = json.loads(build.read_text(os.path.join(home, ".claude", "plugins", "installed_plugins.json")))["plugins"]
        on = json.loads(build.read_text(os.path.join(home, ".claude", "settings.json"))).get("enabledPlugins", {})
    except (OSError, ValueError, KeyError):
        return {}
    return {key.split("@")[0]: copies[0]["installPath"] for key, copies in installed.items() if on.get(key) and copies}


PARENTS = ("Documents", "Projects", "projects", "code", "src", "dev", "repos", "git", "work", "workspace")


def places(root=build.ROOT, home=HOME):
    """Where to look: {"home", "project_folders" (parents of projects), "projects" (single project folders), "own_skills"}."""
    cfg = build.settings(root)
    seen = learn.signal_path(root, "skills.json")
    try:
        worked_in = json.loads(build.read_text(seen)).get("folders", []) if os.path.isfile(seen) else []
    except ValueError:
        worked_in = []
    parents = cfg.get("project_folders") or [os.path.join(home, name) for name in PARENTS]
    return {"home": home, "project_folders": [p for p in parents if os.path.isdir(p)],
            "projects": [p for p in worked_in if os.path.isdir(p)], "own_skills": cfg.get("own_skills") or "",
            "suggested": os.path.join(root, "skills")}


def project_skill_files(where):
    """(project name, SKILL.md path) for every project skill, each file once."""
    out, done = [], set()
    folders = [p for parent in where["project_folders"] for p in sorted(glob.glob(os.path.join(parent, "*")))] + list(where["projects"])
    for folder in folders:
        for path in skill_files(os.path.join(folder, ".claude", "skills")):
            real = os.path.normcase(os.path.abspath(path))
            if real not in done and not real.startswith(os.path.normcase(os.path.join(where["home"], ".claude")) + os.sep):
                done.add(real)
                out.append((os.path.basename(os.path.normpath(folder)), path))
    return out


def inventory(where):
    """Every skill on disk, one entry per skill. A skill found in several projects lists them all."""
    home = where["home"]
    found = {}
    for path in skill_files(os.path.join(home, ".claude", "skills")):
        e = entry(None, path, "your skills folder", "everywhere")
        found[e["key"]] = e
    for path in sorted(glob.glob(os.path.join(home, ".claude", "skills", "synced", "*", "*", "SKILL.md"))):
        e = entry("anthropic-skills:" + describe(path)[0], path, "Claude app", "everywhere")
        found.setdefault(e["key"], e)
    for plugin, folder in sorted(enabled_plugins(home).items()):
        for path in skill_files(os.path.join(folder, "skills")) + skill_files(os.path.join(folder, ".claude", "skills")):
            e = entry("%s:%s" % (plugin, describe(path)[0]), path, "plugin " + plugin, "everywhere")
            found.setdefault(e["key"], e)
    for project, path in project_skill_files(where):
        e = entry(None, path, "project", "projects", [project])
        if e["key"] in found and found[e["key"]]["where"] == "projects":
            if project not in found[e["key"]]["projects"]:
                found[e["key"]]["projects"].append(project)
        else:
            found.setdefault(e["key"], e)
    yours = [(path, "written by you") for path in (skill_files(where["own_skills"]) if where["own_skills"] else [])]
    yours += [(path, "suggested from your chats") for path in (skill_files(where["suggested"]) if where.get("suggested") else [])]
    for path, source in yours:
        e = entry(None, path, source, "nowhere")
        mine = os.path.dirname(path)
        if e["key"] not in found:
            e.update(library=mine, switch=["install"])
            found[e["key"]] = e
            continue
        have = found[e["key"]]                                   # installed already: keep where it is active
        if have.get("library"):
            continue                                             # the same name in both folders: the first one counts
        have.update(source=source, library=mine)
        if have["where"] == "everywhere" and os.path.isfile(os.path.join(have["path"], MARKER)):
            same = build.read_text(path) == build.read_text(os.path.join(have["path"], "SKILL.md"))
            have["switch"] = ["remove"] if same else ["remove", "update"]
    for e in found.values():
        e["projects"].sort()
    return sorted(found.values(), key=lambda e: e["key"].lower())


def switch(key, action, where=None):
    """action: install, update or remove one of the owner's own skills in ~/.claude/skills. Raises BuildError when
    the skill does not offer that action (not the owner's, already there, or a folder this script did not write)."""
    where = where or places()
    home = where["home"]
    skill = next((s for s in inventory(where) if s["key"] == key), None)
    if skill is None or action not in skill["switch"]:
        raise build.BuildError("`%s` cannot be %sd from here" % (key, action.rstrip("e")))
    target = os.path.join(home, ".claude", "skills", os.path.basename(skill["library"]))
    if action in ("remove", "update"):
        shutil.rmtree(target)                                  # holds the marker: inventory offered the action only then
    elif os.path.exists(target):
        raise build.BuildError("a folder named %s is already in your skills folder" % os.path.basename(target))
    if action in ("install", "update"):
        shutil.copytree(skill["library"], target, ignore=LEFT_OUT)
        build.write_text(os.path.join(target, MARKER), "Copied from %s by skills.py. Remove it there, not by hand.\n" % build.folder_text(skill["library"]))


def count_use(root=build.ROOT, projects_dir=None):
    """Counts new skill loads since the last call. Returns {skill: {"count", "last", "projects": {name: n}}}."""
    projects_dir = projects_dir or learn.sessions_dir()
    path = learn.signal_path(root, "skills.json")
    state = json.loads(build.read_text(path)) if os.path.isfile(path) else {"files": {}, "use": {}}
    folders = set(state.get("folders", []))
    changed = False
    for file in sorted(glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))):
        key = os.path.relpath(file, projects_dir).replace(os.sep, "/")
        start, size = state["files"].get(key, 0), os.path.getsize(file)
        if start == size:
            continue
        rows, state["files"][key] = learn.lines_from(file, 0 if start > size else start, SKILL_CALL)
        changed = True
        for d in rows:
            if d.get("entrypoint") == "sdk-cli" or d.get("isSidechain"):
                continue
            if d.get("cwd"):
                folders.add(build.folder_text(str(d["cwd"])))        # a project folder: its own skills are looked for there
            for block in (d.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                    skill = str((block.get("input") or {}).get("skill") or "").strip()
                    if not skill:
                        continue
                    use = state["use"].setdefault(skill, {"count": 0, "last": "", "projects": {}})
                    project = learn.project_of(d)
                    use["count"] += 1
                    use["last"] = max(use["last"], str(d.get("timestamp") or "")[:10])
                    use["projects"][project] = use["projects"].get(project, 0) + 1
    if changed:
        state["folders"] = sorted(folders)
        learn.write_whole(path, json.dumps(state, ensure_ascii=False, indent=1))
    return state["use"]


def library(root=build.ROOT, where=None, projects_dir=None, rules=None):
    """The inventory with use and the closest rules added; skills that were used but are not on disk come last."""
    use = count_use(root, projects_dir)                 # first: it also notes the project folders the chats worked in
    skills = inventory(where or places(root))
    rules = rules if rules is not None else build.load_rules(root)[0]
    lk = learn.rule_likeness(rules) if rules else None
    known = {s["key"] for s in skills}
    for key in sorted(set(use) - known):
        skills.append({"key": key, "name": key, "description": "", "source": "built in, or removed since", "where": "everywhere",
                       "projects": [], "path": "", "switch": []})
    for s in skills:
        u = use.get(s["key"], {"count": 0, "last": "", "projects": {}})
        s["used"], s["last_used"], s["used_in"] = u["count"], u["last"], u["projects"]
        text = s["name"].replace("-", " ") + " " + s["description"]
        s["rules"] = [rules[i]["id"] for i, score in lk.best(text) if score >= RULE_FLOOR][:RULES_PER_SKILL] if lk else []
    return skills


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    if len(argv) == 2 and argv[0] in ("install", "update", "remove"):
        try:
            switch(argv[1], argv[0])
        except build.BuildError as e:
            print("problem: %s" % e)
            return 1
        print("%s: done. It counts from the next session." % argv[1])
        return 0
    for s in library():
        where = {"everywhere": "every project", "nowhere": "not installed"}.get(s["where"]) or ", ".join(s["projects"][:3]) + (
            " and %d more" % (len(s["projects"]) - 3) if len(s["projects"]) > 3 else "")
        print("%-44s %-22s %-28s used %dx%s" % (s["key"][:44], s["source"][:22], where[:28], s["used"],
                                                 ", last " + s["last_used"] if s["last_used"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
