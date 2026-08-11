"""Tracks which ledger records have reached the server.

The local ledger is written first and is always complete. This module only
remembers how much of it has been *delivered*, as one watermark per repo: the
highest seq the server has acknowledged. Everything above the watermark is
unsent.

Why a watermark and not a queue: the ledger already is the queue. It is
append-only, ordered by seq, and on disk before anything is sent. Duplicating
records into a separate outbox file would create a second copy that can drift
from the first, and a drifted copy is indistinguishable from tampering. Holding
a single integer means an interrupted send, a dead battery, or three weeks
offline all resolve the same way: the next send starts from the watermark and
walks forward.

The watermark is advisory in exactly one direction. Setting it too low costs a
duplicate send, which the server discards. Setting it too high would silently
drop records, so it only ever advances to a seq the server explicitly confirmed
storing.
"""

import json
import os
import time

from . import ledger, paths

OUTBOX_NAME = "outbox.json"
OUTBOX_VERSION = 1


def outbox_path():
    return os.path.join(paths.plugin_data_dir(), OUTBOX_NAME)


def load():
    try:
        with open(outbox_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"v": OUTBOX_VERSION, "repos": {}}
    if not isinstance(data, dict) or data.get("v") != OUTBOX_VERSION:
        return {"v": OUTBOX_VERSION, "repos": {}}
    data.setdefault("repos", {})
    return data


def _write(data):
    path = outbox_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def state(rid):
    entry = load()["repos"].get(rid) or {}
    return {
        "sent_seq": int(entry.get("sent_seq", -1)),
        "last_attempt": float(entry.get("last_attempt", 0) or 0),
        "last_success": float(entry.get("last_success", 0) or 0),
        "failures": int(entry.get("failures", 0)),
    }


def _update(rid, **fields):
    with ledger.FileLock(outbox_path() + ".lock"):
        data = load()
        entry = data["repos"].get(rid) or {}
        entry.update(fields)
        data["repos"][rid] = entry
        _write(data)


def mark_attempt(rid):
    """Record that a send was tried, whether or not it worked.

    Debouncing keys off this rather than off success, so a server that is down
    is retried on the same gentle schedule as one that is up. Keying off
    success would turn an outage into a tight retry loop on every edit.
    """
    _update(rid, last_attempt=time.time())


def mark_sent(rid, seq):
    """Advance the watermark to a seq the server confirmed storing."""
    current = state(rid)["sent_seq"]
    if seq <= current:
        return current
    _update(rid, sent_seq=int(seq), last_success=time.time(), failures=0)
    return int(seq)


def rewind(rid, seq):
    """Force the watermark backwards, on the server's own say-so.

    The only path allowed to lower it, and it exists because mark_sent refuses
    to. That refusal is right as a default: a confused reply must never be able
    to silently drop records. But it made recovery impossible in the one case
    the recovery was written for. A server whose copy ends *before* our
    watermark -- wiped and restored, re-keyed, or restored from a backup older
    than the client's -- will not store records numbered above where its copy
    ends, so every retry sends a batch it discards, the watermark never moves,
    and the repo stops delivering permanently while the failure counter climbs.

    Rewinding is the safe direction to be wrong in. Setting the watermark too
    low costs duplicate sends, which the server drops. Setting it too high
    loses records for good.
    """
    seq = int(seq)
    _update(rid, sent_seq=seq, failures=0)
    return seq


def mark_failure(rid):
    _update(rid, failures=state(rid)["failures"] + 1)


def forget(rid):
    """Drop a repo's watermark. Used when a student opts the repo out."""
    with ledger.FileLock(outbox_path() + ".lock"):
        data = load()
        if data["repos"].pop(rid, None) is not None:
            _write(data)
            return True
    return False


def unsent(rid, ledger_file, limit=500):
    """Records above the watermark, oldest first.

    Returns whole records exactly as they appear on disk, because the server's
    copy has to hash to the same value the local one does. Stripping or
    rewriting a field here would produce a stored record whose hash disagrees
    with the chain it belongs to, which reads as tampering by a student who did
    nothing wrong.

    `limit` caps one batch so a student who worked offline for a month sends in
    chunks rather than one request large enough to be rejected.
    """
    records, _bad = ledger.read_all(ledger_file)
    after = state(rid)["sent_seq"]
    pending = [r for r in records if int(r.get("seq", -1)) > after]
    pending.sort(key=lambda r: int(r.get("seq", -1)))
    return pending[:limit], len(pending)


def should_send(rid, backlog, min_interval, burst):
    """Is it worth making a request right now?

    Called on every edit, so most invocations must answer no. Sending on each
    edit would mean an HTTP request per tool call, which is both rude to the
    server and slow enough that students would notice. Two conditions override
    the interval: a backlog big enough to be worth flushing early, and a
    watermark that has never advanced (a machine that has not yet delivered
    anything is the case where waiting is least useful).
    """
    if backlog <= 0:
        return False
    if backlog >= burst:
        return True
    st = state(rid)
    if st["sent_seq"] < 0:
        return True
    return (time.time() - st["last_attempt"]) >= min_interval
