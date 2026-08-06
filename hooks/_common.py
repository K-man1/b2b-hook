"""Shared hook bootstrap.

Every hook in this plugin obeys two rules:

  1. It never blocks. PostToolUse cannot block by design, but SessionStart and
     PreToolUse can, and a monitoring tool that wedges a student's session
     would simply get uninstalled. Every entry point exits 0 no matter what.

  2. It never writes source code into the repo. Only counts and hashes go into
     the ledger; file contents stay in the plugin data directory.
"""

import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (config, counting, ledger, paths,  # noqa: E402
                  provenance, registry, repoutil)

VERSION = "0.5.1"


def read_input():
    """Parse the hook payload from stdin, or {} if unreadable."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        return {}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def context(payload):
    """Resolve repo context, or None when there is nothing to track.

    No git repo means no transport to the instructor, so the plugin stays
    silent rather than accumulating state that can never be delivered.
    """
    cwd = payload.get("cwd") or os.getcwd()
    root = paths.repo_root(cwd)
    if not root:
        return None
    # Installed at user scope, the hooks fire in every repo the student opens,
    # including personal projects unrelated to the course. Honour the opt-out
    # list rather than writing a ledger into all of them.
    if config.is_ignored(root):
        return None
    return {  # noqa: E122
        "cwd": cwd,
        "root": root,
        "rid": paths.repo_id(root),
        "ledger": paths.ledger_path(root),
        "session_id": payload.get("session_id", ""),
    }


def skip_reason(payload):
    """Why context() declined, so callers can say something accurate.

    Both cases return None from context(), but they mean opposite things to a
    student: "not a repo" is a problem they should fix, "ignored" is a choice
    they made. Telling someone their repo is not a git repository because they
    opted out of it is just wrong.
    """
    cwd = payload.get("cwd") or os.getcwd()
    root = paths.repo_root(cwd)
    if not root:
        return "no_repo"
    if config.is_ignored(root):
        return "ignored"
    return None


def rel_in_repo(ctx, file_path):
    """Repo-relative path, or None if outside the repo or excluded."""
    if not file_path:
        return None
    try:
        abspath = os.path.realpath(
            file_path if os.path.isabs(file_path)
            else os.path.join(ctx["cwd"], file_path)
        )
        rel = os.path.relpath(abspath, ctx["root"])
    except (OSError, ValueError):
        return None
    if rel.startswith(".."):
        return None
    rel = rel.replace(os.sep, "/")
    if counting.is_excluded(rel):
        return None
    return rel


def emit(ctx, kind, **fields):
    """Append one record to the ledger."""
    body = {
        "kind": kind,
        "ts": now_iso(),
        "session_id": ctx["session_id"],
        "v": VERSION,
    }
    body.update(fields)
    return ledger.append(ctx["ledger"], body)


def read_file_state(ctx, rel):
    """Current (text, lines) for a repo-relative path, or (None, None)."""
    abspath = os.path.join(ctx["root"], rel)
    text = repoutil.read_text(abspath)
    if text is None:
        return None, None
    lines = repoutil.splitlines(text)
    if counting.too_large(lines=lines):
        return None, None
    return text, lines


def sync_drift(ctx, rel, current_text, current_lines):
    """Reconcile a file against its snapshot before attributing a new change.

    If the file on disk no longer matches what we last recorded, something
    changed it outside a Claude tool call. Those lines belong to the student,
    so they are tagged `human` and logged before the current edit is scored.
    Doing this at edit time as well as session start keeps a student from
    hand-editing a file mid-session and having it silently absorbed into the
    AI bucket, or vice versa.

    Returns (lines, tags) representing the reconciled pre-edit state.
    """
    snap_file = paths.snapshot_path(ctx["rid"], rel)
    snap = provenance.load_snapshot(snap_file)
    digest = provenance.sha256_text(current_text)

    if snap is None:
        # First sighting. Unobserved origin, so everything is `unobserved`.
        return current_lines, provenance.baseline_tags(current_lines)

    if snap.get("sha256") == digest:
        return current_lines, snap.get("tags", [])

    prior_lines = snap.get("lines", [])
    prior_tags = snap.get("tags", [])
    tags, new_idx, removed = provenance.retag(
        prior_lines, prior_tags, current_lines, provenance.TAG_HUMAN
    )
    if new_idx or removed:
        mask = counting.significant_mask(current_lines, rel)
        raw, sig = provenance.score(new_idx, mask)
        emit(ctx, "drift", path=rel,
             lines_human=raw, sig_human=sig, lines_removed=removed,
             after_sha256=digest)
    return current_lines, tags


def guard(main):
    """Run a hook entry point, swallowing every failure. Always exits 0."""
    try:
        main()
    except Exception:
        if os.environ.get("AIATTR_DEBUG"):
            traceback.print_exc(file=sys.stderr)
    sys.exit(0)
