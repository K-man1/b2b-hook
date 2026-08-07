"""Roll per-file provenance up into a repo-wide report.

Three buckets, and `unobserved` is the one to read first. It is every line the
plugin never watched arrive: code that predates tracking, and code written
while no session was open. It is not a rounding error, it is the measure of how
much of this repo the tool can say anything about at all.

Computed from local snapshots, so it describes current file state rather than a
sum of past events. That matters: rewriting the same file ten times produces ten
edit records but one final set of lines, and the honest question is who wrote
the lines that are actually there now.
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


def percentages(totals, basis="sig"):
    """Percent of counted lines per tag. `basis` is "sig" or "raw"."""
    denom = sum(v[basis] for v in totals.values())
    if denom == 0:
        return {t: 0.0 for t in totals}, 0
    return {t: round(100.0 * v[basis] / denom, 1) for t, v in totals.items()}, denom


def format_text(report):
    """Human-readable summary for the /ai-attribution:ai-report command."""
    totals = report["totals"]
    sig_pct, sig_total = percentages(totals, "sig")
    raw_pct, raw_total = percentages(totals, "raw")
    cov = report["coverage"]

    out = []
    out.append("AI attribution report")
    out.append("=" * 52)
    out.append("")
    out.append("{:<14} {:>10} {:>8} {:>10} {:>8}".format(
        "", "significant", "%", "raw", "%"))
    for tag in provenance.ALL_TAGS:
        out.append("{:<14} {:>10} {:>7}% {:>10} {:>7}%".format(
            tag, totals[tag]["sig"], sig_pct.get(tag, 0.0),
            totals[tag]["raw"], raw_pct.get(tag, 0.0)))
    out.append("{:<14} {:>10} {:>8} {:>10}".format("total", sig_total, "", raw_total))
    out.append("")
    out.append("files counted {}, skipped {} (generated/binary/excluded), "
               "never observed {}".format(
                   cov["files_counted"], cov["files_skipped"],
                   cov["files_never_observed"]))
    if cov.get("files_drifted"):
        out.append("{} file(s) changed since the plugin last saw them; those "
                   "changes count as yours.".format(cov["files_drifted"]))
    out.append("")
    out.append("'unobserved' means the plugin never saw those lines written:")
    out.append("they were on disk before tracking started, or added while no")
    out.append("session was open. Lines you changed between sessions show as")
    out.append("'human' once the next session start sweeps for them.")

    if report["files"]:
        out.append("")
        out.append("Largest files:")
        for f in report["files"][:10]:
            t = f["tags"]
            out.append("  {:<40} {:>5}L  ai={} human={} unobs={}".format(
                f["path"][:40], f["lines"],
                t.get("ai", {}).get("raw", 0),
                t.get("human", {}).get("raw", 0),
                t.get("unobserved", {}).get("raw", 0)))
    return "\n".join(out)
