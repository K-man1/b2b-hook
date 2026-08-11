"""Where each agent's hook config lives, and what shape it wants.

Verified against each tool's own docs (2026-08). Three groups, and the split
is the reason this module exists rather than one function per tool:

CLAUDE_SHAPED -- tools whose hook system independently converged on Claude
Code's own shape: stdin JSON, `{"hooks": {Event: [{matcher, hooks:
[{type, command}]}]}}`, the same field names (`tool_input.file_path`,
`session_id`, `hook_event_name`). Confirmed for Codex CLI, Gemini CLI, Qwen
Code, Copilot CLI, GitHub Copilot (VS Code agent mode), Devin CLI, and Qoder,
which documents itself as deliberately mirroring Claude Code. One generator
covers all seven; only the config path and which events exist differ.

CUSTOM -- real hook support, but a different shape: Cursor, Windsurf,
Antigravity, Kiro, and Goose (Goose keeps Claude Code's field names but nests
its config inside an "Open Plugins" plugin directory, not a flat file).
Each gets its own small builder.

SCRIPT -- Cline. Not a config entry at all: a hook is a single executable
file whose *name* is the event (e.g. `.clinerules/hooks/PostToolUse`), no
JSON wrapper.

Deliberately absent, and why, because the omission is easy to mistake for an
oversight otherwise:
  - Trae, Roo Code, Sourcegraph Cody: no shell-command hook exists. Verified
    against their own docs / issue trackers, not inferred from silence.
  - opencode, Amp: hooks exist but require real JS/TS plugin code, not a
    declarative "run this shell command" entry. install-hooks does not write
    code in a language this plugin has no other footprint in.
  - VSCodium: an editor, not an agent. An AI extension running inside it
    (Cline, Copilot, ...) is covered by that extension's own entry; the host
    adds no separate hook surface.

MATCHER ASSUMPTION, stated once here rather than in every builder: every
CLAUDE_SHAPED tool's docs describe an absent or empty `matcher` as "run for
every tool," but only Claude Code's own semantics are directly confirmed. An
over-broad matcher costs a few no-op invocations of agent_hook.py (it exits
quietly when there is no file to attribute); a matcher that is wrong in the
*narrow* direction costs the edit going unrecorded, silently. So every
generated config omits `matcher` rather than guessing a tool-specific tool
name, because the failure mode of "too broad" is the safe one.
"""

import os

# fields a Claude-Code-shaped hooks.json needs beyond the config path itself:
# which lifecycle events the tool actually documents, and where the file
# lives. `events` maps our verb (agent_hook.py's argv) to that tool's event
# name; a tool missing SessionStart/End simply omits those keys.
CLAUDE_SHAPED = {
    "codex": {
        "label": "Codex CLI",
        "config": (".codex/hooks.json", "hooks.json"),
        "events": {"session-start": "SessionStart", "pre-edit": "PreToolUse",
                   "edit": "PostToolUse", "session-end": "SessionEnd"},
    },
    "gemini-cli": {
        "label": "Gemini CLI",
        "config": (".gemini/settings.json", "settings.json"),
        "events": {"session-start": "SessionStart", "pre-edit": "BeforeTool",
                   "edit": "AfterTool", "session-end": "SessionEnd"},
    },
    "qwen-code": {
        "label": "Qwen Code",
        "config": (".qwen/settings.json", "settings.json"),
        "events": {"session-start": "SessionStart", "pre-edit": "PreToolUse",
                   "edit": "PostToolUse", "session-end": "SessionEnd"},
    },
    "github-copilot-cli": {
        "label": "GitHub Copilot CLI",
        "config": (".github/hooks/aiattr.json", "hooks.json"),
        "events": {"session-start": "sessionStart", "pre-edit": "preToolUse",
                   "edit": "postToolUse", "session-end": "sessionEnd"},
    },
    "github-copilot": {
        "label": "GitHub Copilot (VS Code agent mode)",
        "config": (".github/hooks/aiattr.json", "hooks.json"),
        "events": {"session-start": "SessionStart", "pre-edit": "PreToolUse",
                   "edit": "PostToolUse"},
    },
    "devin": {
        "label": "Devin CLI",
        "config": (".devin/hooks.v1.json", "hooks.v1.json"),
        "events": {"session-start": "SessionStart", "pre-edit": "PreToolUse",
                   "edit": "PostToolUse", "session-end": "SessionEnd"},
    },
    "qoder": {
        "label": "Qoder",
        "config": (".qoder/settings.json", "settings.json"),
        "events": {"session-start": "SessionStart", "pre-edit": "PreToolUse",
                   "edit": "PostToolUse", "session-end": "SessionEnd"},
    },
}


def _hook_cmd(plugin_root, verb, slug):
    """One shell command, identical shape to what hooks.json already runs for
    Claude Code, minus the ${CLAUDE_PLUGIN_ROOT} substitution only Claude Code
    performs -- these tools get the real absolute path baked in instead."""
    py_sh = os.path.join(plugin_root, "hooks", "py.sh")
    agent_hook = os.path.join(plugin_root, "hooks", "agent_hook.py")
    return 'bash "{}" "{}" {} --agent {}'.format(py_sh, agent_hook, verb, slug)


def build_claude_shaped(spec, plugin_root, slug):
    hooks = {}
    for verb, event_name in spec["events"].items():
        hooks[event_name] = [{"hooks": [{
            "type": "command",
            "command": _hook_cmd(plugin_root, verb, slug),
        }]}]
    return {"hooks": hooks}


def build_cursor(plugin_root, slug):
    return {"version": 1, "hooks": {
        "afterFileEdit": [{"command": _hook_cmd(plugin_root, "edit", slug)}],
        "sessionStart": [{"command": _hook_cmd(plugin_root, "session-start", slug)}],
        "sessionEnd": [{"command": _hook_cmd(plugin_root, "session-end", slug)}],
    }}


def build_windsurf(plugin_root, slug):
    return {"hooks": {
        "post_write_code": [{"command": _hook_cmd(plugin_root, "edit", slug)}],
    }}


def build_antigravity(plugin_root, slug):
    return {"hooks": {
        "PostToolUse": [{"type": "command",
                          "command": _hook_cmd(plugin_root, "edit", slug)}],
    }}


def build_kiro(plugin_root, slug):
    # Kiro's exact payload field names are not published beyond "session/file
    # context on stdin" -- agent_hook.py's STDIN_FILE_KEYS search covers the
    # candidates that would match every other tool's convention, but this one
    # config is best-effort. install-hooks says so out loud when it writes it.
    return {"schema": "v1", "trigger": {"event": "PostFileSave"},
            "action": {"type": "command",
                       "command": _hook_cmd(plugin_root, "edit", slug)}}


def build_goose(plugin_root, slug):
    return {"hooks": {
        "SessionStart": [{"command": _hook_cmd(plugin_root, "session-start", slug)}],
        "AfterFileEdit": [{"command": _hook_cmd(plugin_root, "edit", slug)}],
        "SessionEnd": [{"command": _hook_cmd(plugin_root, "session-end", slug)}],
    }}


CUSTOM = {
    "cursor": {"label": "Cursor", "config": (".cursor/hooks.json", None),
               "build": build_cursor},
    "windsurf": {"label": "Windsurf", "config": (".windsurf/hooks.json", None),
                 "build": build_windsurf},
    "antigravity": {"label": "Antigravity", "config": (".agents/hooks.json", None),
                    "build": build_antigravity},
    "kiro": {"label": "Kiro", "config": (".kiro/hooks/aiattr.json", None),
             "build": build_kiro, "unverified": True},
    "goose": {"label": "Goose",
              "config": (".agents/plugins/aiattr/hooks/hooks.json", None),
              "build": build_goose},
}

# Cline wants one executable file per event, named exactly the event, no JSON.
SCRIPT = {
    "cline": {
        "label": "Cline",
        "dir": ".clinerules/hooks",
        "events": {"PostToolUse": "edit", "SessionStart": "session-start",
                   "SessionEnd": "session-end"},
    },
}

UNSUPPORTED_LABELS = {
    "trae": "Trae", "roo-code": "Roo Code", "cody": "Cody",
    "opencode": "opencode", "amp": "Amp", "vscodium": "VSCodium",
}

UNSUPPORTED = {
    "trae": "No hook mechanism ships in Trae itself (an open feature request "
            "exists in bytedance/trae-agent, unimplemented). Best available "
            "hook: none; a VS Code extension using onDidSaveTextDocument "
            "would need writing, which install-hooks does not do.",
    "roo-code": "Hooks are an open, unmerged feature request. Roo Code keeps a "
                "shadow-git checkpoint commit per file change, which can be "
                "read after the fact instead of observed live.",
    "cody": "No hook mechanism found in current docs; Cody Individual is "
            "deprecated (Enterprise-only since 2025-07).",
    "opencode": "Hooks exist but are real TypeScript/JS plugin code "
                "(tool.execute.after), not a declarative shell-command entry. "
                "install-hooks does not generate plugin code.",
    "amp": "No documented declarative 'run this shell command' hook action; "
           "reaching a shell command requires writing a JS plugin.",
    "vscodium": "An editor, not an agent -- nothing writes code here on its "
                "own. Whatever AI extension runs inside it (Cline, Copilot, "
                "...) is covered by that extension's own adapter.",
}


def known():
    """Every agent slug this module has an opinion about, sorted."""
    return sorted(set(CLAUDE_SHAPED) | set(CUSTOM) | set(SCRIPT) | set(UNSUPPORTED))
