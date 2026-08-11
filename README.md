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
alias aiattr='python3 "$(ls -d ~/.claude/plugins/cache/ai-attribution-marketplace/ai-attribution/*/ | sort -V | tail -1)cli/aiattr.py"'
```

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
  `human`. The plugin cannot tell it apart from code you typed.
- **Work with Claude closed is invisible.** Nothing observes it, so nothing is
  recorded. It shows up as drift the next time a session starts.
- **Every folder counts, including ones you did not mean.** Same fallback
  wakatime-cli uses. Open a scratch directory and it becomes a project named
  after that directory. Opt out of anything you would rather it left alone.
- **It only knows about agents it is installed for.** This one tracks Claude
  Code. Declare whichever agent you actually use when you enrol.

---

## Uninstalling

```bash
claude plugin uninstall ai-attribution@ai-attribution-marketplace
```

Tracking data in `~/.claude/ai-attribution/` deliberately survives an uninstall,
so that reinstalling does not wipe your history. Delete that folder yourself if
you want it gone.
