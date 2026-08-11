"""SessionEnd: roll the session up and record where the repo ended.

This is what keeps the website's project list showing real numbers. The totals
are computed here, from local snapshots, and written into the registry, which
`sync.py` then reports. Without this step every project would show as null.

What it deliberately no longer does. An earlier version also ran on `git
commit`: it staged `.aiattr/` into the student's commit so the ledger would
travel with their push, and afterwards recorded the resulting commit SHA so the
ledger could be anchored to git history. Both jobs existed to make the repo the
delivery channel. Records stream to the server now, so there is nothing to
stage and nothing to anchor, and the plugin no longer writes to the student's
working tree or touches their staging area at all.

The HEAD sha is still recorded, once per session rather than once per commit.
It costs nothing and gives the server a coarse trail of where each repo was
when we last saw it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402
from core import report as report_mod  # noqa: E402


def ledger_head(ctx):
    records, _ = C.ledger.read_all(ctx["ledger"])
    if not records:
        return "", -1
    return records[-1].get("hash", ""), records[-1].get("seq", -1)


def totals_summary(data):
    t = data["totals"]
    return {
        "ai_raw": t.get("ai", {}).get("raw", 0),
        "ai_sig": t.get("ai", {}).get("sig", 0),
        "human_raw": t.get("human", {}).get("raw", 0),
        "human_sig": t.get("human", {}).get("sig", 0),
        "pre_raw": t.get("unobserved", {}).get("raw", 0),
        "pre_sig": t.get("unobserved", {}).get("sig", 0),
    }


def main():
    payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        return

    data = report_mod.build(ctx["root"], ctx["rid"])
    head, seq = ledger_head(ctx)

    # Keep the project index current so the picker shows real numbers without
    # having to re-scan every repo on the student's disk.
    # The band travels with the totals rather than being recomputed server-side,
    # so the label a student sees and the numbers a reviewer sees can never
    # disagree about which side of a threshold a project falls on.
    band = report_mod.band(data.get("totals", {}))

    C.registry.update(
        ctx["rid"], ctx["name"],
        C.repoutil.remote_url(ctx["root"]),
        totals=data.get("totals"), band=band,
        ledger_head=head, ledger_records=seq + 1,
        path=ctx["root"],
    )

    C.emit(ctx, "session_end",
           ledger_head=head, ledger_seq=seq,
           band=band["level"], ai_pct_of_observed=band["ai_pct_of_observed"],
           **totals_summary(data))


if __name__ == "__main__":
    C.guard(main)
