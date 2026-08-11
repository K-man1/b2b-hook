"""SessionStart: sweep for out-of-session edits, then attest the environment.

Between sessions the student may have written code by hand, in another editor,
or pasted it in from a browser. None of that fires a tool hook. The sweep here
catches all of it as *observed* change (tagged `human`) by comparing every
tracked file against its snapshot.

Note the limit honestly, because it is the sharpest edge in this tool: the
sweep sees that lines changed, not who or what produced them. Pasted AI code
and hand-typed code are identical on disk, so both land in `human`. Nothing
downstream recovers that distinction; it is gone at the moment of observation.
A file the plugin has never seen at all is different, and better: it baselines
as `unobserved` rather than being credited to anyone.
"""

import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402
from core import report as report_mod  # noqa: E402

PENDING_MAX_AGE = 6 * 3600


def settings_fingerprint(root):
    """Hash the hook-relevant settings, and note if hooks are disabled.

    A caveat worth stating plainly: if `disableAllHooks` is true, this hook
    never runs, so it cannot report its own suppression. The value is in the
    fingerprint changing across sessions, and in the ledger going silent while
    commits keep arriving. Absence is the signal, not this field.
    """
    relevant = {}
    disabled = False
    candidates = [
        os.path.expanduser("~/.claude/settings.json"),
        os.path.expanduser("~/.claude/settings.local.json"),
        os.path.join(root, ".claude", "settings.json"),
        os.path.join(root, ".claude", "settings.local.json"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        key = os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path)
        relevant[key] = {
            "hooks": data.get("hooks"),
            "disableAllHooks": data.get("disableAllHooks"),
            "enabledPlugins": data.get("enabledPlugins"),
        }
        if data.get("disableAllHooks"):
            disabled = True
    blob = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32], disabled


def migrate_legacy():
    """Pull snapshots from old per-install data directories into the fixed one.

    Runs once, guarded by a marker file. Without this, anyone who used the
    plugin before the storage path was fixed would silently lose their
    attribution history the first time they changed how it was installed.

    Where the same snapshot exists in several legacy directories, the newest
    wins: a later run reflects more of the file's history than an earlier one.
    """
    target = C.paths.plugin_data_dir()
    marker = os.path.join(target, ".migrated")
    if os.path.exists(marker):
        return 0

    best = {}
    for legacy in C.paths.legacy_data_dirs():
        snaps = os.path.join(legacy, "snapshots")
        for dirpath, _dirs, files in os.walk(snaps):
            for name in files:
                if not name.endswith(".json.gz"):
                    continue
                src = os.path.join(dirpath, name)
                rel = os.path.relpath(src, snaps)
                try:
                    mtime = os.path.getmtime(src)
                except OSError:
                    continue
                if rel not in best or mtime > best[rel][1]:
                    best[rel] = (src, mtime)

    moved = 0
    for rel, (src, _mtime) in best.items():
        dest = os.path.join(target, "snapshots", rel)
        if os.path.exists(dest):
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            moved += 1
        except OSError:
            pass

    try:
        os.makedirs(target, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(C.now_iso())
    except OSError:
        pass
    return moved


def clean_pending(ctx):
    """Drop handoff files from edits whose PostToolUse never fired."""
    directory = C.paths.pending_dir(ctx["rid"])
    cutoff = time.time() - PENDING_MAX_AGE
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except OSError:
            pass


def sweep(ctx):
    """Compare every tracked file against its snapshot."""
    drifted = baselined = reset = 0
    known_paths = ledger_paths(ctx)

    for rel in C.repoutil.tracked_files(ctx["root"]):
        if C.counting.is_excluded(rel):
            continue
        text, lines = C.read_file_state(ctx, rel)
        if text is None:
            continue

        snap_file = C.paths.snapshot_path(ctx["rid"], rel)
        snap = C.provenance.load_snapshot(snap_file)
        digest = C.provenance.sha256_text(text)

        if snap is None:
            tags = C.provenance.baseline_tags(lines)
            C.provenance.save_snapshot(snap_file, lines, tags, digest)
            # A file the ledger has history for, but whose snapshot is gone,
            # means the plugin's state directory was deleted. That is a
            # reportable event: it destroys prior attribution.
            if rel in known_paths:
                C.emit(ctx, "baseline_reset", path=rel, lines=len(lines),
                       after_sha256=digest)
                reset += 1
            else:
                baselined += 1
            continue

        if snap.get("sha256") == digest:
            continue

        tags, new_idx, removed = C.provenance.retag(
            snap.get("lines", []), snap.get("tags", []), lines,
            C.provenance.TAG_HUMAN,
        )
        mask = C.counting.significant_mask(lines, rel)
        raw, sig = C.provenance.score(new_idx, mask)
        C.provenance.save_snapshot(snap_file, lines, tags, digest)
        if raw or removed:
            C.emit(ctx, "drift", path=rel,
                   lines_human=raw, sig_human=sig, lines_removed=removed,
                   file_lines=len(lines), after_sha256=digest)
            drifted += 1

    return drifted, baselined, reset


def ledger_paths(ctx):
    """Every path the ledger has ever recorded, to tell a first run apart from
    a wiped state directory."""
    records, _ = C.ledger.read_all(ctx["ledger"])
    return {r.get("path") for r in records if r.get("path")}


def main():
    payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        # The only way to get here now is an opt-out, which is a deliberate
        # choice, so stay quiet about it. There used to be a warning here for
        # folders that were not git repositories; wakatime-cli falls back to the
        # folder name rather than refusing, this now does the same, and so the
        # case the warning existed for cannot happen.
        return

    migrate_legacy()
    clean_pending(ctx)

    # Register this repo so it appears in the student's project picker, even if
    # the session produces no edits. Working in a folder is what makes it a
    # candidate project, not whether Claude happened to write anything.
    C.registry.touch(ctx["rid"], ctx["name"],
                     C.repoutil.remote_url(ctx["root"]), path=ctx["root"])

    drifted, baselined, reset = sweep(ctx)

    # Totals, here as well as at session end, and after the sweep so they
    # include drift it just found.
    #
    # Computing them only at SessionEnd was a real bug with a real symptom: a
    # student who worked and then submitted without closing Claude Code had a
    # project entry carrying no totals at all, which the website rendered as
    # 0% AI / 0% human / 0% unobserved. That reads as "the plugin saw nothing"
    # when the truth is "nobody has added it up yet", and the two mean opposite
    # things to a reviewer. Sessions do not reliably end; session start always
    # happens. This is the same reasoning sync.py already applies to delivery,
    # applied to the numbers being delivered.
    data = report_mod.build(ctx["root"], ctx["rid"])
    C.registry.update(ctx["rid"], ctx["name"],
                      C.repoutil.remote_url(ctx["root"]),
                      totals=data.get("totals"),
                      band=report_mod.band(data.get("totals", {})),
                      path=ctx["root"])
    fingerprint, disabled = settings_fingerprint(ctx["root"])

    C.emit(ctx, "attestation",
           plugin_version=C.VERSION,
           settings_fingerprint=fingerprint,
           hooks_disabled_flag=disabled,
           files_drifted=drifted,
           files_baselined=baselined,
           files_reset=reset,
           start_reason=payload.get("reason", ""))

    if reset:
        # Surfaced to the student rather than hidden: they should know their
        # attribution history was lost, since it will read as a gap.
        print(json.dumps({
            "systemMessage": "AI attribution: prior tracking state for {} file(s) "
                             "was missing and has been re-baselined.".format(reset)
        }))


if __name__ == "__main__":
    C.guard(main)
