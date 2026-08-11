# AI Attribution for VS Code

Records how much of your code you typed and how much appeared, and reports the
split to [Hackatime](https://hackatime.hackclub.com) on your normal heartbeats.

---

## Read this before installing it

**This is not an agent hook, and for most agents a hook is the better tool.**

An agent hook runs inside the agent, fires on its tool calls, and therefore
*knows* the author: "this agent's edit tool wrote these 40 lines." Cursor,
Windsurf, Antigravity, Kiro, Qoder, Codex, Gemini CLI, Qwen Code, Copilot,
Cline, Devin and Goose all support one — see `core/adapters.py`, which
generates the config for each. **If your agent is on that list, install the
hook and skip this.**

This extension runs in the editor instead and answers a narrower question:

> was this text typed, or did it appear whole?

Typing arrives one character at a time because that is how keyboards work. A
generated block materialises. So `human_line_changes` means keystrokes were
observed and `ai_line_changes` means they were not — which covers an agent
writing a file *and* a paste from a browser, with no way to tell them apart.

## So what is it actually for

**Pasted code.** No agent hook can see a paste, because no agent acted. A
student who copies AI output from a browser into their editor is invisible to
every hook in `adapters.py` and visible here. That is the one gap hooks
structurally cannot cover.

**Agents with no hook surface.** Trae, Roo Code and Cody have no shell-command
hook at all (verified against their docs, not inferred from silence). For those,
this is the only observation available.

**Belt and braces.** It runs alongside a hook without double-counting: buckets
holding only typed lines are never sent, and both producers tag heartbeats
`ai coding`, so neither adds to your paid hours.

It reports itself under the real editor's name, so a Cursor heartbeat says
`cursor` rather than pretending to be plain VS Code. Cursor, Windsurf, Trae,
Antigravity, Kiro, Qoder, VSCodium and Positron all run it, since
they are all VS Code forks.

## Install

Not on a marketplace yet. Build it:

```bash
cd vscode && npm install && npm run compile
```

Then press `F5` in VS Code to launch a window with it loaded, or package it
with `npx vsce package` and install the `.vsix`.

Turn it on in Settings (it is off until you do):

```json
{ "aiAttribution.enabled": true }
```

It reads your Hackatime key from `~/.wakatime.cfg`, so if the WakaTime
extension already works there is nothing else to configure.

Run **AI Attribution: Show Status** from the command palette to check what it
resolved.

## It does not add to your hours

Heartbeats go out tagged `ai coding`, a category Hackatime already excludes from
time totals when a caller passes `no_ai_coding=true`.

Two reasons that matters. Your editor's own WakaTime extension is already
reporting these same minutes as `coding`, so a second row claiming them would be
double-counting. And a program paying by the hour should not pay for time an
agent spent writing while you were away.

Buckets containing only typed lines are never sent at all, for the same reason:
your editor already reported that work.

## What counts as a project

Same rule as wakatime-cli, so this and your editor never disagree about a name:

1. a `.wakatime-project` file, nearest one walking up (line 1 the project, line
   2 the branch)
2. a git repository, named for its folder
3. otherwise the folder itself

One deliberate exception: your home directory and its top-level folders
(`Desktop`, `Downloads`, `Documents`, …) are refused at step 3. wakatime-cli
allows them, but it records that a folder existed while this reads file
contents, and a home directory is not a project anyone meant to track.

## What leaves your machine

Line counts, repo-relative file paths, and a project name. No source text, ever.

## Limits

- **It cannot tell paste from generation.** Both are "appeared".
- **It only sees files open in the editor.** An agent writing to a file you do
  not have open produces no event.
- **Fast typing is still typing.** The thresholds sit well above any plausible
  keystroke, because wrongly calling typed code "appeared" is the failure that
  accuses an honest person.
- **Undo and redo are ignored**, so neither bucket can be inflated by mashing
  ctrl-Z.

## Development

```bash
npm run compile && node out/test.js
```

31 assertions, no framework. The classifier is the only place this can be wrong
in a way that accuses somebody, so most of them are cases that must *not* read
as "appeared".
