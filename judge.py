#!/usr/bin/env python3
# Behaviour map, https://github.com/mos-ks/behavior-map. Copyright (c) 2026 Dr. Moussa Kassem Sbeyti. MIT licence, see LICENSE.
"""Lets the owner's local model sort the corrections that learn.py found. Optional: without it nothing changes.

    python judge.py           # say whether the model answers, and which one it is

The model runs on the owner's own machine behind an OpenAI-compatible address (vLLM). The address, the key and
the model name are read at run time from the OpenCode settings (~/.config/opencode/opencode.json), so nothing
about them is stored here. A file `signals/judge.json` may override them: {"base_url": ..., "model": ..., "api_key": ...}.

For each correction the model is given the sentence and the few rules closest to it, and answers three things:
is this a standing instruction or just talk about the task at hand, which of those rules it belongs to (or none),
and, when it is a new standing instruction, the rule in one sentence. It never sees anything but the owner's
own sentence and the rule texts, and it cannot name a rule that was not offered.

Python 3.9+, standard library only.
"""
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.request

import build
import learn

OPENCODE = os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.json")
CANDIDATES = 8            # how many of the closest rules the model may choose from
AT_ONCE = 4               # requests sent side by side; the model serves the owner's other work too
ASK_TIMEOUT, PING_TIMEOUT = 90, 3

SYSTEM = """You sort short messages that one person typed to an AI coding assistant.
Decide whether the message states a STANDING instruction: how the assistant should work, write, behave or
decide in future sessions too (for example "never add a co-author line", "stop asking me before you push").
A message that is only about the task at hand (what to build, fix or change now, a bug report, an answer to a
question, approval, small talk) is NOT a standing instruction, even when it contains "dont", "never" or "again".
If it is a standing instruction, pick the ONE listed rule that already says the same thing, or "none" when no
listed rule covers it. For "none", write the instruction as one short imperative sentence in plain English
without dashes; otherwise leave that field empty. Answer with JSON only."""

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["standing", "rule", "new_rule"],
    "properties": {"standing": {"type": "boolean"}, "rule": {"type": "string"}, "new_rule": {"type": "string"}},
}


def settings(root=build.ROOT, opencode=OPENCODE):
    """{"base_url", "model", "api_key"} or None when no local model is configured."""
    found = {}
    try:
        text = re.sub(r"(^|\s)//.*$", "", build.read_text(opencode), flags=re.M)     # comments, but not the // of an address
        for provider in (json.loads(text).get("provider") or {}).values():
            options = provider.get("options") or {}
            if "openai-compatible" in str(provider.get("npm")) and options.get("baseURL"):
                key, model = next(iter((provider.get("models") or {}).items()), (None, {}))
                found = {"base_url": options["baseURL"], "api_key": options.get("apiKey") or "", "model": model.get("id") or key}
                break
    except (OSError, ValueError, AttributeError):
        pass
    override = learn.signal_path(root, "judge.json")
    if os.path.isfile(override):
        try:
            found.update(json.loads(build.read_text(override)))
        except ValueError:
            pass
    return found if found.get("base_url") and found.get("model") else None


def call(cfg, path, body=None, timeout=ASK_TIMEOUT):
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    req = urllib.request.Request(cfg["base_url"].rstrip("/") + path, headers=headers,
                                 data=json.dumps(body).encode("utf-8") if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def available(cfg):
    """True when the model server answers and serves the configured model."""
    if not cfg:
        return False
    try:
        models = call(cfg, "/models", timeout=PING_TIMEOUT).get("data") or []
    except (OSError, ValueError):
        return False
    return any(m.get("id") == cfg["model"] for m in models)


def ask(cfg, text, offered):
    """The model's verdict on one correction: {"standing", "rule" (an offered id or None), "new_rule"}.
    Raises OSError or ValueError when the model cannot be reached or answers with something unusable."""
    listing = "\n".join("%s: %s" % (r["id"], r.get("short") or r["rule"]) for r in offered)
    body = {
        "model": cfg["model"], "temperature": 0, "max_tokens": 300,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": "Message:\n%s\n\nRules to choose from:\n%s\nnone: no listed rule covers it" % (text, listing)}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "verdict", "schema": SCHEMA}},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    content = call(cfg, "/chat/completions", body)["choices"][0]["message"].get("content") or ""
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("the model did not answer with JSON")
    verdict = json.loads(m.group(0))
    rule = verdict.get("rule") if verdict.get("rule") in {r["id"] for r in offered} else None
    standing = verdict.get("standing") is True
    return {"standing": standing, "rule": rule if standing else None,
            "new_rule": learn.one_line(verdict.get("new_rule")) if standing and not rule else ""}


def sort_signals(root=build.ROOT, cfg=None, asker=ask, reachable=available):
    """Has the model judge every correction it has not judged yet. Returns {"on", "model", "judged", "note"}.
    A correction the model calls task talk is set aside, one it places on a rule is attached (learn.review then
    counts it), and a new standing instruction stays in the inbox with the model's wording as a proposal."""
    cfg = cfg if cfg is not None else settings(root)
    if not cfg:
        return {"on": False, "model": "", "judged": 0, "note": "no local model is set up"}
    if not reachable(cfg):
        return {"on": False, "model": cfg["model"], "judged": 0, "note": "the local model did not answer"}
    sigs = learn.load_signals(root)
    todo = [s for s in sigs if not s.get("judged") and s["status"] in ("open", "waiting")]
    rules, _ = build.load_rules(root)
    lk = learn.rule_likeness(rules)

    def judge_one(sig):
        closest = [rules[i] for i, _score in lk.best(sig["text"], top=CANDIDATES)]
        return asker(cfg, sig["text"], closest)

    judged, failed = 0, ""
    with concurrent.futures.ThreadPoolExecutor(max_workers=AT_ONCE) as pool:
        for sig, result in zip(todo, pool.map(lambda s: attempt(judge_one, s), todo)):
            if isinstance(result, Exception):
                failed = str(result)
                continue
            sig["judged"], judged = True, judged + 1
            if not result["standing"]:
                sig["status"], sig["rule"] = "set aside", None
            elif result["rule"]:
                sig["status"], sig["rule"] = "attached", result["rule"]
            else:
                sig["status"], sig["rule"], sig["proposal"] = "open", None, result["new_rule"]
    if judged:
        learn.save_signals(sigs, root)
    note = "the local model stopped answering (%s)" % failed[:80] if failed else ""
    return {"on": True, "model": cfg["model"], "judged": judged, "note": note}


def attempt(fn, arg):
    try:
        return fn(arg)
    except (OSError, ValueError, KeyError, IndexError) as e:
        return e


if __name__ == "__main__":
    cfg = settings()
    if not cfg:
        print("No local model is set up (looked in %s)." % OPENCODE)
        sys.exit(1)
    print("%s: %s" % (cfg["model"], "answers" if available(cfg) else "does not answer right now"))
