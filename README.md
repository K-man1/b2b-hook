# AI attribution for Back to Basics

A Claude Code plugin that records how much of your code you wrote and how much
an AI agent wrote, line by line, as it happens.

It is not a cheating detector and it does not gate your submission. Back to
Basics pays for hours, and AI-assisted work is normal work. The point is that
the number describing your project comes from recorded file diffs instead of
from someone's guess.

If you already track time with [Hackatime](https://hackatime.hackclub.com), the
split is reported straight onto your existing heartbeats, so your hours and your
AI percentage live in one place.

---

## Install

**1. Add the marketplace.**

```bash
claude plugin marketplace add https://github.com/K-man1/b2b-hook.git
```

**2. Install it for every project, not just this one.**

```bash
claude plugin install ai-attribution@ai-attribution-marketplace --scope user
```

**3. Make the CLI easy to reach.** The plugin lives inside Claude Code's cache,
so give yourself a shortcut. Add this to `~/.zshrc` (or `~/.bashrc`):

```bash
alias aiattr='$(command -v python3 || command -v python) "$(ls -d ~/.claude/plugins/cache/ai-attribution-marketplace/ai-attribution/*/ | sort -V | tail -1)cli/aiattr.py"'
```

(`command -v python3 || command -v python` rather than a plain `python3`
because on plenty of machines — Windows especially, where `python3` is often
the Microsoft Store stub — the working Python 3 is called `python`.)

**4. Connect it to your account.** Your Back to Basics dashboard shows a
`configure` command with your key already filled in. It looks like this:

```bash
aiattr configure --key YOUR_KEY --endpoint https://back-to-basics-cyan.vercel.app
```

**5. Report the AI split to Hackatime too.** Optional, and recommended. It reads
the key already in your `~/.wakatime.cfg`, so there is nothing else to set up.

```bash
aiattr configure --enable-hackatime
```

**6. Restart Claude Code**, then check it took:

```bash
aiattr status
```

You are looking for `reporting: on`, and `hackatime: on` if you did step 5.

That is the whole setup. Work normally from here.

### Staying up to date

Step 4 also switches on Claude Code's auto-update for this marketplace, so you
get new versions without doing anything. That is not the default: Claude Code
auto-updates its own marketplaces, but third-party ones like this one ship with
it off, and a student who never turns it on keeps running whatever version they
first installed. Tracking that is months out of date does not announce itself —
it just quietly reports the wrong number — so `configure` sets the flag rather
than trusting anybody to remember.

If you skipped `configure`, or want to check, `/plugin` → **Marketplaces** →
this marketplace shows the auto-update toggle.

---

## Not using Claude Code?

Everything above goes through `claude plugin install`, which only exists if you
have Claude Code. For Cursor, Codex, Gemini CLI, Cline and the rest, use the
standalone installer instead — it needs nothing but `curl` and Python 3:

```bash
curl -fsSL https://raw.githubusercontent.com/K-man1/b2b-hook/main/install.sh | sh -s -- --key YOUR_KEY --endpoint https://b2b.hackclub.app --tool cursor
```

That one command is the whole setup: it downloads the plugin to
`~/.ai-attribution/plugin`, connects it to your account, and wires up your
app's hooks **for every project on the machine** — same as Hackatime, set up
once and forget it. Your dashboard shows this line with your key already
filled in, so you never copy the key by hand.

Run `~/.ai-attribution/bin/aiattr install-hooks list` to see the supported
names, and re-run the `curl` line any time to update.

### The two exceptions

Machine-wide needs a user-level hook location, and two tools do not have a
usable one. For these, run this inside each project folder you build in:

```bash
~/.ai-attribution/bin/aiattr install-hooks kiro
```

| Tool | Why |
|---|---|
| Kiro | `~/.kiro/hooks/` exists, but only in the v3 CLI (early access); the IDE does not read it yet ([Kiro#9075](https://github.com/kirodotdev/Kiro/issues/9075)) |
| GitHub Copilot (repo agent) | GitHub documents hooks for the CLI and the cloud agent, not for VS Code. The cloud agent runs in a throwaway clone that cannot reach machine-wide settings, so its hooks belong in the repo. Using Copilot in the terminal? Pick `github-copilot-cli`, which installs once. |

`install-hooks` refuses to run these from your home directory rather than
writing a config no tool will ever read.

Cline is a special case that still installs machine-wide: its docs say global
hooks live in `~/Documents/Cline/Rules/Hooks/` while its runtime reads
`~/Cline/Hooks/` ([cline#9994](https://github.com/cline/cline/issues/9994), open),
so both get written. They are three-line scripts; picking one and being wrong
would cost you every record silently.

opencode is the other special case, and it also installs once for every
project. It has no hook config file at all — its hooks are a JavaScript
module — so `install-hooks opencode` copies one into
`~/.config/opencode/plugins/aiattr.js` instead of writing a config entry. The
file it copies is [assets/opencode/aiattr.js](assets/opencode/aiattr.js), and
it is a shim: it hands each edit to the same `hooks/agent_hook.py` every other
tool calls. **Restart opencode afterwards** — plugins are loaded once, at
startup, so until you do, nothing is recorded.

---

## Your AI usage label

Your project page shows one of these:

| Label | Meaning |
|---|---|
| **Low** | 20% or less of your tracked code was written by an agent |
| **Moderate** | somewhere in between |
| **High** | 60% or more was written by an agent |
| **Not enough tracked** | too little of the project was watched to say |

**It is measured against code the plugin actually saw**, not against every line
in the folder. Files that were already there before you installed it are not
counted on either side, because nobody knows who wrote them. That is also what
"Not enough tracked" means: rather than guess from a handful of lines, it says
so.

None of these labels block a submission. AI-assisted work is normal work, and
reviewers see the underlying numbers either way.

---

## What leaves your machine

Counts and file paths. That is all.

Your source code, your prompts, and Claude's replies never leave. File snapshots
are what make per-line attribution work, and they are stored locally in
`~/.claude/ai-attribution/` and never uploaded. If everything this plugin ever
sent were made public tomorrow, it would reveal which repos you worked in and
how much of each was AI-written, and nothing else.

Agent heartbeats sent to Hackatime are tagged `ai coding`, which is a category
Hackatime already excludes from paid time totals on request. Reporting that
Claude wrote 40 lines does not earn you hours.

---

## What counts as a project

Whatever your editor already calls one. Project detection copies wakatime-cli's
order exactly, so this plugin and Hackatime never disagree about a name:

1. a `.wakatime-project` file, nearest one walking up. Line 1 is the project
   name, line 2 is the branch.
2. a git repository, named for its folder.
3. otherwise the folder itself, named for itself.

There is no step where you get told "this isn't a project." Every folder counts,
which is also why plain folders show up in your Hackatime project list.

Use a `.wakatime-project` when you want to pick the name yourself:

```bash
echo my-project-name > .wakatime-project
```

Starting a real project? `git init` first anyway. You get version control, and
`.gitignore` keeps build output and dependencies out of your line counts. Plain
folders fall back to a coarser built-in exclude list.

Either way, do it **before** you start writing. Code that already existed
baselines as `unobserved`, because nothing watched it arrive and the plugin will
not guess.

---

## Working on something private

The plugin is installed once and follows you into every folder you open, not
only repositories and not only coursework. Opt out of anything personal:

```bash
aiattr ignore ~/code/my-personal-thing
```

That stops future tracking and also deletes what was already recorded for that
repo: its index entry, its local ledger, and any undelivered heartbeats. Nothing
about it is reported afterwards.

---

## What this cannot do

Worth knowing up front, so the numbers are not oversold.

- **Pasted code counts as yours.** If you copy code from a browser into your
  editor, it changed the file while nothing was watching, so it lands in
  `human`. The plugin cannot tell it apart from code you typed. The same goes
  for an agent this plugin has no hook installed for. Anything that changes a
  file without a tool call is credited to you, because attributing it to you is
  the best estimate available once the agent's own tool calls are accounted
  for, not because anybody watched you write it.
- **Work with Claude closed is invisible.** Nothing observes it, so nothing is
  recorded. It shows up as drift the next time a session starts.
- **A reformat can move lines between buckets.** Attribution is matched on
  lines with their whitespace removed, so reindenting, restyling braces or
  changing line endings all keep a line's original author. What survives none
  of that is a rewrite that moves real tokens: a formatter that splits one long
  line into three produces lines nobody has written before, and no comparison
  of two file versions can tell that apart from someone authoring them.
- **Changes an agent makes through the terminal are attributed to the agent,
  but less precisely.** A `Bash` or MCP tool call that writes files is detected
  by comparing them against their snapshots afterwards, so the plugin knows the
  agent's tool call caused the change without having watched it happen. Those
  records are marked `via: tool_call` to say so. When a single tool call
  changes a lot of files at once, that is a checkout, a pull or an install
  rather than authorship, so it is recorded as a bulk change and credited to
  nobody.
- **Every folder counts, including ones you did not mean.** Same fallback
  wakatime-cli uses. Open a scratch directory and it becomes a project named
  after that directory. Opt out of anything you would rather it left alone.
- **It only knows about agents it is installed for.** This one tracks Claude
  Code. Declare whichever agent you actually use when you enrol.

---

## Releasing a new version

**Bump `version` in `.claude-plugin/plugin.json` in the same commit as the
change.** Claude Code caches the plugin in a directory keyed by that string and
only offers an update when it moves. Push ten commits without touching it and
every student, including you, keeps running the old code — the marketplace
refreshes, sees the version it already has, and installs nothing. Nothing warns
you. The hooks keep firing, they are just the previous version's hooks.

That is not hypothetical. It shipped: `post_bash.py` sat unreleased across ten
commits, so writes made through Bash went unattributed the whole time, and the
number those sessions produced was wrong with no sign that anything had failed.

To check what students are actually running, rather than what you pushed:

```bash
cat ~/.claude/plugins/cache/ai-attribution-marketplace/ai-attribution/*/.claude-plugin/plugin.json
```

---

## Uninstalling

```bash
claude plugin uninstall ai-attribution@ai-attribution-marketplace
```

Tracking data in `~/.claude/ai-attribution/` deliberately survives an uninstall,
so that reinstalling does not wipe your history. Delete that folder yourself if
you want it gone.
