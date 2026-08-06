"""Roll per-file provenance up into a repo-wide report.

This is the student-facing view. It can only describe what the plugin actually
observed, so it reports three buckets (ai / human / unobserved) plus coverage.
The fourth and most important bucket, `unattributed`, cannot be computed here:
it comes from comparing the ledger against real commit diffs, which is the
instructor's verifier's job. A student-side report that claimed to know its own
blind spots would be the same self-reporting mistake this design exists to
avoid.
"""

import os

from . import counting, paths, provenance, repoutil


def build(root, rid):
    """Walk git-tracked files and tally provenance across the repo."""
    totals = {t: {"raw": 0, "sig": 0} for t in provenance.ALL_TAGS}
    files = []
    tracked = repoutil.tracked_files(root)
    counted = skipped = untracked_by_plugin = 0

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
        if snap and snap.get("sha256") == provenance.sha256_text(text):
            tags = snap.get("tags", [])
        else:
            # Either never seen, or changed since the snapshot was written.
            # Both mean we cannot vouch for these lines' origin right now.
            tags = provenance.baseline_tags(lines)
            if snap is None:
                untracked_by_plugin += 1

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
    out.append("")
    out.append("'unobserved' means the plugin never saw those lines written.")
    out.append("Lines added outside a Claude session appear only after the next")
    out.append("session start. Your instructor's verifier computes a fourth")
    out.append("bucket, 'unattributed', by reconciling this against git history.")

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
