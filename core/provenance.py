"""Per-line provenance: who wrote each line, tracked across arbitrary edits.

The unit of state is a tag list running parallel to a file's lines:

    lines = ["import os",   "def solve():",  "    return 1"]
    tags  = ["unobserved",  "ai",           "human"      ]

Three tags, and the distinction between them is the whole point:

    ai           inserted by a Claude tool call, observed as it happened
    human        changed on disk between Claude sessions, observed via drift
    unobserved   appeared without the plugin watching: either already on disk
                 when tracking began, or written while no session was running

`unobserved` is deliberately not folded into `human`. Crediting a student for
scaffolding that was in the repo before they arrived would overstate their
authorship exactly as badly as crediting it to AI would understate it, and a
file that materialised between sessions is exactly the case the instructor's
verifier needs to see. A bucket we did not observe should say so.

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


def retag(before_lines, before_tags, after_lines, actor):
    """Re-derive the tag list after a change, attributing new lines to `actor`.

    Returns (after_tags, new_indices, removed_count) where new_indices are
    positions in `after_lines` that this change introduced. Indices rather than
    a count, so the caller can score them against a significant-line mask
    without this module needing to know anything about comment syntax.
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
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)

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
