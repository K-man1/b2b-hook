"""Turns: the unit that separates steering an agent from watching one.

Hackatime already counts AI coding minutes. What it cannot see is whether a
human was doing anything during them: a four-hour autonomous run and four hours
of prompt, read, test, correct, prompt again both land as four hours of
`ai coding`. The difference lives in the shape of each turn, so this module
records turns.

A turn is one prompt and the agent run it starts, ending at Stop. Each one
leaves two ledger records, written by hooks/prompt.py and hooks/stop.py:

  prompt   what the human brought to the turn: how long since the agent last
           stopped (the review gap), whether the previous turn was cut off,
           how many lines they changed by hand in between, the permission
           mode, and the size of the prompt.
  stop     what the agent did with it: how long it ran, how many edits and
           AI-written lines, and how many edits it attempted that never
           landed (denied, or failed).

WHO MEASURES, AND WHY IT IS NOT THE MODEL

Every number here is taken by a hook process, outside the model, from
timestamps and files on disk. None of it is the agent's account of what
happened. That is deliberate: everything in a model's context is the
student's to write -- CLAUDE.md, the prompt, a pasted file -- so a model asked
to describe the session can be told what to say. A hook cannot. An instruction
in CLAUDE.md changes what the agent writes; it does not change when the
UserPromptSubmit hook fired or what the file on disk contains afterwards.

Judging is not done here either. These records are facts, not a verdict on
whether a turn was real iteration. The verdict belongs on the server, where
the student cannot read the rule they are being measured against.

PROMPT TEXT NEVER ENTERS THE LEDGER

The ledger streams to a server, so it carries the prompt's length and a
salted hash, never its text. The text is kept locally in prompts.jsonl (0600)
next to the turn state, because it is the evidence a reviewer will eventually
need for a submitted hour. Sealing it for that upload is a separate step;
until then it does not leave the machine, and it is purged after
RETENTION_DAYS.

The hash is salted per prompt because a plain one leaks exactly the prompts
that matter most. "continue", "fix it" and "keep going" are a handful of
strings; anyone holding the ledger could hash them and read those turns back.
The salt is stored only beside the text, so the hash can be checked against
the text once it is unsealed, and not before.
"""

import hashlib
import hmac
import json
import os
import time

from . import ledger, paths

STATE_VERSION = 1

# Raw prompt text is the most sensitive thing this plugin holds. Kept long
# enough to cover a program's submission window, then deleted.
RETENTION_DAYS = 45

# Files reconciled for hand edits at each prompt. Bounded so a student who
# regenerated half the repo between turns does not stall their next prompt;
# anything past the cap is still picked up by the next turn or session sweep.
MAX_RECONCILE_FILES = 200

# Ledger kinds that are an agent acting on the repo during a turn.
_EDIT_KINDS = ("edit", "bulk_change")


def key(ctx):
    """The id turn state is kept under for this hook invocation's session.

    Some agents send no session id. All of those used to share one
    "nosession" slot per repo, so Cursor's turns and Codex's turns in the
    same repo were numbered as one conversation and closed each other as
    interrupted. Falling back to the agent keeps different tools apart. Two
    sessions of the same id-less agent still share a slot, which nothing on
    this side can fix, so their records carry `no_session` for the server.
    """
    return ctx.get("session_id") or "nosession:" + (ctx.get("agent") or "")


def _session_key(session_key):
    # Hashed: session ids come from the agent, and some agents use values that
    # are not safe as filenames.
    raw = session_key or "nosession"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def turns_dir(rid):
    return os.path.join(paths.plugin_data_dir(), "turns", rid)


def state_path(rid, session_id):
    return os.path.join(turns_dir(rid), _session_key(session_id) + ".json")


def prompts_path(rid, session_id):
    return os.path.join(turns_dir(rid), _session_key(session_id) + ".prompts.jsonl")


def _fresh():
    return {"v": STATE_VERSION, "turn": 0, "open": None, "last_stop": None}


def load(rid, session_id):
    try:
        with open(state_path(rid, session_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _fresh()
    if not isinstance(data, dict) or data.get("v") != STATE_VERSION:
        return _fresh()
    for key, value in _fresh().items():
        data.setdefault(key, value)
    return data


def save(rid, session_id, data):
    path = state_path(rid, session_id)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, path)


def lock(rid, session_id):
    """One lock per session. Prompt and Stop are sequential within a session,
    but a resumed session can overlap its own previous process briefly."""
    path = state_path(rid, session_id) + ".lock"
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    return ledger.FileLock(path)


def ledger_offset(ledger_file):
    """Byte length of the ledger now. Everything appended after this point
    belongs to the turn that is starting, so Stop reads only that slice rather
    than parsing a whole semester of records once per turn."""
    try:
        return os.path.getsize(ledger_file)
    except OSError:
        return 0


def records_since(ledger_file, offset):
    """Records appended after `offset`.

    If the file is now shorter than the offset, the ledger was replaced (a
    damaged chain is moved aside and restarted), so the whole new file is
    this turn's.
    """
    try:
        size = os.path.getsize(ledger_file)
    except OSError:
        return []
    start = offset if 0 <= offset <= size else 0
    out = []
    try:
        with open(ledger_file, "rb") as fh:
            fh.seek(start)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line.decode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    continue
    except OSError:
        return []
    return out


def salted_hash(salt, text):
    """HMAC-SHA256 of the prompt, keyed by its own random salt."""
    return hmac.new(bytes.fromhex(salt), (text or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()


def prompt_features(text):
    """Size and fingerprint of a prompt, and the salt behind the fingerprint.

    Mechanical only. Whether a prompt is specific, or a bare "fix it", is a
    judgement, and judgements are made on the server from the sealed text.
    What is computed here is what a reviewer would want to line up against
    that text: how big it was, and a hash proving which text it was.

    Returns (fields, salt). The salt goes into local storage with the text and
    never into the ledger; see the module docstring.
    """
    text = text or ""
    salt = os.urandom(16).hex()
    return {
        "prompt_chars": len(text),
        "prompt_lines": text.count("\n") + 1 if text else 0,
        "prompt_hmac": salted_hash(salt, text),
        "prompt_code_fences": text.count("```") // 2,
        "prompt_is_command": text.lstrip().startswith("/"),
    }, salt


def summarize_turn(records, ctx):
    """What the agent did during one turn, from the ledger slice it produced.

    Filtered by session as well as by position, because two sessions in the
    same repo share one ledger and their records interleave. With no session
    id the agent is the only thing left to tell them apart.
    """
    edits = ai_lines = ai_sig = removed = 0
    files = set()
    for rec in records:
        if rec.get("session_id") != ctx["session_id"]:
            continue
        if not ctx["session_id"] and rec.get("agent") != ctx["agent"]:
            continue
        kind = rec.get("kind")
        if kind == "edit":
            edits += 1
            ai_lines += int(rec.get("lines_ai") or 0)
            ai_sig += int(rec.get("sig_ai") or 0)
            removed += int(rec.get("lines_removed") or 0)
            if rec.get("path"):
                files.add(rec["path"])
        elif kind == "bulk_change":
            edits += 1
            ai_lines += int(rec.get("lines") or 0)
            ai_sig += int(rec.get("sig_lines") or 0)
            removed += int(rec.get("lines_removed") or 0)
    return {"edits": edits, "lines_ai": ai_lines, "sig_ai": ai_sig,
            "lines_removed": removed, "files": sorted(files)}


def unfinished_edits(rid, since):
    """Edits the agent started during this turn that never produced a result.

    PreToolUse stashes a before-image and PostToolUse consumes it. A stash
    still on disk at Stop is a tool call that was denied, or failed: the
    human said no, or the agent got it wrong. Either is part of how the turn
    went. Counted by mtime, so a second session in the same repo at the same
    moment can be counted here too; that is rare and over-counts steering by
    one, which the server weighs alongside everything else.
    """
    directory = paths.pending_dir(rid)
    count = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            if os.path.getmtime(os.path.join(directory, name)) >= since:
                count += 1
        except OSError:
            continue
    return count


def store_prompt(rid, session_id, turn, ts, text, salt):
    """Keep the raw prompt, and the salt for its hash, locally.

    Never sent by anything in this version. The salt sits here and nowhere
    else, which is what keeps the ledger's prompt_hmac unreadable until this
    file is sealed and submitted.
    """
    path = prompts_path(rid, session_id)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"turn": turn, "ts": ts, "salt": salt,
                             "text": text}, ensure_ascii=False) + "\n")


def prune(max_age_days=RETENTION_DAYS):
    """Delete turn state and stored prompts older than the retention window."""
    base = os.path.join(paths.plugin_data_dir(), "turns")
    cutoff = time.time() - max_age_days * 86400
    try:
        rids = os.listdir(base)
    except OSError:
        return 0
    removed = 0
    for rid in rids:
        directory = os.path.join(base, rid)
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    removed += 1
            except OSError:
                pass
        try:
            os.rmdir(directory)
        except OSError:
            pass
    return removed


def forget(rid):
    """Drop everything recorded about a repo's turns. Used by `aiattr ignore`."""
    directory = turns_dir(rid)
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    for name in names:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass
    try:
        os.rmdir(directory)
    except OSError:
        pass
    return True
