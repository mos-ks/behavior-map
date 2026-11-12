#!/usr/bin/env python3
# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
"""Keeps the behaviour map the same on every computer through its GitHub repo.

    python sync.py            # commit, take in the other computers' changes, scan for secrets, push (the Update button does this)
    python sync.py --follow   # take in the other computers' changes, rebuild, refresh the installed copies
    python sync.py --update   # all of the Update button without the page: follow, read this computer's chats, save

Each computer reads its own chats and pushes what it learned; every other computer takes it in on its next
pull. A rule edited on two computers merges field by field (counts add up, quotes join); only the same field
edited differently on both stops the pull and names the file.

Nothing is pushed without a clean secret scan (gitleaks, from a pinned Docker image). When the scan cannot run
(Docker is not running) or finds something, the change is committed on this computer only and the next run
tries again. The commit carries the owner's own name as configured in the repo and no other byline.

Python 3.9+, standard library only.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile

import build

GITLEAKS = "ghcr.io/gitleaks/gitleaks:v8.28.0@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854"
QUIET = 0x08000000 if os.name == "nt" else 0          # no console window flashing up on Windows


def run(args, cwd, timeout=60):
    """(exit code, output) of a command; code -1 when it could not be started or took too long."""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=QUIET)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def is_repo(root):
    code, top = run(["git", "rev-parse", "--show-toplevel"], root)
    return code == 0 and os.path.normcase(os.path.abspath(top)) == os.path.normcase(os.path.abspath(root))


def secret_scan(root):
    """How many possible secrets gitleaks finds in the folder; None when the scan could not run."""
    with tempfile.TemporaryDirectory() as out:
        code, _ = run(["docker", "run", "--rm", "-v", "%s:/scan:ro" % build.folder_text(root), "-v", "%s:/out" % build.folder_text(out),
                       GITLEAKS, "dir", "/scan", "--no-banner", "--no-color", "--redact", "--exit-code", "0",
                       "--report-format", "json", "--report-path", "/out/report.json"], root, timeout=180)
        report = os.path.join(out, "report.json")
        if code != 0 or not os.path.isfile(report):
            return None
        try:
            return len(json.loads(build.read_text(report)))
        except ValueError:
            return None


def what_changed(root):
    """A short line for the commit message from the staged files."""
    _, names = run(["git", "diff", "--cached", "--name-only"], root)
    files = [n for n in names.split("\n") if n]
    rules = sum(n.startswith("rules/") for n in files)
    parts = []
    if rules:
        parts.append("%d rule%s" % (rules, "" if rules == 1 else "s"))
    if any(n.startswith("signals/") for n in files):
        parts.append("corrections")
    if any(n.startswith("profiles/") for n in files):
        parts.append("profiles")
    if any(n.endswith((".py", ".html", ".cmd")) for n in files):
        parts.append("the page")
    return ", ".join(parts) or "generated files"


def generated(name):
    """True for the files build.py writes. Two computers that both rebuilt disagree on them harmlessly:
    after a pull they are written again from the merged rules."""
    return name.startswith("exports/") or name in ("index.md", "rules.json")


def commit(root, message=None):
    """Commits everything that changed. Returns None, or the line saying why it failed."""
    run(["git", "add", "-A"], root)
    code, _ = run(["git", "diff", "--cached", "--quiet"], root)
    if code != 1:
        return None
    message = message or "Update %s: %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), what_changed(root))
    code, out = run(["git", "commit", "-q", "-m", message], root)
    return None if code == 0 else "the commit failed (%s)" % out.split("\n")[-1][:120]


def merged_value(key, base, ours, theirs):
    """(value, True) for one field changed on two computers, or (None, False) when they truly disagree."""
    if ours == theirs or theirs == base:
        return ours, True
    if ours == base:
        return theirs, True
    if key == "times_seen":                    # each computer counted its own corrections: add both counts
        return max(1, int(ours or 0) + int(theirs or 0) - int(base or 0)), True
    if key == "last_seen":
        return max(str(ours or ""), str(theirs or "")) or None, True
    if isinstance(ours, list) and isinstance(theirs, list):      # quotes, sources: keep what either added, drop what either took out
        old = base if isinstance(base, list) else []
        gone = [x for x in old if x not in ours or x not in theirs]
        out = []
        for x in ours + theirs:
            if x not in out and x not in gone:
                out.append(x)
        return out, True
    return None, False


def merge_rule(base, ours, theirs, path):
    """A rule file changed on two computers, merged field by field. None when one field was edited differently on both."""
    try:
        b = build.parse_rule(base, path) if base is not None else {}
        o, t = build.parse_rule(ours, path), build.parse_rule(theirs, path)
    except build.BuildError:
        return None
    out = {}
    for key in list(o) + [k for k in t if k not in o]:
        value, ok = merged_value(key, b.get(key), o.get(key), t.get(key))
        if not ok:
            return None
        out[key] = value
    return build.render_rule(out)


def merge_signals(base, ours, theirs):
    """The corrections log changed on two computers, merged by correction id. A correction the owner decided
    on (attached or dismissed) wins over the same one still open; otherwise a changed copy wins over the old one."""
    def rows(text):
        return {s["id"]: s for s in (json.loads(line) for line in (text or "").split("\n") if line.strip())}
    b, o, t = rows(base), rows(ours), rows(theirs)
    out = []
    for sid in list(o) + [k for k in t if k not in o]:
        mine, other = o.get(sid), t.get(sid)
        if mine is None or other is None or mine == other:
            out.append(mine or other)
        elif mine == b.get(sid):
            out.append(other)
        elif other == b.get(sid) or other.get("status") == "open":
            out.append(mine)
        else:
            out.append(other if mine.get("status") == "open" else mine)
    out.sort(key=lambda s: (s["date"], s["id"]))
    return "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in out)


def stage_text(root, stage, name):
    """One side of a clash: 1 the common base, 2 the other computers (the rebase target), 3 this computer."""
    code, out = run(["git", "show", ":%d:%s" % (stage, name)], root)
    return out + "\n" if code == 0 else None


def settle(root, name):
    """Settles one file that clashed in a pull. False when it needs the owner."""
    if generated(name):
        if run(["git", "checkout", "--ours", "--", name], root)[0] != 0:
            return run(["git", "rm", "-q", "--", name], root)[0] == 0        # deleted on one side
        return run(["git", "add", "--", name], root)[0] == 0
    base, ours, theirs = (stage_text(root, n, name) for n in (1, 2, 3))
    if ours is None or theirs is None:
        return False                            # edited on one computer, deleted on the other
    if name == "signals/signals.jsonl":
        text = merge_signals(base, ours, theirs)
    elif name.startswith("rules/") and name.endswith(".md"):
        text = merge_rule(base, ours, theirs, name)
    else:
        return False
    if text is None:
        return False
    build.write_text(build.full_path(root, name), text)
    return run(["git", "add", "--", name], root)[0] == 0


def conflicted(root):
    _, out = run(["git", "diff", "--name-only", "--diff-filter=U"], root)
    return [n for n in out.split("\n") if n]


def rebasing(root):
    _, git_dir = run(["git", "rev-parse", "--git-dir"], root)
    git_dir = os.path.join(root, git_dir) if not os.path.isabs(git_dir) else git_dir
    return any(os.path.isdir(os.path.join(git_dir, d)) for d in ("rebase-merge", "rebase-apply"))


def pull(root=build.ROOT):
    """Brings in what the other computers saved, with this computer's own commits replayed on top.

    Returns (whether anything came in, one short line). A clash in a generated file is settled by taking
    either side (the caller rebuilds them). A clash in a rule the owner edited on two computers stops the
    pull, leaves this computer as it was and names the files. A rule both computers changed in different
    fields (one counted a correction, the other edited the wording) and the corrections log merge by themselves."""
    if not is_repo(root):
        return False, "not synced: this folder is not a git repo"
    code, _ = run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"], root)
    if code != 0:
        return False, "not synced: the repo has no GitHub branch"
    code, out = run(["git", "fetch", "-q"], root, timeout=90)
    if code != 0:
        return False, "not synced: %s" % (out.split("\n")[-1][:120] or "GitHub did not answer")
    code, behind = run(["git", "rev-list", "--count", "HEAD..@{upstream}"], root)
    if code != 0 or behind.strip() == "0":
        return False, "nothing new from the other computers"
    failed = commit(root)
    if failed:
        return False, "not synced: " + failed
    _, before = run(["git", "rev-parse", "HEAD"], root)
    code, out = run(["git", "-c", "core.editor=true", "rebase", "-q", "@{upstream}"], root, timeout=90)
    while code != 0 and rebasing(root):
        clash = [f for f in conflicted(root) if not settle(root, f)]
        if clash:
            run(["git", "rebase", "--abort"], root)
            return False, "not synced: changed here and on another computer: %s. Keep one version by hand, then Update again" % ", ".join(clash)
        code, out = run(["git", "-c", "core.editor=true", "rebase", "--continue"], root, timeout=90)
        if code != 0 and rebasing(root) and not conflicted(root):
            code, out = run(["git", "-c", "core.editor=true", "rebase", "--skip"], root, timeout=90)     # nothing of ours was left
    if code != 0:
        if rebasing(root):
            run(["git", "rebase", "--abort"], root)
        return False, "not synced: %s" % (out.split("\n")[-1][:120] or "git could not combine the changes")
    _, after = run(["git", "rev-parse", "HEAD"], root)
    count = behind.strip()
    return after != before, "took in %s change%s from the other computers" % (count, "" if count == "1" else "s")


def rebuild(root):
    """Writes the generated files again from the rules and keeps the installed copies current."""
    errors, outputs, _rules = build.run(root)
    if errors:
        return "the merged rules do not build: %s" % errors[0]
    build.refresh_installed(outputs, root)
    return commit(root, "Rebuilt after taking in the other computers' changes")


def follow(root=build.ROOT):
    """Pull, rebuild, refresh the installed copies. For a computer that only follows the map
    (`python sync.py --follow` from cron) and for the page when it starts."""
    changed, line = pull(root)
    if changed:
        problem = rebuild(root)
        if problem:
            return line + "; " + problem
    return line


def update(root=build.ROOT, scan=secret_scan):
    """Everything the Update button does without the page: pull, read this computer's new chats for
    corrections, rebuild, save. For a computer without a browser (`python sync.py --update` from cron)."""
    import learn
    pulled = follow(root)
    added = learn.scan(root)
    learn.review(root)
    errors, outputs, _rules = build.run(root)
    if errors:
        return "%s; %d new correction(s); not rebuilt: %s" % (pulled, added, errors[0])
    build.refresh_installed(outputs, root)
    return "%s; %d new correction(s); %s" % (pulled, added, save(root, scan))


def save(root=build.ROOT, scan=secret_scan):
    """Commits, takes in the other computers' changes, pushes. Returns one short line saying what happened, for the page."""
    if not is_repo(root):
        return "not saved to GitHub: this folder is not a git repo"
    failed = commit(root)
    if failed:
        return "not saved to GitHub: " + failed
    changed, line = pull(root)
    if line.startswith("not synced: changed here"):
        return "saved on this computer, not pushed: " + line[len("not synced: "):]
    if changed:
        problem = rebuild(root)
        if problem:
            return "saved on this computer, not pushed: " + problem
    code, ahead = run(["git", "rev-list", "--count", "@{upstream}..HEAD"], root)
    if code != 0:
        return "saved on this computer only: the repo has no GitHub branch to push to"
    if ahead.strip() == "0":
        return "GitHub is up to date"
    found = scan(root)
    if found is None:
        return "saved on this computer, not pushed: the secret scan could not run (is Docker running?)"
    if found:
        return "saved on this computer, not pushed: the secret scan found %d possible secret%s" % (found, "" if found == 1 else "s")
    code, out = run(["git", "push", "-q"], root, timeout=90)
    if code != 0:
        return "saved on this computer, not pushed: %s" % (out.split("\n")[-1][:120] or "GitHub did not answer")
    return "pushed to GitHub"


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    args = sys.argv[1:]
    if "--follow" in args:
        print(follow())
    elif "--update" in args:
        print(update())
    else:
        print(save())
