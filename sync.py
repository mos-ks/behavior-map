#!/usr/bin/env python3
# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
"""Saves the behaviour map to its GitHub repo: commit what changed, scan for secrets, push.

    python sync.py            # the same thing the Update button does after reading the chats

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


def save(root=build.ROOT, scan=secret_scan):
    """Commits and pushes. Returns one short line saying what happened, for the page."""
    if not is_repo(root):
        return "not saved to GitHub: this folder is not a git repo"
    run(["git", "add", "-A"], root)
    code, staged = run(["git", "diff", "--cached", "--quiet"], root)
    if code == 1:
        message = "Update %s: %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), what_changed(root))
        code, out = run(["git", "commit", "-q", "-m", message], root)
        if code != 0:
            return "not saved to GitHub: the commit failed (%s)" % out.split("\n")[-1][:120]
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
    print(save())
