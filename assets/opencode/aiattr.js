// ai-attribution for opencode.
//
// Every other agent this plugin supports is wired up by writing a config file:
// a JSON entry naming a shell command. opencode has no such entry -- its hook
// surface is a JavaScript module -- so this file IS the config, and
// `aiattr install-hooks opencode` copies it into the plugin directory with the
// two paths below substituted in. See core/adapters.py, group PLUGIN.
//
// It is deliberately the thinnest shim that can work. It resolves a file path
// and shells out to hooks/agent_hook.py, the same entry point every other
// non-Claude agent drives. It decides nothing about what counts as an
// AI-written line, because a second definition of that living in JavaScript --
// drifting from the Python one nobody would notice for months -- is the exact
// failure this file is shaped to avoid.
//
// Two rules it inherits from every other hook here: never block the student's
// session for long, and never throw. A crash in a plugin hook is opencode's
// problem to report, and an attribution tool that interrupts the work it is
// measuring will be uninstalled by lunchtime. Every path below resolves.

import { spawn } from "node:child_process"

// Both placeholders are replaced with real absolute paths at install time,
// quotes and all (adapters.render_plugin_asset). They are written as quoted
// strings rather than bare tokens so this file stays valid, lintable
// JavaScript in the repo instead of only after substitution.
const PY_SH = "__AIATTR_PY_SH__"
const AGENT_HOOK = "__AIATTR_AGENT_HOOK__"
const AGENT = "opencode"

// Matches the timeouts hooks.json gives Claude Code for the same work. The
// session sweep walks every tracked file, so it gets the long one.
const EDIT_TIMEOUT_MS = 30_000
const SESSION_TIMEOUT_MS = 120_000

// Tools that write. Matched by NAME, which is a deliberate exception to this
// project's usual "when in doubt, fire too often" rule (see the MATCHER
// ASSUMPTION note in core/adapters.py). The usual rule holds where a spurious
// call is a no-op. Here it would not be: opencode's `read` tool carries a
// filePath too, so an args-based filter would fire a snapshot-and-diff pair on
// every file the agent so much as looks at. That is two Python starts per read
// for nothing, on the single most frequent tool call there is.
const WRITE_TOOLS = new Set(["edit", "write", "patch", "multiedit", "notebookedit"])

function isWrite(tool) {
  const name = String(tool || "").toLowerCase()
  if (WRITE_TOOLS.has(name)) return true
  // MCP tools arrive namespaced (`mcp__server__edit_file`), so match the verb
  // inside the name rather than the whole string. Anchored to word boundaries
  // so `todowrite` and `webfetch` stay out.
  return /(^|_)(edit|write|patch)(_|$)/.test(name)
}

// Where each tool keeps the path it is about to write. First match wins, same
// idea as agent_hook.py's STDIN_FILE_KEYS: one list covers the built-in tools
// and whatever an MCP server happens to call the field.
const PATH_KEYS = ["filePath", "file_path", "path", "notebookPath", "notebook_path"]

function filePathFrom(args) {
  if (!args || typeof args !== "object") return null
  for (const key of PATH_KEYS) {
    const value = args[key]
    if (typeof value === "string" && value) return value
  }
  return null
}

/**
 * Run one agent_hook.py verb to completion.
 *
 * Resolves rather than rejects, always, including when bash is missing
 * entirely (Windows without Git Bash). A student whose machine cannot run the
 * hook gets no records, which is a gap the instructor's verifier already reads
 * as a gap. A student whose session dies on every edit gets nothing at all.
 */
function run(verb, args, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false
    const finish = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve()
    }
    let child
    const timer = setTimeout(() => {
      try {
        child?.kill()
      } catch {}
      finish()
    }, timeoutMs)
    // Do not let a pending timer hold the process open at shutdown.
    timer.unref?.()

    try {
      child = spawn("bash", [PY_SH, AGENT_HOOK, verb, "--agent", AGENT, ...args], {
        stdio: "ignore",
      })
      child.on("error", finish)
      child.on("close", finish)
    } catch {
      finish()
    }
  })
}

export const AiAttribution = async ({ directory, worktree } = {}) => {
  // The repo root the records belong to. `worktree` is git's own answer, which
  // is what core/repoutil.py resolves anyway, so prefer it.
  const cwd = worktree || directory || process.cwd()

  // The session sweep catches everything written while no session was open --
  // hand edits, pastes, another editor. It has to finish before the first edit
  // hook runs, or that edit's before-image is compared against a stale
  // snapshot and lines the student typed get credited to the agent.
  //
  // Started here but not awaited here: awaiting would stall opencode's startup
  // behind a full repo walk. The edit hooks await it instead, so the ordering
  // that matters is guaranteed and the ordering that does not costs nothing.
  const ready = run("session-start", ["--cwd", cwd], SESSION_TIMEOUT_MS)

  // callID -> the path that call is writing. `tool.execute.after` carries
  // `args` in current opencode and did not in older ones, so the path is
  // remembered from the `before` hook rather than trusted to be there twice.
  const pending = new Map()

  const remember = (callID, file) => {
    if (!callID) return
    // A call that is denied at the permission prompt fires `before` and never
    // `after`, so entries can leak. Bounded rather than cleaned up precisely:
    // insertion order is the eviction order, and a live call is always recent.
    if (pending.size > 256) pending.delete(pending.keys().next().value)
    pending.set(callID, file)
  }

  const common = (input, file) => {
    const args = ["--file", file, "--cwd", cwd, "--tool", String(input.tool || "edit")]
    // Pairs pre-edit with edit exactly, instead of falling back to the file
    // path -- which agent_hook.py would otherwise do, and which collides when
    // two calls are in flight against one file.
    if (input.callID) args.push("--tool-use-id", String(input.callID))
    if (input.sessionID) args.push("--session-id", String(input.sessionID))
    return args
  }

  return {
    "tool.execute.before": async (input, output) => {
      if (!isWrite(input.tool)) return
      const file = filePathFrom(output?.args)
      if (!file) return
      remember(input.callID, file)
      await ready
      // Awaited, and this is the one await in the file that is load-bearing:
      // this captures the file as it stands BEFORE the write. Let opencode
      // race ahead and the before-image is the after-image, the diff is empty,
      // and the agent's work is recorded as nobody's.
      await run("pre-edit", common(input, file), EDIT_TIMEOUT_MS)
    },

    "tool.execute.after": async (input) => {
      if (!isWrite(input.tool)) return
      const file = filePathFrom(input?.args) || pending.get(input.callID)
      if (!file) return
      pending.delete(input.callID)
      await ready
      await run("edit", common(input, file), EDIT_TIMEOUT_MS)
    },

    // Rolls the session up into the totals the website renders. Not the only
    // thing that does: post_edit.py already recomputes them on its streaming
    // path, so a session that ends by having its process killed -- where
    // dispose never runs -- still reports real numbers, just without a closing
    // record. That is why this is not defended any harder than it is.
    dispose: async () => {
      await run("session-end", ["--cwd", cwd], SESSION_TIMEOUT_MS)
    },
  }
}
