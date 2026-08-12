"""Client configuration: who this student is and where to report.

Written once by the website's "Install to Claude Code" flow, then read by the
sync hook. Kept separate from the snapshot store so that clearing tracking data
does not silently un-enrol the student.

Nothing here is a security boundary. The API key sits in a file on a machine the
student controls, so it identifies them, it does not authenticate them in any
adversarial sense. Integrity still comes from server-side verification of pushed
git history, never from trusting this file.
"""

import json
import os

from . import paths

CONFIG_NAME = "config.json"
DEFAULT_ENDPOINT = ""
DEFAULT_SYNC_PATH = "/api/attribution/sync"
DEFAULT_RECORDS_PATH = "/api/attribution/records"

# How hard the streaming hook tries. Both are overridable per-install because
# the right values depend on the server, not the student.
DEFAULT_STREAM_INTERVAL = 20    # seconds between sends during active work
DEFAULT_STREAM_BURST = 25       # unsent records that trigger a send regardless


def config_path():
    return os.path.join(paths.plugin_data_dir(), CONFIG_NAME)


def load():
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data):
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    # Created 0600 rather than chmod'ed to 0600 after the fact. The old order
    # wrote the API key through a temp file under the default umask and only
    # tightened permissions once the rename had happened, leaving the key
    # world-readable for the duration of the write on any machine with a
    # permissive umask.
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def insecure_endpoint(url):
    """Why this endpoint should not carry a bearer token, or None if it is fine.

    The API key travels as an Authorization header on every send, so the scheme
    is not cosmetic. `endpoint` is a plain config field a student can point
    anywhere, and nothing checked it: `http://` shipped the key in cleartext to
    whatever host was named. Localhost is exempt because it never leaves the
    machine and is how the plugin is developed against a local server.
    """
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("https://"):
        return None
    if not url.startswith("http://"):
        return "endpoint must be an http:// or https:// URL"
    host = url[len("http://"):].split("/")[0].split(":")[0].lower()
    if host in ("localhost", "127.0.0.1", "::1", "[::1]"):
        return None
    return ("endpoint is http://, so your API key would be sent in cleartext. "
            "Use https://")


def api_key():
    return (load().get("api_key") or "").strip()


def endpoint():
    return (load().get("endpoint") or DEFAULT_ENDPOINT).strip().rstrip("/")


def student_id():
    return (load().get("student_id") or "").strip()


def _route(value, default):
    """A configured route, forced to stay a path on the configured host.

    Both routes are concatenated onto `endpoint` as raw strings. A value that
    does not begin with a single `/` does not have to stay a path: `//evil.tld/`
    reads as a protocol-relative URL and `https://evil.tld` replaces the host
    outright, either of which sends the bearer token somewhere else entirely.
    """
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return default
    return value


def sync_path():
    """Path the index is posted to, appended to the endpoint.

    Overridable so a server can move the route without every installed client
    breaking. Defaults to the Back to Basics convention, which namespaces
    integrations under /api/<integration>/ (see /api/hackatime/*).
    """
    return _route(load().get("sync_path"), DEFAULT_SYNC_PATH)


def records_path():
    """Path individual ledger records are streamed to.

    Separate from sync_path because the two carry different things and have
    different trust levels. The sync payload is a self-reported roll-up used to
    draw the picker; this one is the raw append-only record stream, which the
    server stores immutably and later verifies against the repo. Keeping them
    on distinct routes means a server can rate-limit or retain them differently.
    """
    return _route(load().get("records_path"), DEFAULT_RECORDS_PATH)


def stream_interval():
    try:
        return max(0, int(load().get("stream_interval", DEFAULT_STREAM_INTERVAL)))
    except (TypeError, ValueError):
        return DEFAULT_STREAM_INTERVAL


def stream_burst():
    try:
        return max(1, int(load().get("stream_burst", DEFAULT_STREAM_BURST)))
    except (TypeError, ValueError):
        return DEFAULT_STREAM_BURST


def sync_enabled():
    """Reporting requires both an endpoint and a key. Absent either, the plugin
    still tracks locally and stays entirely offline.

    The endpoint is re-checked here, not only where it is set. `configure`
    refuses an insecure one, but config.json is a plain file and hand-editing
    it walked straight past that check into a send that puts the API key on the
    wire in cleartext. Validating at the point of use is what makes the rule
    hold regardless of how the value got there.
    """
    cfg = load()
    if cfg.get("sync") is False:
        return False
    if not (cfg.get("api_key") and cfg.get("endpoint")):
        return False
    return insecure_endpoint(cfg.get("endpoint")) is None


def ignored_repos():
    """Repo paths the student opted out of tracking.

    System-wide installation means the plugin sees every git repo the student
    opens, including ones that have nothing to do with the course. An opt-out
    list is the minimum courtesy; without it the plugin would write a ledger
    into unrelated personal projects.
    """
    return [os.path.realpath(os.path.expanduser(p))
            for p in load().get("ignore", []) if isinstance(p, str)]


def is_ignored(root):
    real = os.path.realpath(root)
    for ign in ignored_repos():
        if real == ign or real.startswith(ign.rstrip(os.sep) + os.sep):
            return True
    return False
