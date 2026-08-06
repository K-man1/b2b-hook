"""Anchor the ledger to git history.

Three modes:

  stage   PreToolUse on `git commit`. Writes a checkpoint and stages .aiattr/
          so the ledger lands in the very commit being made. Without this the
          ledger is perpetually one commit behind, and a student who never runs
          `git add .aiattr` never delivers it at all.

  anchor  PostToolUse on `git commit`. Records the SHA that was just created.
          This record necessarily lands in the NEXT commit, since a commit
          cannot contain its own hash. The verifier accounts for the offset.

  flush   SessionEnd. Writes the final report and a closing checkpoint.

Staging is the one place this plugin writes to the student's repo state. It is
scoped to .aiattr/ and touches nothing else, and it is disclosed in the README.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C  # noqa: E402
from core import report as report_mod  # noqa: E402


def write_report(ctx):
    data = report_mod.build(ctx["root"], ctx["rid"])
    data["generated"] = C.now_iso()
    data["plugin_version"] = C.VERSION
    data["head"] = C.repoutil.head_sha(ctx["root"]) or ""
    path = C.paths.report_path(ctx["root"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return data


def stage_ledger(ctx):
    """Stage .aiattr/ so it rides along with the commit being made."""
    try:
        subprocess.run(
            ["git", "add", "--", C.paths.LEDGER_DIRNAME],
            cwd=ctx["root"], capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        pass


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
    mode = sys.argv[1] if len(sys.argv) > 1 else "flush"
    payload = C.read_input()
    ctx = C.context(payload)
    if ctx is None:
        return

    if mode == "anchor":
        head, seq = ledger_head(ctx)
        C.emit(ctx, "checkpoint",
               phase="post_commit",
               head=C.repoutil.head_sha(ctx["root"]) or "",
               ledger_head=head, ledger_seq=seq)
        return

    data = write_report(ctx)
    head, seq = ledger_head(ctx)

    # Keep the project index current so the picker shows real numbers without
    # having to re-scan every repo on the student's disk.
    C.registry.update(
        ctx["rid"], os.path.basename(ctx["root"]),
        C.repoutil.remote_url(ctx["root"]),
        totals=data.get("totals"), ledger_head=head, ledger_records=seq + 1,
        head=C.repoutil.head_sha(ctx["root"]) or "", path=ctx["root"],
    )

    C.emit(ctx, "checkpoint",
           phase="pre_commit" if mode == "stage" else "session_end",
           head=C.repoutil.head_sha(ctx["root"]) or "",
           ledger_head=head, ledger_seq=seq,
           **totals_summary(data))

    if mode == "stage":
        stage_ledger(ctx)


if __name__ == "__main__":
    C.guard(main)
