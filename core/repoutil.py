"""Thin git wrappers. Every call is read-only and failure-tolerant.

Nothing here may raise into a hook. A student mid-rebase, in a repo with no
commits, or with git missing from PATH must still get a working session, so
each helper degrades to None or an empty result.
"""

import os
import subprocess


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


def head_sha(root):
    """Current HEAD, or None in a repo with no commits yet."""
    out = _git(root, ["rev-parse", "HEAD"])
    return out.strip() if out else None


def tracked_files(root):
    """Files git knows about, which gives us .gitignore compliance for free.

    -z because filenames may contain newlines; git would otherwise quote them
    and we would parse the quoting wrong.
    """
    out = _git(root, ["ls-files", "-z"])
    if out is None:
        return []
    return [p for p in out.split("\0") if p]


def numstat(root, rev_range):
    """[(added, removed, path)] for a commit or range. Binary files skipped."""
    out = _git(root, ["diff", "--numstat", "-z", rev_range])
    if out is None:
        return []
    return _parse_numstat_z(out)


def numstat_commit(root, sha):
    """Lines added/removed by a single commit, against its first parent.

    Merges report nothing: a merge's diff against one parent double-counts
    work already attributed to the branch it came from.
    """
    if is_merge(root, sha):
        return []
    out = _git(root, ["show", "--numstat", "-z", "--format=", sha])
    if out is None:
        return []
    return _parse_numstat_z(out)


def _parse_numstat_z(out):
    """Parse `--numstat -z` output.

    The -z format is awkward: normal entries are "adds\tdels\tpath\0", but a
    rename emits "adds\tdels\t\0oldpath\0newpath\0", spending three NUL-
    separated fields on one record. Getting this wrong silently mis-attributes
    every renamed file, so the two shapes are handled explicitly.
    """
    parts = out.split("\0")
    rows = []
    i = 0
    while i < len(parts):
        rec = parts[i]
        if not rec:
            i += 1
            continue
        fields = rec.split("\t")
        if len(fields) < 3:
            i += 1
            continue
        adds, dels, path = fields[0], fields[1], fields[2]
        if path == "":
            # Rename/copy: the real paths are the next two NUL-separated fields.
            if i + 2 < len(parts):
                path = parts[i + 2]
                i += 3
            else:
                break
        else:
            i += 1
        if adds == "-" or dels == "-":
            continue  # binary
        try:
            rows.append((int(adds), int(dels), path))
        except ValueError:
            continue
    return rows


def is_merge(root, sha):
    out = _git(root, ["rev-list", "--parents", "-n", "1", sha])
    return bool(out) and len(out.split()) > 2


def commits_touching(root, relpath):
    """Commit SHAs that modified a path, oldest first."""
    out = _git(root, ["log", "--reverse", "--format=%H", "--", relpath])
    return out.split() if out else []


def all_commits(root):
    """Every commit reachable from HEAD, oldest first."""
    out = _git(root, ["log", "--reverse", "--format=%H"])
    return out.split() if out else []


def commit_meta(root, sha):
    out = _git(root, ["show", "-s", "--format=%H%x1f%an%x1f%aI%x1f%s", sha])
    if not out:
        return None
    f = out.strip().split("\x1f")
    if len(f) < 4:
        return None
    return {"sha": f[0], "author": f[1], "date": f[2], "subject": f[3]}


def file_at_commit(root, sha, relpath):
    """File contents as of a commit, or None if absent there."""
    return _git(root, ["show", "{}:{}".format(sha, relpath)])


def is_ignored(root, relpath):
    """True if .gitignore excludes this path. Used to catch a student who
    gitignores the ledger so it never reaches the instructor."""
    try:
        out = subprocess.run(
            ["git", "check-ignore", "-q", relpath],
            cwd=root, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def read_text(path):
    """Read a file as text, or None if missing/binary/unreadable."""
    try:
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
    """
    if not text:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines
