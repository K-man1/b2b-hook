"""Submit a project's sealed prompts: the evidence behind submitted hours.

Logged turns stay on the student's machine. Submitting is the one moment any
prompt text leaves it, and it happens only when the student runs `aiattr
submit`, for one project and an optional time range. What leaves is sealed
boxes, which only the server can open; the student's machine cannot read
them back either.

Order matters and is enforced by the CLI: the ledger is delivered *first*.
Each box is checked against the prompt record the server received at prompt
time, so a server that does not hold those records yet cannot verify
anything, and would be accepting commitments and reveals in the same breath.

The wire format is specified in docs/evidence-spec.md, and core/verify.py is
the reference for what the server does with it.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import VERSION, config, ledger, paths, repoutil, turns

TIMEOUT = 30
SUBMISSIONS_NAME = "submissions.json"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class SubmitError(Exception):
    pass


def normalize_bound(value, end=False):
    """A --since/--until value as a full UTC timestamp, or None.

    Entries are compared as `%Y-%m-%dT%H:%M:%SZ` strings, so a bare date has
    to become the first (or, for --until, the last) second of that day.
    Otherwise `--until 2026-11-21` would silently drop everything on the 21st.
    """
    if not value:
        return None
    value = value.strip()
    if _DATE.match(value):
        return value + ("T23:59:59Z" if end else "T00:00:00Z")
    if _STAMP.match(value):
        return value
    raise SubmitError("dates must look like 2026-11-21 or 2026-11-21T14:30:00Z")


def build_bundle(root, since=None, until=None, program=None):
    """The submission body for one project. Raises SubmitError if empty."""
    rid = paths.repo_id(root)
    entries = turns.collect_sealed(rid, since, until)
    if not entries:
        raise SubmitError("no sealed prompts to submit for this project"
                          + (" in that range" if since or until else ""))
    records, _bad = ledger.read_all(paths.ledger_path(rid))
    last = records[-1] if records else {}
    return {
        "v": 1,
        "client_version": VERSION,
        "student_id": config.student_id(),
        "program": program or None,
        "repo": {"key": rid, "name": repoutil.project_name(root),
                 "remote": repoutil.remote_url(root)},
        "head_commit": repoutil.head_commit(root),
        # Where the client's ledger ends. The server must already hold
        # everything up to here before any box can be trusted.
        "ledger": {"next_seq": int(last.get("seq", -1)) + 1,
                   "head_hash": last.get("hash")},
        "since": since,
        "until": until,
        "prompts": [{"session": e["session"], "turn": e["turn"],
                     "ts": e.get("ts"), "box": e["box"]} for e in entries],
    }


def summarize(bundle):
    """What the student is about to send, in plain numbers."""
    prompts = bundle["prompts"]
    stamps = sorted(p["ts"] for p in prompts if p.get("ts"))
    return {
        "project": bundle["repo"]["name"],
        "prompts": len(prompts),
        "sessions": len({p["session"] for p in prompts}),
        "first": stamps[0] if stamps else None,
        "last": stamps[-1] if stamps else None,
    }


def post(bundle):
    """POST the bundle. Returns the server's JSON reply."""
    url = config.endpoint() + config.submit_path()
    req = urllib.request.Request(
        url,
        data=json.dumps(bundle).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + config.api_key(),
                 "User-Agent": "ai-attribution/" + VERSION},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except OSError:
            pass
        raise SubmitError("server refused the submission ({}): {}".format(
            exc.code, detail or exc.reason))
    except (urllib.error.URLError, OSError) as exc:
        raise SubmitError("could not reach the server: {}".format(exc))
    try:
        reply = json.loads(raw)
    except ValueError:
        raise SubmitError("server sent a reply that is not JSON")
    if not isinstance(reply, dict):
        raise SubmitError("server sent a reply that is not a JSON object")
    return reply


def log_path():
    return os.path.join(paths.plugin_data_dir(), SUBMISSIONS_NAME)


def record(bundle, reply):
    """Remember a submission locally, so a student can see what they sent."""
    path = log_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = []
    if not isinstance(data, list):
        data = []
    s = summarize(bundle)
    data.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "id": reply.get("submission_id"), "repo": bundle["repo"]["key"],
                 "project": s["project"], "program": bundle.get("program"),
                 "prompts": s["prompts"], "since": bundle.get("since"),
                 "until": bundle.get("until")})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
