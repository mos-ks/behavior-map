#!/usr/bin/env python3
# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
"""Keeps the behaviour map current from the owner's own corrections. No model, no network.

    python learn.py scan      # read new owner prompts from the Claude Code transcripts, keep the corrections
    python learn.py review    # count matched corrections on their rules, group the rest, age the inbox
    python learn.py status    # the same report (review changes nothing when there is nothing new)
    python learn.py note "the owner's words" [--rule <id>] [--project <name>]   # add one correction by hand
    python learn.py prompts   # write everything the owner typed to map/_work/prompts.jsonl (the input of BOOTSTRAP.md)

A correction is a short prompt in which the owner says how to work ("dont", "never", "i said", "again").
A correction that matches a rule that is switched on counts as that rule being broken: the assistant had the
rule and still needed telling. Corrections that match nothing are grouped; a group said three times on two
days is offered as a new rule. Nothing here switches a rule off or writes a new one: that is the owner's call
(the page, `python ui.py`).

Python 3.9+, standard library only.
"""
import datetime
import glob
import hashlib
import json
import math
import os
import re
import sys

import build

ROOT = build.ROOT
MATCH_SURE = 0.42                  # at or above: the correction counts for the rule without asking
MATCH_MAYBE = 0.20                 # at or above: offered as a suggestion in the inbox
MATCH_TOP = 3                      # how many suggestions a correction keeps
GROUP_ALIKE = 0.34                 # two unmatched corrections this alike are the same wish
GROUP_MIN, GROUP_MIN_DAYS = 3, 2   # a group becomes a proposal when said this often on this many days
INBOX_DAYS = 30                    # an open correction nobody acted on leaves the inbox after this long
FRESH_DAYS, FADING_DAYS = 90, 180  # a rule last stated longer ago than this is fading, then stale
RELATED_PER_RULE, RELATED_FLOOR = 2, 0.16   # the graph links each rule to its closest neighbours above this likeness
MAX_TEXT = 300
MAX_QUOTES = 4

MARKERS = re.compile(
    r"\b(dont|do not|doesnt|never|always|stop|from now on|i said|i told you|told you|as i said|like i said|again|"
    r"why did you|why do you|why are you|why would you|not what i|no more|no need|instead of|should have|shouldnt|"
    r"you forgot|forgot to|remember|next time|wtf|nein|nicht so|immer|niemals|hoer auf|warum hast)\b")
STARTS_NO = re.compile(r"^(no|nope|nah|wrong)\b")
# whole links and addresses first, then what build.py already treats as private, then anything that looks like a key
REDACT = ([re.compile(r"https?://\S+", re.I), re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")]
          + [pattern for _label, pattern in build.PRIVATE_PATTERNS]
          + [re.compile(r"\b[A-Za-z0-9_\-+/=]{28,}\b")])
STOP = set("""a an the and or but if then so to of in on at for from with without by as is are was were be been it its this that
these those i you he she we they me my your our their them his her do does did done have has had not no yes can could would
should will shall may might must just also very more most some any all each every one two up down out over under into than
there here what which who whom when where why how about after before again only own same too s t d ll re ve m am im youre
der die das und oder aber wenn dann zu von im auf fuer mit ohne als ist sind war ich du er sie wir ihr es ein eine einen dem den
nicht auch nur noch schon wie was wer wo warum""".split())
USER_LINE = (b'"type":"user"', b'"type": "user"')   # a transcript line that can be an owner prompt holds one of these


def sessions_dir():
    """Where Claude Code keeps its sessions: ~/.claude/projects, or under CLAUDE_CONFIG_DIR when that is set."""
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"), "projects")


def today():
    return datetime.date.today().isoformat()


def days_between(a, b):
    a, b = build.parse_day(a), build.parse_day(b)
    return (b - a).days if a and b else None


def one_line(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


# ----------------------------------------------------------------------------- words and likeness

def plain(text):
    return re.sub(r"[’'`]", "", str(text).lower())


def tokens(text):
    out = []
    for w in re.findall(r"[a-zäöüß]{2,}", plain(text)):
        if w in STOP:
            continue
        for end in ("ing", "ed", "es", "s"):
            if len(w) > len(end) + 3 and w.endswith(end):
                w = w[:-len(end)]
                break
        out.append(w)
    return out


def rule_words(r):
    return " ".join([str(r.get("title") or ""), str(r.get("rule") or ""), str(r.get("short") or ""),
                     str(r.get("boundary") or "")] + [q["quote"] for q in r.get("quotes") or []])


class Likeness:
    """Word overlap between texts, weighting rare words up (tf-idf, cosine)."""

    def __init__(self, texts):
        docs = [tokens(t) for t in texts]
        df = {}
        for d in docs:
            for w in set(d):
                df[w] = df.get(w, 0) + 1
        n = max(len(docs), 1)
        self.idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}
        self.default_idf = math.log(n + 1) + 1
        self.vectors = [self.vector(d) for d in docs]

    def vector(self, toks):
        tf = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        v = {w: (1 + math.log(c)) * self.idf.get(w, self.default_idf) for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / norm for w, x in v.items()}

    @staticmethod
    def cosine(a, b):
        if len(a) > len(b):
            a, b = b, a
        return sum(x * b.get(w, 0.0) for w, x in a.items())

    def best(self, text, top=MATCH_TOP):
        v = self.vector(tokens(text))
        scored = sorted(((self.cosine(v, d), i) for i, d in enumerate(self.vectors)), reverse=True)
        return [(i, round(s, 3)) for s, i in scored[:top] if s > 0]


def rule_likeness(rules):
    return Likeness([rule_words(r) for r in rules])


def related(rules):
    """Pairs of rules that speak about the same thing: each rule's closest neighbours. For the graph."""
    lk = rule_likeness(rules)
    pairs = {}
    for i, v in enumerate(lk.vectors):
        scored = sorted(((lk.cosine(v, w), j) for j, w in enumerate(lk.vectors) if j != i), reverse=True)
        for s, j in scored[:RELATED_PER_RULE]:
            if s >= RELATED_FLOOR:
                key = tuple(sorted((rules[i]["id"], rules[j]["id"])))
                pairs[key] = max(pairs.get(key, 0), round(s, 3))
    return [{"a": a, "b": b, "score": s} for (a, b), s in sorted(pairs.items())]


# ----------------------------------------------------------------------------- the signal file

def signal_path(root, name):
    return os.path.join(root, "signals", name)


def write_whole(path, text):
    """Replaces the file in one step, so a crash never leaves half a file."""
    build.write_text(path + ".tmp", text)
    os.replace(path + ".tmp", path)


def load_signals(root=ROOT):
    path = signal_path(root, "signals.jsonl")
    if not os.path.isfile(path):
        return []
    return [json.loads(line) for line in build.read_text(path).split("\n") if line.strip()]


def save_signals(sigs, root=ROOT):
    sigs.sort(key=lambda s: (s["date"], s["id"]))
    write_whole(signal_path(root, "signals.jsonl"), "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in sigs))


def load_state(root=ROOT):
    path = signal_path(root, "state.json")
    return json.loads(build.read_text(path)) if os.path.isfile(path) else {"since": today(), "files": {}}       # a new map learns from today on


def clean(text):
    t = one_line(text)
    for pattern in REDACT:
        t = pattern.sub("[redacted]", t)
    t = t.replace('"', "'")
    return t if len(t) <= MAX_TEXT else t[:MAX_TEXT - 1].rstrip() + "…"


def is_short_prompt(text):
    """A prompt short enough to be an instruction rather than a paste, and not a command or a system note."""
    t = plain(text).strip()
    return bool(t) and t[0] not in "<[/#" and len(t) <= 400 and len(t.split()) >= 3


def is_correction(text):
    t = plain(text).strip()
    return is_short_prompt(text) and bool(MARKERS.search(t) or STARTS_NO.match(t))


def new_signal(text, stamp, project, session, source):
    """`stamp` is a date or a full timestamp; two corrections in one session differ by it."""
    return {"id": hashlib.sha1(("%s|%s|%s" % (session, stamp, text)).encode("utf-8")).hexdigest()[:12],
            "date": stamp[:10], "project": project, "session": session, "source": source, "text": clean(text),
            "status": "open", "rule": None, "suggest": [], "counted": False}


def quote_of(sig):
    """The correction as a quote line of a rule file (no comma in the project, no cut-off mark)."""
    return {"date": sig["date"], "project": sig["project"].replace(",", " "), "quote": sig["text"].rstrip("…")}


# ----------------------------------------------------------------------------- reading the transcripts

def lines_from(path, start, marks):
    """The transcript lines after byte `start` that hold one of `marks`, parsed. Returns (lines, end).
    The file is read line by line (some are over a gigabyte) and only lines that can matter are parsed."""
    out, end = [], start
    with open(path, "rb") as f:
        f.seek(start)
        for raw in f:
            if not raw.endswith(b"\n"):
                break                                  # a line still being written: the next scan takes it
            end += len(raw)
            if any(mark in raw for mark in marks):
                try:
                    out.append(json.loads(raw.decode("utf-8", "replace")))
                except ValueError:
                    pass
    return out, end


def project_of(line):
    """The project a transcript line belongs to: the name of the folder the session worked in."""
    return os.path.basename(str(line.get("cwd") or "").replace("\\", "/").rstrip("/")) or "unknown"


def owner_prompts(path, start=0):
    """Prompts a person typed, from one session file, reading on from byte `start`. Returns (prompts, end)."""
    lines, end = lines_from(path, start, USER_LINE)
    out = []
    for d in lines:
        if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain") or d.get("isCompactSummary"):
            continue
        if (d.get("origin") or {}).get("kind") != "human" or d.get("entrypoint") == "sdk-cli":
            continue
        content = (d.get("message") or {}).get("content")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                               and not b.get("text", "").lstrip().startswith("<"))
        if not isinstance(content, str) or not content.strip():
            continue
        out.append({"text": content.strip(), "stamp": str(d.get("timestamp") or ""), "project": project_of(d),
                    "session": str(d.get("sessionId") or os.path.splitext(os.path.basename(path))[0])})
    return out, end


def all_prompts(root=ROOT, projects_dir=None):
    """Writes every prompt the owner typed, oldest first, to _work/prompts.jsonl with private details blanked.
    Long pastes are cut: what a person asks for is in the first lines. Returns (how many, the file)."""
    projects_dir = projects_dir or sessions_dir()
    rows, seen = [], set()
    for path in sorted(glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))):
        for p in owner_prompts(path)[0]:
            text = clean(p["text"][:1200])
            if text and text[0] not in "<[/#" and (p["stamp"], text) not in seen:
                seen.add((p["stamp"], text))
                rows.append({"date": p["stamp"][:10], "project": p["project"], "text": text})
    rows.sort(key=lambda r: r["date"])
    out = os.path.join(root, "_work", "prompts.jsonl")
    write_whole(out, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return len(rows), out


def match(sig, lk, rules):
    """Fills in the suggestions; a sure match is attached straight away."""
    sig["suggest"] = [[rules[i]["id"], s] for i, s in lk.best(sig["text"]) if s >= MATCH_MAYBE]
    if sig["suggest"] and sig["suggest"][0][1] >= MATCH_SURE:
        sig["rule"], sig["status"] = sig["suggest"][0][0], "attached"


def scan(root=ROOT, projects_dir=None, wide=False):
    """Adds the corrections typed since the last scan. Returns how many were added to the inbox or to a rule.
    With `wide` (a local model will sort them) every short prompt is kept as "waiting": the model can tell a
    standing instruction without a marker word, which the word list cannot. Waiting ones are not shown."""
    projects_dir = projects_dir or sessions_dir()
    state, sigs = load_state(root), load_signals(root)
    known = {s["id"] for s in sigs}
    rules, _ = build.load_rules(root)
    lk = rule_likeness(rules)
    added, kept = 0, False
    for path in sorted(glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))):
        key = os.path.relpath(path, projects_dir).replace(os.sep, "/")
        size = os.path.getsize(path)
        start = state["files"].get(key, 0)
        if start > size:
            start = 0                      # the file was replaced
        if start == size:
            continue
        if start == 0 and datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat() < state["since"]:
            state["files"][key] = size     # untouched since before the map was installed
            continue
        prompts, state["files"][key] = owner_prompts(path, start)
        for p in prompts:
            marked = is_correction(p["text"])
            if p["stamp"][:10] < state["since"] or not (marked or (wide and is_short_prompt(p["text"]))):
                continue
            sig = new_signal(p["text"], p["stamp"] or today(), p["project"], p["session"], "scan")
            if sig["id"] in known:
                continue
            if marked:
                match(sig, lk, rules)
                added += 1
            else:
                sig["status"] = "waiting"
            sigs.append(sig)
            known.add(sig["id"])
            kept = True
    if kept:
        save_signals(sigs, root)
    write_whole(signal_path(root, "state.json"), json.dumps(state, ensure_ascii=False, indent=1))
    return added


def note(text, rule=None, project="by hand", root=ROOT):
    rules, _ = build.load_rules(root)
    if rule and all(r["id"] != rule for r in rules):
        raise build.BuildError("no rule with the id `%s`" % rule)
    sigs = load_signals(root)
    sig = new_signal(text, datetime.datetime.now().isoformat(), project, "note", "note")
    match(sig, rule_likeness(rules), rules)
    if rule:
        sig["rule"], sig["status"] = rule, "attached"
    sigs.append(sig)
    save_signals(sigs, root)
    return sig


# ----------------------------------------------------------------------------- the owner's decisions on a signal

def write_rule(rule, root=ROOT):
    build.write_text(build.full_path(root, rule["path"]), build.render_rule(rule))


def uncount(sig, rules_by_id, root):
    """Takes a counted correction off its rule again (the owner moved or dismissed it). True when a rule changed."""
    r = rules_by_id.get(sig.get("rule"))
    changed = bool(sig.get("counted") and r)
    if changed:
        r["times_seen"] = max(1, int(r["times_seen"]) - 1)
        r["quotes"] = [q for q in r["quotes"] if q != quote_of(sig)]
        write_rule(r, root)
    sig["counted"] = False
    return changed


def decide(sig_id, action, rule=None, root=ROOT):
    """action: attach (to `rule`), dismiss, reopen. Returns True when a rule file changed."""
    sigs = load_signals(root)
    sig = next((s for s in sigs if s["id"] == sig_id), None)
    if sig is None:
        raise build.BuildError("no correction with the id `%s`" % sig_id)
    by_id = {r["id"]: r for r in build.load_rules(root)[0]}
    changed = False
    if action == "attach":
        if rule not in by_id:
            raise build.BuildError("no rule with the id `%s`" % rule)
        if sig.get("rule") != rule:
            changed = uncount(sig, by_id, root)
        sig["rule"], sig["status"] = rule, "attached"
    elif action in ("dismiss", "reopen"):
        changed = uncount(sig, by_id, root)
        sig["rule"], sig["status"] = None, "dismissed" if action == "dismiss" else "open"
    else:
        raise build.BuildError("unknown action `%s`" % action)
    sig["founding"] = False
    save_signals(sigs, root)
    return changed


# ----------------------------------------------------------------------------- review

def groups(open_sigs):
    """Unmatched corrections that say the same thing, largest group first."""
    lk = Likeness([s["text"] for s in open_sigs])
    parent = list(range(len(open_sigs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(open_sigs)):
        for j in range(i + 1, len(open_sigs)):
            if lk.cosine(lk.vectors[i], lk.vectors[j]) >= GROUP_ALIKE:
                parent[find(i)] = find(j)
    by_root = {}
    for i, s in enumerate(open_sigs):
        by_root.setdefault(find(i), []).append(s)
    out = []
    for members in by_root.values():
        days = {s["date"] for s in members}
        if len(members) >= GROUP_MIN and len(days) >= GROUP_MIN_DAYS:
            out.append({"ids": [s["id"] for s in members], "count": len(members), "days": len(days),
                        "first": min(days), "last": max(days), "texts": [s["text"] for s in members][:6]})
    return sorted(out, key=lambda g: (-g["count"], g["first"]))


def freshness(rule, now):
    age = days_between(rule.get("last_seen"), now)
    if age is None:
        return "undated"
    return "fresh" if age <= FRESH_DAYS else ("fading" if age <= FADING_DAYS else "stale")


def summarise(sigs, rules, since, now=None):
    """What the map has learned, from the signals and rules as they are. Changes nothing."""
    now = now or today()
    on = {r["id"] for r in rules if r.get("enabled") is True}
    broken = {}
    for s in sigs:
        # a correction a rule was made from is where the rule comes from, not the rule being broken
        if s["status"] == "attached" and not s.get("founding") and s["date"] >= since and s["rule"] in on:
            broken[s["rule"]] = broken.get(s["rule"], 0) + 1
    return {
        "reviewed": now, "since": since,
        "counts": {k: sum(s["status"] == k for s in sigs) for k in ("open", "attached", "dismissed", "expired", "waiting", "set aside")},
        "broken": broken,
        "freshness": {r["id"]: freshness(r, now) for r in rules},
        "proposals": groups([s for s in sigs if s["status"] == "open"]),
    }


def review(root=ROOT, now=None):
    """Counts attached corrections on their rules and ages the inbox. Returns (summary, ids of the rules it changed)."""
    now = now or today()
    sigs = load_signals(root)
    rules, _ = build.load_rules(root)
    by_id = {r["id"]: r for r in rules}
    changed, touched = set(), False
    for s in sigs:
        r = by_id.get(s.get("rule"))
        if s["status"] == "attached" and r is None:
            s["status"], s["rule"], s["counted"], touched = "open", None, False, True      # its rule was deleted
        elif s["status"] == "attached" and not s.get("counted"):
            r["times_seen"] = int(r["times_seen"]) + 1
            if not r.get("last_seen") or str(r["last_seen"]) < s["date"]:
                r["last_seen"] = s["date"]
            if len(r["quotes"]) < MAX_QUOTES and quote_of(s) not in r["quotes"]:
                r["quotes"].append(quote_of(s))
            s["counted"], touched = True, True
            changed.add(r["id"])
        elif s["status"] == "open" and (days_between(s["date"], now) or 0) > INBOX_DAYS:
            s["status"], touched = "expired", True
    # what was only kept for the model, or set aside by it, is dropped after the same time: the file stays small
    keep = [s for s in sigs if s["status"] not in ("waiting", "set aside") or (days_between(s["date"], now) or 0) <= INBOX_DAYS]
    if len(keep) != len(sigs):
        sigs[:], touched = keep, True
    for rid in changed:
        write_rule(by_id[rid], root)
    if touched:
        save_signals(sigs, root)
    return summarise(sigs, rules, load_state(root)["since"], now), sorted(changed)


def status_text(summary, rules):
    by_id = {r["id"]: r for r in rules}
    c = summary["counts"]
    lines = ["Since %s: %d corrections counted on a rule, %d waiting in the inbox, %d dismissed, %d aged out."
             % (summary["since"], c["attached"], c["open"], c["dismissed"], c["expired"])]
    if summary["broken"]:
        lines.append("Rules that still needed telling:")
        for rid, n in sorted(summary["broken"].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append("  %dx  %s" % (n, by_id[rid]["title"] if rid in by_id else rid))
    if summary["proposals"]:
        lines.append("Said often enough to become a rule:")
        for g in summary["proposals"]:
            lines.append('  %dx on %d days: "%s"' % (g["count"], g["days"], g["texts"][0]))
    fr = list(summary["freshness"].values())
    lines.append("Rules: %d fresh, %d fading, %d stale, %d undated."
                 % (fr.count("fresh"), fr.count("fading"), fr.count("stale"), fr.count("undated")))
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "note" and len(argv) > 1:
            opts = {argv[i]: argv[i + 1] for i in range(2, len(argv) - 1, 2)}
            sig = note(argv[1], rule=opts.get("--rule"), project=opts.get("--project", "by hand"))
            print("noted %s%s" % (sig["id"], ", counts for " + sig["rule"] if sig["rule"] else ", waiting in the inbox"))
        elif cmd == "prompts":
            count, out = all_prompts()
            print("%d prompts written to %s" % (count, build.folder_text(out)))
            return 0
        elif cmd == "scan":
            print("%d new correction(s)." % scan())
        elif cmd not in ("review", "status"):
            print(__doc__.strip())
            return 2
        summary, changed = review()
        if changed:
            build.main([])                 # the counts are in the rule files now: bring the exports up to date
        if cmd != "note":
            print(status_text(summary, build.load_rules()[0]))
    except build.BuildError as e:
        print("problem: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
