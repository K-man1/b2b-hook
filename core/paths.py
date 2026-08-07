"""Where state lives, and why it lives there.

Everything is local. Nothing this plugin writes goes into the student's repo.

Earlier versions committed a hash-chained ledger to `<repo>/.aiattr/` and used
the student's own `git push` as the delivery channel. That is gone. Records are
streamed to the course server as they are written, so the repo never needed to
carry a copy, and carrying one meant writing into every project the student
opened and staging a file into commits they did not ask us to touch.

What remains, all under plugin_data_dir():

  ledgers/<rid>.jsonl   the record stream for one repo. A local cache and a
                        send queue, not evidence: the server's copy is the one
                        that counts. Kept on disk so that work done offline
                        survives until it can be delivered.

  snapshots/<rid>/      file content snapshots, the only place source text is
                        kept. Never leaves the machine, which is what makes the
                        privacy promise true rather than aspirational.

$CLAUDE_PLUGIN_ROOT is explicitly documented as ephemeral (the directory is
cleaned up roughly two weeks after a plugin update), so nothing durable may be
written there.
"""

import hashlib
import os

# Legacy. Repos tracked by an older version still have a committed .aiattr/
# directory; nothing reads it any more, but counting.EXCLUDE_GLOBS still drops
# it so an abandoned ledger is never counted as authored code.
LEGACY_LEDGER_DIRNAME = ".aiattr"


def plugin_data_dir():
    """Persistent state directory. Deliberately NOT ${CLAUDE_PLUGIN_DATA}.

    ${CLAUDE_PLUGIN_DATA} resolves to ~/.claude/plugins/data/{id}/, where {id}
    encodes how the plugin was installed. That id changes when the plugin is
    loaded via --plugin-dir instead of installed, when the install scope moves
    between user and project, and when the marketplace is renamed. Every one of
    those silently orphans the snapshot store: attribution history becomes
    unreachable, every line reverts to `unobserved`, and the verifier reads the
    result as if tracking had been switched off. That would falsely accuse a
    student who did nothing wrong, so this state lives at a fixed path instead.

    Surviving uninstall is a feature here, not a leak. Evidence should not
    disappear because a plugin was removed and reinstalled.
    """
    env = os.environ.get("AIATTR_DATA_DIR")
    if env:
        return os.path.abspath(env)
    return os.path.expanduser("~/.claude/ai-attribution")


def legacy_data_dirs():
    """Old ${CLAUDE_PLUGIN_DATA}-style locations, for one-time migration."""
    base = os.path.expanduser("~/.claude/plugins/data")
    try:
        names = os.listdir(base)
    except OSError:
        return []
    return [os.path.join(base, n) for n in names
            if "ai-attribution" in n or n == "b2b-hook-local"]


def repo_id(root):
    """Stable per-checkout identifier, used to namespace snapshots.

    Path-derived rather than commit-derived on purpose: it must work before the
    first commit exists, and two clones of the same repo on one machine should
    not share snapshot state.
    """
    return hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()[:16]


def ledger_path(rid):
    """The local record stream for one repo.

    Keyed by repo id rather than living inside the repo, so two clones of the
    same project on one machine keep separate streams and neither writes into
    the working tree.
    """
    return os.path.join(plugin_data_dir(), "ledgers", rid + ".jsonl")


def snapshots_dir(rid):
    return os.path.join(plugin_data_dir(), "snapshots", rid)


def snapshot_path(rid, relpath):
    """One snapshot file per tracked source file, keyed by path hash.

    Hashed rather than mirrored so that arbitrarily deep paths, unicode names,
    and case-insensitive filesystems all behave.
    """
    h = hashlib.sha256(relpath.encode("utf-8")).hexdigest()
    return os.path.join(snapshots_dir(rid), h[:2], h + ".json.gz")


def pending_dir(rid):
    """Handoff slot between PreToolUse and PostToolUse, keyed by tool_use_id."""
    return os.path.join(plugin_data_dir(), "pending", rid)


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path
