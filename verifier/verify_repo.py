"""Instructor-side verification. Run on a clone of a student's repository.

    python3 verify_repo.py /path/to/student-repo [--json]
    python3 verify_repo.py /path/to/student-repo --ledger server-copy.jsonl

Nothing here trusts the student's machine. Whichever ledger it is given is
checked against git history, which is the one thing in play the student cannot
alter without leaving evidence in what they pushed.

**Prefer --ledger.** The plugin streams its records to the course server as it
writes them, so the server holds a copy from before the student had any reason
to want different numbers. Handed that copy, this tool is checking a record
they cannot reach against a diff they cannot fake. Run without it, the only
ledger available is the one in their repo, which they hold and can rewrite;
that mode still works and still catches a lot, but it is strictly weaker and
the report says so.

Four checks, in rough order of how hard they are to defeat:

  1. Reconciliation. For each commit, compare lines actually added against
     what the ledger explains. The residual is `unattributed`. THIS is what
     catches AI code pasted in from a browser: the lines exist in the diff and
     no hook ever saw them arrive. It is also the only check that fundamentally
     requires the repo, which is why submission-time cloning still happens.

  2. Server/repo comparison (--ledger only). Records the two copies disagree
     about mean the committed file was edited after delivery. Records present
     locally but never delivered mean offline work, or a hand-written ledger.

  3. Append-only proof. Reconstruct every committed version of the ledger and
     require each to be a strict prefix of the next. This used to be the
     strongest check here, back when the repo held the only copy. It is now a
     cross-check: with --ledger the attack it existed to catch is already dead,
     because rewriting local history cannot touch what the server stored.

  4. Chain integrity and coverage. Recompute every record hash and check the
     linkage; flag a missing or gitignored ledger, wiped state, and stretches
     of commits with no ledger growth.

What none of this can do: distinguish AI code the student retyped by hand from
code they actually wrote. That is not recoverable from a git diff by any means,
and the report says so rather than implying a verdict.
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import counting, ledger, paths, repoutil  # noqa: E402

LEDGER_REL = paths.LEDGER_DIRNAME + "/" + paths.LEDGER_FILENAME

CRITICAL, WARNING, INFO = "critical", "warning", "info"


class Findings:
    def __init__(self):
        self.items = []

    def add(self, severity, code, message, **extra):
        item = {"severity": severity, "code": code, "message": message}
        item.update(extra)
        self.items.append(item)

    def ranked(self):
        order = {CRITICAL: 0, WARNING: 1, INFO: 2}
        return sorted(self.items, key=lambda f: order.get(f["severity"], 9))

    def worst(self):
        for level in (CRITICAL, WARNING):
            if any(f["severity"] == level for f in self.items):
                return level
        return INFO


def check_append_only(root, findings):
    """Require every committed ledger version to extend the previous one.

    Compares line lists rather than raw bytes so that a differing trailing
    newline is not reported as tampering.
    """
    commits = repoutil.commits_touching(root, LEDGER_REL)
    if not commits:
        return []

    history = []
    prev_lines, prev_sha = [], None
    for sha in commits:
        text = repoutil.file_at_commit(root, sha, LEDGER_REL)
        lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        history.append({"sha": sha, "count": len(lines)})

        if len(lines) < len(prev_lines):
            findings.add(
                CRITICAL, "ledger_truncated",
                "Ledger shrank from {} to {} records in commit {}. Records can "
                "only be appended, so entries were deleted."
                .format(len(prev_lines), len(lines), sha[:10]),
                commit=sha, before=len(prev_lines), after=len(lines),
            )
        else:
            for i, old in enumerate(prev_lines):
                if lines[i] != old:
                    findings.add(
                        CRITICAL, "ledger_rewritten",
                        "Record #{} was altered in commit {}. Earlier entries "
                        "must never change."
                        .format(i, sha[:10]),
                        commit=sha, record_index=i,
                    )
                    break
        prev_lines, prev_sha = lines, sha
    return history


def check_chain(path, findings):
    """Recompute the hash chain over a ledger file."""
    if not os.path.exists(path):
        return []
    records, bad = ledger.read_all(path)
    if bad:
        findings.add(WARNING, "ledger_unparsable",
                     "{} ledger line(s) are not valid JSON: {}"
                     .format(len(bad), bad[:10]))
    for problem in ledger.verify_chain(records):
        findings.add(CRITICAL, "chain_" + problem["kind"],
                     "Ledger record seq={} failed integrity: {}"
                     .format(problem["seq"], problem["detail"]),
                     seq=problem["seq"])
    return records


def compare_to_repo_copy(root, authoritative, findings):
    """Diff the server's records against the copy committed in the repo.

    Only meaningful when an external ledger was supplied. The server's copy is
    the one that counts, so a disagreement is never a reason to doubt the
    server; it is a reason to look at the repo. Three shapes, each meaning
    something different:

      repo shorter   normal. The last few records were written after the final
                     commit, or the student has not pushed since. Not a finding.

      repo longer    records exist locally that never reached us. Usually a
                     student who worked offline and submitted before the plugin
                     could deliver, occasionally one who edited the file by
                     hand. Worth surfacing, not worth accusing over: the
                     server's copy is still what gets scored.

      contradiction  the two copies disagree about a record they both have.
                     The local file was rewritten after the record was
                     delivered. There is no innocent explanation for this one.
    """
    path = os.path.join(root, LEDGER_REL)
    if not os.path.exists(path):
        # Nothing to compare against. check_coverage reports the absence.
        return
    local, _bad = ledger.read_all(path)

    by_seq = {}
    for rec in local:
        seq = rec.get("seq")
        if isinstance(seq, int):
            by_seq[seq] = rec

    contradictions = []
    for rec in authoritative:
        seq = rec.get("seq")
        other = by_seq.get(seq)
        if other is not None and other.get("hash") != rec.get("hash"):
            contradictions.append(seq)

    if contradictions:
        findings.add(
            CRITICAL, "ledger_contradicts_server",
            "{} record(s) in the repo's ledger disagree with the copy this "
            "server received when the work happened (first at seq {}). The "
            "committed file was altered after the fact."
            .format(len(contradictions), contradictions[0]),
            seqs=contradictions[:20],
        )

    authoritative_seqs = {r.get("seq") for r in authoritative}
    undelivered = sorted(s for s in by_seq if s not in authoritative_seqs)
    if undelivered:
        findings.add(
            WARNING, "records_never_delivered",
            "{} record(s) exist in the repo's ledger but were never received "
            "by the server (first at seq {}). Work done offline that was never "
            "flushed looks like this, and so does a hand-written ledger."
            .format(len(undelivered), undelivered[0]),
            seqs=undelivered[:20],
        )


def _parse_ts(value):
    """ISO timestamp to epoch seconds, or None.

    Handles both the ledger's trailing "Z" and git's numeric offset. Python
    only learned to parse "Z" in fromisoformat at 3.11, and students run older
    interpreters, so it is normalised by hand.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


def allocate_by_time(records, commits):
    """Assign each ledger record to the first commit made at or after it.

    Records are NOT attributed to the commit whose diff happens to contain
    them. A student committing from an IDE that stages only the source file
    leaves the ledger a commit or more behind, and blaming the commit the
    records physically landed in would score their AI work as unattributed.
    That is a false accusation of an honest student, which is the worst
    mistake this tool can make, so allocation follows when work happened
    rather than when the ledger caught up.

    Records newer than the last commit are returned separately as uncommitted.
    """
    buckets = {c["sha"]: [] for c in commits}
    pending = []
    ordered = [c for c in commits if c["time"] is not None]

    for rec in records:
        ts = _parse_ts(rec.get("ts"))
        if ts is None or not ordered:
            pending.append(rec)
            continue
        target = None
        for c in ordered:
            # At-or-after, with one second of slack and no more. A record
            # describes work already done, so it belongs to the next commit to
            # close over it. The single second absorbs git storing commit times
            # at second granularity, which can make a commit look marginally
            # older than a record it actually contains. Wider slack would let a
            # record attach to a commit that had already finished when it was
            # written, moving attribution onto the wrong commit.
            if c["time"] + 1 >= ts:
                target = c["sha"]
                break
        if target is None:
            pending.append(rec)
        else:
            buckets[target].append(rec)
    return buckets, pending


def reconcile(root, records, findings):
    """Compare lines added against what the ledger explains.

    Headline totals are cumulative across the whole history, deliberately.
    Per-commit figures are useful for pointing at where unexplained code
    entered, but summing capped per-commit values would leak ordering noise
    into the total. The totals answer "how much of this repo did we observe",
    which does not depend on which commit carried which record.
    """
    shas = repoutil.all_commits(root)
    if not shas:
        findings.add(WARNING, "no_commits", "Repository has no commits.")
        return {}, []

    commits = []
    for sha in shas:
        meta = repoutil.commit_meta(root, sha) or {}
        added = 0
        for adds, _dels, path in repoutil.numstat_commit(root, sha):
            if counting.is_excluded(path):
                continue
            added += adds
        commits.append({
            "sha": sha, "subject": meta.get("subject", ""),
            "date": meta.get("date", ""), "author": meta.get("author", ""),
            "time": _parse_ts(meta.get("date")), "added": added,
        })

    buckets, pending = allocate_by_time(records, commits)

    def events(recs):
        ai = sum(int(r.get("lines_ai", 0) or 0) for r in recs)
        human = sum(int(r.get("lines_human", 0) or 0) for r in recs)
        return ai, human

    def split(added, ai_events, human_events):
        # Ledger events can legitimately exceed net additions: rewriting the
        # same file five times before committing logs five sets of insertions,
        # but git only ever sees the final text. Cap the explained portion at
        # what git shows and divide it in proportion to observed activity, so
        # the buckets always sum to `added` and never exceed 100%.
        churn = ai_events + human_events
        explained = min(added, churn)
        if churn > 0 and explained > 0:
            ai = int(round(explained * ai_events / float(churn)))
            return ai, explained - ai, added - explained
        return 0, 0, added

    per_commit = []
    for c in commits:
        ai_ev, human_ev = events(buckets.get(c["sha"], []))
        ai, human, unattr = split(c["added"], ai_ev, human_ev)
        per_commit.append({
            "sha": c["sha"], "subject": c["subject"], "date": c["date"],
            "author": c["author"], "added": c["added"],
            "ai": ai, "human": human, "unattributed": unattr,
        })

    total_added = sum(c["added"] for c in commits)
    all_ai, all_human = events(records)
    ai, human, unattributed = split(total_added, all_ai, all_human)
    totals = {
        "added": total_added, "ai": ai, "human": human,
        "unattributed": unattributed,
        "ai_events": all_ai, "human_events": all_human,
        "uncommitted_records": len(pending),
    }

    if total_added > 0:
        pct = 100.0 * unattributed / total_added
        if pct >= 60:
            findings.add(CRITICAL, "high_unattributed",
                         "{:.0f}% of added lines were never observed by the "
                         "plugin. Either most work happened outside Claude Code, "
                         "or tracking was not running.".format(pct))
        elif pct >= 30:
            findings.add(WARNING, "moderate_unattributed",
                         "{:.0f}% of added lines were never observed by the "
                         "plugin.".format(pct))

        churn = all_ai + all_human
        if churn > total_added * 1.5:
            findings.add(INFO, "high_churn",
                         "{} line-events recorded against {} lines finally "
                         "committed. Heavy rewriting before committing is "
                         "normal; the split above is capped at what git shows."
                         .format(churn, total_added))

    if pending:
        findings.add(INFO, "ledger_ahead_of_commits",
                     "{} ledger record(s) are newer than the last commit. Work "
                     "was done after the final commit, or the ledger has not "
                     "been committed yet.".format(len(pending)))

    return totals, per_commit


def check_coverage(root, records, findings, external=False):
    if not os.path.exists(os.path.join(root, LEDGER_REL)):
        # With an external ledger this is cosmetic: the records were delivered
        # to the server as the work happened, so a missing file in the repo
        # costs us no evidence. Without one it is fatal, because the file was
        # the only evidence there was.
        findings.add(
            WARNING if external else CRITICAL, "no_ledger",
            "No ledger at {}. {}".format(
                LEDGER_REL,
                "The records were streamed to the server, so this does not "
                "affect the numbers." if external else
                "The plugin was never installed, never ran, or the file was "
                "deleted."),
        )
        if not external:
            return
    if repoutil.is_ignored(root, LEDGER_REL):
        findings.add(CRITICAL, "ledger_gitignored",
                     "The ledger is listed in .gitignore, so it would never "
                     "reach you through a normal push.")

    resets = [r for r in records if r.get("kind") == "baseline_reset"]
    if resets:
        findings.add(WARNING, "baseline_reset",
                     "{} baseline_reset event(s): the plugin's state directory "
                     "was deleted, discarding prior attribution."
                     .format(len(resets)))

    attests = [r for r in records if r.get("kind") == "attestation"]
    if any(r.get("hooks_disabled_flag") for r in attests):
        findings.add(CRITICAL, "hooks_disabled",
                     "A session reported disableAllHooks set in settings.")

    prints = {r.get("settings_fingerprint") for r in attests if r.get("settings_fingerprint")}
    if len(prints) > 1:
        findings.add(INFO, "settings_changed",
                     "Hook settings changed {} time(s) across sessions."
                     .format(len(prints) - 1))

    versions = {r.get("plugin_version") for r in attests if r.get("plugin_version")}
    if len(versions) > 1:
        findings.add(INFO, "version_changed",
                     "Multiple plugin versions used: {}".format(sorted(versions)))


def format_report(root, totals, per_commit, findings, records, external=False):
    out = []
    out.append("=" * 68)
    out.append("AI attribution verification")
    out.append("repo: {}".format(root))
    out.append("ledger: {}".format(
        "server copy (student cannot rewrite it)" if external
        else "committed in the repo (student-held; weaker)"))
    out.append("=" * 68)
    out.append("")

    added = totals.get("added", 0)
    if added:
        def pct(n):
            return 100.0 * n / added
        out.append("Lines added across all commits: {}".format(added))
        out.append("")
        out.append("  {:<16}{:>10}{:>10}".format("bucket", "lines", "share"))
        out.append("  " + "-" * 36)
        for label, key in (("AI-attributed", "ai"),
                           ("human-observed", "human"),
                           ("unattributed", "unattributed")):
            out.append("  {:<16}{:>10}{:>9.1f}%".format(
                label, totals[key], pct(totals[key])))
        out.append("")
    else:
        out.append("No countable lines added.")
        out.append("")

    sessions = len({r.get("session_id") for r in records if r.get("session_id")})
    out.append("Ledger records: {} across {} session(s)".format(len(records), sessions))
    out.append("Raw line-events observed: {} AI, {} human (before capping)".format(
        totals.get("ai_events", 0), totals.get("human_events", 0)))
    out.append("")

    ranked = findings.ranked()
    if not ranked:
        out.append("Integrity: no problems found.")
    else:
        out.append("Integrity findings:")
        for f in ranked:
            out.append("  [{}] {}".format(f["severity"].upper(), f["message"]))
    out.append("")

    noteworthy = [c for c in per_commit if c["unattributed"] > 0]
    if noteworthy:
        noteworthy.sort(key=lambda c: -c["unattributed"])
        out.append("Commits with unattributed lines (largest first):")
        out.append("  {:<12}{:>8}{:>7}{:>8}{:>8}  {}".format(
            "commit", "added", "ai", "human", "unattr", "subject"))
        for c in noteworthy[:15]:
            out.append("  {:<12}{:>8}{:>7}{:>8}{:>8}  {}".format(
                c["sha"][:10], c["added"], c["ai"], c["human"],
                c["unattributed"], c["subject"][:34]))
        out.append("")

    out.append("-" * 68)
    out.append("Reading this: 'unattributed' means the lines appeared in a commit")
    out.append("without the plugin observing them being written. That covers")
    out.append("normal typing, edits made while Claude Code was closed, AND code")
    out.append("pasted in from another AI tool. This report cannot tell those")
    out.append("apart, and a high figure is a prompt to ask, not a conclusion.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Verify a student repo's AI attribution ledger.")
    ap.add_argument("repo", nargs="?", default=".", help="path to the cloned repo")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--ledger", metavar="FILE",
        help="verify against this ledger instead of the one committed in the "
             "repo. Use the server's streamed copy: the student cannot rewrite "
             "it, so the append-only git proof is no longer load-bearing.",
    )
    args = ap.parse_args()

    root = paths.repo_root(os.path.abspath(args.repo))
    if not root:
        print("Not a git repository: {}".format(args.repo), file=sys.stderr)
        return 2

    findings = Findings()
    external = bool(args.ledger)

    if external:
        # The server's copy is authoritative, so the checks change shape. The
        # git append-only proof existed to catch someone editing the only copy
        # of the ledger; when we hold our own copy from before they could have
        # touched it, that attack is already dead. It still runs, because a
        # student who rewrote the committed file is worth knowing about, but a
        # failure is now evidence about the repo rather than about the numbers.
        check_append_only(root, findings)
        records = check_chain(args.ledger, findings)
        compare_to_repo_copy(root, records, findings)
    else:
        check_append_only(root, findings)
        records = check_chain(os.path.join(root, LEDGER_REL), findings)

    check_coverage(root, records, findings, external=external)
    totals, per_commit = reconcile(root, records, findings)

    if args.json:
        print(json.dumps({
            "repo": root,
            "ledger_source": "server" if external else "repo",
            "totals": totals,
            "commits": per_commit,
            "findings": findings.ranked(),
            "verdict": findings.worst(),
            "ledger_records": len(records),
        }, indent=2))
    else:
        print(format_report(root, totals, per_commit, findings, records,
                            external=external))

    return 1 if findings.worst() == CRITICAL else 0


if __name__ == "__main__":
    sys.exit(main())
