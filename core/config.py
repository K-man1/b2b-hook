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
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def api_key():
    return (load().get("api_key") or "").strip()


def endpoint():
    return (load().get("endpoint") or DEFAULT_ENDPOINT).strip().rstrip("/")


def student_id():
    return (load().get("student_id") or "").strip()


def sync_path():
    """Path the index is posted to, appended to the endpoint.

    Overridable so a server can move the route without every installed client
    breaking. Defaults to the Back to Basics convention, which namespaces
    integrations under /api/<integration>/ (see /api/hackatime/*).
    """
    return (load().get("sync_path") or DEFAULT_SYNC_PATH).strip()


def records_path():
    """Path individual ledger records are streamed to.

    Separate from sync_path because the two carry different things and have
    different trust levels. The sync payload is a self-reported roll-up used to
    draw the picker; this one is the raw append-only record stream, which the
    server stores immutably and later verifies against the repo. Keeping them
    on distinct routes means a server can rate-limit or retain them differently.
    """
    return (load().get("records_path") or DEFAULT_RECORDS_PATH).strip()


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
    still tracks locally and stays entirely offline."""
    cfg = load()
    if cfg.get("sync") is False:
        return False
    return bool(cfg.get("api_key")) and bool(cfg.get("endpoint"))


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
