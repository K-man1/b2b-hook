"""One entry point every non-Claude-Code agent can drive.

Claude Code's hooks hand us a JSON payload on stdin with a settled shape:
`cwd`, `session_id`, `tool_use_id`, `tool_name`, `tool_input.file_path`. No
other agent speaks that schema. What most of them can do is run a shell command
and pass a file path, either as an argument or in an environment variable.

So this is a translator and nothing else. It builds that same dict and calls
the existing hooks with it. Everything downstream (drift reconciliation, the
retag, the ledger chain, heartbeats) is the code that already shipped, which is
the point: a second agent must not get a second, subtly different definition of
what counts as an AI-written line.

    python3 agent_hook.py session-start --agent cursor
    python3 agent_hook.py pre-edit      --agent cursor --file src/main.py
    python3 agent_hook.py edit          --agent cursor --file src/main.py
    python3 agent_hook.py session-end   --agent cursor

Every field can come from argv, the environment, or stdin JSON, because which
of those an agent can populate is not our choice. Argv wins, then stdin, then
the environment: most specific to the invocation wins.

ON RUNNING `edit` WITHOUT `pre-edit`

Many agents only offer a post-write event. That is supported and degrades in a
known direction rather than breaking: with no captured before-image, post_edit
falls back to the stored snapshot, which is the file as this plugin last
observed it. If the student hand-edited between two agent writes, those manual
lines are inside the diff and get credited to the agent until the next
session-start sweep reconciles them. That over-credits the AI, which is the
safe direction for a tool whose failure mode must never be a false accusation
of a student -- but it is a real inaccuracy, so pair pre-edit with edit
wherever an agent makes it possible.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402
import post_edit  # noqa: E402
import pre_edit  # noqa: E402
import session  # noqa: E402
import session_end  # noqa: E402

# Environment fallbacks, in preference order per field. Agents that expose their
# state as variables rather than arguments are covered without a bespoke adapter
# each; the names are the ones the agents actually set, plus our own AIATTR_*
# which is always available to a wrapper script.
ENV_FILE = ("AIATTR_FILE", "CLAUDE_FILE_PATH", "CURSOR_FILE_PATH", "FILE_PATH")
ENV_SESSION = ("AIATTR_SESSION_ID", "CURSOR_SESSION_ID", "SESSION_ID")
ENV_CWD = ("AIATTR_CWD", "CURSOR_WORKSPACE_ROOT", "PWD")

EVENTS = {
    "session-start": session.main,
    "pre-edit": pre_edit.main,
    "edit": post_edit.main,
    "session-end": session_end.main,
}


def _first_env(names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def parse_argv(argv):
    """Flags only, no argparse.

    Deliberate: this runs on every agent write, and the import cost of argparse
    is paid on a path that is already competing with the student's keystrokes.
    Unknown flags are ignored rather than fatal, because an agent that grows a
    new variable should not start failing every hook it fires.
    """
    out = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("--") and i + 1 < len(argv):
            out[token[2:].replace("-", "_")] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


def _dig(obj, dotted):
    """Look up "a.b.0.c" through nested dicts and lists. Missing path -> None."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            i = int(part)
            cur = cur[i] if i < len(cur) else None
        else:
            return None
    return cur


# Where the edited file's path lives in each agent's own stdin JSON. Checked in
# order, first match wins. This list is why one parser covers a dozen tools
# instead of needing a bespoke one per agent: most of them independently
# reinvented Claude Code's own `tool_input.file_path` shape (confirmed for
# Codex, Gemini CLI, Qwen Code, Goose, Copilot CLI, Qoder), and the rest each
# picked one different top-level or one-level-nested key.
STDIN_FILE_KEYS = (
    "tool_input.file_path",   # Claude Code, Codex, Gemini CLI, Qwen Code, Qoder
    "file_path",              # Cursor, Goose, Copilot CLI (snake_case variant)
    "filePath",                # Copilot CLI (camelCase variant)
    "tool_info.file_path",     # Windsurf
    "toolCall.filePath",       # Antigravity
    "toolCall.file_path",
    "edits.0.file_path",       # Cursor's afterFileEdit, belt-and-suspenders
)

# Same idea for the session/conversation id. Not required for attribution
# (rid + path already key the ledger); it only makes tool_use_id pairing and
# per-session reporting more precise when an agent happens to supply one.
STDIN_SESSION_KEYS = (
    "session_id", "sessionId", "conversation_id", "conversationId",
    "trajectory_id", "execution_id",
)


def _first_stdin(stdin, dotted_keys):
    for key in dotted_keys:
        value = _dig(stdin, key)
        if value:
            return value
    return None


def build_payload(argv):
    """Assemble a Claude-Code-shaped payload from whatever the agent gave us.

    Precedence is argv > agent's own stdin JSON > our env fallbacks. Argv wins
    because a wrapper script that was hand-configured with an explicit flag
    said something more specific than whatever the agent's stdin happens to
    carry. Most of the tools this adapts to only ever supply stdin, never
    argv, so in practice this is "argv override, else parse their JSON."
    """
    args = parse_argv(argv)
    stdin = C.read_input()

    def pick(key, env_names, default=""):
        return args.get(key) or stdin.get(key) or _first_env(env_names) or default

    file_path = args.get("file") or _first_stdin(stdin, STDIN_FILE_KEYS) or _first_env(ENV_FILE) or ""
    session_id = args.get("session_id") or _first_stdin(stdin, STDIN_SESSION_KEYS) or _first_env(ENV_SESSION) or ""

    payload = {
        "cwd": pick("cwd", ENV_CWD, os.getcwd()),
        "session_id": session_id,
        # Pairs pre-edit with edit. Without a real id from the agent, the file
        # path is a good enough key: it collides only when one agent has two
        # concurrent writes in flight to the same file, which is a conflict the
        # agent itself would have to resolve first.
        "tool_use_id": pick("tool_use_id", ("AIATTR_TOOL_USE_ID",)) or file_path,
        "tool_name": pick("tool", ("AIATTR_TOOL",), "edit"),
        "tool_input": {"file_path": file_path},
        "aiattr_agent": args.get("agent") or stdin.get("agent")
        or os.environ.get("AIATTR_AGENT"),
        "reason": pick("reason", ("AIATTR_REASON",)),
    }
    return payload


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] not in EVENTS:
        # Not an error worth failing on. An agent misconfigured to call this
        # with the wrong verb should be silent, not noisy on every keystroke.
        if os.environ.get("AIATTR_DEBUG"):
            sys.stderr.write("agent_hook: expected one of {}\n".format(
                ", ".join(sorted(EVENTS))))
        return
    event = argv[0]
    payload = build_payload(argv[1:])
    # The agent slug has to be visible to context(), which reads the
    # environment. Setting it here means a wrapper that passes --agent works
    # identically to one that exports the variable.
    if payload.get("aiattr_agent"):
        os.environ["AIATTR_AGENT"] = payload["aiattr_agent"]
    EVENTS[event](payload)


if __name__ == "__main__":
    C.guard(main)
