"""Where state lives, and why it lives there.

Two separate stores, deliberately:

  <repo>/.aiattr/            committed to the student's repo. Metrics and
                             hashes only. This is the channel that reaches
                             the instructor, via their normal git push.

  $CLAUDE_PLUGIN_DATA/       NOT committed. Holds file content snapshots,
                             which is the only place source text is kept.
                             Keeping it out of the repo is what guarantees
                             source code never leaves the machine.

$CLAUDE_PLUGIN_ROOT is explicitly documented as ephemeral (the directory is
cleaned up roughly two weeks after a plugin update), so nothing durable may be
written there. $CLAUDE_PLUGIN_DATA resolves to ~/.claude/plugins/data/{id}/ and
survives updates, so that is the snapshot home.
"""

import hashlib
import os
import subprocess

LEDGER_DIRNAME = ".aiattr"
LEDGER_FILENAME = "ledger.jsonl"
REPORT_FILENAME = "report.json"


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


def repo_root(start_dir):
    """Absolute path to the enclosing git work tree, or None if not in one."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    root = out.stdout.strip()
    return os.path.realpath(root) if root else None


def repo_id(root):
    """Stable per-checkout identifier, used to namespace snapshots.

    Path-derived rather than commit-derived on purpose: it must work before the
    first commit exists, and two clones of the same repo on one machine should
    not share snapshot state.
    """
    return hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()[:16]


def ledger_dir(root):
    return os.path.join(root, LEDGER_DIRNAME)


def ledger_path(root):
    return os.path.join(root, LEDGER_DIRNAME, LEDGER_FILENAME)


def report_path(root):
    return os.path.join(root, LEDGER_DIRNAME, REPORT_FILENAME)


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
