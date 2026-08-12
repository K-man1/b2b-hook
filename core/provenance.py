"""Per-line provenance: who wrote each line, tracked across arbitrary edits.

The unit of state is a tag list running parallel to a file's lines:

    lines = ["import os",   "def solve():",  "    return 1"]
    tags  = ["unobserved",  "ai",           "human"      ]

Three tags, and the distinction between them is the whole point:

    ai           inserted by an agent tool call, observed as it happened
    human        changed on disk outside any tool call, while the repo was
                 already being tracked. Inferred, not watched: see below
    unobserved   appeared without the plugin watching: either already on disk
                 when tracking began, or in a file it has never seen

`unobserved` is deliberately not folded into `human`. Crediting a student for
scaffolding that was in the repo before they arrived would overstate their
authorship exactly as badly as crediting it to AI would understate it. A bucket
we did not observe should say so.

WHY DRIFT COUNTS AS HUMAN, since it is the one tag assigned by inference.

A hook fires when an agent makes a tool call. Nothing fires when a person
types, so no hook can ever *watch* a human write a line; the only component
that sees keystrokes is the editor extension. If `human` were restricted to
directly observed authorship it would therefore always be empty here, `ai`
would be the only populated bucket with a producer, and any ratio built on the
two would read 100% for everyone who opened an agent once.

So drift is attributed to the student. It is an estimate, and it is a
defensible one because the largest thing that used to contaminate it is gone:
agent writes through `Bash` and MCP tools now land at the tool call (see
hooks/post_bash.py) instead of turning up later as an anonymous file change.
What remains in drift for a student running one agent is mostly them.

What is still wrong with it, and belongs in the README rather than being
quietly absorbed: code pasted from a browser lands here, and so does an agent
this plugin has no hook installed for. Both are AI work counted as the
student's. That is the known error bar on the number, not a bug to be fixed by
a better diff, because nothing in two versions of a file distinguishes them.

Drift records carry `via: "drift"` so the distinction survives into the
ledger. A reviewer can then tell a line the plugin watched an agent write from
a line it merely found already changed.

Every observed change re-derives the tag list by diffing old content against
new. Lines the diff calls `equal` keep whatever tag they already carried, which
is what lets attribution survive any number of later edits to the same file.
Lines the diff calls `insert` or `replace` are new, so they take the tag of
whoever made this particular change.
"""

import difflib
import gzip
import hashlib
import json
import os

TAG_AI = "ai"
TAG_HUMAN = "human"
TAG_UNOBSERVED = "unobserved"
ALL_TAGS = (TAG_AI, TAG_HUMAN, TAG_UNOBSERVED)

SNAPSHOT_VERSION = 1


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def baseline_tags(lines):
    """Tags for a file seen for the first time: origin unobserved."""
    return [TAG_UNOBSERVED] * len(lines)


def normalize(line):
    """A line reduced to the characters that carry authorship.

    All whitespace is removed, so `x = 1`, `x=1` and `    x = 1` are one line
    as far as the diff is concerned. This is not cosmetic; it closes the
    cheapest way to launder attribution that exists.

    Diffing raw text meant any pass that rewrote every line reassigned every
    line. `sed -i 's/$/ /'` was enough: a trailing space per line, one session
    restart, and a file that was 100% agent-written reported 100% student-
    written, because SequenceMatcher correctly saw sixty replaced lines and
    retag correctly gave replaced lines to whoever made the change. Both steps
    were right and the result was a lie. Running a formatter did the same thing
    by accident, which is worse: an honest student got the cheat for free.

    Removing whitespace rather than merely stripping the ends is deliberate.
    Reindentation, brace-style changes and most of what black/prettier/gofmt do
    to an existing line survive it, so the line keeps the tag it already had.

    What this cannot survive is a rewrite that moves real tokens: a formatter
    splitting one long line into three genuinely produces lines nobody has
    written before, and there is no way to tell that apart from authorship by
    looking at two versions of a file. That case is named in the README limits
    and belongs to the server, which can see that a whole file flipped bucket
    in one step.
    """
    return "".join(line.split())


def retag(before_lines, before_tags, after_lines, actor):
    """Re-derive the tag list after a change, attributing new lines to `actor`.

    Returns (after_tags, new_indices, removed_count) where new_indices are
    positions in `after_lines` that this change introduced. Indices rather than
    a count, so the caller can score them against a significant-line mask
    without this module needing to know anything about comment syntax.

    Matching happens on normalized lines (see `normalize`) while tags stay
    attached to real positions, so whitespace-only rewrites carry attribution
    forward instead of reassigning it.
    """
    # Defensive: tag state and content can drift apart if a snapshot was
    # partially written or hand-edited. Realign rather than raising, since a
    # crash here would take out the student's session.
    if len(before_tags) != len(before_lines):
        before_tags = (
            before_tags[: len(before_lines)]
            + [TAG_UNOBSERVED] * max(0, len(before_lines) - len(before_tags))
        )

    # autojunk=False is load-bearing. difflib's autojunk heuristic treats any
    # line appearing in more than 1% of a sequence of 200+ elements as noise
    # and refuses to anchor matches on it. In source code the most common lines
    # are "", "}", "    return" and similar, so the heuristic throws away
    # precisely the anchors real code has, and diffs of long files come back
    # wildly overstated. Left on, it would inflate every AI attribution.
    matcher = difflib.SequenceMatcher(
        None,
        [normalize(l) for l in before_lines],
        [normalize(l) for l in after_lines],
        autojunk=False,
    )

    # Built fresh rather than mutated in place. The reason is worth working out
    # from the shape of get_opcodes() before reading further; see the note in
    # the plan file.
    after_tags = []
    new_indices = []
    removed = 0

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            # Unchanged text: carry prior attribution forward untouched.
            after_tags.extend(before_tags[i1:i2])
        elif op == "insert":
            for j in range(j1, j2):
                after_tags.append(actor)
                new_indices.append(j)
        elif op == "replace":
            for j in range(j1, j2):
                after_tags.append(actor)
                new_indices.append(j)
            removed += i2 - i1
        elif op == "delete":
            removed += i2 - i1

    return after_tags, new_indices, removed


def score(new_indices, sig_mask):
    """Split a set of newly attributed lines into raw and significant counts."""
    raw = len(new_indices)
    sig = sum(1 for j in new_indices if j < len(sig_mask) and sig_mask[j])
    return raw, sig


def tally(tags, sig_mask):
    """Total raw and significant lines per tag for a whole file."""
    out = {t: {"raw": 0, "sig": 0} for t in ALL_TAGS}
    for i, tag in enumerate(tags):
        if tag not in out:
            out[tag] = {"raw": 0, "sig": 0}
        out[tag]["raw"] += 1
        if i < len(sig_mask) and sig_mask[i]:
            out[tag]["sig"] += 1
    return out


# --- snapshot store -------------------------------------------------------
#
# Snapshots hold real source text, so they live in the plugin data directory
# and are never written into the repo. This is the mechanism that keeps the
# privacy promise: the committed ledger carries only counts and hashes.


def load_snapshot(path):
    """Stored {lines, tags, sha256} for a file, or None if never seen."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, EOFError):
        return None
    if not isinstance(data, dict) or data.get("v") != SNAPSHOT_VERSION:
        return None
    return data


def save_snapshot(path, lines, tags, digest):
    """Write a snapshot atomically.

    Via a temp file and os.replace because a hook can be killed mid-write when
    a student hits Ctrl-C; a half-written snapshot would desynchronise tags
    from content and silently corrupt attribution for that file.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    payload = {"v": SNAPSHOT_VERSION, "sha256": digest, "lines": lines, "tags": tags}
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, path)
