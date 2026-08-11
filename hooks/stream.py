"""Stream ledger records to the course server as they are written.

This is the only channel that carries evidence. `sync.py` sends a roll-up the
student's machine computed; this sends the raw records, and the server stores
them append-only.

Why streaming rather than collecting and sending later: a record that is
already on the server before the student has any reason to want it different
cannot be revised afterwards. Everything on their machine can be. The gap
between an edit happening and its record leaving the machine is the entire
window in which the numbers can still be shaped, so the hook keeps that window
as small as a debounce allows.

What this buys, precisely: the student cannot change what was reported. It does
not make what gets reported complete. Work done with no session open is never
observed by anything, so it is never recorded, and no amount of delivery
integrity recovers it. See the README's limits.

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
import sync  # noqa: E402
from core import config, heartbeat, outbox  # noqa: E402
from core import report as report_mod  # noqa: E402

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
            "name": ctx["name"],
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
    rewound = False

    while True:
        records, backlog = outbox.unsent(rid, ctx["ledger"])
        if not records:
            return

        try:
            reply = send_batch(ctx, records)
        except (urllib.error.URLError, urllib.error.HTTPError,
                OSError, ValueError):
            # Offline, server down, key rotated. All non-events: the local
            # ledger is untouched and complete, the watermark has not moved,
            # and the next session start picks up exactly where this left off.
            outbox.mark_failure(rid)
            return

        # The server tells us where its copy actually ends. Trusting our own
        # arithmetic here would let a partially-applied batch look delivered.
        next_seq = reply.get("next_seq")
        if not isinstance(next_seq, int):
            outbox.mark_failure(rid)
            return

        # The server asking for a seq we believe we already delivered means its
        # copy is missing records ours thinks it has: a wiped or restored
        # server, a rotated repo key, an old plugin-data backup. Rewind and
        # refill rather than leaving a hole -- a hole in the server's copy is
        # indistinguishable from deleted records at verification time.
        #
        # Compared against our own watermark, not against next_seq. It used to
        # test `expected < next_seq`, and those two are the same number in the
        # exact case this branch exists to handle: an empty server answers
        # next_seq 0, expected_seq 0, so the rewind never fired. Delivery then
        # fell through to the `after <= before` guard below and marked a
        # failure, forever, on every attempt -- the repo simply stopped
        # reporting, with a failure count climbing past 100 as the only sign.
        expected = reply.get("expected_seq")
        current = outbox.state(rid)["sent_seq"]
        if isinstance(expected, int) and expected <= current and not rewound:
            # Once per delivery pass. Re-sending is free, but a server that
            # answers with the same expectation after a refill would otherwise
            # spin here, and this runs unattended.
            rewound = True
            outbox.rewind(rid, expected - 1)
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


def refresh(ctx):
    """Recompute this repo's roll-up and report it to the website.

    The picker on the website renders the roll-up, not the record stream, so a
    project reads as 0% until this has run no matter how many records have been
    delivered. It used to run at session start and session end only.

    Session start is the reliable half of that pair, but it runs *before* the
    work. So the first session in a fresh project reported zeros for its entire
    length: a student who installed the plugin, wrote a file, and checked the
    site was told "Only 0% of this project was tracked" while their own machine
    held complete, correct numbers. That is the same wrong-in-the-worst-
    direction failure session.py's comment already warns about -- the site says
    the plugin saw nothing, when the truth is that nobody had added it up yet.

    Runs here because this path is already detached and already debounced by
    should_send. The other obvious home is post_edit.py, right next to the
    write, but report.build re-walks and re-counts every tracked file in the
    repo, and putting a full repo scan on every Write and Edit tool call is a
    cost that grows with the project until students notice it.
    """
    data = report_mod.build(ctx["root"], ctx["rid"])
    records, _bad = C.ledger.read_all(ctx["ledger"])
    head = records[-1].get("hash", "") if records else ""
    seq = int(records[-1].get("seq", -1)) if records else -1

    C.registry.update(
        ctx["rid"], ctx["name"],
        C.repoutil.remote_url(ctx["root"]),
        totals=data.get("totals"),
        band=report_mod.band(data.get("totals", {})),
        ledger_head=head, ledger_records=seq + 1,
        path=ctx["root"],
    )
    sync.report()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "flush"
    payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        return

    # Independent of sync_enabled: Hackatime delivery is the student's own
    # account and their own key, and works on an install that reports to no
    # course server at all. Rate-limited internally in `edit` mode, which fires
    # on every tool call; the once-per-session flushes are not.
    heartbeat.flush(force=(mode != "edit"))

    if not config.sync_enabled():
        return  # purely local install; tracking continues, nothing is sent

    if mode == "edit":
        _records, backlog = outbox.unsent(ctx["rid"], ctx["ledger"])
        if not outbox.should_send(ctx["rid"], backlog,
                                  config.stream_interval(),
                                  config.stream_burst()):
            return

    deliver(ctx)

    # Edit mode only. The flush modes are already paired in hooks.json with
    # session.py / session_end.py, which compute the same roll-up, and with
    # sync.py, which posts it. Repeating it here would mean two full repo scans
    # at every session boundary for one set of numbers.
    if mode == "edit":
        refresh(ctx)


if __name__ == "__main__":
    C.guard(main)
