"""PreToolUse on Write|Edit|MultiEdit|NotebookEdit: capture the "before".

This hook exists for one reason. PostToolUse fires after the write has landed,
so by then the disk holds the post-edit content and there is nothing left to
diff against. Reading the file here, before the tool runs, is the only way to
get a true before-image for the edit that is about to happen.

The captured text is stashed under the tool_use_id so that parallel edits to
different files cannot collide.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402


def pending_slot(ctx, payload):
    tool_use_id = payload.get("tool_use_id") or ""
    if not tool_use_id:
        # Older payloads or odd tools may omit it. Fall back to the path so the
        # common single-edit case still pairs correctly.
        tool_use_id = "nopath"
    safe = "".join(c for c in tool_use_id if c.isalnum() or c in "-_")[:80]
    return os.path.join(C.paths.pending_dir(ctx["rid"]), safe + ".json")


def main():
    payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        return

    tool_input = payload.get("tool_input") or {}
    rel = C.rel_in_repo(ctx, tool_input.get("file_path"))
    if rel is None:
        return

    text, lines = C.read_file_state(ctx, rel)
    if text is None:
        # File does not exist yet (a fresh Write) or is binary/oversized.
        # An empty before-image is correct for creation: every line is new.
        text, lines = "", []

    slot = pending_slot(ctx, payload)
    os.makedirs(os.path.dirname(slot), exist_ok=True)
    tmp = slot + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"rel": rel, "text": text, "existed": bool(lines)}, fh)
    os.replace(tmp, slot)


if __name__ == "__main__":
    # Must be guarded: post_edit.py imports pending_slot from this module, and
    # an unguarded call here would run the pre-hook (and exit) on import.
    C.guard(main)
