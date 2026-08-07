"""Thin git wrappers. Every call is read-only and failure-tolerant.

Git is used to answer three local questions and nothing else: where does this
repo start, which files does it track, and where is it pointed. History is
never walked. An earlier version reconstructed commit diffs here to reconcile
them against recorded edits; that check is gone, and so are the helpers that
served it.

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


def repo_root(start_dir):
    """Absolute path to the enclosing git work tree, or None if not in one."""
    out = _git(start_dir, ["rev-parse", "--show-toplevel"], timeout=10)
    root = (out or "").strip()
    return os.path.realpath(root) if root else None


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
    """Files git knows about, which gives us .gitignore compliance for free.

    -z because filenames may contain newlines; git would otherwise quote them
    and we would parse the quoting wrong.
    """
    out = _git(root, ["ls-files", "-z"])
    if out is None:
        return []
    return [p for p in out.split("\0") if p]


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
