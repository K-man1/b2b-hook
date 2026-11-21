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
  lines_human      new text written outside the agent since the last turn,
                   anywhere in the project. Moved, duplicated and blank lines
                   are excluded (see provenance.authored), and sig_human
                   further drops comment-only lines, so neither reshuffling
                   code nor padding it with comments reads as writing it.
  lines_human_moved  the lines excluded above, kept so a reviewer can see
                   that something happened even though nothing was written.
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
import post_bash  # noqa: E402
import stop  # noqa: E402


def reconcile_hand_edits(ctx):
    """Credit every change made outside the agent since the last look.

    Every agent write is snapshotted the moment it lands (post_edit.py,
    post_bash.py), so any file whose mtime has moved past its snapshot was
    changed by something else: the student, their editor, a paste. That
    covers files the agent never touched and files that did not exist at the
    last turn, which a list of "files the agent touched" could not.

    A file with no snapshot follows the session sweep's rule. In a repo the
    ledger has seen before it is new and the student's; in one it has never
    seen, it is pre-existing code and is baselined as `unobserved`, because
    crediting a whole repo to the student on their first prompt would be as
    wrong as crediting it to the agent.

    Snapshots are advanced as each file is read, so the same change is never
    counted at two prompts, and the next agent edit finds nothing to absorb.
    """
    stats = {"lines_human": 0, "sig_human": 0, "lines_human_moved": 0,
             "lines_removed": 0, "files_hand_edited": 0,
             "files_hand_created": 0}
    seen_before = C.ledger.head_seq(ctx["ledger"]) >= 0
    changed = post_bash.changed_since_snapshot(ctx)
    for rel in changed[:C.turns.MAX_RECONCILE_FILES]:
        text, lines = C.read_file_state(ctx, rel)
        if text is None:
            continue
        snap_file = C.paths.snapshot_path(ctx["rid"], rel)
        snap = C.provenance.load_snapshot(snap_file)
        digest = C.provenance.sha256_text(text)

        if snap is None:
            if not seen_before:
                C.provenance.save_snapshot(
                    snap_file, lines, C.provenance.baseline_tags(lines), digest)
                continue
            before_lines, before_tags, created = [], [], True
        elif snap.get("sha256") == digest:
            # Touched, not changed. Re-save so the mtime check stops
            # flagging it on every later prompt.
            C.provenance.save_snapshot(snap_file, snap.get("lines", []),
                                       snap.get("tags", []), digest)
            continue
        else:
            before_lines = snap.get("lines", [])
            before_tags = snap.get("tags", [])
            created = False

        tags, new_idx, removed = C.provenance.retag(
            before_lines, before_tags, lines, C.provenance.TAG_HUMAN)
        C.provenance.save_snapshot(snap_file, lines, tags, digest)
        if not (new_idx or removed):
            continue  # whitespace-only: normalize() already absorbed it

        mask = C.counting.significant_mask(lines, rel)
        raw, sig = C.provenance.score(new_idx, mask)
        # The drift record keeps its existing meaning (who owns which line
        # now), so the server sees one shape for drift whichever hook found it.
        C.emit(ctx, "drift", path=rel, via="new_file" if created else "drift",
               lines_human=raw, sig_human=sig, lines_removed=removed,
               file_lines=len(lines), after_sha256=digest)
        C.heartbeat.record(ctx["rid"], ctx["name"], rel,
                           human_lines=raw, session_id=ctx["session_id"],
                           branch=ctx["branch"], agent=ctx["agent"])

        wrote = C.provenance.authored(before_lines, lines, new_idx)
        w_raw, w_sig = C.provenance.score(wrote, mask)
        stats["lines_human"] += w_raw
        stats["sig_human"] += w_sig
        stats["lines_human_moved"] += raw - w_raw
        stats["lines_removed"] += removed
        stats["files_hand_created" if created else "files_hand_edited"] += 1
    return stats


def main(payload=None):
    if payload is None:
        payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        return
    now = time.time()
    text = payload.get("prompt") or ""
    key = C.turns.key(ctx)

    with C.turns.lock(ctx["rid"], key):
        state = C.turns.load(ctx["rid"], key)

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
        hand = reconcile_hand_edits(ctx)

        turn = int(state.get("turn") or 0) + 1
        fields, salt = C.turns.prompt_features(text)
        fields.update(hand)
        fields.update({
            "turn": turn,
            "review_gap_s": review_gap,
            "prev_open": prev_open,
            "permission_mode": payload.get("permission_mode") or "",
            "no_session": not ctx["session_id"],
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
        C.turns.save(ctx["rid"], key, state)
        C.turns.store_prompt(ctx["rid"], key, turn, C.now_iso(), text, salt)

    C.spawn_stream(ctx)


if __name__ == "__main__":
    C.guard(main)
