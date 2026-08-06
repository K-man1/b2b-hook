"""PostToolUse on Write|Edit|MultiEdit|NotebookEdit: attribute the change.

Sequence, and the order matters:

  1. Recover the pre-edit content captured by pre_edit.py.
  2. Reconcile that against the stored snapshot. Any difference happened
     outside a Claude tool call, so it is the student's work and is tagged
     `human` FIRST. Skipping this step would fold hand-written edits into the
     AI bucket on the next AI touch of the same file.
  3. Diff pre-edit against post-edit and attribute the difference to `ai`.
  4. Persist the new snapshot.

PostToolUse cannot block (exit code 2 is non-blocking for this event), so a
failure here can never wedge the session.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402
from pre_edit import pending_slot  # noqa: E402


def load_pending(slot):
    try:
        with open(slot, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        os.unlink(slot)
        return data
    except (OSError, ValueError):
        return None


def main():
    payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        return

    tool_input = payload.get("tool_input") or {}
    rel = C.rel_in_repo(ctx, tool_input.get("file_path"))
    if rel is None:
        return

    after_text, after_lines = C.read_file_state(ctx, rel)
    if after_text is None:
        return  # deleted, binary, or too large to diff

    slot = pending_slot(ctx, payload)
    pending = load_pending(slot)

    if pending is not None and pending.get("rel") == rel:
        before_text = pending.get("text", "")
        before_lines = C.repoutil.splitlines(before_text)
        before_lines, before_tags = C.sync_drift(ctx, rel, before_text, before_lines)
    else:
        # The pre hook did not run (plugin enabled mid-session, or the tool
        # call was replayed). Fall back to the snapshot as the before-image.
        # If there is no snapshot either, baseline instead of guessing: over-
        # attributing to AI is worse than admitting the origin is unobserved.
        snap = C.provenance.load_snapshot(C.paths.snapshot_path(ctx["rid"], rel))
        if snap is None:
            tags = C.provenance.baseline_tags(after_lines)
            C.provenance.save_snapshot(
                C.paths.snapshot_path(ctx["rid"], rel),
                after_lines, tags, C.provenance.sha256_text(after_text),
            )
            C.emit(ctx, "baseline", path=rel, lines=len(after_lines),
                   reason="no_before_image",
                   after_sha256=C.provenance.sha256_text(after_text))
            return
        before_lines = snap.get("lines", [])
        before_tags = snap.get("tags", [])

    tags, new_idx, removed = C.provenance.retag(
        before_lines, before_tags, after_lines, C.provenance.TAG_AI
    )
    mask = C.counting.significant_mask(after_lines, rel)
    raw, sig = C.provenance.score(new_idx, mask)
    digest = C.provenance.sha256_text(after_text)

    C.provenance.save_snapshot(
        C.paths.snapshot_path(ctx["rid"], rel), after_lines, tags, digest
    )

    if raw or removed:
        C.emit(ctx, "edit",
               path=rel,
               tool=payload.get("tool_name", ""),
               lines_ai=raw, sig_ai=sig,
               lines_removed=removed,
               file_lines=len(after_lines),
               after_sha256=digest)


if __name__ == "__main__":
    C.guard(main)
