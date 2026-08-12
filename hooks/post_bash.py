"""PostToolUse on Bash and MCP tools: attribute writes that bypass the editor.

The edit hooks only see Write, Edit, MultiEdit and NotebookEdit. An agent has
at least two other ways to change a file, and both were completely invisible:

    Bash        heredocs, `sed -i`, `python -c`, patch application, and every
                scaffolding command (`django-admin startproject`, `npm init`,
                `cargo new`) that writes source nobody typed.
    MCP tools   a filesystem server's write_file is a tool call like any other,
                but its name matches none of the editor matchers.

Neither fired a hook, so the change was first seen by the next SessionStart
sweep, which has no way of knowing an agent caused it and therefore tagged it
`human`. Asking the agent to use a heredoc was a complete bypass: its own
output arrived in the student's bucket. Nothing in the ledger even hinted at
it. That is a worse failure than not tracking at all, because the number came
out confidently wrong rather than absent.

WHAT THIS CAN AND CANNOT KNOW, because the distinction decides the tagging:

A tool call ran and files changed. That is real evidence the agent caused the
change, which is exactly what the SessionStart sweep lacks. It is not evidence
the agent *authored* the content: `git checkout`, `git pull`, `npm install` and
a build step all change files under an agent's hand without anybody writing a
line. So the sweep splits on scale:

    few files    the shape of authoring. Tagged `ai`, with the tool name on
                 the record so a reviewer can see it was indirect.
    many files   the shape of a checkout, a pull, or an install. Tagged
                 `unobserved` and recorded as `bulk_change`. Saying "this
                 changed and we do not know who wrote it" is honest; crediting
                 a merge commit to the agent would not be.

Either way the snapshot is refreshed, which is the part that matters most: an
unrefreshed snapshot is what fed those lines to the next sweep as `human`.

Cheap by construction. Detection is one `git ls-files` plus a stat per tracked
file, and files are only read when their mtime has moved past their snapshot's.
An agent running `cat` or `ls` does no file reads here at all.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402

# More changed files than this in a single tool call is not somebody writing
# code, so it is not attributed to anybody. Deliberately low: a student's own
# commit touches a handful of files, while the operations this exists to
# exclude (checkout, pull, install, build) touch dozens at minimum.
BULK_THRESHOLD = 25

# Hard ceiling on files re-read in one sweep, so a pathological repo cannot
# turn a Bash call into a stall. Anything above it is bulk by definition.
MAX_FILES = 500


def changed_since_snapshot(ctx):
    """Repo-relative paths whose bytes moved after we last snapshotted them.

    mtime rather than content hash: this runs after every Bash call, and
    hashing every tracked file that often would put a full repo read on a path
    the agent uses constantly. Snapshots are always written after the content
    they describe is read, so a source file newer than its snapshot has changed
    since we looked. A file with no snapshot at all is new and counts too.
    """
    out = []
    for rel in C.repoutil.tracked_files(ctx["root"]):
        if C.counting.is_excluded(rel):
            continue
        source = os.path.join(ctx["root"], rel)
        snap_file = C.paths.snapshot_path(ctx["rid"], rel)
        try:
            source_mtime = os.path.getmtime(source)
        except OSError:
            continue  # deleted, or never really there
        try:
            if source_mtime <= os.path.getmtime(snap_file):
                continue
        except OSError:
            pass  # no snapshot yet: new file, so it counts
        out.append(rel)
        if len(out) >= MAX_FILES:
            break
    return out


def attribute(ctx, rel, actor):
    """Retag one changed file against its snapshot.

    Returns a dict of what changed, or None if nothing did. Everything the
    caller needs to emit a record is in here, so the record is built from the
    single read this function already did rather than reading the file again.
    """
    text, lines = C.read_file_state(ctx, rel)
    if text is None:
        return None

    snap_file = C.paths.snapshot_path(ctx["rid"], rel)
    snap = C.provenance.load_snapshot(snap_file)
    digest = C.provenance.sha256_text(text)

    if snap is None:
        before_lines, before_tags = [], []
    elif snap.get("sha256") == digest:
        return None  # mtime moved but the bytes did not (touch, rebuild)
    else:
        before_lines = snap.get("lines", [])
        before_tags = snap.get("tags", [])

    tags, new_idx, removed = C.provenance.retag(
        before_lines, before_tags, lines, actor
    )
    mask = C.counting.significant_mask(lines, rel)
    raw, sig = C.provenance.score(new_idx, mask)
    C.provenance.save_snapshot(snap_file, lines, tags, digest)
    if not (raw or removed):
        return None
    return {"raw": raw, "sig": sig, "removed": removed,
            "file_lines": len(lines), "sha256": digest}


def main(payload=None):
    if payload is None:
        payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        return

    changed = changed_since_snapshot(ctx)
    if not changed:
        return

    tool = payload.get("tool_name", "")
    bulk = len(changed) > BULK_THRESHOLD
    actor = C.provenance.TAG_UNOBSERVED if bulk else C.provenance.TAG_AI

    total_raw = total_sig = total_removed = touched = 0
    for rel in changed:
        got = attribute(ctx, rel, actor)
        if got is None:
            continue
        touched += 1
        total_raw += got["raw"]
        total_sig += got["sig"]
        total_removed += got["removed"]
        if not bulk:
            # One record per file, matching what post_edit.py emits, so the
            # server does not need a second shape to understand an indirect
            # write. `via` is what marks the attribution as inferred from a
            # tool call rather than observed as one.
            C.emit(ctx, "edit", path=rel, tool=tool, via="tool_call",
                   lines_ai=got["raw"], sig_ai=got["sig"],
                   lines_removed=got["removed"],
                   file_lines=got["file_lines"], after_sha256=got["sha256"])
            C.heartbeat.record(ctx["rid"], ctx["name"], rel,
                               ai_lines=got["raw"], session_id=ctx["session_id"],
                               branch=ctx["branch"], agent=ctx["agent"])

    if not touched:
        return

    if bulk:
        # Deliberately one record, not one per file. The claim being made is
        # about the operation, not about any individual file in it.
        C.emit(ctx, "bulk_change", tool=tool, via="tool_call",
               files=touched, lines=total_raw, sig_lines=total_sig,
               lines_removed=total_removed)

    C.spawn_stream(ctx)


if __name__ == "__main__":
    C.guard(main)
