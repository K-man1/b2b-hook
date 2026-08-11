"""Report the AI/human line split to Hackatime as WakaTime heartbeats.

Hackatime's heartbeat schema already carries the exact number this plugin
computes. `ai_line_changes` and `human_line_changes` are real columns, are on
the API's permit list, and are part of the dedup `fields_hash`. Nothing in the
Hackatime tree populates them outside its own tests, because the editor plugins
that send heartbeats have no idea which lines an agent wrote. This plugin does:
`provenance.retag` separates them on every edit. So this module is the missing
producer, not a second tracking system.

What that buys, and it is the whole reason to prefer this over our own
transport: the student's hours and the AI split end up on the same row of the
same table, keyed to the same user, instead of in two systems that have to be
joined on a folder name and hoped for. Hackatime's own anti-forgery machinery
(JA4 TLS fingerprints, per-heartbeat IP, trust levels) then applies to our
numbers for free.

THE TRAP, and how it is defused:

    Heartbeats are how time is computed. Hackatime sums the gap between
    consecutive heartbeats, each gap capped at the 2-minute timeout
    (Heartbeatable#duration_seconds). So a heartbeat landing after the student
    stopped working can add up to two minutes of billable time it did not earn,
    and at $5/hr uncapped that is the exact fraud the rest of this codebase
    exists to detect. A plugin that fired on every agent write would be minting
    currency for a student who walked away mid-task.

    The fix is theirs, not ours: every heartbeat here is `category = "ai
    coding"`, and their stats API drops that category from time totals whenever
    a caller passes `no_ai_coding=true`. The rows still carry the line split;
    they simply stop being hours. Back to Basics asks for payable time with that
    flag set and agent edits contribute nothing.

    Two further guards, because a category is only honoured by callers who pass
    the flag: we emit only for moments at which an edit actually landed, never
    inventing one to fill a gap, and the whole path stays behind
    `hackatime.enabled`.

Two rules inherited from the rest of the plugin: never block a session, and
never send source text. Accumulation happens inline on the edit hook because it
is a file write with no network in it; delivery happens on the streaming hook,
which is already wired async and already debounced.
"""

import json
import os
import time
import urllib.error
import urllib.request

from . import VERSION, agents, config, ledger, paths

STATE_NAME = "hackatime.json"
STATE_VERSION = 1

# Minimum seconds between deliveries during active work. Matched to Hackatime's
# own 2-minute heartbeat timeout: gaps are capped there when time is summed, so
# sending more often than that buys no accuracy and only costs requests. The
# streaming hook fires on every tool call, so without this gate an active
# session would POST once per edit.
SEND_INTERVAL = 120

# How long a bucket holding only human lines waits for an agent edit on the same
# file to merge into it. We never send human-only rows (the student's editor
# already reported that work, and a second row would inflate their paid time),
# but discarding one the instant it appears throws away context that was about
# to be useful. After this it is genuinely orphaned, so it goes.
HUMAN_ONLY_TTL = 900

# Hackatime caps a bulk post at 100 (MAX_BULK_HEARTBEATS in their controller).
MAX_BATCH = 100

# Buckets waiting on a failed network. Kept small: this is time tracking, not
# evidence, and the ledger is still the record that matters. Dropping the oldest
# is better than growing without bound in a repo someone left open for a week.
MAX_PENDING = 500

TIMEOUT = 8

DEFAULT_API_URL = "https://hackatime.hackclub.com/api/hackatime/v1"

# Their parser reads a bare product name fine; several of their own regression
# tests use `claude` with no version. `editor` is also sent explicitly below,
# so recognition does not depend on this string being parsed correctly.
#
# Both of these used to be module constants naming Claude Code. They are now
# per-heartbeat, resolved from the agent that recorded the bucket, because a
# student can drive the same repo with more than one agent and a row that
# named the wrong one would be a false statement about who wrote the line.
# See core/agents.py.

# NOT "coding". Hackatime's stats API takes `no_ai_coding=true`, which drops
# `category = "ai coding"` out of every time total it reports
# (stats_controller.rb, and `excluded_categories` in Heartbeatable). Tagging our
# heartbeats this way is what keeps agent edits from paying out: the rows still
# carry the line split, they just stop counting as hours the moment a caller
# asks for payable time. Their mechanism, not a trick of ours.
CATEGORY = "ai coding"

LANGUAGES = {
    ".c": "C", ".cc": "C++", ".cpp": "C++", ".cs": "C#", ".css": "CSS",
    ".go": "Go", ".h": "C", ".hpp": "C++", ".html": "HTML", ".java": "Java",
    ".js": "JavaScript", ".json": "JSON", ".jsx": "JavaScript", ".kt": "Kotlin",
    ".lua": "Lua", ".md": "Markdown", ".php": "PHP", ".py": "Python",
    ".rb": "Ruby", ".rs": "Rust", ".scss": "SCSS", ".sh": "Bash",
    ".sql": "SQL", ".swift": "Swift", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".yaml": "YAML", ".yml": "YAML",
}


def language_for(rel):
    return LANGUAGES.get(os.path.splitext(rel)[1].lower())


# --- credentials ----------------------------------------------------------
#
# Read from ~/.wakatime.cfg, the file every WakaTime-compatible editor plugin
# already uses. Worth being precise about why this is not the credential
# handling the rest of the codebase refuses to do: the key is the student's own,
# it stays on their machine, and it is sent to the one service it belongs to.
# It is never forwarded to the Back to Basics server, which is what would make
# it exfiltration rather than use.


def wakatime_cfg_path():
    home = os.environ.get("WAKATIME_HOME") or os.path.expanduser("~")
    return os.path.join(home, ".wakatime.cfg")


def wakatime_settings():
    """The [settings] section of ~/.wakatime.cfg, or {} if unreadable.

    Parsed by hand rather than with configparser because the file is frequently
    written by editor plugins with duplicate keys and stray whitespace, and
    configparser raises on duplicates. A hook may not raise.
    """
    out = {}
    section = None
    try:
        with open(wakatime_cfg_path(), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip().lower()
                    continue
                if section != "settings" or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                out[key.strip().lower()] = value.strip()
    except OSError:
        return {}
    return out


def api_key():
    cfg = config.load().get("hackatime") or {}
    return (cfg.get("api_key")
            or wakatime_settings().get("api_key")
            or "").strip()


def api_url():
    """Base URL for the heartbeat API, normalised to end at /api/hackatime/v1.

    wakatime.cfg's api_url is written by whichever setup script ran last, and
    turns up both with and without the version suffix. Appending blindly gives
    a 404 half the time, so normalise instead of trusting it.
    """
    cfg = config.load().get("hackatime") or {}
    url = (cfg.get("api_url")
           or wakatime_settings().get("api_url")
           or DEFAULT_API_URL).strip().rstrip("/")
    if url.endswith("/heartbeats"):
        url = url[: -len("/heartbeats")]
    if url.endswith("/users/current"):
        url = url[: -len("/users/current")]
    return url


def enabled():
    """Off unless explicitly turned on AND a key exists.

    Two gates rather than one. The key alone is not consent: nearly every
    student has a wakatime.cfg already, so keying off its presence would opt
    everyone in to having agent time counted, silently. See the trap above.
    """
    cfg = config.load().get("hackatime") or {}
    return bool(cfg.get("enabled")) and bool(api_key())


# --- accumulation ---------------------------------------------------------
#
# One bucket per (project, file). An agent editing the same file eight times in
# a minute should produce one heartbeat carrying the total, not eight heartbeats
# each claiming their own moment in time. Buckets close when the streaming hook
# flushes them.


def state_path():
    return os.path.join(paths.plugin_data_dir(), STATE_NAME)


def _load():
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"v": STATE_VERSION, "pending": []}
    if not isinstance(data, dict) or data.get("v") != STATE_VERSION:
        return {"v": STATE_VERSION, "pending": []}
    data.setdefault("pending", [])
    return data


def _write(data):
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, path)


def record(rid, project, rel, ai_lines=0, human_lines=0, session_id="",
           branch=None, agent=None):
    """Add one edit's line split to the open bucket for that file.

    Called from the edit hook, so it must stay cheap and must not touch the
    network. Locked, because Claude issues tool calls in parallel and two hooks
    merging into the same bucket would otherwise lose one of them.

    `rid` is stored but never sent. It is what lets forget() purge a repo's
    undelivered buckets when the student opts out; matching on the project name
    alone would be ambiguous across two checkouts sharing a basename.
    """
    if not (ai_lines or human_lines):
        return
    # Gated here rather than at every call site: an install that has not opted
    # in should do no work at all, not accumulate state it will never send.
    if not enabled():
        return
    now = time.time()
    slug = (agent or agents.DEFAULT)
    with ledger.FileLock(state_path() + ".lock"):
        data = _load()
        for bucket in data["pending"]:
            # The agent is part of the merge key, not just cargo. Two agents
            # touching one file in one window are two different claims about
            # authorship, and folding them together would attribute a block of
            # Cursor's lines to whichever tool happened to open the bucket.
            if (bucket.get("rid") == rid and bucket.get("entity") == rel
                    and bucket.get("agent", agents.DEFAULT) == slug):
                bucket["ai"] = bucket.get("ai", 0) + ai_lines
                bucket["human"] = bucket.get("human", 0) + human_lines
                bucket["time"] = now
                break
        else:
            data["pending"].append({
                "rid": rid,
                "project": project,
                "entity": rel,
                "language": language_for(rel),
                "ai": ai_lines,
                "human": human_lines,
                "time": now,
                "session": session_id,
                "branch": branch,
                "agent": slug,
            })
        if len(data["pending"]) > MAX_PENDING:
            data["pending"] = data["pending"][-MAX_PENDING:]
        _write(data)


def forget(rid):
    """Drop a repo's undelivered buckets. Used when a student opts the repo out.

    The same discipline registry.remove() and outbox.forget() already follow,
    and for the same reason: ceasing to track a repo is not enough on its own.
    Buckets recorded before the opt-out would otherwise still be delivered, so
    opting a personal project out would send its name and line counts anyway.
    """
    with ledger.FileLock(state_path() + ".lock"):
        data = _load()
        keep = [b for b in data["pending"] if b.get("rid") != rid]
        if len(keep) == len(data["pending"]):
            return False
        data["pending"] = keep
        _write(data)
    return True


def build(bucket):
    """One WakaTime-shaped heartbeat.

    `entity` is the repo-relative path, not the absolute one every editor plugin
    sends. Absolute paths leak the student's home directory layout and real
    name, which registry.py already refuses to send anywhere, and Hackatime does
    not need it to attribute the row. The cost is that these rows will not
    collapse against the editor's own heartbeats for the same file, which is
    correct: they are genuinely different observations.
    """
    # Buckets written before agents.py existed carry no `agent` key. They came
    # from the only agent that could produce one, so defaulting is accurate
    # rather than merely convenient.
    identity = agents.resolve(bucket.get("agent"))
    hb = {
        "entity": bucket["entity"],
        "type": "file",
        "project": bucket["project"],
        "time": round(bucket["time"], 6),
        "is_write": True,
        "category": CATEGORY,
        "editor": identity["editor"],
        "plugin": agents.UA.format(identity["editor"], VERSION),
        "ai_line_changes": int(bucket.get("ai", 0)),
        "human_line_changes": int(bucket.get("human", 0)),
    }
    if bucket.get("language"):
        hb["language"] = bucket["language"]
    if bucket.get("session"):
        hb["ai_session"] = bucket["session"]
    if bucket.get("branch"):
        hb["branch"] = bucket["branch"]
    model = (config.load().get("hackatime") or {}).get("ai_model")
    if model:
        # Only ever what the install was told. Guessing a model string would put
        # a fact on a record that nothing actually observed.
        hb["ai_model"] = model
    return hb


def post(batch):
    """POST a bulk batch. Rails wraps a top-level JSON array into params[:_json],
    which is the shape their bulk endpoint reads, so the body is a bare array."""
    url = api_url() + "/users/current/heartbeats.bulk"
    req = urllib.request.Request(
        url,
        data=json.dumps(batch).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key(),
            # Deliberately not the agent's name. One batch can carry rows from
            # several agents, so the transport identifies the sender (this
            # plugin) while each heartbeat's own `editor`/`plugin` field
            # identifies who wrote that line.
            "User-Agent": agents.UA.format("ai-attribution", VERSION),
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status


def flush(force=False):
    """Send every closed bucket. Returns how many heartbeats were delivered.

    Rate-limited unless `force`, because the streaming hook that calls this runs
    on every tool call. Ungated, an active session would POST once per edit and
    every edit would land as its own Hackatime row instead of one heartbeat
    carrying the total. Session start and session end pass force=True: those
    happen once and should not leave work sitting in the queue.

    Failure is a non-event: the buckets stay pending and the next session start
    tries again. Unlike the ledger stream there is no watermark to protect,
    because a lost heartbeat costs a little tracked time and nothing else. The
    ledger remains the thing with evidentiary weight.
    """
    if not enabled():
        return 0

    now = time.time()
    with ledger.FileLock(state_path() + ".lock"):
        data = _load()
        if not force and now - float(data.get("last_send") or 0) < SEND_INTERVAL:
            return 0

        # Buckets with no agent lines are never sent. The student typed those
        # lines in an editor that already reported them to Hackatime, so a
        # heartbeat from us would be a second row for the same minutes and would
        # inflate their paid time. They are kept for a while rather than dropped
        # on sight, because an agent edit landing on the same file merges into
        # the bucket and carries the human count along as context.
        ready, held = [], []
        for b in data["pending"]:
            if b.get("ai"):
                ready.append(b)
            elif now - float(b.get("time") or 0) < HUMAN_ONLY_TTL:
                held.append(b)

        if not ready:
            if len(held) != len(data["pending"]):
                data["pending"] = held
                _write(data)
            return 0

        batch, rest = ready[:MAX_BATCH], ready[MAX_BATCH:]
        data["pending"] = rest + held
        data["last_send"] = now
        _write(data)

    try:
        post([build(b) for b in batch])
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        # `last_send` deliberately stays where the successful path put it, so a
        # machine that is offline backs off for the interval instead of retrying
        # on every tool call. Session start and end pass force=True and are not
        # subject to it.
        with ledger.FileLock(state_path() + ".lock"):
            data = _load()
            data["pending"] = (batch + data["pending"])[-MAX_PENDING:]
            _write(data)
        return 0
    return len(batch)
