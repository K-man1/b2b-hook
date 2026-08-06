"""Stream ledger records to the course server as they are written.

This is the integrity channel. `sync.py` sends a roll-up the student's machine
computed; this sends the raw records, and the server stores them append-only.

Why that difference matters more than it sounds. When the only copy of the
ledger lives in the student's repo, a student who wants different numbers can
edit it, recompute the hash chain so it self-validates, and force-push. Nothing
in the repo contradicts them afterwards; catching it depends entirely on having
observed the old history first. Once the server holds its own copy, that whole
class of attack stops working, because a force-push cannot reach the server's
database. Editing history locally now just makes the two copies disagree, and
the server's copy is the one that counts.

What this hook is NOT allowed to do:

  1. Block. Wired async and given a short timeout. A student on bad wifi must
     never wait on it, and no failure here may interrupt a session.
  2. Send source code. Records carry paths, line counts and content hashes.
     They have never carried line text and still do not.
  3. Advance the watermark on anything but an explicit server acknowledgement.
     A dropped record is invisible and permanent; a duplicated one is free.

Modes:

  edit    PostToolUse. Debounced, so ordinary editing does not mean an HTTP
          request per tool call.
  flush   SessionStart and SessionEnd. Unconditional. SessionStart is what
          drains a backlog built up while offline.
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402
from core import config, outbox  # noqa: E402

TIMEOUT = 10


def post(url, body, key):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "ai-attribution/" + C.VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8") or "{}"
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def send_batch(ctx, records):
    """Post one batch and return the server's reply, or None if it failed."""
    body = {
        "client_version": C.VERSION,
        "student_id": config.student_id(),
        "repo": {
            "key": ctx["rid"],
            "name": os.path.basename(ctx["root"]),
            "remote": C.repoutil.remote_url(ctx["root"]),
        },
        "records": records,
    }
    url = config.endpoint() + config.records_path()
    return post(url, body, config.api_key())


def deliver(ctx):
    """Send everything above the watermark, in batches, until caught up."""
    rid = ctx["rid"]
    outbox.mark_attempt(rid)

    while True:
        records, backlog = outbox.unsent(rid, ctx["ledger"])
        if not records:
            return

        try:
            reply = send_batch(ctx, records)
        except (urllib.error.URLError, urllib.error.HTTPError,
                OSError, ValueError):
            # Offline, server down, key rotated. All non-events: the ledger on
            # disk is untouched and complete, the watermark has not moved, and
            # the next session start picks up exactly where this left off.
            outbox.mark_failure(rid)
            return

        # The server tells us where its copy actually ends. Trusting our own
        # arithmetic here would let a partially-applied batch look delivered.
        next_seq = reply.get("next_seq")
        if not isinstance(next_seq, int):
            outbox.mark_failure(rid)
            return

        # A gap means the server is missing records we believed we had sent,
        # usually because the watermark survived a wiped server or the student
        # restored an old plugin-data backup. Rewind and refill rather than
        # leaving a hole: a hole in the server's copy is indistinguishable from
        # deleted records at verification time.
        expected = reply.get("expected_seq")
        if isinstance(expected, int) and expected < next_seq:
            outbox.mark_sent(rid, expected - 1)
            continue

        before = outbox.state(rid)["sent_seq"]
        after = outbox.mark_sent(rid, next_seq - 1)
        if after <= before:
            # The server acknowledged without taking anything new. Retrying the
            # identical batch would spin, so stop and let the next hook try.
            outbox.mark_failure(rid)
            return
        if backlog <= len(records):
            return


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "flush"
    payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        return
    if not config.sync_enabled():
        return  # purely local install; tracking continues, nothing is sent

    if mode == "edit":
        _records, backlog = outbox.unsent(ctx["rid"], ctx["ledger"])
        if not outbox.should_send(ctx["rid"], backlog,
                                  config.stream_interval(),
                                  config.stream_burst()):
            return

    deliver(ctx)


if __name__ == "__main__":
    C.guard(main)
