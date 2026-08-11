# AI Attribution for VS Code

Records how much of your code you typed and how much appeared, and reports the
split to [Hackatime](https://hackatime.hackclub.com) on your normal heartbeats.

**One extension, ten editors.** Cursor, Windsurf, Trae, Antigravity, Kiro,
Qoder, VSCodium, Positron and code-server are all VS Code forks, so they all run
this. It reports itself under the real editor's name, so a Cursor heartbeat says
`cursor`.

---

## What it actually measures

**Whether text was typed, not who wrote it.**

These editors keep their agent internals closed, so nothing here can see which
assistant produced an edit, and it never claims to. What it can see is the shape
of a change: typing arrives one character at a time because that is how
keyboards work, while a generated block materialises whole.

- `human_line_changes` — keystrokes were observed
- `ai_line_changes` — lines appeared without them

**A paste from a browser and an agent writing a file look identical here**, and
both land in `ai_line_changes`. That is a real limit, not a rounding error. If
you show these numbers to anyone, say so.

The companion [Claude Code plugin](../README.md) is stronger where it applies:
it observes tool calls directly, so it knows the author rather than inferring
it. Run both if you use Claude Code — they agree on project names and neither
double-counts your time.

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
