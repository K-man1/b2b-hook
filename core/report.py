"""Roll per-file provenance up into a repo-wide report.

Three buckets, and `unobserved` is the one to read first. It is every line the
plugin never watched arrive: code that predates tracking, and code written
while no session was open. It is not a rounding error, it is the measure of how
much of this repo the tool can say anything about at all.

Computed from local snapshots, so it describes current file state rather than a
sum of past events. That matters: rewriting the same file ten times produces ten
edit records but one final set of lines, and the honest question is who wrote
the lines that are actually there now.

Nothing here renders. The text report and its percentage table lived here to
back a `/ai-report` slash command that no longer exists: students see a coarse
band on the website and reviewers see the exact totals there too, so a local
renderer would only be a third place for those numbers to disagree.
"""

import os

from . import counting, paths, provenance, repoutil


def build(root, rid):
    """Walk git-tracked files and tally provenance across the repo."""
    totals = {t: {"raw": 0, "sig": 0} for t in provenance.ALL_TAGS}
    files = []
    tracked = repoutil.tracked_files(root)
    counted = skipped = untracked_by_plugin = drifted_files = 0

    for rel in tracked:
        if counting.is_excluded(rel):
            skipped += 1
            continue
        abspath = os.path.join(root, rel)
        text = repoutil.read_text(abspath)
        if text is None:
            skipped += 1
            continue
        lines = repoutil.splitlines(text)
        if counting.too_large(lines=lines):
            skipped += 1
            continue

        snap = provenance.load_snapshot(paths.snapshot_path(rid, rel))
        if snap is None:
            # Never seen. Everything on disk predates tracking.
            tags = provenance.baseline_tags(lines)
            untracked_by_plugin += 1
        elif snap.get("sha256") == provenance.sha256_text(text):
            tags = snap.get("tags", [])
        else:
            # Changed since the snapshot was written, i.e. edited outside a
            # Claude session. Carry the surviving tags forward and attribute
            # only the new lines, exactly as the drift sweep will at the next
            # session start.
            #
            # Discarding every tag here instead, on the grounds that the file
            # "changed", is wrong and was a real bug: appending one line to a
            # file Claude wrote made the whole file read as unobserved, so a
            # student who touched their code after an edit saw 0% AI and a
            # tool that looked broken. The snapshot still holds correct
            # per-line provenance for every line that did not change, and
            # SequenceMatcher is what tells us which those are.
            tags, _new_idx, _removed = provenance.retag(
                snap.get("lines", []), snap.get("tags", []),
                lines, provenance.TAG_HUMAN,
            )
            drifted_files += 1

        mask = counting.significant_mask(lines, rel)
        per_file = provenance.tally(tags, mask)
        for tag, vals in per_file.items():
            if tag not in totals:
                totals[tag] = {"raw": 0, "sig": 0}
            totals[tag]["raw"] += vals["raw"]
            totals[tag]["sig"] += vals["sig"]

        files.append({"path": rel, "lines": len(lines), "tags": per_file})
        counted += 1

    return {
        "totals": totals,
        "files": sorted(files, key=lambda f: -f["lines"]),
        "coverage": {
            "files_counted": counted,
            "files_skipped": skipped,
            "files_never_observed": untracked_by_plugin,
            "files_drifted": drifted_files,
        },
    }


# --- banding --------------------------------------------------------------
#
# The coarse label a student sees, defined here rather than in the website so
# that one set of thresholds governs everything. A server recomputing them from
# raw totals would drift the moment either side was edited, and two different
# answers to "is this high" is worse than either answer alone.
#
# Reviewers get the exact figures regardless; the band never replaces them.

BAND_HIGH = 60.0        # at or above this share of observed code: "high"
BAND_LOW = 20.0         # at or below: "low"

# Below either of these the ratio is not worth stating. A project the plugin
# barely watched can read as 100% AI off three observed lines, and showing a
# student "High" on that basis would be a straightforward falsehood.
MIN_OBSERVED_PCT = 20.0
MIN_OBSERVED_LINES = 25

BAND_LABELS = {
    "high": "High",
    "moderate": "Moderate",
    "low": "Low",
    "unknown": "Not enough tracked",
}


def band(totals, basis="sig"):
    """Coarse AI-usage label, plus the numbers behind it.

    Measured against *observed* code (ai + human), not against every line in the
    project. Including `unobserved` in the denominator answers a different
    question: it would report a repo that was 90% written before tracking began
    as barely-any-AI, when the truth is that nobody knows what that 90% was.

    Returns level, label, the percentage, and the coverage that percentage rests
    on, so a reviewer can see at a glance whether to believe it.
    """
    ai = totals.get("ai", {}).get(basis, 0)
    human = totals.get("human", {}).get(basis, 0)
    unobserved = totals.get("unobserved", {}).get(basis, 0)

    observed = ai + human
    counted = observed + unobserved
    observed_pct = round(100.0 * observed / counted, 1) if counted else 0.0
    ai_pct = round(100.0 * ai / observed, 1) if observed else 0.0

    if observed < MIN_OBSERVED_LINES or observed_pct < MIN_OBSERVED_PCT:
        level = "unknown"
    elif ai_pct >= BAND_HIGH:
        level = "high"
    elif ai_pct <= BAND_LOW:
        level = "low"
    else:
        level = "moderate"

    return {
        "level": level,
        "label": BAND_LABELS[level],
        "ai_pct_of_observed": ai_pct,
        "observed_pct_of_project": observed_pct,
        "observed_lines": observed,
        "counted_lines": counted,
        "basis": basis,
        "thresholds": {"high": BAND_HIGH, "low": BAND_LOW},
    }
