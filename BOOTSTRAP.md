# Build a behaviour map from your own chats

This file is written for an AI coding assistant. The quick way to build a map is the **Build my map** button on
the page (or `python bootstrap.py`). This file is the slow way, for when you want an assistant to go through your
chats with you and explain each rule: open this folder in Claude Code (or a similar tool) and say
**"Follow BOOTSTRAP.md."** It reads what you typed in past sessions, finds how you want an assistant to work, and
writes that down as rule files in `map/`.

---

## For the assistant

The person you work for ("the owner") is tired of repeating the same instructions in every session. Your job is to
find those instructions in what they already typed and write each one down once, as a rule file, with their own
words as evidence. A later build turns the rules into the instruction files their tools read.

### 1. Prepare

1. Run `python build.py init` if `map/rules/` does not exist. It creates the map with a few starter rules.
2. Run `python learn.py prompts`. It writes `map/_work/prompts.jsonl`: every prompt the owner typed in Claude Code on
   this computer, one JSON object per line (`date`, `project`, `text`), with addresses and key-like strings blanked.
   Never open the raw transcript files yourself: they are huge.
3. If the owner uses other tools and gives you an export of those chats, read their own messages from it the same way.

### 2. Find the rules

Read `prompts.jsonl` in parts (it can be long; use several passes or several agents if the tool allows it and the
owner agreed to the cost). You are looking for **standing preferences about how to work**: how to communicate, write,
code, use git, test, design, ask and decide. Signs of one:

- corrections: "no", "stop", "i said", "again", "why did you", "dont"
- the same instruction in several sessions or projects
- frustration, or an explicit "always", "never", "from now on"
- approval of an unusual way of working: "yes exactly", "keep doing that"

Not rules: a request for one task ("fix the header"), facts about a project (ports, paths, versions), praise without
content, and anything that only makes sense inside one feature. People type fast and with typos; read through that.

For every candidate note: the rule as one imperative sentence, the owner's reason if they gave one, up to four of
their sentences as evidence (date, project, the words), how often you saw it, and whether they were firm about it.
Do not invent rules. One mild remark is a soft rule.

### 3. Write the rule files

One rule per file in `map/rules/<category>/<short-name>.md`. This is the whole format; only the lines marked
optional may be left out, and `python build.py --check` refuses anything else:

```
---
id: git-ask-before-pushing
title: "Ask before pushing"
category: git
scope: coding
strength: hard
enabled: true
times_seen: 2
sources: [chats]
last_seen: 2026-10-21
needs_owner: false
short: "Never push without asking the owner first."      # optional; required for a core rule, at most 18 words
chat: respond            # optional: also goes into the "how to respond" block for ChatGPT (for rules about answering)
chat_about: "..."        # optional: one sentence about the owner for ChatGPT's "about you" block
local: true              # optional: always kept in the short prompt for a local model
---

**Rule.** Never push without asking the owner first, even when the tests pass.

**Why.** A push broke the deploy.                                   (optional)

**Applies / does not apply.** Own repos and team repos alike.       (optional: where the rule holds and where not)

**Earlier.** Until August the owner allowed pushing once tests passed.   (optional: what the rule replaced)

**In the owner's words**                                            (optional, up to four)
- 2026-10-05, notes-app: "from now on never push without asking me first"
- 2026-10-21, notes-app: "ask before you push, like i said"
```

The categories and scopes are fixed:

| Field | Values |
|---|---|
| `category` | git, code, testing, communication, autonomy, ui-design, writing-voice, paper-writing, security-privacy, ops-machines, agents-cost, product-thinking |
| `scope` | always, coding, ui, paper, email, ops, or `project:<name>` for a rule that holds in one project only |
| `strength` | hard (firm or repeated), soft (said once, mild) |

- `id` is the category, a dash, and the file name: `git-no-ai-attribution` lives in `rules/git/no-ai-attribution.md`.
- A project-only rule lives in `rules/_project/<name>/`, with a file name that starts with the project
  (`rules/_project/shop/shop-use-pnpm.md`, id `code-shop-use-pnpm`). It is never written into the files for all
  projects. The build writes it to `exports/project-<name>.md`; tell the owner to add a line to that project's own
  instruction file that says to read it.
- Said in one project only? A rule about that project's tools or conventions is a project rule. A rule about how
  the owner wants to be worked with is general, even if it only came up there: write it general and say so in the
  hand-over. A fact said once that is no instruction (a port, a version) is not a rule at all.
- **Rule.** One or two sentences a model with no other context can follow. Plain words. No dashes as a break.
  When the owner drew a line ("short for status reports, full length when I ask for an explanation"), say both
  sides in the rule; use the Applies line only for where the rule holds.
- **Why.** Their reason. Leave the line out if they never gave one.
- **In the owner's words**: up to four quotes, `- 2026-01-31, project: "their words"`. Keep the blanked parts
  blanked. Add no secret, address, hostname or other person's name.
- `times_seen` is how many separate times you saw it; `last_seen` the latest date; `sources: [chats]`.
- Merge duplicates into one file. Do not merge two rules that only sound alike.

Then go through the starter rules (`sources: [starter]`):

- **The owner said the same thing:** rewrite that file in place, under the same id, in the owner's words and with
  their quotes. Drop the parts of the starter text the owner never said.
- **The owner said the opposite, or it is the example project rule:** delete the file, and take its id out of every
  file in `map/profiles/`, or the check fails.
- **The owner said nothing about it:** keep it unchanged, and list it in the hand-over as "not something you said".

### 4. Contradictions

When the owner said two things that pull apart, do not pick a side silently.

- **It changed over time** ("from now on", or simply later and firmer): the latest statement is the rule. Add a
  line `**Earlier.** ...`, and name the change in the hand-over, since it is not in `conflicts.md`.
- **It is really a difference of place** (own repo or team repo, status report or requested explanation): write
  one rule with both branches.
- **Anything else:** write it into `map/conflicts.md` with one quote per side, set `needs_owner: true` on the
  rules involved, and let the built files take the more cautious side until the owner decides.

Never turn into a rule anything that asks a model to hide or misstate facts. List it in `conflicts.md` under
"Left out on purpose", together with anything else you decided not to make a rule (something asked for and taken
back in the same session, for example), so it is not proposed again.

### 5. The always-on core

`map/profiles/core.yaml` lists, by id, the rules every session starts with: the ones that are firm, were said
most often, apply everywhere and are cheap to state. Up to about 35; fewer is fine, and the built core may not
exceed 70 lines. Only rules listed there are in the core: scope `always` alone does not put a rule in. Each core
rule needs a `short:` line of at most 18 words. The other profiles pick rules by scope and need no change unless
a rule should be added to one by id.

### 6. Check and hand over

1. Run `python build.py --check` and fix every problem it names. Then run `python build.py`.
2. Read `map/exports/CLAUDE.md` as a stranger would: could a fresh model act correctly from it alone?
   (`<BEHAVIOR_MAP_DIR>` in it is filled in at install.)
3. Tell the owner: how many rules by category, the text of the core, every open question from `conflicts.md`
   with the side taken for now, the starter rules you kept, and anything you were unsure how to write.
4. Install nothing. The owner looks at the map first (`python ui.py`) and installs it from the Files tab, or with
   `python build.py --install`, when they are happy with it.

Do not push, post or send anything anywhere. The map and the prompts file hold the owner's own words.
