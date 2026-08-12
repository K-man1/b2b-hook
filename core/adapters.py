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

PLUGIN -- opencode. No declarative hook entry exists at all; its hooks are a
JavaScript module. So there is nothing to generate, and instead a plugin
file that lives in the repo (assets/opencode/aiattr.js) is copied into place
with the absolute hook paths substituted in. Shipping that file rather than
generating it is the point: generated code is code nobody reads, and this is
the only adapter whose payload is a program.

Deliberately absent, and why, because the omission is easy to mistake for an
oversight otherwise:
  - Trae, Roo Code, Sourcegraph Cody: no shell-command hook exists. Verified
    against their own docs / issue trackers, not inferred from silence.
  - Amp: hooks exist but reaching a shell command requires writing a JS
    plugin, and unlike opencode its plugin API is not documented well enough
    to ship one against.
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

import json
import os

# SCOPE. `user` is a path relative to $HOME that the tool reads for EVERY
# project; `config` is the per-repo path. Installing to `user` is what makes
# this behave like Hackatime -- set it up once and every repo is covered --
# so install-hooks writes there by default and only falls back to the repo
# path for tools that have no user-level location.
#
# Every `user` path below was verified against that tool's own docs (2026-08).
# A wrong path here is the worst possible bug: the file writes successfully,
# the tool never reads it, and the student's edits go unrecorded with no error
# anywhere. So a tool whose user-level path could not be confirmed gets no
# `user` key at all rather than a plausible guess -- per-repo and working
# beats machine-wide and silent.

# fields a Claude-Code-shaped hooks.json needs beyond the config path itself:
# which lifecycle events the tool actually documents, and where the file
# lives. `events` maps our verb (agent_hook.py's argv) to that tool's event
# name; a tool missing SessionStart/End simply omits those keys.
CLAUDE_SHAPED = {
    "codex": {
        "label": "Codex CLI",
        "config": (".codex/hooks.json", "hooks.json"),
        "user": ".codex/hooks.json",
        # Codex ignores hooks entirely unless this is switched on in
        # config.toml. See enable_codex_hooks() -- without it the config below
        # is valid, written, and dead.
        "feature_flag": ("codex_hooks", "features"),
        "events": {"session-start": "SessionStart", "pre-edit": "PreToolUse",
                   "edit": "PostToolUse", "session-end": "SessionEnd"},
    },
    "gemini-cli": {
        "label": "Gemini CLI",
        "config": (".gemini/settings.json", "settings.json"),
        "user": ".gemini/settings.json",
        "events": {"session-start": "SessionStart", "pre-edit": "BeforeTool",
                   "edit": "AfterTool", "session-end": "SessionEnd"},
    },
    "qwen-code": {
        "label": "Qwen Code",
        "config": (".qwen/settings.json", "settings.json"),
        "user": ".qwen/settings.json",
        "events": {"session-start": "SessionStart", "pre-edit": "PreToolUse",
                   "edit": "PostToolUse", "session-end": "SessionEnd"},
    },
    "github-copilot-cli": {
        "label": "GitHub Copilot CLI",
        "config": (".github/hooks/aiattr.json", "hooks.json"),
        "user": ".copilot/hooks/aiattr.json",
        "events": {"session-start": "sessionStart", "pre-edit": "preToolUse",
                   "edit": "postToolUse", "session-end": "sessionEnd"},
    },
    "github-copilot": {
        # Genuinely repo-only, and not for the reason the label suggests:
        # GitHub documents hooks for Copilot CLI and the Copilot *cloud agent*
        # and does not list VS Code at all. The cloud agent runs in an
        # ephemeral clone where user-level hooks are unreachable by design, so
        # .github/hooks/ committed to the repo is the only thing it can read.
        "label": "GitHub Copilot (repo agent)",
        "config": (".github/hooks/aiattr.json", "hooks.json"),
        "events": {"session-start": "SessionStart", "pre-edit": "PreToolUse",
                   "edit": "PostToolUse"},
    },
    "devin": {
        "label": "Devin CLI",
        "config": (".devin/hooks.v1.json", "hooks.v1.json"),
        "user": ".config/devin/config.json",
        # Devin is the one tool whose two scopes want different shapes: in
        # .devin/hooks.v1.json the hooks object IS the whole file, while every
        # other location it reads (including the user-level config.json this
        # now writes) nests it under a "hooks" key.
        "project_unwrapped": True,
        "events": {"session-start": "SessionStart", "pre-edit": "PreToolUse",
                   "edit": "PostToolUse", "session-end": "SessionEnd"},
    },
    "qoder": {
        "label": "Qoder",
        "config": (".qoder/settings.json", "settings.json"),
        "user": ".qoder/settings.json",
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
               "user": ".cursor/hooks.json", "build": build_cursor},
    "windsurf": {"label": "Windsurf", "config": (".windsurf/hooks.json", None),
                 "user": ".codeium/windsurf/hooks.json",
                 "build": build_windsurf},
    "antigravity": {"label": "Antigravity", "config": (".agents/hooks.json", None),
                    "user": ".gemini/config/hooks.json",
                    "build": build_antigravity},
    # Kiro does have ~/.kiro/hooks/, but only in CLI v3 (early access) -- the
    # IDE, which is how most people use Kiro, has an open bug for exactly this
    # (kirodotdev/Kiro#9075: user-level hooks not discovered). Installing there
    # by default would look machine-wide and record nothing for IDE users, so
    # it stays per-project until the IDE reads it too.
    "kiro": {"label": "Kiro", "config": (".kiro/hooks/aiattr.json", None),
             "build": build_kiro, "unverified": True},
    # Open Plugins layout: any plugin directory containing hooks/hooks.json is
    # auto-discovered, at ~/.agents/plugins/<name>/ for user scope and
    # <project>/.agents/plugins/<name>/ for project scope.
    "goose": {"label": "Goose",
              "config": (".agents/plugins/aiattr/hooks/hooks.json", None),
              "user": ".agents/plugins/aiattr/hooks/hooks.json",
              "build": build_goose},
}

# Cline wants one executable file per event, named exactly the event, no JSON.
#
# `user_dirs` is a list, and that is not hedging -- Cline's own sources
# disagree about where global hooks live, so there is no single path to pick:
#
#   ~/Documents/Cline/Rules/Hooks/  what the docs and the v3.36 release post say
#   ~/Cline/Hooks/                  what the runtime actually reads, per
#                                   cline/cline#9994 (open: the management UI
#                                   writes ~/Documents/Cline/Hooks/ while the
#                                   runtime looks in ~/Cline/Hooks/, so global
#                                   hooks silently never fire)
#
# Each hook is a three-line shell script, so writing both costs nothing and
# means the install works whichever path a given build resolves. Picking one
# and being wrong costs a student their entire attribution history, silently.
SCRIPT = {
    "cline": {
        "label": "Cline",
        "dir": ".clinerules/hooks",
        "user_dirs": ["Documents/Cline/Rules/Hooks", "Cline/Hooks"],
        "events": {"PostToolUse": "edit", "SessionStart": "session-start",
                   "SessionEnd": "session-end"},
    },
}

# opencode. The whole adapter is a file rather than a dict, because its hook
# surface is a JS module and not a config entry -- see the PLUGIN note at the
# top. `asset` is relative to the plugin root so it survives the install being
# moved; `token` names the placeholders render_plugin_asset fills in.
#
# Paths confirmed against opencode's own plugin docs (2026-08): both
# directories are `plugins`, plural, and every .js/.ts file in them is loaded
# at startup with no registration step. Project-level plugins load after
# global ones, so a repo that installs its own copy simply runs a second time
# -- harmless, since agent_hook.py's records are keyed by path and content.
PLUGIN = {
    "opencode": {
        "label": "opencode",
        "asset": ("assets", "opencode", "aiattr.js"),
        "config": (".opencode/plugins/aiattr.js", None),
        "user": ".config/opencode/plugins/aiattr.js",
    },
}

UNSUPPORTED_LABELS = {
    "trae": "Trae", "roo-code": "Roo Code", "cody": "Cody",
    "amp": "Amp", "vscodium": "VSCodium",
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
    "amp": "No documented declarative 'run this shell command' hook action; "
           "reaching a shell command requires writing a JS plugin, and Amp's "
           "plugin API is not documented well enough to ship one against.",
    "vscodium": "An editor, not an agent -- nothing writes code here on its "
                "own. Whatever AI extension runs inside it (Cline, Copilot, "
                "...) is covered by that extension's own adapter.",
}


def spec_for(slug):
    """The adapter spec for a slug, or None if it has no hook support."""
    return (CLAUDE_SHAPED.get(slug) or CUSTOM.get(slug) or SCRIPT.get(slug)
            or PLUGIN.get(slug))


def render_plugin_asset(spec, plugin_root):
    """The plugin file's source with this install's real paths baked in.

    Substitution rather than string-building for the same reason the asset is
    a file rather than a generated string: the thing that runs on a student's
    machine should be a file someone can read, diff, and lint in this repo,
    not JavaScript assembled by a Python function.

    The paths are inserted via json.dumps, which is not decoration -- a JSON
    string literal is a JavaScript string literal, so a Windows install path
    full of backslashes survives instead of turning into escape sequences.
    """
    path = os.path.join(plugin_root, *spec["asset"])
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()

    replacements = {
        '"__AIATTR_PY_SH__"': os.path.join(plugin_root, "hooks", "py.sh"),
        '"__AIATTR_AGENT_HOOK__"': os.path.join(
            plugin_root, "hooks", "agent_hook.py"),
    }
    for token, value in replacements.items():
        if token not in source:
            # An asset that no longer carries the placeholder would install
            # cleanly and then run against a literal "__AIATTR_PY_SH__", doing
            # nothing forever. Loud here beats silent there.
            raise ValueError(
                "{} is missing the {} placeholder".format(path, token))
        source = source.replace(token, json.dumps(value))
    return source


def user_scope_path(spec):
    """Absolute path to this tool's user-level config, or None if it has none."""
    rel = spec.get("user")
    return os.path.join(os.path.expanduser("~"), rel) if rel else None


def user_scope_dirs(spec):
    """Absolute user-level hook directories for a SCRIPT tool (may be several).

    Returns [] when the tool has no user-level location. See SCRIPT's comment
    for why Cline needs more than one.
    """
    home = os.path.expanduser("~")
    return [os.path.join(home, rel) for rel in spec.get("user_dirs") or []]


def has_user_scope(spec):
    return bool(spec.get("user") or spec.get("user_dirs"))


def enable_codex_hooks(config_path):
    """Turn on Codex's `codex_hooks` feature flag in config.toml.

    Codex reads hooks.json only when this is set, so writing the hooks without
    it produces a setup that looks complete and records nothing. Edited as text
    rather than parsed: stdlib can read TOML (tomllib) but not write it, and
    round-tripping through a hand-rolled writer would reformat a file the
    student owns and may have their own settings in.

    Returns one of "already-on", "added", or "manual" -- the caller tells the
    student to do it by hand only in that last case.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except IOError:
        text = None

    if text is None:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write("[features]\ncodex_hooks = true\n")
        return "added"

    # Already mentioned anywhere: leave it alone. It may be deliberately off,
    # and flipping a student's own setting is not this command's business.
    if "codex_hooks" in text:
        return "already-on" if "codex_hooks = true" in text.replace(
            "codex_hooks=true", "codex_hooks = true") else "manual"

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "[features]":
            lines.insert(i + 1, "codex_hooks = true")
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            return "added"

    # No [features] table yet. Appending one is safe; a second [features]
    # header would not be, which is why the loop above ran first.
    with open(config_path, "a", encoding="utf-8") as fh:
        fh.write("\n[features]\ncodex_hooks = true\n" if text.endswith("\n")
                 else "\n\n[features]\ncodex_hooks = true\n")
    return "added"


def known():
    """Every agent slug this module has an opinion about, sorted."""
    return sorted(set(CLAUDE_SHAPED) | set(CUSTOM) | set(SCRIPT) | set(PLUGIN)
                  | set(UNSUPPORTED))
