"""Stop: the agent finished a turn. Record what it did with the prompt.

A turn ends one of three ways, and all three go through close_turn() so the
record has one shape whatever ended it:

  stop          the agent finished on its own. This hook.
  interrupted   a new prompt arrived while the turn was still open. Claude
                Code does not fire Stop when the student interrupts, so the
                next prompt is the first moment anything can notice. The
                student cutting the agent off is steering, and is recorded as
                such. See prompt.py.
  session_end   the session closed with a turn still open. See session_end.py.

Never blocks: Stop *can* block (exit 2 makes the agent keep going), so this
hook exits 0 unconditionally through C.guard, like every other.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402


def close_turn(ctx, state, now, ended):
    """Emit the record that ends the open turn, and mark it closed in `state`.

    Returns the emitted record, or None if no turn was open. The caller holds
    the session lock and saves `state` afterwards.
    """
    open_turn = state.get("open")
    if not open_turn:
        return None
    records = C.turns.records_since(ctx["ledger"], open_turn.get("offset", 0))
    did = C.turns.summarize_turn(records, ctx)
    started = float(open_turn.get("started") or now)
    rec = C.emit(ctx, "turn_end",
                 turn=open_turn.get("turn"),
                 ended=ended,
                 agent_s=round(max(0.0, now - started), 1),
                 edits=did["edits"],
                 lines_ai=did["lines_ai"],
                 sig_ai=did["sig_ai"],
                 lines_removed=did["lines_removed"],
                 files_touched=len(did["files"]),
                 edits_unfinished=C.turns.unfinished_edits(ctx["rid"], started),
                 no_session=not ctx["session_id"])
    state["open"] = None
    state["last_stop"] = now
    return rec


def main(payload=None):
    if payload is None:
        payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        return
    now = time.time()
    key = C.turns.key(ctx)
    with C.turns.lock(ctx["rid"], key):
        state = C.turns.load(ctx["rid"], key)
        rec = close_turn(ctx, state, now, "stop")
        if rec is None:
            # A Stop with no prompt seen: the plugin was enabled mid-turn, or
            # the session was resumed. Still a moment the agent handed control
            # back, so the review gap for the next prompt starts here.
            state["last_stop"] = now
        C.turns.save(ctx["rid"], key, state)
    if rec is not None:
        C.spawn_stream(ctx)


if __name__ == "__main__":
    C.guard(main)
