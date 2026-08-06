"""Verification worker. Runs outside the website, once per submission.

    ATTRIBUTION_ADMIN_KEY=... SITE_URL=https://... python3 worker/verify_worker.py

For each repo the site says to check:

  1. Writes out the server's copy of that repo's ledger.
  2. Clones the repository WITH FULL HISTORY.
  3. Confirms every commit SHA we recorded previously still exists.
  4. Runs verify_repo.py, pointed at the server's ledger, against the clone.
  5. Posts the result back to the site.

Two things about this are load-bearing and easy to get wrong.

**Never shallow clone.** `--depth 1` silently makes the diff reconciliation
meaningless: with one commit there is no history to compare records against,
and every check passes for a repo nobody has actually examined.

**The ledger comes from the site, not from the clone.** The file committed in
the student's repo is a copy they hold and can rewrite. The site's copy was
streamed as the work happened and cannot be reached by a force-push. Passing
--ledger is what makes this verification mean something; without it the worker
would be grading the student's own account of themselves.

This used to run every six hours, to build a trail of observed commit SHAs so
that a later force-push had something to contradict it. It no longer needs to:
the records reach the server live, so there is nothing for repeated cloning to
protect. Running per-submission is what makes this affordable at a few thousand
students, and the arithmetic is not close, 4,500 repos at ~5s each is over six
hours of work per sweep, which exceeded both the old interval and the GitHub
Actions job limit.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER = os.path.join(HERE, "verifier", "verify_repo.py")

SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
ADMIN_KEY = os.environ.get("ATTRIBUTION_ADMIN_KEY", "")
CLONE_TIMEOUT = 300
VERIFY_TIMEOUT = 300


def api(path, body=None):
    url = SITE_URL + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data else "GET",
        headers={
            "Authorization": "Bearer " + ADMIN_KEY,
            "Content-Type": "application/json",
            "User-Agent": "ai-attribution-worker",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def clone(url, dest):
    """Full clone. No --depth, ever. See module docstring."""
    out = subprocess.run(
        ["git", "clone", "--quiet", "--no-single-branch", url, dest],
        capture_output=True, text=True, timeout=CLONE_TIMEOUT,
    )
    return out.returncode == 0, (out.stderr or "").strip()


def sha_reachable(repo, sha):
    """Is this commit still reachable from some branch?

    Reachability, not mere existence. `git cat-file -e` only asks whether the
    object is present, and a rewritten commit can still be sitting in the
    object store: cloning from a local path shares objects outright, and even
    over the network a repo can retain unreferenced objects until it is gc'd.
    Asking whether a branch still contains the commit is the question that
    actually distinguishes "history intact" from "history rewritten".
    """
    exists = subprocess.run(
        ["git", "cat-file", "-e", sha + "^{commit}"],
        cwd=repo, capture_output=True, timeout=30,
    )
    if exists.returncode != 0:
        return False
    contains = subprocess.run(
        ["git", "branch", "--all", "--contains", sha],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    return contains.returncode == 0 and bool(contains.stdout.strip())


def head_sha(repo):
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
        text=True, timeout=30,
    )
    return out.stdout.strip() if out.returncode == 0 else None


def write_ledger(records, path):
    """Write the server's records out in the plugin's own on-disk format.

    json.dumps with sorted keys and compact separators, matching core.ledger's
    canonical(), so the verifier recomputes the same hashes the plugin did. A
    difference in encoding here would read as a broken chain, which is to say
    as tampering by a student who did nothing wrong.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")))
            fh.write("\n")
    return path


def run_verifier(repo, ledger_path):
    cmd = [sys.executable, VERIFIER, repo, "--json"]
    if ledger_path:
        cmd += ["--ledger", ledger_path]
    out = subprocess.run(
        cmd, capture_output=True, text=True, timeout=VERIFY_TIMEOUT,
    )
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def check_one(entry, workdir):
    """Verify one repo. Always returns a result, never raises."""
    repo_id = entry["repo_id"]
    dest = os.path.join(workdir, repo_id)

    findings = []

    # An attempt to overwrite an already-stored record. The plugin only ever
    # re-sends bytes it already sent, so this cannot happen by accident: it
    # means the local ledger was rewritten between two deliveries. Reported
    # first because it is the least ambiguous finding this tool produces.
    conflicts = int(entry.get("conflicts") or 0)
    if conflicts:
        findings.append({
            "severity": "critical", "code": "record_overwrite_attempted",
            "message": (
                "{} record(s) were re-sent with different content than the "
                "server already held. The local ledger was edited after those "
                "records were delivered.".format(conflicts)
            ),
        })

    ok, err = clone(entry["clone_url"], dest)
    if not ok:
        # A repo that cannot be cloned is a real finding, not an error to
        # swallow: it is also what a deleted or newly-private repo looks like.
        findings.append({
            "severity": "warning", "code": "clone_failed",
            "message": "Could not clone {}: {}".format(
                entry["clone_url"], err[:200]),
        })
        return {
            "repo_id": repo_id,
            "verdict": "critical" if conflicts else "warning",
            "findings": findings,
        }

    missing = [s for s in entry.get("known_shas", []) if s and not sha_reachable(dest, s)]
    if missing:
        findings.append({
            "severity": "warning", "code": "history_rewritten",
            "message": (
                "{} previously recorded commit(s) no longer exist in this "
                "repository, so history was rewritten. This no longer changes "
                "the attribution numbers, those come from records the server "
                "already holds, but it is worth asking about."
                .format(len(missing))
            ),
        })

    records = entry.get("records") or []
    ledger_path = None
    if records:
        ledger_path = write_ledger(records, os.path.join(workdir, repo_id + ".jsonl"))
    else:
        findings.append({
            "severity": "critical", "code": "no_server_records",
            "message": (
                "The server holds no ledger records for this repo. The plugin "
                "was never installed, never ran, or never reached us. Falling "
                "back to the copy in the repo, which the student controls."
            ),
        })

    report = run_verifier(dest, ledger_path)
    if report is None:
        findings.append({
            "severity": "warning", "code": "verifier_failed",
            "message": "The verifier produced no usable output for this repo.",
        })
        return {"repo_id": repo_id, "verdict": "warning", "findings": findings}

    totals = report.get("totals") or {}
    findings.extend(report.get("findings") or [])
    verdict = report.get("verdict", "info")
    if any(f.get("severity") == "critical" for f in findings):
        verdict = "critical"
    elif any(f.get("severity") == "warning" for f in findings) and verdict == "info":
        verdict = "warning"

    return {
        "repo_id": repo_id,
        "head_sha": head_sha(dest),
        "verdict": verdict,
        "added": totals.get("added", 0),
        "ai": totals.get("ai", 0),
        "human": totals.get("human", 0),
        "unattributed": totals.get("unattributed", 0),
        "findings": findings,
    }


def main():
    if not SITE_URL or len(ADMIN_KEY) < 16:
        print("SITE_URL and ATTRIBUTION_ADMIN_KEY must both be set", file=sys.stderr)
        return 2

    try:
        queue = api("/api/attribution/pending").get("repos", [])
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        print("could not fetch work queue: {}".format(exc), file=sys.stderr)
        return 1

    if not queue:
        print("nothing to verify")
        return 0

    workdir = tempfile.mkdtemp(prefix="aiattr-verify-")
    results = []
    try:
        for entry in queue:
            try:
                result = check_one(entry, workdir)
            except (OSError, subprocess.SubprocessError) as exc:
                result = {
                    "repo_id": entry["repo_id"], "verdict": "warning",
                    "findings": [{
                        "severity": "warning", "code": "worker_error",
                        "message": str(exc)[:200],
                    }],
                }
            results.append(result)
            print("  {:<24} {:<9} unattributed={}".format(
                entry.get("name", "?")[:24], result["verdict"],
                result.get("unattributed", "-")))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        recorded = api("/api/attribution/verify", {"results": results})
        print("recorded {} result(s)".format(recorded.get("recorded", 0)))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        print("could not post results: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
