"""What counts as a line, and which files count at all.

Two numbers are reported side by side:

  raw          every physical line
  significant  excludes blank lines and comment-only lines

Both are kept because neither is honest alone. Raw over-credits whoever wrote
the boilerplate; significant is a better proxy for authored logic but depends
on a comment heuristic that is approximate (see below).

File-level exclusion matters more than either. A single generated
package-lock.json write is ~30k lines and would swamp every real signal in the
repo, so lockfiles, build output, vendored code and minified assets are dropped
from both numerator and denominator.
"""

import fnmatch
import posixpath

# Single-line comment openers by file extension. Deliberately approximate:
# correctly detecting comments needs a real lexer per language, and a wrong
# answer here shifts a percentage slightly rather than breaking correctness.
# Block comments (/* ... */, ''' ... ''') are NOT tracked, so their interior
# lines count as significant. Documented rather than half-solved.
_COMMENT_PREFIXES = {
    ".py": ("#",), ".pyi": ("#",), ".sh": ("#",), ".bash": ("#",), ".zsh": ("#",),
    ".rb": ("#",), ".pl": ("#",), ".r": ("#",), ".yml": ("#",), ".yaml": ("#",),
    ".toml": ("#",), ".ini": ("#", ";"), ".cfg": ("#", ";"), ".conf": ("#",),
    ".c": ("//", "*"), ".h": ("//", "*"), ".cc": ("//", "*"), ".cpp": ("//", "*"),
    ".cxx": ("//", "*"), ".hpp": ("//", "*"), ".hh": ("//", "*"),
    ".java": ("//", "*"), ".kt": ("//", "*"), ".scala": ("//", "*"),
    ".js": ("//", "*"), ".jsx": ("//", "*"), ".ts": ("//", "*"), ".tsx": ("//", "*"),
    ".mjs": ("//", "*"), ".cjs": ("//", "*"), ".go": ("//",), ".rs": ("//",),
    ".swift": ("//",), ".cs": ("//",), ".php": ("//", "#", "*"),
    ".css": ("*",), ".scss": ("//", "*"), ".less": ("//", "*"),
    ".sql": ("--",), ".lua": ("--",), ".hs": ("--",), ".elm": ("--",),
    ".ex": ("#",), ".exs": ("#",), ".erl": ("%",), ".tex": ("%",),
    ".vim": ('"',), ".lisp": (";",), ".clj": (";",), ".scm": (";",),
}

# Paths never counted, on either side of the ratio.
EXCLUDE_GLOBS = (
    "*/node_modules/*", "node_modules/*",
    "*/vendor/*", "vendor/*",
    "*/dist/*", "dist/*", "*/build/*", "build/*", "*/target/*", "target/*",
    "*/.venv/*", ".venv/*", "*/venv/*", "venv/*",
    "*/__pycache__/*", "*.pyc", "*.pyo",
    "*/.git/*",
    "*.min.js", "*.min.css", "*.map",
    "package-lock.json", "*/package-lock.json",
    "yarn.lock", "*/yarn.lock",
    "pnpm-lock.yaml", "*/pnpm-lock.yaml",
    "poetry.lock", "*/poetry.lock",
    "Cargo.lock", "*/Cargo.lock",
    "composer.lock", "*/composer.lock",
    "Gemfile.lock", "*/Gemfile.lock",
    "go.sum", "*/go.sum",
    "*.pb.go", "*_pb2.py", "*_pb2_grpc.py", "*.generated.*", "*.g.dart",
    # The ledger, and the settings file that enables this plugin, must never be
    # counted as authored code. Counting our own plumbing inflates the
    # denominator with lines nobody wrote.
    ".aiattr/*", "*/.aiattr/*",
    ".claude/*", "*/.claude/*",
    # Binaries and assets: line counts are meaningless.
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico", "*.webp",
    "*.pdf", "*.zip", "*.tar", "*.gz", "*.woff", "*.woff2", "*.ttf", "*.eot",
    "*.mp4", "*.mp3", "*.wav", "*.so", "*.dylib", "*.dll", "*.exe", "*.bin",
)

# Above this, diffing is not worth the time and the file is almost certainly
# generated or vendored. SequenceMatcher is quadratic in the worst case, so an
# unbounded input is a real hang risk on a student's machine.
MAX_LINES = 50_000
MAX_BYTES = 2 * 1024 * 1024


def is_excluded(relpath):
    """True if this path should be ignored entirely."""
    p = relpath.replace("\\", "/")
    return any(fnmatch.fnmatch(p, g) for g in EXCLUDE_GLOBS)


def significant_mask(lines, relpath):
    """Per-line booleans: is this line non-blank and not comment-only?

    Returned as a mask rather than a count so callers can score an arbitrary
    subset of lines (for example, only the lines a diff just inserted).
    """
    ext = posixpath.splitext(relpath.replace("\\", "/"))[1].lower()
    prefixes = _COMMENT_PREFIXES.get(ext, ())
    mask = []
    for line in lines:
        s = line.strip()
        if not s:
            mask.append(False)
            continue
        mask.append(not any(s.startswith(p) for p in prefixes))
    return mask


def too_large(lines=None, nbytes=None):
    if nbytes is not None and nbytes > MAX_BYTES:
        return True
    if lines is not None and len(lines) > MAX_LINES:
        return True
    return False
