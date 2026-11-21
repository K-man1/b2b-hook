"""Reference verifier for submissions. Server-side; no hook calls this.

docs/evidence-spec.md says what a server must check when a student submits
sealed prompts. This module is those checks written once, in Python, so the
spec has an executable meaning: the test suite drives a fake server through
it, and anyone building the real server can port it line by line or test
their implementation against it.

It opens boxes with core/seal.open_box, which is fine for tests and for a
reference, but a production server should use a real crypto library (see the
note in core/seal.py). Nothing else here is Python-specific.

Inputs are what a server already holds:

  bundle     the submission body, as posted (see core/submit.py)
  records    this repo's ledger records as the server stored them from the
             record stream, in seq order
  keys       {kid: X25519 private key bytes}, every key still in retention
  received   optional {seq: unix time the server received that record}

Output is a verdict per box plus the turns that should have had one:

  accepted   opened, bound to its record, and matching the commitment
  rejected   [{session, turn, reason}] -- see REASONS
  missing    prompt records in the submitted range that say a sealed copy
             exists but were not submitted. A student who submits only
             their best turns shows up here.
  ledger_ok  whether the server's copy of the ledger covers the submission
"""

import calendar
import hmac
import json
import time

from . import seal, turns

# How long after a prompt its commitment may reach the server before it is
# flagged. Records stream within seconds when online; an hour covers patchy
# wifi. A commitment that arrives days later was not a commitment at the time,
# so it is flagged rather than trusted.
MAX_COMMIT_LAG = 3600

REASONS = {
    "ledger_incomplete": "the server's ledger copy does not reach the head the client reported",
    "duplicate": "the same session and turn was submitted twice",
    "unknown_key": "sealed to a key this server does not hold",
    "no_commitment": "no prompt record for this session and turn",
    "not_marked_sealed": "the prompt record says no sealed copy was kept",
    "does_not_open": "the box does not open against its record's context",
    "malformed": "the box opened but its contents are not a prompt",
    "commitment_mismatch": "the text does not match the hash streamed at prompt time",
    "turn_mismatch": "the box names a different turn than its record",
}


def session_key_of(record):
    """The session key a record was sealed under (core/turns.key)."""
    return record.get("session_id") or "nosession:" + (record.get("agent") or "")


def _epoch(ts):
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def ledger_covers(bundle, records):
    """The server must already hold every record up to the client's head.

    Opening a box proves its text matches a commitment the server holds. If
    the server's copy stops short of where the client says the ledger ends,
    some commitments are arriving with the submission itself, and a
    commitment sent alongside its reveal commits to nothing.
    """
    head = bundle.get("ledger") or {}
    want = head.get("next_seq")
    if not isinstance(want, int) or want < 0:
        return False
    if want == 0:
        return True
    if len(records) < want:
        return False
    rec = records[want - 1]
    return rec.get("seq") == want - 1 and rec.get("hash") == head.get("head_hash")


def verify_submission(bundle, records, keys, received=None):
    rid = (bundle.get("repo") or {}).get("key")
    result = {"accepted": [], "rejected": [], "missing": [],
              "ledger_ok": ledger_covers(bundle, records)}

    prompts = {}
    for rec in records:
        if rec.get("kind") == "prompt":
            prompts.setdefault((session_key_of(rec), rec.get("turn")), []).append(rec)

    seen = set()
    submitted = set()
    for entry in bundle.get("prompts") or []:
        session, turn, box = entry.get("session"), entry.get("turn"), entry.get("box") or {}
        where = {"session": session, "turn": turn}

        def reject(reason):
            result["rejected"].append(dict(where, reason=reason))

        if not result["ledger_ok"]:
            reject("ledger_incomplete")
            continue
        if (session, turn) in seen:
            reject("duplicate")
            continue
        seen.add((session, turn))
        sk = keys.get(box.get("kid"))
        if sk is None:
            reject("unknown_key")
            continue
        candidates = prompts.get((session, turn)) or []
        if not candidates:
            reject("no_commitment")
            continue

        # Normally one candidate. More than one means the turn counter was
        # reset (state expired, then the session resumed); the commitment
        # baked into the AAD is what picks the right record.
        opened = rec = None
        for cand in candidates:
            try:
                opened = seal.open_box(sk, box, turns.prompt_aad(
                    rid, session, turn, cand.get("prompt_hmac")))
                rec = cand
                break
            except (seal.SealError, ValueError, KeyError, TypeError):
                continue
        if rec is None:
            reject("does_not_open")
            continue
        submitted.add(rec.get("seq"))
        if rec.get("prompt_sealed") is not True:
            reject("not_marked_sealed")
            continue
        try:
            plain = json.loads(opened.decode("utf-8"))
            salt, text = plain["salt"], plain["text"]
            bytes.fromhex(salt)
            if not isinstance(text, str):
                raise TypeError("text")
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            reject("malformed")
            continue
        if plain.get("turn") != turn:
            reject("turn_mismatch")
            continue
        if not hmac.compare_digest(turns.salted_hash(salt, text),
                                   str(rec.get("prompt_hmac") or "")):
            reject("commitment_mismatch")
            continue

        flags = []
        if received is not None:
            got, ts = received.get(rec.get("seq")), _epoch(rec.get("ts"))
            if got is None or ts is None or got - ts > MAX_COMMIT_LAG:
                flags.append("late_commitment")
            elif ts - got > 300:
                flags.append("clock_ahead")
        if rec.get("no_session"):
            flags.append("no_session")
        result["accepted"].append(dict(where, ts=rec.get("ts"), seq=rec.get("seq"),
                                       text=text, flags=flags))

    since, until = bundle.get("since"), bundle.get("until")
    for group in prompts.values():
        for rec in group:
            ts = rec.get("ts") or ""
            if since and ts < since or until and ts > until:
                continue
            if rec.get("prompt_sealed") is True and rec.get("seq") not in submitted:
                result["missing"].append({"session": session_key_of(rec),
                                          "turn": rec.get("turn"),
                                          "ts": rec.get("ts")})
    result["missing"].sort(key=lambda m: m["ts"] or "")
    return result
