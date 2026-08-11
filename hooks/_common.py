"""Shared hook bootstrap.

Every hook in this plugin obeys two rules:

  1. It never blocks. PostToolUse cannot block by design, but SessionStart and
     PreToolUse can, and a monitoring tool that wedges a student's session
     would simply get uninstalled. Every entry point exits 0 no matter what.

  2. It never writes into the repo at all. Records and snapshots both live
     under the plugin data directory, so a tracked project's working tree is
     exactly as the student left it.
"""

import json
import os
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (VERSION, agents, config, counting, heartbeat,  # noqa: E402,F401
                  ledger, paths, provenance, registry, repoutil)


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
    """Resolve project context, or None when there is nothing to track.

    Project resolution follows wakatime-cli's detector order, so any folder the
    student's editor is already reporting time for is a folder this attributes
    code in. Since that order ends in a folder-name fallback, the only way to
    get None here now is an opt-out.

    `name`, `branch` and `agent` are resolved once, here, because they must be
    identical everywhere they are reported. The same work filed under two
    spellings would land as two projects on Hackatime.
    """
    cwd = payload.get("cwd") or os.getcwd()
    root = repoutil.repo_root(cwd)
    if not root:
        return None
    # Installed at user scope, the hooks fire in every repo the student opens,
    # including personal projects unrelated to the course. Honour the opt-out
    # list rather than recording all of them.
    if config.is_ignored(root):
        return None
    rid = paths.repo_id(root)
    return {  # noqa: E122
        "cwd": cwd,
        "root": root,
        "rid": rid,
        "name": repoutil.project_name(root),
        "branch": repoutil.project_branch(root),
        "ledger": paths.ledger_path(rid),
        "session_id": payload.get("session_id", ""),
        "agent": agents.current(payload)["slug"],
    }


def skip_reason(payload):
    """Why context() declined. Effectively always "ignored" now.

    repo_root falls back to the directory itself, matching wakatime-cli, so
    "this is not a project" is no longer a state a student can be in. The
    unresolvable case is kept only because a filesystem error can still make a
    path unreadable, and reporting that as an opt-out would be a lie.
    """
    cwd = payload.get("cwd") or os.getcwd()
    root = repoutil.repo_root(cwd)
    if not root:
        return "unresolvable"
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
        # On every record, not only edits. An instructor reading the ledger has
        # to be able to tell which agent produced a given claim, and attestation
        # and drift records are the ones that establish whether an agent was
        # even running at the time.
        "agent": ctx["agent"],
        "v": VERSION,
    }
    body.update(fields)
    return ledger.append(ctx["ledger"], body)


def spawn_stream(ctx, mode="edit"):
    """Kick off delivery, as a detached child, once a record is on disk.

    Deliberately spawned from the code that writes the record rather than wired
    as another PostToolUse hook. It was wired that way: a sibling of
    post_edit.py carrying `async: true`. The two raced. An async hook is spawned
    in parallel with its synchronous siblings, not after them, so the streaming
    process read the ledger before post_edit.py had appended the record it
    existed to send, saw an empty backlog, and returned early -- before even
    marking a send attempt, which is why the failure left no trace anywhere.

    The record then waited for the *next* edit to fire the hook again, and a
    session containing a single edit delivered nothing at all until SessionEnd.
    Calling from here makes the ordering structural instead of hopeful: the
    append has already returned by the time this line runs.

    Also the only reason the non-Claude adapters deliver anything. agent_hook.py
    routes those tools straight into post_edit.main and never had a streaming
    hook of its own to wire up.

    Never blocks and never raises. A student on bad wifi must not wait on this,
    and a machine that cannot spawn it is no worse off than one that is offline:
    the local ledger is already complete, and the next flush catches up.
    """
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stream.py")
    if not sys.executable or not os.path.exists(script):
        return
    # Reuse the interpreter already running, rather than re-running py.sh's
    # discovery. This one is known to work: it is executing this function.
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, so the child is not
        # killed with the console the agent is running in.
        extra = {"creationflags": 0x00000008 | 0x00000200}
    else:
        extra = {"start_new_session": True}
    try:
        child = subprocess.Popen(
            [sys.executable, script, mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **extra
        )
    except (OSError, ValueError):
        return
    # stream.py resolves its own context from the payload, exactly as it does
    # when the hook system feeds it stdin. Small enough to never fill the pipe
    # buffer, so this write cannot block.
    try:
        child.stdin.write(json.dumps({
            "cwd": ctx["cwd"],
            "session_id": ctx["session_id"],
        }).encode("utf-8"))
        child.stdin.close()
    except (OSError, ValueError):
        pass


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
        heartbeat.record(ctx["rid"], ctx["name"], rel,
                         human_lines=raw, session_id=ctx["session_id"],
                         branch=ctx["branch"], agent=ctx["agent"])
    return current_lines, tags


def guard(main):
    """Run a hook entry point, swallowing every failure. Always exits 0."""
    try:
        main()
    except Exception:
        if os.environ.get("AIATTR_DEBUG"):
            traceback.print_exc(file=sys.stderr)
    sys.exit(0)
