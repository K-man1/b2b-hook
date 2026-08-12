"""Finding a project on disk, and reading it.

Project detection copies wakatime-cli's order exactly, because this plugin fills
in fields on heartbeats that WakaTime-compatible editor plugins also produce. If
the two disagreed about what a project is called, the same work would arrive at
Hackatime under two names and neither number would mean anything.

Their order, from wakatime-cli's `WithDetection`:

    1. .wakatime-project   nearest one walking up. Line 1 is the project name,
                           line 2 is the branch. Checked FIRST, before git,
                           because it exists to override what git would say.
    2. revision control    the git work tree, named for its folder.
    3. folder name         last resort. Any directory is a project under its own
                           basename, which is why plain folders show up in a
                           Hackatime project list at all.

An earlier version of this file required git and stopped there, because git used
to be the delivery channel. That stopped being true when records moved to the
network, but the requirement stayed behind and quietly meant a student could
earn Hackatime hours in a plain folder while this plugin recorded nothing and
gave no reason why.

Step 3 is the one with teeth: it means opening any directory at all makes it a
project. The bounds that keep that safe are elsewhere, in counting.EXCLUDE_GLOBS,
the walk cap below, and the student's opt-out list.

Git still does real work when it is there: `git ls-files` gives .gitignore
compliance for free. Without it the file list falls back to walking the tree
against counting.EXCLUDE_GLOBS, which is a coarser filter, so git remains the
better of the two paths rather than merely the older one.

Nothing here may raise into a hook. A student mid-rebase, in a repo with no
commits, or with git missing from PATH must still get a working session, so
each helper degrades to None or an empty result.
"""

import os
import subprocess

PROJECT_FILE = ".wakatime-project"

# Directories never descended into when walking a non-git project. Pruned at
# the directory level rather than filtered per file, because walking into
# node_modules to reject 40k paths one at a time is what makes a naive
# implementation take seconds on every session start.
_SKIP_DIRS = frozenset((
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", "target",
    ".venv", "venv", "__pycache__", ".next", ".nuxt", ".cache", ".tox",
    ".gradle", ".idea", ".vscode", "Pods", "DerivedData",
))

# A plain folder has no .gitignore equivalent to bound it, so a runaway walk is
# a real hang risk in, say, a home directory someone opened by accident.
_MAX_WALK_FILES = 20_000

# Directories that hold projects rather than being one. The folder-name fallback
# is refused here, and ONLY there: a git repo or a .wakatime-project in one of
# these is an explicit statement by the student and is honoured normally.
#
# This is a deliberate divergence from wakatime-cli, which happily falls back to
# the folder name anywhere. The justification is that the two tools carry
# different risk: wakatime-cli records that a folder called "User" existed,
# while this takes content snapshots of every file it finds. Opening a home
# directory once produced 12,139 snapshots totalling 56MB of personal files
# before this guard existed, which is not a cost anyone opted into.
_CONTAINER_DIRS = frozenset((
    "Desktop", "Documents", "Downloads", "Music", "Pictures", "Movies",
    "Public", "Library", "Applications", "Sites", "src", "code", "projects",
    "repos", "dev", "workspace",
))


def _is_container(path):
    """True for a directory that is somewhere projects live, not a project."""
    path = os.path.realpath(path)
    if os.path.dirname(path) == path:
        return True  # filesystem root
    home = os.path.realpath(os.path.expanduser("~"))
    if path == home:
        return True
    for base in (home, "/", "/Users", "/home", "/tmp", "/var/tmp"):
        try:
            if os.path.dirname(path) == os.path.realpath(base) \
                    and os.path.basename(path) in _CONTAINER_DIRS:
                return True
        except OSError:
            continue
    return False


def _git(root, args, timeout=20):
    try:
        out = subprocess.run(
            ["git"] + args,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def git_root(start_dir):
    """Absolute path to the enclosing git work tree, or None if not in one."""
    out = _git(start_dir, ["rev-parse", "--show-toplevel"], timeout=10)
    root = (out or "").strip()
    return os.path.realpath(root) if root else None


def marker_root(start_dir):
    """Nearest ancestor containing a .wakatime-project file, or None.

    Walks up rather than checking only `start_dir` so that a session opened in a
    subdirectory resolves to the same project the editor's own heartbeats do.
    """
    try:
        cur = os.path.realpath(start_dir)
    except (OSError, ValueError):
        return None
    while True:
        if os.path.isfile(os.path.join(cur, PROJECT_FILE)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _marker_lines(directory):
    """(project, branch) from a .wakatime-project, or (None, None).

    Line 1 names the project and line 2 names the branch, which is wakatime-cli's
    format. Only two lines are read because only two are defined; anything below
    them is somebody's note to themselves.
    """
    try:
        with open(os.path.join(directory, PROJECT_FILE), "r",
                  encoding="utf-8", errors="replace") as fh:
            lines = [fh.readline().strip(), fh.readline().strip()]
    except OSError:
        return None, None
    return (lines[0] or None), (lines[1] or None)


def repo_root(start_dir):
    """Project root, in wakatime-cli's detector order.

    The marker is checked before git on purpose: a student who writes a
    .wakatime-project inside a repo is deliberately overriding what git would
    have called it, and honouring git anyway would file their work under a name
    their editor never uses.

    The folder-name fallback is wakatime-cli's behaviour and is why folders with
    no repository still appear in a Hackatime project list. It is refused for
    the container directories above, which is the one place this deliberately
    departs from them; see _CONTAINER_DIRS for why.
    """
    marker = marker_root(start_dir)
    if marker:
        return marker
    git = git_root(start_dir)
    if git:
        return git
    try:
        root = os.path.realpath(start_dir)
    except (OSError, ValueError):
        return None
    return None if _is_container(root) else root


def project_name(root):
    """What to call this project. Must match what the editor already reports."""
    name, _branch = _marker_lines(root)
    return name or os.path.basename(root.rstrip(os.sep))


def project_branch(root):
    """Current branch: line 2 of the marker if set, else whatever git says.

    Marker first for the same reason as the name. Returns None when neither
    source knows, and the caller then omits the field rather than guessing.
    """
    _name, branch = _marker_lines(root)
    if branch:
        return branch
    out = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    branch = (out or "").strip()
    return branch if branch and branch != "HEAD" else None


def remote_url(root):
    """origin's URL, or None. Used to identify a repo to the instructor's side.

    Credentials are stripped: a student who cloned with a token in the URL
    would otherwise have it reported to the server verbatim.
    """
    out = _git(root, ["remote", "get-url", "origin"])
    if not out:
        return None
    url = out.strip()
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        url = scheme + "://" + rest.split("@", 1)[1]
    return url or None


def tracked_files(root):
    """Every file in the project, repo-relative, using the best source available.

    Git first: `git ls-files` respects .gitignore, so build output and secrets a
    student deliberately excluded never enter the counts. Only when there is no
    git does this fall back to walking the tree, which cannot know about
    .gitignore and leans on the coarser EXCLUDE_GLOBS instead.
    """
    # --others --exclude-standard is load-bearing, not a refinement. Plain
    # `ls-files` lists only what git already has in the index, so a student who
    # ran `git init` and started writing sees an empty list until their first
    # `git add`, and every report reads zero while the ledger is quietly
    # recording their work. --others adds untracked files and
    # --exclude-standard keeps .gitignore honoured, which is the whole reason to
    # prefer git over walking in the first place.
    #
    # -z because filenames may contain newlines; git would otherwise quote them
    # and we would parse the quoting wrong.
    out = _git(root, ["ls-files", "-z", "--cached", "--others",
                      "--exclude-standard"])
    if out is not None:
        return [p for p in out.split("\0") if p]
    return walk_files(root)


def walk_files(root):
    """File list for a project with no git, capped so a stray folder cannot hang.

    Directories in _SKIP_DIRS are pruned in place rather than filtered after the
    fact: os.walk descending into node_modules to yield 40,000 paths we then
    reject is the difference between a fast session start and a visible stall.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".git")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                rel = os.path.relpath(full, root)
            except ValueError:
                continue
            found.append(rel.replace(os.sep, "/"))
            if len(found) >= _MAX_WALK_FILES:
                return found
    return found


def read_text(path):
    """Read a file as text, or None if missing/binary/symlinked/unreadable."""
    # Symlinks are refused outright, and this is a privacy fix rather than a
    # correctness one. `git ls-files` lists symlinks like any other entry, so a
    # link committed in a repo -- `config.env -> ~/.aws/credentials`, or a link
    # to a home directory checked in by accident -- had its *target* read and
    # written verbatim into the snapshot store, in plaintext, in a directory
    # that deliberately survives uninstall. Nothing outside the repo should
    # ever end up there, and a symlink is not authored code in this repo
    # anyway: whatever it points at is either counted at its real path or is
    # none of our business.
    try:
        if os.path.islink(path):
            return None
        if os.path.getsize(path) > 2 * 1024 * 1024:
            return None
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def splitlines(text):
    """Split into lines, without inventing a phantom final line.

    A plain text.split("\\n") yields a trailing "" for every newline-terminated
    file, which is almost all of them. Counting that as a line inflated every
    single attribution by exactly one line per file. Dropping one trailing
    empty element matches `wc -l` semantics.

    Carriage returns are stripped, which matters more than it looks. Splitting
    on "\\n" alone left a "\\r" on the end of every line of a CRLF file, so the
    moment anything changed a file's line endings -- git's autocrlf, an editor
    preference, a checkout on Windows -- every line differed from its snapshot
    and the whole file was retagged to whoever touched it last. An entire
    project's attribution could flip on a setting nobody thought of as an edit.
    Line endings are not authorship.
    """
    if not text:
        return []
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()
    return lines
