"""Append-only, hash-chained event log.

Each record embeds the hash of the record before it, and carries a sequence
number assigned under a lock. Both are load-bearing, but not for the reason
they originally were.

The chain used to be a tamper-evidence mechanism, back when this file was
committed to the student's repo and had to defend itself. It cannot do that job
and never really could: the student holds the file and can recompute the whole
chain. What it does now is give the server a way to tell a resend apart from a
rewrite. Records arrive over the network, possibly out of order, possibly twice
after a dropped connection; `seq` says where each one belongs and `hash` says
whether the copy being offered matches the copy already stored. A record
offered at a seq the server already holds, with a different hash, is the local
stream having been edited between two deliveries.

`seq` is also what the delivery watermark counts (see core/outbox.py).

Concurrency is a correctness requirement, not a nicety. Claude issues tool
calls in parallel, so several PostToolUse hooks can append at the same moment.
An interleaved write would corrupt the chain, and a corrupt chain reads as
tampering. Falsely accusing a student of cheating is the worst failure this
tool can have, so appends take an exclusive lock.
"""

import errno
import hashlib
import json
import os
import time

GENESIS = "0" * 64
_LOCK_TIMEOUT = 15.0
_LOCK_STALE = 120.0


class LockTimeout(Exception):
    pass


class _ChainUnreadable(Exception):
    """A ledger with content in it that yielded no recoverable record."""


class FileLock:
    """Cross-platform advisory lock built on O_CREAT|O_EXCL.

    fcntl.flock is POSIX-only and msvcrt.locking has different semantics, so
    neither is portable to the mix of machines students actually use. An
    exclusive-create lockfile behaves the same everywhere.
    """

    def __init__(self, path, timeout=_LOCK_TIMEOUT, stale=_LOCK_STALE):
        self.path = path
        self.timeout = timeout
        self.stale = stale
        self._held = False

    def __enter__(self):
        start = time.time()
        delay = 0.01
        # Written into the lockfile and checked again on release. Two hooks
        # racing is the normal case here, not the exotic one.
        self._token = "{}:{}".format(os.getpid(), time.time())
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, self._token.encode("ascii"))
                os.close(fd)
                self._held = True
                return self
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                # A crashed hook can leave a lockfile behind forever. Break any
                # lock older than the stale threshold rather than wedging every
                # future session.
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.unlink(self.path)
                        continue
                except OSError:
                    pass
                if time.time() - start > self.timeout:
                    raise LockTimeout(self.path)
                time.sleep(delay)
                delay = min(delay * 2, 0.2)

    def __exit__(self, *exc):
        if self._held:
            # Only remove the lock if it is still ours. Without this check the
            # stale-breaking path above is unsafe: a slow holder whose lock got
            # broken at the 120s mark would, on finishing, delete the lockfile
            # belonging to whoever took over, and a third process could then
            # enter while that one was still writing. Two concurrent appends
            # corrupt the chain, and a corrupt chain reads as tampering.
            try:
                with open(self.path, "r", encoding="ascii") as fh:
                    mine = fh.read() == self._token
            except OSError:
                mine = False
            if mine:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
            self._held = False
        return False


def canonical(obj):
    """Byte-identical JSON for any equal object, on any platform.

    sort_keys removes dict-ordering dependence, the compact separators remove
    whitespace ambiguity, and ensure_ascii removes any dependence on the
    machine's unicode handling. All three matter: the instructor recomputes
    these hashes on a different OS than the student's.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def record_hash(body):
    """Hash of a record body. `prev_hash` lives inside the body, so hashing the
    body is what chains the records together."""
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def _tail_record(path):
    """Last usable record in the log, without reading the whole file.

    Reads a trailing window rather than the full ledger because this runs on
    every single edit; an O(file) read per keystroke-sized change would grow
    into a noticeable stall over a semester.

    Raises _ChainUnreadable rather than returning None when the file has
    content but no record can be recovered from it. That distinction is the
    whole point of this function's contract: None means "empty ledger, start
    the chain at zero", and returning it for a *damaged* ledger was a bug bad
    enough to be an attack.

    A single unparseable trailing line -- one torn write, one stray byte, one
    `echo x >> ledger.jsonl` -- made this return None, so `append` restarted at
    seq 0 with a genesis prev_hash. Two things then went wrong at once. The
    server, which already held records at those sequence numbers, saw different
    hashes offered for seqs it had stored, which is precisely its definition of
    a rewritten stream. And the outbox watermark was already past those
    numbers, so every subsequent record counted as delivered and none was ever
    sent again. Real work stopped being reported, with an apparent tamper as
    the only trace.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size == 0:
        return None
    window = min(size, 65536)
    try:
        with open(path, "rb") as fh:
            fh.seek(size - window)
            chunk = fh.read(window)
    except OSError as exc:
        raise _ChainUnreadable(path) from exc
    lines = [ln for ln in chunk.split(b"\n") if ln.strip()]

    # Backwards, not just the final line. A crash between write and fsync can
    # leave a partial record at the end; the records before it are intact and
    # are what the chain should continue from.
    for line in reversed(lines):
        try:
            rec = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(rec, dict) and "seq" in rec:
            return rec

    # Nothing usable in the window. The window may simply have started inside
    # one enormous record, so re-read the whole file before concluding damage.
    records, _bad = read_all(path)
    for rec in reversed(records):
        if isinstance(rec, dict) and "seq" in rec:
            return rec
    raise _ChainUnreadable(path)


def append(ledger_file, body):
    """Chain `body` onto the log and return the completed record.

    `body` must not contain seq, prev_hash or hash; those are assigned here
    under the lock so parallel hooks cannot both claim the same sequence number.
    """
    directory = os.path.dirname(ledger_file)
    os.makedirs(directory, exist_ok=True)
    lock = FileLock(ledger_file + ".lock")
    with lock:
        restarted = False
        try:
            last = _tail_record(ledger_file)
        except _ChainUnreadable:
            # Unrecoverable, so the chain has to start over -- but silently
            # renumbering from zero is what makes a damaged ledger look like a
            # rewritten one. Move the wreckage aside so it can still be
            # inspected, and mark the first record of the new chain as a
            # restart. A declared discontinuity is a fact the server can act
            # on; an undeclared one is indistinguishable from tampering.
            try:
                os.replace(ledger_file, "{}.damaged.{}".format(
                    ledger_file, int(time.time())))
            except OSError:
                pass
            last, restarted = None, True

        if last is None:
            seq, prev = 0, GENESIS
        else:
            try:
                seq = int(last.get("seq", -1)) + 1
            except (TypeError, ValueError):
                seq, restarted = 0, True
            prev = last.get("hash", GENESIS)

        rec = dict(body)
        if restarted:
            rec["chain_restarted"] = True
        rec["seq"] = seq
        rec["prev_hash"] = prev
        rec["hash"] = record_hash(rec)

        line = canonical(rec) + "\n"
        with open(ledger_file, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    return rec


def head_seq(ledger_file):
    """The highest seq on disk, or -1 for an empty or unreadable ledger.

    Exists so callers that only need "how far along is this log" do not have to
    parse the whole thing. `unsent` reads every record because it has to return
    them; the debounce check on the edit path only needs a count, and paying a
    full-file parse for it on every single tool call is a cost that grows with
    the semester.
    """
    try:
        last = _tail_record(ledger_file)
    except _ChainUnreadable:
        return -1
    if not last:
        return -1
    try:
        return int(last.get("seq", -1))
    except (TypeError, ValueError):
        return -1


def read_all(ledger_file):
    """Every record, plus the line numbers of any that failed to parse."""
    records, bad = [], []
    try:
        with open(ledger_file, "r", encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    bad.append(n)
    except OSError:
        return [], []
    return records, bad


# Chain verification deliberately does not live here. The client has no reason
# to audit a chain it wrote itself, and the copy that matters is the server's:
# it compares an offered record against the one already stored at that seq, so
# the check belongs where the two copies meet. `record_hash` and `canonical`
# above are what the server needs to reproduce, and both are pinned by tests.
