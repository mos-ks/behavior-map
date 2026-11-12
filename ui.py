#!/usr/bin/env python3
# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
"""The page for looking at and changing the behaviour map. Runs on this computer only.

    python ui.py                 # start it and open the page in the browser
    python ui.py --background    # the same without a console window: it stops by itself when the page's tab is closed
    python ui.py --no-open       # start it without opening a window
    python ui.py --port 8765     # another port (default 8765; the next free one is taken if it is busy)
    python ui.py --root <folder> # work on a copy of the map (used by the tests)

Every change on the page is checked first, exactly as `python build.py` would check it, and written only when
it builds; otherwise the page says why and nothing on disk changes. Nothing leaves this computer: the server
listens on 127.0.0.1 and takes changes only from its own page.

When one of the page's own Python files changes while it runs, the server answers the next request with
"restarting", closes, and starts itself again; the page reloads itself. Nobody has to close and reopen anything
after an update.

Started with --background (what brain.cmd and brain.sh do), it runs without a window and stops when the page's
last tab is closed, or when no tab has been seen for IDLE_MINUTES. A build from the chats is finished first.

Python 3.9+, standard library only.
"""
import datetime
import json
import os
import re
import secrets
import subprocess
import sys
import time
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bootstrap
import build
import judge
import learn
import skills
import sync

LOCK = threading.Lock()
SOURCES = [os.path.join(build.CODE, name) for name in ("ui.py", "build.py", "learn.py", "skills.py", "sync.py", "judge.py", "bootstrap.py")]
IDLE_MINUTES = 15                        # --background: a tab not heard from for this long counts as gone
BYE_SECONDS = 20                         # --background: after the last tab is gone, stop unless one comes back in this time
STARTED_WITH = [os.path.getmtime(path) for path in SOURCES]


WAIT_PAGE = """<!doctype html><meta charset="utf-8"><title>Behaviour map</title>
<body style="font:15px system-ui;background:#191715;color:#ddd;padding:24px">Updating the page.
<script>setInterval(async () => { try { if ((await fetch("/", { cache: "no-store" })).ok) location.reload(); } catch (e) {} }, 700);</script>"""


def code_changed():
    """True when a Python file of the page was changed after this server started."""
    return [os.path.getmtime(path) for path in SOURCES] != STARTED_WITH
EDITABLE = ("title", "rule", "why", "boundary", "short", "strength", "scope", "enabled", "local")
RELATED = {"key": None, "pairs": []}     # the graph's links, recomputed only when a rule's words change
JUDGE = {}                               # root -> what the local model did at the last Update
UPDATED = {}                             # root -> when the chats were last read (HH:MM), shown beside the Update button
LIBRARY = {}                             # root -> the skill library, read when the page starts and on "Read new chats"
BUILDS = {}                              # root -> the build from the chats: state, step, progress, summary or error
SEEN = {"last": time.time(), "tabs": {}, "had_tab": False}   # the last request, and each open tab's last check-in


class Refused(Exception):
    """A change the page asked for that cannot be made. `problems` is what to show."""

    def __init__(self, problems):
        Exception.__init__(self, "; ".join(problems))
        self.problems = list(problems)


# ----------------------------------------------------------------------------- reading

def snapshot(root=build.ROOT):
    """The map as it is on disk, checked and built in memory: (rules, profiles, errors, outputs)."""
    rules, profiles, errors = build.load_all(root)
    outputs = {}
    if not errors:
        errors, outputs = build.compile_map(rules, profiles, root)
    return rules, profiles, errors, outputs


def related(rules):
    key = hash(tuple((r["id"], learn.rule_words(r)) for r in rules))
    if RELATED["key"] != key:
        RELATED["key"], RELATED["pairs"] = key, learn.related(rules)
    return RELATED["pairs"]


def library(root, rules, refresh=False):
    if refresh or root not in LIBRARY:
        LIBRARY[root] = skills.library(root, rules=rules) if root == build.ROOT else []     # a test copy has no skills of its own
    return LIBRARY[root]


def exports_info(root, profiles, rules):
    """Each generated file: what it is, where it is written, and whether anything reads it."""
    folder = build.folder_text(root) + "/exports/"
    tools = {}                                   # generated file -> the tools that can read it in every project
    for tool, (label, rel, target) in build.INSTALL_TARGETS.items():
        tools.setdefault(rel.split("/")[-1], []).append(
            {"tool": tool, "label": label, "target": build.folder_text(target), "live": build.installed_from(root, target)})
    live = any(t["live"] for group in tools.values() for t in group)

    def read_by(name):
        on = [t["label"] for t in tools[name] if t["live"]]
        return ("%s: the installed copy is read at the start of every session, in every project." % " and ".join(on) if on
                else "Not installed. Install it below, and the tool reads it in every project.")
    rows = [
        ("CLAUDE.md", "Claude Code", "The always-on core", read_by("CLAUDE.md")),
        ("AGENTS.md", "Codex and OpenCode", "The same core, without Claude's name", read_by("AGENTS.md")),
        ("chatgpt-custom-instructions.txt", "ChatGPT", "Two blocks for Custom instructions",
         "Nothing reads it from here. Copy each block into ChatGPT under Settings, Personalization, Custom instructions; copy again after a change."),
        ("system-prompt-local.md", "Local model", "The core in %d words" % build.MAX_LOCAL_WORDS,
         "Nothing reads it from here. Copy the text into the system prompt of a local model."),
    ]
    for p in profiles:
        if p["name"] != "core":
            rows.append(("profile-%s.md" % p["name"], "Task file: " + p["name"], p["description"],
                         "The core file names it. The assistant opens it from this folder before work of this kind." if live
                         else "Nothing reads it yet: the core file that names it is not installed."))
    for name in build.project_names(rules):
        rows.append(("project-%s.md" % name, "Project: " + name, "Rules that hold in this project only",
                     "Nothing reads it until that project's own instruction file (its CLAUDE.md or AGENTS.md) says to read this file."))
    return [{"name": name, "label": label, "about": about, "used": text, "file": folder + name, "tools": tools.get(name, []),
             "live": any(t["live"] for t in tools[name]) if name in tools else live and name.startswith("profile-")}
            for name, label, about, text in rows]


def state(root=build.ROOT, snap=None):
    """Everything the page shows. Reads only."""
    rules, profiles, errors, outputs = snap or snapshot(root)
    membership = {}
    for p in profiles:
        try:
            membership[p["name"]] = [r["id"] for r in build.select(p, rules)]
        except build.BuildError:
            membership[p["name"]] = []
    signals = learn.load_signals(root)
    core = outputs.get("exports/CLAUDE.md", "")
    return {
        "rules": rules,
        "profiles": [{"name": p["name"], "description": p["description"], "ids": membership[p["name"]]} for p in profiles],
        "categories": build.CATEGORIES, "scopes": build.SCOPES,
        "related": related(rules),
        "signals": [s for s in signals if s["status"] in ("open", "attached")],
        "summary": learn.summarise(signals, rules, learn.load_state(root)["since"]),
        "limits": {"core_lines": len(core.rstrip("\n").split("\n")) if core else 0, "max_core_lines": build.MAX_CORE_LINES,
                   "max_short_words": build.MAX_SHORT_WORDS, "max_local_words": build.MAX_LOCAL_WORDS,
                   "inbox_days": learn.INBOX_DAYS, "group_min": learn.GROUP_MIN, "group_min_days": learn.GROUP_MIN_DAYS},
        "skills": library(root, rules),
        "exports": exports_info(root, profiles, rules),
        "updated": UPDATED.get(root, ""),
        "judge": JUDGE.get(root, {"on": False, "model": "", "judged": 0, "note": ""}),
        "installed": build.installed_from(root),
        "folder": build.folder_text(root),
        "build": build_status(root),
        "problems": errors,
    }


# ----------------------------------------------------------------------------- writing

def publish(outputs, root):
    """Writes the built files, and every installed copy that comes from this folder."""
    build.write_outputs(outputs, root)
    build.refresh_installed(outputs, root)


def rebuilt(root):
    """Builds from disk after the rule files changed. Returns the snapshot for state()."""
    snap = snapshot(root)
    if not snap[2]:
        publish(snap[3], root)
    return snap


def checked(rules, profiles, root, path):
    """Builds the changed map in memory. Raises Refused, naming the problems of the rule at `path` without its path."""
    errors, outputs = build.compile_map(rules, profiles, root)
    if errors:
        own = path + ": "
        raise Refused([e[len(own):] if e.startswith(own) else e for e in errors])
    return outputs


def save_rule(rule_id, changes, root=build.ROOT):
    rules, profiles, errors = build.load_all(root)
    at = next((i for i, r in enumerate(rules) if r["id"] == rule_id), None)
    if at is None:
        raise Refused(errors or ["no rule with the id `%s`" % rule_id])
    rule = rules[at]
    for key in EDITABLE:
        if key in changes:
            rule[key] = bool(changes[key]) if key in ("enabled", "local") else (learn.one_line(changes[key]) or None)
    if rule.get("local") is not True:
        rule.pop("local", None)
    if not rule["title"]:
        raise Refused(["the title cannot be empty"])
    rules[at] = build.parse_rule(build.render_rule(rule), rule["path"])      # what the file will hold
    core = next(p for p in profiles if p["name"] == "core")
    if "core" in changes:
        core["include_ids"] = [i for i in core["include_ids"] if i != rule_id] + ([rule_id] if changes["core"] else [])
    outputs = checked(rules, profiles, root, rule["path"])
    learn.write_rule(rules[at], root)
    if "core" in changes:
        build.write_text(build.full_path(root, "profiles/core.yaml"), build.render_profile(core))
    publish(outputs, root)
    return rules, profiles, [], outputs


def slug(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")[:48].rstrip("-")


def new_rule(fields, root=build.ROOT):
    """Returns (id of the new rule, snapshot)."""
    category, title, text = fields.get("category"), learn.one_line(fields.get("title")), learn.one_line(fields.get("rule"))
    if category not in build.CATEGORIES:
        raise Refused(["pick a category"])
    if not text or not slug(title):
        raise Refused(["a new rule needs a title and the rule itself"])
    rules, profiles, _errors = build.load_all(root)
    taken = {r["id"] for r in rules}
    base = rid = "%s-%s" % (category, slug(title))
    n = 2
    while rid in taken:
        rid, n = "%s-%d" % (base, n), n + 1
    sigs = learn.load_signals(root)
    mine = [s for s in sigs if s["id"] in set(fields.get("signals") or []) and s["status"] == "open"]
    path = "rules/%s/%s.md" % (category, rid[len(category) + 1:])
    rule = {
        "id": rid, "title": title, "category": category,
        "scope": fields.get("scope") if fields.get("scope") in build.SCOPES else "always",
        "strength": "soft" if fields.get("strength") == "soft" else "hard", "enabled": True,
        "times_seen": max(1, len(mine)), "sources": ["owner"],
        "last_seen": max([s["date"] for s in mine] or [learn.today()]), "needs_owner": False,
        "rule": text, "why": learn.one_line(fields.get("why")) or None,
        "quotes": [learn.quote_of(s) for s in mine[:learn.MAX_QUOTES]], "path": path,
    }
    rules.append(build.parse_rule(build.render_rule(rule), path))
    outputs = checked(rules, profiles, root, path)
    learn.write_rule(rules[-1], root)
    for s in mine:
        s["rule"], s["status"], s["counted"], s["founding"] = rid, "attached", True, True
    if mine:
        learn.save_signals(sigs, root)
    publish(outputs, root)
    return rid, (rules, profiles, [], outputs)


def decide_signal(sig_id, action, rule=None, root=build.ROOT):
    try:
        changed = learn.decide(sig_id, action, rule, root)
    except build.BuildError as e:
        raise Refused([str(e)])
    _summary, counted = learn.review(root)
    return rebuilt(root) if changed or counted else None


def switch_skill(key, action, root=build.ROOT):
    """Installs, updates or removes one of the owner's own skills, then reads the library again."""
    if root != build.ROOT:
        raise Refused(["a copy of the map does not change your skills folder"])
    try:
        skills.switch(key, str(action), skills.places(root))
    except build.BuildError as e:
        raise Refused([str(e)])
    library(root, build.load_rules(root)[0], refresh=True)


def install_tool(tool, action, root=build.ROOT, targets=None):
    """Installs the core for one tool, or takes it out again. Returns what happened, in a sentence."""
    if targets is None and root != build.ROOT:
        raise Refused(["a copy of the map does not change your tools' files"])
    targets = targets or build.INSTALL_TARGETS
    if tool not in targets or action not in ("install", "remove"):
        raise Refused(["unknown tool or action"])
    _label, rel, target = targets[tool]
    if action == "remove":
        return build.uninstall(target)
    _rules, _profiles, errors, outputs = snapshot(root)
    if errors:
        raise Refused(errors)
    return build.install(outputs[rel], target, root)


def build_status(root):
    """What the page shows about the build from the chats: the running job, the last result, the engines."""
    job = dict(BUILDS.get(root) or {"state": "idle"})
    job.pop("stop", None)
    rules = build.load_rules(root)[0]
    return {"job": job, "last": bootstrap.last_summary(root), "engines": bootstrap.engines(root) if root == build.ROOT else [],
            "starter_only": bool(rules) and all(r.get("sources") == ["starter"] for r in rules)}


def run_build(root, ask):
    """The build behind the page: reads and asks without holding the lock, writes with it."""
    job = BUILDS[root]

    def progress(step, done, total):
        job.update(step=step, done=done, total=total)
    try:
        found = bootstrap.prepare(root, ask, progress=progress, stopped=lambda: job["stop"])
        with LOCK:
            job["summary"] = bootstrap.apply(found, root)
            rebuilt(root)
            library(root, build.load_rules(root)[0], refresh=True)
        job["state"] = "done"
    except bootstrap.Stopped:
        job["state"] = "stopped"
    except Exception as e:                       # shown on the page; the map is left as it was
        job.update(state="failed", error=str(e) or e.__class__.__name__)


def start_build(engine, root=build.ROOT, ask=None):
    """Starts the build from the chats in the background. `ask` replaces the engine (tests)."""
    if (BUILDS.get(root) or {}).get("state") == "running":
        raise Refused(["a build is already running"])
    if ask is None:
        if root != build.ROOT:
            raise Refused(["a copy of the map is not built from your chats"])
        try:
            ask = bootstrap.asker(engine, root)
        except bootstrap.Failed as e:
            raise Refused([str(e)])
    BUILDS[root] = {"state": "running", "step": "Starting", "done": 0, "total": 0, "engine": engine, "stop": False}
    worker = threading.Thread(target=run_build, args=(root, ask), daemon=True)
    worker.start()
    return worker


def stop_build(root=build.ROOT):
    if (BUILDS.get(root) or {}).get("state") == "running":
        BUILDS[root]["stop"] = True


def save_engine(fields, root=build.ROOT, call=judge.call):
    """Saves a model address for the build and the Inbox (signals/judge.json, never committed). Checks it answers."""
    cfg = {"base_url": str(fields.get("base_url") or "").strip().rstrip("/"), "model": str(fields.get("model") or "").strip(),
           "api_key": str(fields.get("api_key") or "").strip()}
    if not re.match(r"^https?://\S+$", cfg["base_url"]) or not cfg["model"]:
        raise Refused(["enter the address (it starts with http) and the model name"])
    if not cfg["api_key"]:
        old = judge.settings(root) or {}
        cfg["api_key"] = old.get("api_key", "") if old.get("base_url") == cfg["base_url"] else ""   # an empty field keeps the saved key
    try:
        models = [m.get("id") for m in call(cfg, "/models", timeout=8).get("data") or []]
    except (OSError, ValueError) as e:
        raise Refused(["that address did not answer: %s" % learn.one_line(str(e))[:200]])
    if cfg["model"] not in models:
        raise Refused(["the address answers, but has no model named %s. It has: %s" % (cfg["model"], ", ".join(m for m in models if m)[:300])])
    learn.write_whole(learn.signal_path(root, "judge.json"), json.dumps({k: v for k, v in cfg.items() if v}))


def rescan(root=build.ROOT):
    """The Update button: reads the new chats (corrections and skill use), looks for skills again and rebuilds what
    changed; the server then saves the folder to GitHub (sync.py). Returns (how many corrections were added, snapshot or None)."""
    cfg = judge.settings(root) if root == build.ROOT else None       # a test copy never talks to the model
    added = learn.scan(root, wide=bool(cfg))
    JUDGE[root] = judge.sort_signals(root, cfg) if cfg else {"on": False, "model": "", "judged": 0, "note": "no local model is set up"}
    _summary, counted = learn.review(root)
    library(root, build.load_rules(root)[0], refresh=True)
    UPDATED[root] = datetime.datetime.now().strftime("%H:%M")
    return added, (rebuilt(root) if counted else None)


# ----------------------------------------------------------------------------- the server

class Handler(BaseHTTPRequestHandler):
    server_version = "BehaviourMap"
    token = ""
    root = build.ROOT

    def log_message(self, *_args):
        pass

    def send(self, code, body, kind="application/json; charset=utf-8"):
        data = (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        """Only the page on this computer may talk to the server (stops other sites reaching it through the browser)."""
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def restarting(self):
        """Hands over to a fresh start when the code changed under the running server."""
        if not code_changed():
            return False
        if self.command == "GET" and self.path.split("?")[0].split("#")[0] in ("/", "/index.html"):
            self.send(503, WAIT_PAGE, "text/html; charset=utf-8")       # a page load: show a page that comes back by itself
        else:
            self.send(503, {"restart": True})
        self.server.restart = True
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        return True

    def do_GET(self):
        if self.restarting():
            return
        if not self.host_ok():
            return self.send(403, {"problems": ["this page only answers on this computer"]})
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            page = build.read_text(os.path.join(build.CODE, "ui", "index.html")).replace("__TOKEN__", self.token)
            return self.send(200, page, "text/html; charset=utf-8")
        if self.headers.get("X-Brain-Token") != self.token:
            return self.send(403, {"problems": ["reload the page"]})
        SEEN["last"] = time.time()                          # only the page's own requests count as a sign of life
        if path == "/api/state":
            with LOCK:
                return self.send(200, state(self.root))
        if path == "/api/build":
            return self.send(200, build_status(self.root))
        if path == "/api/alive":                            # each open tab checks in; see watch_until_closed
            tab = self.path.split("tab=", 1)[-1][:40] if "tab=" in self.path else ""
            if tab:
                SEEN["tabs"][tab], SEEN["had_tab"] = time.time(), True
            return self.send(200, {"ok": True})
        exports = os.path.join(self.root, "exports")
        name = path[len("/api/export/"):] if path.startswith("/api/export/") else None
        if name and os.path.isdir(exports) and name in os.listdir(exports):
            return self.send(200, {"text": build.read_text(os.path.join(exports, name))})
        return self.send(404, {"problems": ["not found"]})

    def do_POST(self):
        if self.path.split("?")[0] == "/api/bye":            # sent by a closing tab, which cannot set headers
            query = dict(part.split("=", 1) for part in self.path.split("?", 1)[-1].split("&") if "=" in part)
            if self.host_ok() and query.get("t") == self.token:
                SEEN["tabs"].pop(query.get("tab", ""), None)
                SEEN["last"] = time.time()
            return self.send(204, "")
        if self.restarting():
            return
        if not self.host_ok() or self.headers.get("X-Brain-Token") != self.token:
            return self.send(403, {"problems": ["reload the page"]})
        SEEN["last"] = time.time()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
        except ValueError:
            return self.send(400, {"problems": ["could not read the change"]})
        path, extra, snap = self.path.split("?")[0], {}, None
        try:
            with LOCK:
                if path.startswith("/api/rule/"):
                    snap = save_rule(path[len("/api/rule/"):], body, self.root)
                elif path == "/api/rule":
                    extra["id"], snap = new_rule(body, self.root)
                elif path.startswith("/api/signal/"):
                    snap = decide_signal(path[len("/api/signal/"):], body.get("action"), body.get("rule"), self.root)
                elif path == "/api/skill":
                    switch_skill(body.get("key"), body.get("action"), self.root)
                elif path == "/api/build":
                    start_build(body.get("engine"), self.root)
                elif path == "/api/build/stop":
                    stop_build(self.root)
                elif path == "/api/engine":
                    save_engine(body, self.root)
                elif path == "/api/install":
                    extra["note"] = install_tool(body.get("tool"), body.get("action"), self.root)
                elif path == "/api/scan":
                    extra["added"], snap = rescan(self.root)
                    extra["repo"] = sync.save(self.root)
                else:
                    return self.send(404, {"problems": ["not found"]})
                out = state(self.root, snap)
        except Refused as e:
            with LOCK:
                out = state(self.root)
            out["refused"] = e.problems
            return self.send(409, out)
        except OSError as e:
            return self.send(500, {"problems": ["could not write the file: %s" % e]})
        out.update(extra)
        return self.send(200, out)


def serve(port=8765, root=build.ROOT, token=None):
    """Starts the server on the first free port from `port` on. Returns (server, address)."""
    Handler.token = token or secrets.token_urlsafe(24)
    Handler.root = root
    last = None
    for p in range(port, port + 20):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), Handler), "http://127.0.0.1:%d/" % p
        except OSError as e:
            last = e
    raise last


def already_running(port):
    """The address of a page server that already answers on `port`, or None."""
    address = "http://127.0.0.1:%d/" % port
    try:
        with urllib.request.urlopen(address, timeout=2) as res:
            return address if b"<title>Behaviour map</title>" in res.read(4000) else None
    except (OSError, ValueError):
        return None


def read_new_chats(root):
    """Runs once behind the page: the page is up at once and its first load waits for this."""
    with LOCK:
        try:
            rescan(root)
        except (OSError, ValueError) as e:
            print("could not read the new chats: %s" % e, flush=True)


def watch_until_closed(server, root):
    """--background: stops the server once no tab is left, but never in the middle of a build."""
    while not getattr(server, "restart", False):
        time.sleep(5)
        if (BUILDS.get(root) or {}).get("state") == "running":
            continue
        if no_tab_left(time.time()):
            server.shutdown()
            return


def no_tab_left(now):
    """True when the page is no longer open anywhere: every tab said goodbye or has not checked in for
    IDLE_MINUTES, and nothing asked for BYE_SECONDS (a reload says goodbye and comes back within that)."""
    for tab, seen in list(SEEN["tabs"].items()):
        if now - seen > IDLE_MINUTES * 60:
            SEEN["tabs"].pop(tab, None)
    if SEEN["tabs"]:
        return False
    return now - SEEN["last"] > (BYE_SECONDS if SEEN["had_tab"] else IDLE_MINUTES * 60)


def again(argv, background):
    """Starts this page anew (after its code changed) with the same options and no new browser window."""
    args = [a for a in argv if a != "--no-open"] + ["--no-open"]
    if not background:
        return subprocess.call([sys.executable, os.path.abspath(__file__)] + args)
    detach(args + ["--background"])
    return 0


def detach(args):
    """Starts `ui.py args` on its own, without a window, and returns at once. Its output goes to _work/page.log."""
    exe = sys.executable
    quiet = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.name == "nt" and os.path.isfile(quiet):
        exe = quiet
    root = os.path.abspath(args[args.index("--root") + 1]) if "--root" in args else build.ROOT
    os.makedirs(os.path.join(root, "_work"), exist_ok=True)
    log = open(os.path.join(root, "_work", "page.log"), "ab")
    options = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "cwd": build.CODE, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000     # detached, own group, no window
    else:
        options["start_new_session"] = True
    subprocess.Popen([exe, os.path.abspath(__file__), "--served"] + [a for a in args if a != "--served"], **options)
    log.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    background = "--background" in argv
    if background and "--served" not in argv:            # the launcher: hand over to a process without a window
        running = already_running(int(argv[argv.index("--port") + 1]) if "--port" in argv else 8765)
        if running:
            if "--no-open" not in argv:
                webbrowser.open(running)
            return 0
        detach(argv)
        return 0
    argv = [a for a in argv if a != "--served"]
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8765
    root = os.path.abspath(argv[argv.index("--root") + 1]) if "--root" in argv else build.ROOT
    if not build.has_map(root):
        print(build.init(root), flush=True)             # first start: begin with the starter rules
    running = already_running(port)
    if running:                                    # one server is enough: show its page and leave
        print("Behaviour map is already running: %s" % running, flush=True)
        if "--no-open" not in argv:
            webbrowser.open(running)
        return 0
    server, address = serve(port, root)
    threading.Thread(target=read_new_chats, args=(root,), daemon=True).start()
    if background:
        threading.Thread(target=watch_until_closed, args=(server, root), daemon=True).start()
    print("Behaviour map: %s   (%s)" % (address, "stops when its tab is closed" if background else "Ctrl+C stops it"), flush=True)
    if "--no-open" not in argv:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    if getattr(server, "restart", False):
        return again(argv, background)
    return 0


if __name__ == "__main__":
    sys.exit(main())
