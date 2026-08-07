"""Index of every repo this student has worked in.

This is what makes the plugin system-wide rather than per-project. Installed at
user scope, the hooks fire in every git repo the student opens, and each one
gets an entry here. The website's project picker is a view over this index, the
same way Hackatime lets you pick which folders counted toward a project after
the fact.

Deliberately holds metadata only. The picker needs to show a recognisable name
and some activity, so it stores repo name, remote URL, timestamps and aggregate
line counts. It does not store absolute paths, file names, or any source: an
absolute path leaks the student's home directory layout and real names, and the
picker does not need it. Local identification uses the same hashed repo id the
snapshot store already uses.
"""

import json
import os
import time

from . import config, ledger, paths

INDEX_NAME = "index.json"
INDEX_VERSION = 1


def index_path():
    return os.path.join(paths.plugin_data_dir(), INDEX_NAME)


def load():
    try:
        with open(index_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"v": INDEX_VERSION, "repos": {}}
    if not isinstance(data, dict) or data.get("v") != INDEX_VERSION:
        return {"v": INDEX_VERSION, "repos": {}}
    data.setdefault("repos", {})
    return data


def _write(data):
    path = index_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def remove(rid):
    """Drop a repo from the index entirely.

    Used when a student opts out. Stopping future tracking is not enough: the
    existing entry would keep being reported, so opting out of a personal repo
    would still leak its name and remote to the server.
    """
    with ledger.FileLock(index_path() + ".lock"):
        data = load()
        if data["repos"].pop(rid, None) is not None:
            _write(data)
            return True
    return False


def update(rid, name, remote, totals=None, ledger_head=None,
           ledger_records=None, path=None):
    """Upsert one repo's entry. Locked, because hooks run concurrently.

    The same lock discipline as the ledger: several sessions can be open at
    once, and a torn write here would drop projects out of the student's picker
    with no obvious cause.
    """
    os.makedirs(paths.plugin_data_dir(), exist_ok=True)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with ledger.FileLock(index_path() + ".lock"):
        data = load()
        entry = data["repos"].get(rid, {})
        entry.setdefault("first_seen", now)
        entry["name"] = name
        entry["last_activity"] = now
        if remote is not None:
            entry["remote"] = remote
        if totals is not None:
            entry["totals"] = totals
        if ledger_head is not None:
            entry["ledger_head"] = ledger_head
        if ledger_records is not None:
            entry["ledger_records"] = ledger_records
        if path is not None:
            # Local only. Needed to honour the opt-out list, which is expressed
            # as paths while the index is keyed by hashed id. Stripped from
            # anything sent off the machine; see sync_payload.
            entry["path"] = os.path.realpath(path)
        data["repos"][rid] = entry
        _write(data)
    return True


def touch(rid, name, remote=None, path=None):
    """Cheap activity ping, used on session start before totals are known."""
    return update(rid, name, remote, path=path)


def projects(include_ignored=False):
    """Every tracked repo, most recently active first. The picker's data source."""
    data = load()
    rows = []
    for rid, entry in data.get("repos", {}).items():
        if not include_ignored:
            p = entry.get("path")
            if p and config.is_ignored(p):
                continue
        row = dict(entry)
        row["id"] = rid
        rows.append(row)
    rows.sort(key=lambda r: r.get("last_activity", ""), reverse=True)
    return rows


def sync_payload(student_id, client_version):
    """Metadata-only body for the reporting endpoint.

    Everything in here is aggregate. No file paths, no file names, no source
    text, no prompts. If this payload leaked it would reveal which repos a
    student worked on and how much code was AI-written, and nothing else.
    """
    return {
        "client_version": client_version,
        "student_id": student_id,
        "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "projects": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "remote": p.get("remote"),
                "first_seen": p.get("first_seen"),
                "last_activity": p.get("last_activity"),
                "totals": p.get("totals"),
                "ledger_head": p.get("ledger_head"),
                "ledger_records": p.get("ledger_records"),
            }
            for p in projects()
        ],
    }
