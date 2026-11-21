"""UserPromptSubmit: the human hands the agent a turn. Record what they brought.

This is the moment that separates iterating from vibecoding. Everything the
student did since the agent last stopped -- reading its diff, running the
code, fixing a line by hand, deciding what to ask next -- ends here, so this
is where it is measured:

  review_gap_s     seconds since the agent last stopped. Recorded raw. A
                   five-second gap is "continue"; a forty-minute one is either
                   real testing or a walk to the fridge, and telling those
                   apart is the server's job, against the edits and commits
                   around it, not this hook's.
  prev_open        the previous turn never reached Stop, i.e. the student cut
                   the agent off. close_turn() records that turn as
                   `interrupted` first.
  lines_human      lines changed by hand, since the last stop, in the files
                   the agent touched this session. Found by reconciling those
                   files against their snapshots, the same drift check the
                   edit hook runs, so the lines are not credited twice.
  permission_mode  how much the student is reviewing before changes land.
                   `bypassPermissions` and `acceptEdits` mean they are not.

The prompt text is stored locally and never put in the ledger. See
core/turns.py.

Never blocks. UserPromptSubmit can block a prompt (exit 2), and a tracker that
ate a student's prompt would be uninstalled the same afternoon, so this exits
0 unconditionally through C.guard like every other hook.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402
import stop  # noqa: E402


def reconcile_hand_edits(ctx, files):
    """Credit hand edits in `files` to the student now, and count them.

    Snapshots are advanced afterwards, so the next agent edit to the same file
    finds nothing to reconcile and the lines are not credited a second time.
    Files the plugin has no snapshot for are left alone: this only measures
    change against something it has already seen.
    """
    stats = {}
    for rel in files:
        text, lines = C.read_file_state(ctx, rel)
        if text is None:
            continue
        snap_file = C.paths.snapshot_path(ctx["rid"], rel)
        snap = C.provenance.load_snapshot(snap_file)
        if snap is None:
            continue
        digest = C.provenance.sha256_text(text)
        if snap.get("sha256") == digest:
            continue
        lines, tags = C.sync_drift(ctx, rel, text, lines, stats)
        C.provenance.save_snapshot(snap_file, lines, tags, digest)
    return stats


def main(payload=None):
    if payload is None:
        payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        return
    now = time.time()
    text = payload.get("prompt") or ""

    with C.turns.lock(ctx["rid"], ctx["session_id"]):
        state = C.turns.load(ctx["rid"], ctx["session_id"])

        # No review gap after an interrupt: the agent never handed control
        # back, so there was nothing to review. None, not 0, because 0 would
        # read as "answered instantly", which is the vibecoding signature.
        last_stop = state.get("last_stop")
        prev_open = bool(state.get("open"))
        if prev_open:
            stop.close_turn(ctx, state, now, "interrupted")
            review_gap = None
        else:
            review_gap = round(now - float(last_stop), 1) if last_stop else None
        hand = reconcile_hand_edits(ctx, state.get("files") or [])

        turn = int(state.get("turn") or 0) + 1
        fields = C.turns.prompt_features(text)
        fields.update({
            "turn": turn,
            "review_gap_s": review_gap,
            "prev_open": prev_open,
            "lines_human": hand.get("lines_human", 0),
            "sig_human": hand.get("sig_human", 0),
            "files_hand_edited": hand.get("files", 0),
            "permission_mode": payload.get("permission_mode") or "",
        })
        C.emit(ctx, "prompt", **fields)

        state["turn"] = turn
        state["open"] = {
            "turn": turn,
            "started": now,
            # Taken after the prompt record is written, so the turn's slice of
            # the ledger starts with the agent's first action, not this record.
            "offset": C.turns.ledger_offset(ctx["ledger"]),
        }
        C.turns.save(ctx["rid"], ctx["session_id"], state)
        C.turns.store_prompt(ctx["rid"], ctx["session_id"], turn,
                             C.now_iso(), text)

    C.spawn_stream(ctx)


if __name__ == "__main__":
    C.guard(main)
