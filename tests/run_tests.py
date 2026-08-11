"""Regression tests. Run: python3 tests/run_tests.py

Plain asserts and no test framework, so this runs anywhere the plugin does.
Every test here exists because something was actually broken at some point.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import counting, ledger, provenance, repoutil  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  {} {}{}".format("ok  " if cond else "FAIL", name,
                             "" if cond else "  <- " + detail))


# --- provenance -----------------------------------------------------------

def test_carry_forward():
    """AI attribution must survive a later human edit elsewhere in the file."""
    before = ["import os", "def solve():", "    return 1"]
    tags = provenance.baseline_tags(before)
    after = before + ["    # note", "    return 2"]
    tags, new, _ = provenance.retag(before, tags, after, provenance.TAG_AI)
    check("AI lines tagged on insert", tags[3:] == ["ai", "ai"], str(tags))

    # A human inserts a line at the top. The AI lines must keep their tag.
    # Layout is now: os / sys / def / return 1 / # note / return 2, where only
    # the last two were AI-written.
    after2 = ["import os", "import sys"] + after[1:]
    tags2, _, _ = provenance.retag(after, tags, after2, provenance.TAG_HUMAN)
    check("AI tags survive an unrelated human insert",
          tags2 == ["unobserved", "human", "unobserved", "unobserved",
                    "ai", "ai"], str(tags2))

    # A human rewrites one AI line. That line flips; the other AI line does not.
    after3 = list(after2)
    after3[4] = "    return 99"
    tags3, _, _ = provenance.retag(after2, tags2, after3, provenance.TAG_HUMAN)
    check("rewritten AI line flips to human, neighbour stays AI",
          tags3[4] == "human" and tags3[5] == "ai", str(tags3))


def test_no_phantom_line():
    """text.split('\\n') invents a trailing line for newline-ended files.

    This silently inflated every attribution by one line per file.
    """
    check("newline-terminated file counts correctly",
          repoutil.splitlines("a\nb\n") == ["a", "b"],
          str(repoutil.splitlines("a\nb\n")))
    check("file without trailing newline counts correctly",
          repoutil.splitlines("a\nb") == ["a", "b"])
    check("empty file is zero lines", repoutil.splitlines("") == [])


def test_significant_lines():
    lines = ["import os", "", "# a comment", "x = 1"]
    mask = counting.significant_mask(lines, "a.py")
    check("blank and comment lines are not significant",
          mask == [True, False, False, True], str(mask))
    check("lockfiles are excluded", counting.is_excluded("package-lock.json"))
    # Nothing writes .aiattr/ any more, but repos tracked by an older version
    # still have one committed. It must stay excluded or an abandoned ledger
    # counts as thousands of lines of authored code.
    check("a legacy committed ledger is never counted as code",
          counting.is_excluded(".aiattr/ledger.jsonl"))


# --- ledger ---------------------------------------------------------------

def test_chain_is_linked_and_reproducible():
    """The shape the server relies on to compare its copy against an offer.

    Verification itself is the server's job (see core/ledger.py), so what has to
    hold here is only that appends produce a chain it can reproduce: sequential
    seq, each prev_hash pointing at the record before, and every hash derivable
    from the record body alone.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ledger.jsonl")
        for i in range(4):
            ledger.append(path, {"kind": "edit", "lines_ai": i})
        recs, bad = ledger.read_all(path)

        check("every record parses", bad == [] and len(recs) == 4)
        check("seq is sequential", [r["seq"] for r in recs] == [0, 1, 2, 3])
        check("each record links to the one before",
              all(recs[i]["prev_hash"] == recs[i - 1]["hash"]
                  for i in range(1, len(recs)))
              and recs[0]["prev_hash"] == ledger.GENESIS)
        check("hash is derivable from the body alone",
              all(r["hash"] == ledger.record_hash(
                  {k: v for k, v in r.items() if k != "hash"})
                  for r in recs))


def test_opt_out_is_not_reported():
    """Opting out must purge the index, not just stop future tracking.

    Regression: an existing entry kept being synced after the student opted
    out, so a personal repo's name and remote still reached the server.
    """
    import tempfile
    from core import config, paths, registry
    with tempfile.TemporaryDirectory() as d:
        os.environ["AIATTR_DATA_DIR"] = d
        try:
            personal = os.path.join(d, "personal")
            os.makedirs(personal)
            rid = paths.repo_id(personal)
            registry.update(rid, "personal", "https://x/personal.git",
                            path=personal)
            check("repo appears before opt-out",
                  any(p["name"] == "personal" for p in registry.projects()))

            config.save({"ignore": [personal]})
            check("ignored repo is filtered from the picker",
                  not any(p["name"] == "personal"
                          for p in registry.projects()))

            payload = registry.sync_payload("s1", "test")
            check("ignored repo is not in the sync payload",
                  "personal" not in json.dumps(payload))
        finally:
            os.environ.pop("AIATTR_DATA_DIR", None)


def test_remote_credentials_stripped():
    """A repo cloned with a token in its URL must not report the token."""
    import subprocess
    import tempfile
    from core import repoutil
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q"], cwd=d, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin",
                        "https://user:sekrit@github.com/o/r.git"],
                       cwd=d, capture_output=True)
        url = repoutil.remote_url(d) or ""
        check("credentials stripped from reported remote",
              "sekrit" not in url and url.endswith("github.com/o/r.git"), url)


# --- report ---------------------------------------------------------------

def test_report_survives_an_edit_outside_a_session():
    """Touching a file after Claude wrote it must not erase its attribution.

    The bug this guards against: the report treated "file differs from its
    snapshot" as "we cannot vouch for any of it" and re-baselined the whole
    file to `unobserved`. Appending a single line to a file Claude wrote
    therefore dropped the AI count to zero, which made an honest student's
    report show 0% AI and made the tool look broken. Unchanged lines still
    have known provenance, and SequenceMatcher is what identifies them.
    """
    import subprocess
    import tempfile
    from core import paths, provenance, report as report_mod

    with tempfile.TemporaryDirectory() as d:
        os.environ["AIATTR_DATA_DIR"] = os.path.join(d, "data")
        import importlib
        importlib.reload(paths)

        repo = os.path.join(d, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)

        ai_lines = ["line{}".format(i) for i in range(20)]
        src = os.path.join(repo, "app.py")
        with open(src, "w") as fh:
            fh.write("\n".join(ai_lines) + "\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)

        rid = paths.repo_id(repo)
        provenance.save_snapshot(
            paths.snapshot_path(rid, "app.py"), ai_lines,
            [provenance.TAG_AI] * 20,
            provenance.sha256_text("\n".join(ai_lines) + "\n"),
        )

        r = report_mod.build(repo, rid)
        check("a file matching its snapshot reports as AI",
              r["totals"]["ai"]["raw"] == 20, str(r["totals"]))

        # The student appends one line by hand, outside any Claude session.
        with open(src, "a") as fh:
            fh.write("mine\n")

        r = report_mod.build(repo, rid)
        ai = r["totals"]["ai"]["raw"]
        human = r["totals"]["human"]["raw"]
        unobs = r["totals"]["unobserved"]["raw"]
        check("the 20 AI lines survive one hand-added line", ai == 20,
              "ai={} human={} unobserved={}".format(ai, human, unobs))
        check("the new line is attributed to the student", human == 1,
              "human={}".format(human))
        check("nothing is silently re-baselined", unobs == 0,
              "unobserved={}".format(unobs))
        check("the drift is surfaced in coverage",
              r["coverage"].get("files_drifted") == 1,
              str(r["coverage"]))
        del os.environ["AIATTR_DATA_DIR"]
        importlib.reload(paths)


# --- streaming outbox -----------------------------------------------------

def _seed_ledger(path, n):
    """n chained edit records, written the way the plugin writes them."""
    for i in range(n):
        ledger.append(path, {"kind": "edit", "ts": "2026-08-06T00:00:0{}Z".format(i % 10),
                             "session_id": "s", "path": "a.py", "lines_ai": 1})


def test_watermark_only_advances_on_acknowledgement():
    """A send that is not acknowledged must not lose records.

    The failure this guards against is silent and permanent: if the watermark
    advanced on attempt rather than on confirmation, an unreachable server
    would consume records that no longer exist anywhere but the student's disk.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.environ["AIATTR_DATA_DIR"] = d
        import importlib
        from core import outbox, paths
        importlib.reload(paths); importlib.reload(outbox)

        lp = paths.ledger_path("rid")
        _seed_ledger(lp, 5)

        pending, backlog = outbox.unsent("rid", lp)
        check("all records start unsent", backlog == 5, str(backlog))

        outbox.mark_attempt("rid")
        outbox.mark_failure("rid")
        _pending, backlog = outbox.unsent("rid", lp)
        check("a failed send loses nothing", backlog == 5, str(backlog))

        outbox.mark_sent("rid", 2)
        pending, backlog = outbox.unsent("rid", lp)
        check("acknowledged records stop being resent", backlog == 2, str(backlog))
        check("resumption starts at the right seq",
              pending and pending[0]["seq"] == 3,
              str(pending[0]["seq"]) if pending else "none")

        outbox.mark_sent("rid", 1)
        _pending, backlog = outbox.unsent("rid", lp)
        check("the watermark never moves backwards", backlog == 2, str(backlog))
        del os.environ["AIATTR_DATA_DIR"]


def test_debounce_lets_backlog_through():
    """Debouncing must never mean a backlog sits forever.

    should_send() answers no for most edits on purpose, so the guarantee worth
    testing is that the exceptions fire: a machine that has delivered nothing,
    and a backlog past the burst threshold, both send regardless of the clock.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.environ["AIATTR_DATA_DIR"] = d
        import importlib
        from core import outbox, paths
        importlib.reload(paths); importlib.reload(outbox)

        check("nothing to send means no request",
              outbox.should_send("r", 0, 9999, 25) is False)
        check("a machine that never delivered sends immediately",
              outbox.should_send("r", 1, 9999, 25) is True)

        outbox.mark_sent("r", 0)
        outbox.mark_attempt("r")
        check("a small backlog waits for the interval",
              outbox.should_send("r", 2, 9999, 25) is False)
        check("a large backlog overrides the interval",
              outbox.should_send("r", 25, 9999, 25) is True)
        del os.environ["AIATTR_DATA_DIR"]


def test_a_resend_is_distinguishable_from_a_rewrite():
    """What the server needs from the chain, now that it holds the only copy.

    Delivery is unreliable, so the same record can legitimately arrive twice.
    An edited record can also arrive claiming a seq the server already stored.
    Those two cases must not look alike: the first is free to discard, the
    second is the local stream having been altered after delivery. seq plus
    hash is what separates them, and this is the only job the chain still does.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        lp = os.path.join(d, "ledger.jsonl")
        _seed_ledger(lp, 3)
        stored, _ = ledger.read_all(lp)

        by_seq = {r["seq"]: r["hash"] for r in stored}
        honest_resend = dict(stored[1])
        check("an identical resend matches what is stored",
              by_seq[honest_resend["seq"]] == honest_resend["hash"])

        # The student rewrites record 1 and rebuilds the chain so the file
        # validates on its own terms, then the plugin resends it.
        rewritten = dict(stored[1])
        rewritten["lines_human"] = rewritten.pop("lines_ai", 0)
        rewritten["hash"] = ledger.record_hash(
            {k: v for k, v in rewritten.items() if k != "hash"})
        check("a rewritten record collides at the same seq",
              rewritten["seq"] == stored[1]["seq"]
              and by_seq[rewritten["seq"]] != rewritten["hash"])


def test_project_detection_matches_wakatime_cli():
    """Detector order copied from wakatime-cli: marker, then VCS, then folder.

    The order is the point. If this plugin and the student's editor plugin
    disagree about a project's name, the same work arrives at Hackatime under
    two names and neither number means anything. Regression: this used to
    require git and give up otherwise, which silently recorded nothing for a
    student in a plain folder while Hackatime still counted their hours.
    """
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        # 3rd detector: a bare folder is still a project, named for itself.
        # wakatime-cli falls back rather than refusing, which is why folders
        # with no repository show up in a Hackatime project list.
        plain = os.path.join(d, "just-a-folder")
        os.makedirs(plain)
        open(os.path.join(plain, "a.py"), "w").write("x = 1\n")
        check("a bare folder is a project, by folder name",
              repoutil.repo_root(plain) == os.path.realpath(plain)
              and repoutil.project_name(plain) == "just-a-folder")

        # ...but not for directories that hold projects rather than being one.
        # Regression: opening a home directory made it a project and produced
        # 12,139 content snapshots, 56MB, of personal files.
        home = os.path.realpath(os.path.expanduser("~"))
        check("a home directory is not a project", repoutil.repo_root(home) is None)
        check("nor is a container inside it",
              repoutil.repo_root(os.path.join(home, "Downloads")) is None
              if os.path.isdir(os.path.join(home, "Downloads")) else True)

        # An explicit marker overrides the guard: saying so is the whole point.
        guarded = os.path.join(d, "Downloads")
        os.makedirs(guarded)
        open(os.path.join(guarded, ".wakatime-project"), "w").write("on-purpose\n")
        check("an explicit marker still wins in a container-named folder",
              repoutil.project_name(guarded) == "on-purpose")

        # 1st detector: the marker names the project and needs no git.
        marked = os.path.join(d, "marked")
        os.makedirs(marked)
        open(os.path.join(marked, ".wakatime-project"), "w").write("green-monkeys\n")
        open(os.path.join(marked, "page.html"), "w").write("<html>\n")
        check("a .wakatime-project names the project",
              repoutil.project_name(marked) == "green-monkeys")
        check("its files are found without git",
              "page.html" in repoutil.tracked_files(marked))

        sub = os.path.join(marked, "src", "deep")
        os.makedirs(sub)
        check("the marker is found walking up from a subdirectory",
              repoutil.repo_root(sub) == os.path.realpath(marked))

        # 2nd detector: git, named for its folder.
        repo = os.path.join(d, "my-repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
        check("a git repo is named for its folder",
              repoutil.repo_root(repo) == os.path.realpath(repo)
              and repoutil.project_name(repo) == "my-repo")

        # Regression: plain `git ls-files` lists only the index, so a repo with
        # no commits yet returned nothing and every report read zero while the
        # ledger was recording edits normally.
        open(os.path.join(repo, "uncommitted.py"), "w").write("x = 1\n")
        open(os.path.join(repo, ".gitignore"), "w").write("secret/\n")
        os.makedirs(os.path.join(repo, "secret"))
        open(os.path.join(repo, "secret", "keys.txt"), "w").write("nope\n")
        listed = repoutil.tracked_files(repo)
        check("uncommitted files are still counted",
              "uncommitted.py" in listed)
        check(".gitignore is still honoured",
              not any(f.startswith("secret/") for f in listed), str(listed))

        # Order: the marker is checked BEFORE git, because it exists to
        # override what git would otherwise have called the project.
        open(os.path.join(repo, ".wakatime-project"), "w").write("chosen\nmy-branch\n")
        check("the marker beats git on the project name",
              repoutil.project_name(repo) == "chosen")
        check("line 2 of the marker sets the branch",
              repoutil.project_branch(repo) == "my-branch")

        # A marker deeper than the git root wins, and moves the root with it.
        nested = os.path.join(repo, "packages", "widget")
        os.makedirs(nested)
        open(os.path.join(nested, ".wakatime-project"), "w").write("widget\n")
        check("a nested marker overrides the enclosing repo",
              repoutil.repo_root(nested) == os.path.realpath(nested))


def test_band_is_measured_against_observed_code():
    """High/Moderate/Low, over ai+human only, and honest when coverage is thin.

    The denominator is the whole point. Scoring AI against every line in the
    project would report a repo that was 90% written before tracking started as
    barely-any-AI, when in fact nobody knows what that 90% was.
    """
    from core import report as report_mod

    def totals(ai, human, unobserved):
        return {"ai": {"sig": ai, "raw": ai},
                "human": {"sig": human, "raw": human},
                "unobserved": {"sig": unobserved, "raw": unobserved}}

    check("60% of observed reads high",
          report_mod.band(totals(60, 40, 0))["level"] == "high")
    check("20% of observed reads low",
          report_mod.band(totals(20, 80, 0))["level"] == "low")
    check("the middle reads moderate",
          report_mod.band(totals(40, 60, 0))["level"] == "moderate")

    # The denominator check: 100 AI lines beside 900 unobserved is still a
    # heavily-AI project among the code anyone actually watched.
    b = report_mod.band(totals(100, 20, 900))
    check("unobserved lines stay out of the ratio",
          b["ai_pct_of_observed"] == round(100 * 100 / 120.0, 1))

    # ...but coverage that thin has to be visible, not silently folded in.
    check("thin coverage is reported alongside it",
          b["observed_pct_of_project"] == round(100 * 120 / 1020.0, 1))

    check("a barely-tracked project refuses to be labelled",
          report_mod.band(totals(3, 0, 5000))["level"] == "unknown")
    check("so does one with too few observed lines",
          report_mod.band(totals(2, 1, 0))["level"] == "unknown")
    check("an empty project does not divide by zero",
          report_mod.band(totals(0, 0, 0))["level"] == "unknown")


def main():
    import json as _json
    globals()["json"] = _json
    print("project detection");     test_project_detection_matches_wakatime_cli()
    print("provenance");            test_carry_forward()
    print("line counting");         test_no_phantom_line(); test_significant_lines()
    print("ledger integrity");      test_chain_is_linked_and_reproducible()
    print("                ");      test_a_resend_is_distinguishable_from_a_rewrite()
    print("report");                test_report_survives_an_edit_outside_a_session()
    print("ai usage band");         test_band_is_measured_against_observed_code()
    print("streaming outbox");      test_watermark_only_advances_on_acknowledgement()
    print("                 ");     test_debounce_lets_backlog_through()
    print("privacy / opt-out");     test_opt_out_is_not_reported(); test_remote_credentials_stripped()
    print()
    print("{} passed, {} failed".format(len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
