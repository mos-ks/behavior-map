#!/usr/bin/env python3
# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
"""Builds a first map from everything the owner typed in past chats. A model does the reading.

    python bootstrap.py              # build with the first engine that is ready
    python bootstrap.py engines      # say which engines are ready
    python bootstrap.py --engine claude | api

Two engines, nothing else to set up when one of them is already there:

  claude   Claude Code on this computer (`claude` on the PATH), under the login it already has. It is used as a
           plain model: no tools, no project files, one question per call.
  api      Any OpenAI-compatible address: a local model (vLLM, llama.cpp, Ollama) or a hosted one. The address,
           model and key come from OpenCode's settings or from map/signals/judge.json (the page writes that file).

What it does: reads every prompt (learn.all_prompts, private details blanked), asks the model batch by batch for
standing instructions and for tasks asked again and again, merges what says the same thing, and writes rule
files, the always-on core and a suggested skill per repeated task. Every rule keeps the owner's own sentences as
evidence; a rule the model cannot back with a prompt it was shown is dropped. Answers are kept in
map/_work/bootstrap-cache.json, so a run that stopped half way does not pay for the same batch twice.

Your prompts go to the engine you pick and nowhere else. Python 3.9+, standard library only.
"""
import collections
import concurrent.futures
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error

import build
import judge
import learn

BATCH_CHARS = 30000          # prompts per question to the model, by size
MERGE_AT_ONCE = 120          # candidates per merge question; more are merged in rounds
AT_ONCE = 3                  # questions asked side by side
TRIES = 2                    # a batch that fails this often is left out
CLAUDE_TIMEOUT = 600
MIN_SKILL_DAYS = 2           # a task becomes a suggested skill when it was asked on this many different days
CORE_NEW = 25                # how many of the new rules are offered to the always-on core, most said first
SCOPE_WORDS = build.SCOPES + ["project"]

EXTRACT = """You read messages that ONE person typed to an AI coding assistant over many sessions. Find two things.

1. STANDING INSTRUCTIONS: how this person wants an assistant to work in future sessions too. How to communicate,
write, code, use git, test, design, ask and decide. Signs: corrections ("no", "stop", "i said", "again", "why did
you"), the same instruction in several sessions, "always", "never", "from now on", approval of an unusual way of
working. NOT instructions: a request for one task ("fix the header"), facts about a project (ports, paths,
versions), praise without content, anything that only makes sense inside one feature. People type fast and with
typos; read through that. Do not invent anything.

2. REPEATED TASKS: a kind of multi-step job this person asks for again and again in the same way (for example
"release the app", "review the pull request", "write the weekly report"). Only jobs that could become a reusable
recipe. A single request is not one.

Each message has a number. For every finding give the numbers of the messages that show it; a finding without a
message number is thrown away.

Answer with JSON only:
{"rules": [{"rule": "one imperative sentence in plain English, no dashes", "why": "their reason, or empty",
            "category": "one of: %(categories)s",
            "scope": "always, or the kind of work it is about (%(scopes)s), or project when it is about one project's tools or conventions",
            "firm": true when they were firm or said it more than once,
            "prompts": [message numbers]}],
 "skills": [{"name": "short-kebab-name", "what": "what the job is and how they want it done, two sentences",
             "prompts": [message numbers]}]}
Empty lists are a fine answer.""" % {"categories": ", ".join(build.CATEGORIES), "scopes": ", ".join(build.SCOPES[1:])}

MERGE = """You tidy a list of working rules that were collected from one person's messages to an AI assistant.
All are in the category "%(category)s". Candidates are numbered. Some say the same thing in other words: put those
in one group. Do not group rules that only sound alike. When the person changed their mind, the later statement
is the rule: say what it replaced in "earlier". When two candidates pull apart and neither is clearly later or
firmer, keep the more cautious one as the rule and put the question for the person in "open_question".

Below the candidates is the list of rules already in the map, each with its id. When a group says the same thing
as one of them, or the opposite of it, give that id in "existing"; otherwise leave it empty.

Answer with JSON only:
{"rules": [{"from": [candidate numbers], "title": "at most six words", "rule": "one or two imperative sentences a model with no other context can follow, plain English, no dashes",
            "short": "the same in at most 18 words", "why": "their reason, or empty", "earlier": "what it replaced, or empty",
            "open_question": "or empty", "existing": "an id from the list, or empty"}]}
Every candidate number must appear in exactly one group."""

MERGE_SKILLS = """You tidy a list of repeated tasks that one person keeps asking an AI assistant to do. Candidates are
numbered. Put candidates that are the same kind of job into one group and describe the job once.

Answer with JSON only:
{"skills": [{"from": [candidate numbers], "name": "short-kebab-name", "description": "what it does and when to use it, written so an assistant knows when to load it, two sentences",
             "steps": ["the steps, one short imperative sentence each, as this person wants them done"]}]}
Every candidate number must appear in exactly one group."""


class Stopped(Exception):
    """The owner pressed Stop."""


class Failed(Exception):
    """The build could not be finished; the message says why in plain words."""


# ----------------------------------------------------------------------------- engines

def json_from(text):
    """The first JSON object in a model's answer."""
    m = re.search(r"\{.*\}", str(text or ""), re.S)
    if not m:
        raise ValueError("the model did not answer with JSON")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("the model did not answer with a JSON object")
    return data


def claude_path():
    return shutil.which("claude")


def ask_claude(system, user, exe=None, runner=subprocess.run):
    """One question to Claude Code used as a plain model: no tools, no saved session, text in, JSON out."""
    exe = exe or claude_path()
    if not exe:
        raise OSError("Claude Code is not installed on this computer")
    args = [exe, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
            "--disable-slash-commands", "--no-session-persistence"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE")}
    try:
        done = runner(args, input=(system + "\n\n" + user).encode("utf-8"), capture_output=True,
                      timeout=CLAUDE_TIMEOUT, cwd=os.path.expanduser("~"), env=env)
    except subprocess.TimeoutExpired:
        raise OSError("Claude Code did not answer in %d seconds" % CLAUDE_TIMEOUT)
    out = done.stdout.decode("utf-8", "replace").strip()
    try:
        reply = json.loads(out)
    except ValueError:
        raise OSError("Claude Code did not answer: " + learn.one_line(out or done.stderr.decode("utf-8", "replace"))[:300])
    if done.returncode != 0 or reply.get("is_error"):
        raise OSError("Claude Code said: " + learn.one_line(str(reply.get("result") or reply))[:300])
    return json_from(reply.get("result"))


def ask_api(system, user, cfg, call=judge.call):
    """One question to an OpenAI-compatible address. Servers differ in what they accept, so the strict form
    (JSON only, no thinking) is tried first and the plain form after a refusal."""
    body = {"model": cfg["model"], "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    strict = dict(body, response_format={"type": "json_object"}, chat_template_kwargs={"enable_thinking": False})
    try:
        reply = call(cfg, "/chat/completions", strict, timeout=CLAUDE_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code not in (400, 422):
            raise
        reply = call(cfg, "/chat/completions", body, timeout=CLAUDE_TIMEOUT)
    return json_from(reply["choices"][0]["message"].get("content"))


def engines(root=build.ROOT):
    """What can do the reading, in the order it is preferred: [{"id", "label", "ready", "detail"}]."""
    exe, cfg = claude_path(), judge.settings(root)
    return [
        {"id": "claude", "label": "Claude Code on this computer", "ready": bool(exe),
         "detail": "Uses the login Claude Code already has. Your prompts go to Anthropic, where these chats took place."
         if exe else "Not found. Install Claude Code and sign in, then start this page again."},
        {"id": "api", "label": "A local or hosted model", "ready": bool(cfg),
         "detail": ("%s at %s. Your prompts go to that address." % (cfg["model"], cfg["base_url"])) if cfg
         else "Any OpenAI-compatible address: vLLM, Ollama, llama.cpp or a hosted service. Enter the address, the model and a key if it needs one."},
    ]


def asker(engine, root=build.ROOT):
    """The function (system, user) -> dict for an engine. Raises Failed when the engine is not ready."""
    if engine == "claude":
        if not claude_path():
            raise Failed("Claude Code is not installed on this computer.")
        return ask_claude
    cfg = judge.settings(root)
    if engine != "api" or not cfg:
        raise Failed("No model address is set up yet.")
    return lambda system, user: ask_api(system, user, cfg)


# ----------------------------------------------------------------------------- reading

def read_prompts(root=build.ROOT, projects_dir=None):
    """Every prompt worth showing the model, oldest first, each once: [{"date", "project", "text"}]."""
    _count, path = learn.all_prompts(root, projects_dir)
    rows, seen = [], set()
    for line in build.read_text(path).split("\n"):
        if not line.strip():
            continue
        row = json.loads(line)
        if len(row["text"].split()) >= 3 and row["text"] not in seen:
            seen.add(row["text"])
            rows.append(row)
    return rows


def batches(rows):
    """Splits the numbered prompts into lists of (number, row) that fit one question."""
    out, cur, size = [], [], 0
    for n, row in enumerate(rows):
        if cur and size + len(row["text"]) > BATCH_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append((n, row))
        size += len(row["text"]) + 40
    return out + ([cur] if cur else [])


def batch_text(batch):
    return "\n".join("%d | %s | %s | %s" % (n, row["date"], row["project"], row["text"]) for n, row in batch)


def cache_path(root):
    return os.path.join(root, "_work", "bootstrap-cache.json")


def load_cache(root):
    try:
        data = json.loads(build.read_text(cache_path(root)))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def numbers(value, allowed):
    return [n for n in value if isinstance(n, int) and not isinstance(n, bool) and n in allowed] if isinstance(value, list) else []


def clean_find(found, allowed):
    """What a batch answer may contribute: findings in the right shape that name prompts they were shown."""
    rules, skills = [], []
    for r in found.get("rules") if isinstance(found.get("rules"), list) else []:
        if not isinstance(r, dict):
            continue
        text, shown = learn.one_line(r.get("rule")), numbers(r.get("prompts"), allowed)
        if text and shown and r.get("category") in build.CATEGORIES:
            rules.append({"rule": text, "why": learn.one_line(r.get("why")), "category": r["category"],
                          "scope": r.get("scope") if r.get("scope") in SCOPE_WORDS else "always",
                          "firm": r.get("firm") is True, "prompts": shown})
    for s in found.get("skills") if isinstance(found.get("skills"), list) else []:
        if not isinstance(s, dict):
            continue
        name, shown = slug(s.get("name")), numbers(s.get("prompts"), allowed)
        if name and shown and learn.one_line(s.get("what")):
            skills.append({"name": name, "what": learn.one_line(s.get("what")), "prompts": shown})
    return {"rules": rules, "skills": skills}


def extract(rows, ask, root, progress, stopped):
    """Asks the model about every batch. Returns (rule candidates, skill candidates, how many batches failed)."""
    todo, cache = batches(rows), load_cache(root)
    results, failed, done = {}, [], 0

    def one(i):
        if stopped():
            raise Stopped()
        text = batch_text(todo[i])
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if key in cache:
            return i, key, cache[key], None
        error = None
        for _ in range(TRIES):
            try:
                return i, key, ask(EXTRACT, "Messages (number | date | project | text):\n" + text), None
            except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
                error = e
        return i, key, None, error

    progress("Reading your chats", 0, len(todo))
    with concurrent.futures.ThreadPoolExecutor(AT_ONCE) as pool:
        for i, key, found, error in pool.map(one, range(len(todo))):
            done += 1
            if found is None:
                failed.append(str(error))
            else:
                results[i] = found
                if key not in cache:
                    cache[key] = found
                    learn.write_whole(cache_path(root), json.dumps(cache, ensure_ascii=False))
            progress("Reading your chats", done, len(todo))
            if failed and len(failed) == done and done >= min(3, len(todo)):
                raise Failed("The model did not answer. " + failed[0])
    rules, skills = [], []
    for i in sorted(results):
        found = clean_find(results[i], {n for n, _row in todo[i]})
        rules += found["rules"]
        skills += found["skills"]
    return rules, skills, len(failed)


# ----------------------------------------------------------------------------- merging

def slug(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text or "").lower())).strip("-")[:48].rstrip("-")


def plain_text(text):
    """A model's sentence as the rule files want it: one line, no dash breaks, no double quotes."""
    t = learn.one_line(text).replace("—", ", ").replace("–", "-").replace(" -- ", ", ").replace('"', "'")
    return re.sub(r"\s+,", ",", t)


def groups_of(answer, key, count):
    """The groups in a merge answer, each candidate in one group at most; candidates nobody grouped stand alone."""
    out, taken = [], set()
    for g in answer.get(key) if isinstance(answer.get(key), list) else []:
        if not isinstance(g, dict):
            continue
        members = [n for n in numbers(g.get("from"), set(range(count))) if n not in taken]
        if members:
            taken.update(members)
            out.append((members, g))
    return out + [([n], {}) for n in range(count) if n not in taken]


def merge_rules(category, cands, existing, ask):
    """One round over the candidates of a category. Returns merged candidates in the same shape, with what the
    model wrote for the group under "final"."""
    listing = "\n".join("%d | %s | %s" % (i, c["scope"], c["rule"]) for i, c in enumerate(cands))
    have = "\n".join("%s: %s" % (r["id"], r["rule"]) for r in existing) or "(none)"
    answer = ask(MERGE % {"category": category}, "Candidates (number | scope | rule):\n%s\n\nRules already in the map:\n%s" % (listing, have))
    out = []
    for members, g in groups_of(answer, "rules", len(cands)):
        first = cands[members[0]]
        final = dict(first.get("final") or {})
        final.update({k: plain_text(g.get(k)) for k in ("title", "rule", "short", "why", "earlier", "open_question") if plain_text(g.get(k))})
        if g.get("existing") in {r["id"] for r in existing}:
            final["existing"] = g["existing"]
        scopes = collections.Counter(cands[n]["scope"] for n in members)
        out.append({"rule": final.get("rule") or first["rule"], "why": final.get("why") or first["why"], "category": category,
                    "scope": scopes.most_common(1)[0][0], "firm": any(cands[n]["firm"] for n in members),
                    "prompts": sorted({p for n in members for p in cands[n]["prompts"]}), "final": final})
    return out


def merged(cands, merge_once):
    """Merges in rounds until one question has seen every candidate that is left."""
    while len(cands) > MERGE_AT_ONCE:
        before = len(cands)
        cands = [m for at in range(0, len(cands), MERGE_AT_ONCE) for m in merge_once(cands[at:at + MERGE_AT_ONCE])]
        if len(cands) > before * 0.8:          # nothing left to merge between the parts
            return cands
    return merge_once(cands) if cands else []


def quotes_for(numbers_, rows):
    """Up to four of the owner's sentences, the latest first, one per day where there are enough days."""
    picked, days = [], set()
    ordered = sorted(numbers_, key=lambda n: rows[n]["date"], reverse=True)
    for n in ordered + ordered:
        row = rows[n]
        if n in [p[0] for p in picked] or (row["date"] in days and len(days) < len({rows[m]["date"] for m in numbers_})):
            continue
        days.add(row["date"])
        picked.append((n, {"date": row["date"], "project": row["project"].replace(",", " "), "quote": row["text"].rstrip("…")}))
        if len(picked) == learn.MAX_QUOTES:
            break
    return [q for _n, q in sorted(picked, key=lambda p: p[1]["date"])]


def private(text):
    return any(pattern.search(text) for _label, pattern in build.PRIVATE_PATTERNS)


def rule_from(cand, rows, existing_by_id, taken):
    """A merged candidate as a rule dict ready to write, or (None, why not)."""
    final = cand.get("final") or {}
    text, why = plain_text(cand["rule"]), plain_text(cand.get("why"))
    title = plain_text(final.get("title")) or " ".join(text.rstrip(".").split()[:6])
    if not text or not slug(title) or private(" ".join([text, why, final.get("short", "")])):
        return None, "it named something private"
    old = existing_by_id.get(final.get("existing"))
    if old and old.get("sources") != ["starter"]:
        return None, "already in your map"
    projects = {rows[n]["project"] for n in cand["prompts"]}
    project = slug(next(iter(projects))) if cand["scope"] == "project" and len(projects) == 1 else ""
    scope = "project:" + project if project else (cand["scope"] if cand["scope"] in build.SCOPES else "always")
    quotes = [q for q in quotes_for(cand["prompts"], rows) if not (len(q["quote"]) >= 40 and q["quote"] in text)]
    short = final.get("short", "")
    rule = {"title": title, "category": cand["category"], "scope": scope, "enabled": True,
            "strength": "hard" if cand["firm"] or len(cand["prompts"]) > 1 else "soft",
            "times_seen": len(cand["prompts"]), "sources": ["chats"], "last_seen": max(rows[n]["date"] for n in cand["prompts"]),
            "needs_owner": bool(final.get("open_question")), "rule": text, "why": why or None,
            "earlier": final.get("earlier") or None, "quotes": quotes,
            "short": short if short and len(short.split()) <= build.MAX_SHORT_WORDS else None,
            "open_question": final.get("open_question", "")}
    if old and not project:
        rule.update(id=old["id"], path=old["path"], replaces=old["id"])
    else:
        name = (project + "-" if project else "") + slug(title)
        base = rid = "%s-%s" % (cand["category"], name)
        n = 2
        while rid in taken:
            rid, n = "%s-%d" % (base, n), n + 1
        folder = "rules/_project/%s" % project if project else "rules/%s" % cand["category"]
        rule.update(id=rid, path="%s/%s.md" % (folder, rid[len(cand["category"]) + 1:]))
    taken.add(rule["id"])
    return rule, ""


def skills_from(cands, rows, ask, have):
    """The repeated tasks as skill drafts: [{"name", "description", "steps", "quotes", "times"}]."""
    def once(part):
        listing = "\n".join("%d | %s | %s" % (i, c["name"], c["what"]) for i, c in enumerate(part))
        answer = ask(MERGE_SKILLS, "Candidates (number | name | what):\n" + listing)
        out = []
        for members, g in groups_of(answer, "skills", len(part)):
            first = part[members[0]]
            steps = [plain_text(s) for s in g.get("steps") if plain_text(s)] if isinstance(g.get("steps"), list) else first.get("steps", [])
            out.append({"name": slug(g.get("name")) or first["name"], "what": plain_text(g.get("description")) or first["what"],
                        "steps": steps, "prompts": sorted({p for n in members for p in part[n]["prompts"]})})
        return out
    drafts, names = [], set(have)
    for c in merged(cands, once):
        days = {rows[n]["date"] for n in c["prompts"]}
        if len(days) < MIN_SKILL_DAYS or not c.get("steps") or c["name"] in names or private(c["what"] + " ".join(c["steps"])):
            continue
        names.add(c["name"])
        drafts.append({"name": c["name"], "description": c["what"], "steps": c["steps"],
                       "quotes": quotes_for(c["prompts"], rows), "times": len(c["prompts"])})
    return drafts


def skill_text(s):
    title = s["name"].replace("-", " ")
    lines = ["---", "name: " + s["name"], "description: " + json.dumps(s["description"], ensure_ascii=False), "---", "",
             "# " + title[0].upper() + title[1:], "",
             "Suggested from your chats: you asked for this %d times. Read it, change what is off, then switch it on in the Skills tab." % s["times"],
             "", "## Steps"]
    lines += ["%d. %s" % (i + 1, step) for i, step in enumerate(s["steps"])]
    lines += ["", "## What you said"] + ['- %s, %s: "%s"' % (q["date"], q["project"], q["quote"]) for q in s["quotes"]]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- the plan and the writing

def prepare(root=build.ROOT, ask=None, projects_dir=None, progress=lambda *_a: None, stopped=lambda: False):
    """Reads the chats and works out everything to write, without changing the map. Returns the plan for apply().
    Raises Failed (nothing usable), Stopped, or passes on what the engine raised at the merge."""
    progress("Collecting what you typed", 0, 0)
    rows = read_prompts(root, projects_dir)
    if not rows:
        raise Failed("No chats were found in %s. Use Claude Code for a while, then come back." % build.folder_text(projects_dir or learn.sessions_dir()))
    cands, skill_cands, failed = extract(rows, ask, root, progress, stopped)
    rules, profiles, errors = build.load_all(root)
    if errors:
        raise Failed("The map has problems, fix them first: " + "; ".join(errors[:3]))
    by_id = {r["id"]: r for r in rules}
    cats = [c for c in build.CATEGORIES if any(x["category"] == c for x in cands)]
    new, left_out, taken = [], collections.Counter(), set(by_id)
    for i, cat in enumerate(cats):
        if stopped():
            raise Stopped()
        progress("Putting the rules together", i, len(cats) + 1)
        existing = [r for r in rules if r["category"] == cat and not str(r["scope"]).startswith("project:")]
        for cand in merged([x for x in cands if x["category"] == cat], lambda part, c=cat, e=existing: merge_rules(c, part, e, ask)):
            rule, why_not = rule_from(cand, rows, by_id, taken)
            if rule:
                new.append(rule)
            else:
                left_out[why_not] += 1
    progress("Putting the rules together", len(cats), len(cats) + 1)
    have = [os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(root, "skills", "*", "SKILL.md"))]
    drafts = skills_from(skill_cands, rows, ask, have) if skill_cands else []
    progress("Putting the rules together", len(cats) + 1, len(cats) + 1)
    return {"new": new, "skills": drafts, "prompts": len(rows), "failed_batches": failed, "left_out": dict(left_out)}


def fit(rules, profiles, new, root):
    """Drops what the build refuses until the whole map builds. Returns the plan: rules to write, the new core."""
    core = next(p for p in profiles if p["name"] == "core")
    left_out = collections.Counter()
    while True:
        replaced = {r["replaces"] for r in new if r.get("replaces")}
        parsed = [build.parse_rule(build.render_rule(r), r["path"]) for r in new]
        everything = [r for r in rules if r["id"] not in replaced] + parsed
        offered = [r["id"] for r in sorted(parsed, key=lambda r: -r["times_seen"])
                   if r["scope"] == "always" and r["strength"] == "hard" and r.get("short") and r["id"] not in core["include_ids"]][:CORE_NEW]
        kept = [i for i in core["include_ids"] if i in {r["id"] for r in everything}]
        while True:
            trial = dict(core, include_ids=kept + offered)
            errors, _outputs = build.compile_map(everything, [trial if p["name"] == "core" else p for p in profiles], root)
            too_long = [e for e in errors if "Take a rule out of profiles/core.yaml" in e]
            if not too_long or not offered:
                break
            offered.pop()
        if not errors:
            questions = [(r["id"], r["open_question"]) for r in new if r.get("open_question")]
            return {"rules": parsed, "core": trial, "replaced": sorted(replaced), "questions": questions, "left_out": dict(left_out),
                    "starter_kept": sum(r.get("sources") == ["starter"] for r in rules if r["id"] not in replaced)}
        bad = [r for r in new if any(e.startswith(r["path"] + ": ") or ("`%s`" % r["id"]) in e for e in errors)]
        if not bad:
            if not new:
                raise Failed("The rules could not be built: " + "; ".join(errors[:3]))
            bad = [min(new, key=lambda r: r["times_seen"])]          # an error no single rule owns: let go of the least said one
        for r in bad:
            left_out["the build refused it"] += 1
            new.remove(r)


def apply(prepared, root=build.ROOT):
    """Writes what prepare() found into the map as it is now, and builds it. Returns the summary shown to the owner."""
    rules, profiles, errors = build.load_all(root)
    if errors:
        raise Failed("The map has problems, fix them first: " + "; ".join(errors[:3]))
    plan = fit(rules, profiles, [dict(r) for r in prepared["new"]], root)
    plan.update(skills=prepared["skills"], prompts=prepared["prompts"], failed_batches=prepared["failed_batches"],
                left_out=dict(collections.Counter(prepared["left_out"]) + collections.Counter(plan["left_out"])))
    for r in plan["rules"]:
        learn.write_rule(r, root)
    build.write_text(build.full_path(root, "profiles/core.yaml"), build.render_profile(plan["core"]))
    if plan["questions"]:
        path = os.path.join(root, "conflicts.md")
        text = build.read_text(path) if os.path.isfile(path) else "# Open questions\n"
        entries = "".join("- `%s`: %s\n" % (rid, q) for rid, q in plan["questions"] if ("`%s`" % rid) not in text)
        if "Nothing is open." in text:
            text = text.replace("Nothing is open.\n", entries, 1)
        elif "\n## " in text:
            head, rest = text.split("\n## ", 1)
            text = head.rstrip("\n") + "\n" + entries + "\n## " + rest
        else:
            text = text.rstrip("\n") + "\n\n" + entries
        build.write_text(path, text)
    for s in plan["skills"]:
        build.write_text(os.path.join(root, "skills", s["name"], "SKILL.md"), skill_text(s))
    errors, _outputs, rules = build.run(root)
    summary = {"when": learn.today(), "prompts": plan["prompts"], "rules": len(plan["rules"]) - len(plan["replaced"]),
               "rewritten": len(plan["replaced"]), "starter_kept": plan["starter_kept"], "skills": [s["name"] for s in plan["skills"]],
               "questions": len(plan["questions"]), "left_out": plan["left_out"], "failed_batches": plan["failed_batches"],
               "total": len(rules), "problems": errors}
    learn.write_whole(os.path.join(root, "_work", "bootstrap.json"), json.dumps(summary, ensure_ascii=False, indent=1))
    return summary


def last_summary(root=build.ROOT):
    try:
        data = json.loads(build.read_text(os.path.join(root, "_work", "bootstrap.json")))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def summary_text(s):
    parts = ["%d new rules" % s["rules"]]
    if s["rewritten"]:
        parts.append("%d starter rules rewritten in your words" % s["rewritten"])
    if s["skills"]:
        parts.append("%d suggested skills" % len(s["skills"]))
    if s["questions"]:
        parts.append("%d open questions" % s["questions"])
    return "Read %d prompts: %s. The map now has %d rules." % (s["prompts"], ", ".join(parts), s["total"])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    found = engines()
    if argv[:1] == ["engines"]:
        for e in found:
            print("%-7s %s: %s" % (e["id"], "ready" if e["ready"] else "not ready", e["detail"]))
        return 0
    if not build.has_map():
        print(build.init())
    engine = argv[argv.index("--engine") + 1] if "--engine" in argv and argv.index("--engine") + 1 < len(argv) else next((e["id"] for e in found if e["ready"]), None)
    if not engine:
        print("No engine is ready. Install Claude Code, or set up a model address (see README).")
        return 1
    try:
        plan = prepare(ask=asker(engine), progress=lambda step, done, total: print("%s%s" % (step, " %d of %d" % (done, total) if total else ""), flush=True))
    except (Failed, OSError, ValueError) as e:
        print("Not built: %s" % e)
        return 1
    print(summary_text(apply(plan)))
    print("Look at it with `python ui.py`. Nothing is installed until you switch it on there.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
