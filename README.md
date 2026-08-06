# ai-attribution

Measures what share of a codebase was written by AI rather than by the student,
and gives the instructor a way to check that the measurement was not tampered
with.

The design assumes the student may not want an accurate number. Everything runs
on their machine, so nothing here can be made un-cheatable. What it can do is
put the evidence somewhere they cannot reach.

That is the one idea the whole thing rests on. As the plugin records what it
observes, it sends each record to the course server, which stores it and never
lets it change. A student can still edit the copy in their own repo, rebuild its
hash chain so it validates perfectly, and force-push. It will not matter. A
force-push cannot reach a database, so all it does is make the two copies
disagree, and the server's copy is the one that gets graded.

---

## For students

### Install once, everywhere

```bash
claude plugin marketplace add https://github.com/YOUR-ORG/ai-attribution
```

```bash
claude plugin install ai-attribution@ai-attribution-marketplace --scope user
```

`--scope user` is what makes it system-wide: the plugin then runs in every git
repository you open, and each one shows up in your project list automatically.
You pick which ones counted toward a submission later, rather than setting
anything up per project.

Then bind the machine to your account (the website's "Install to Claude Code"
button does this for you):

```bash
python3 cli/aiattr.py configure --key YOUR_KEY --endpoint https://YOUR-SITE --student-id YOU
```

Requires Python 3 and git. Nothing to `pip install`.

### Managing what gets tracked

```bash
python3 cli/aiattr.py projects
```

```bash
python3 cli/aiattr.py ignore ~/code/my-personal-project
```

Ignoring a repo stops tracking it, removes it from your project list, and
purges it from anything reported. Use it for work unrelated to the course.

Skip `configure` entirely and the plugin never touches the network. It still
tracks locally and still writes the ledger into your repos.

### If you worked offline

`status` tells you whether anything is waiting to be sent, and `flush` sends it:

```bash
python3 cli/aiattr.py flush
```

You should not normally need this, the hooks deliver on their own, including at
the start of your next session. It exists so that if you spent a week offline
you can confirm delivery before submitting, rather than trusting that a
background hook fired.

### See your own numbers

```bash
/ai-attribution:ai-report
```

Plugin commands are namespaced by plugin name, so the plain `/ai-report` will
not appear. Type `/ai` and pick it from the list.

```text
               significant        %        raw        %
ai                     14    14.1%         15    14.4%
human                   1     1.0%          2     1.9%
unobserved             84    84.8%         87    83.7%
```

- **ai** lines inserted by a Claude tool call, observed as they happened
- **human** lines you changed on disk between sessions, seen by the drift sweep
- **unobserved** lines the plugin never saw written, either already on disk when
  tracking started or added while no session was running

Two counts are shown because neither is honest alone. **raw** is every physical
line; **significant** drops blank and comment-only lines. Lockfiles, build
output, vendored code and minified assets are excluded from both.

### What leaves your machine

This matters, so it is stated exactly.

Everything collected is written to `.aiattr/ledger.jsonl` **inside your own
repo**. You can read that file at any time; it is the complete list of what
exists about you.

| Recorded | Never recorded |
|---|---|
| File paths within the repo | Source code |
| Line counts per change | The text of any line |
| SHA-256 content hashes | Your prompts |
| Timestamps, session ids | Anything outside the repo |

If you ran `configure`, those same records are also sent to the course server as
they are written, along with your repo's name and `origin` URL. Records go
whole: the same fields listed above, including repo-relative file paths, because
a partial copy could not be checked against its own hash chain and would be
worthless as evidence. Nothing is added to them on the way out.

Also sent, at session end: aggregate ai/human/unobserved counts, so the website
can draw your project list without re-reading anything.

Never sent: source code, prompt text, absolute paths, anything from a repo you
ignored, or credentials embedded in a remote URL (those are stripped first).

Skip `configure` and none of this leaves your machine. The plugin still tracks
locally and still writes the ledger into your repos.

Source code snapshots are kept locally in `~/.claude/ai-attribution/` and are
never committed. That path is fixed rather than tied to how the plugin was
installed, so changing install scope or reinstalling does not throw away your
attribution history. It also means the data outlives an uninstall.

### Two things it does to your repo

1. Appends to `.aiattr/ledger.jsonl` as you work, so `git status` will usually
   show it as modified. That is expected.
2. Runs `git add .aiattr/` immediately before a `git commit` you make through
   Claude Code, so the ledger travels with that commit. It stages nothing else.

### Committing from VS Code or another tool

Fine, and it does not change your numbers. Attribution is recorded when the
edit happens, not when you commit, so code Claude wrote stays AI-attributed no
matter how you commit it. Committing cannot turn AI code into your code: the
`human` bucket only moves when file *contents* change on disk, and committing
does not touch file contents.

Committing `.aiattr/` is still good practice, since it keeps your own copy of
the record next to your code. But it is no longer the only path to your
instructor: the records are sent as you work, so forgetting to stage the ledger
no longer means your work goes unrecorded.

---

## For instructors

Clone the student's repo and run:

```bash
python3 verifier/verify_repo.py /path/to/student-repo
```

Add `--json` for machine-readable output. Exit code is 1 when a critical
finding is present, 0 otherwise, so it drops into a grading script.

```text
  bucket               lines     share
  ------------------------------------
  AI-attributed           15     14.4%
  human-observed           2      1.9%
  unattributed            87     83.7%

Integrity findings:
  [CRITICAL] 84% of added lines were never observed by the plugin.
```

**`unattributed` is the number to read first.** It is lines that appear in a
commit with no observation behind them. A student who disables the plugin does
not get a flattering low AI score, they get a high unattributed score, which is
much harder to explain.

**Pass `--ledger` whenever you can.** Without it, the only ledger available is
the one in the student's repo, which they hold and can rewrite. With it, you are
checking a record they cannot reach against a diff they cannot fake.

```bash
python3 verifier/verify_repo.py /path/to/student-repo --ledger server-copy.jsonl
```

Measured on a real repo, against a student who relabelled their AI lines as
their own, rebuilt the hash chain so it self-validates, and force-pushed. One
check, at submission, in both rows:

| Ledger used | Result |
|---|---|
| The copy in their repo | `verdict: info`, ai=0, no findings. The attack works. |
| The server's copy | `CRITICAL: ledger_contradicts_server`, ai=40 |

Every internal check passes in the first row. Their ledger is self-consistent,
because they rebuilt it. Nothing in the repo can tell you otherwise.

### Running it automatically

`worker/verify_worker.py` fetches the queue, clones each repo, and verifies it
against the server's records. `.github/workflows/verify.yml` runs it daily and
on demand; set `SITE_URL` and `ATTRIBUTION_ADMIN_KEY` as repository secrets. It
has no dependencies.

It runs **per submission, not on a schedule.** It used to run every six hours,
because the force-push check needed commit SHAs from an earlier run to
contradict. Streaming the records removed that need: they reach the server as
the work happens, so there is nothing left for repeated cloning to protect.

That also fixed the arithmetic, which was not close. At ~3,000 students and ~1.5
repos each, a sweep is ~4,500 clones at roughly five seconds apiece, over six
hours, which exceeded both the interval itself and the GitHub Actions job limit.
Per submission, the queue skips repos already verified at their current head, so
a quiet day costs one API call.

### The checks

1. **Reconciliation.** Lines added per commit versus lines the records explain.
   The residual is `unattributed`. This is what catches code pasted in from a
   browser, and it is the only check that fundamentally needs the repo, which
   is why a clone still happens at all.
2. **Server/repo comparison** (`--ledger` only). Records the two copies disagree
   about mean the committed file was edited after delivery; there is no innocent
   explanation. Records present locally but never delivered mean offline work,
   or a hand-written ledger, and are reported as a warning rather than an
   accusation.
3. **Append-only proof.** Every committed version of the ledger is reconstructed
   from git history and must be a strict prefix of the next. This was the
   strongest check here when the repo held the only copy. It is now a
   cross-check: the attack it existed to catch is already dead.
4. **Chain integrity and coverage.** Record hashes and linkage; missing or
   gitignored ledger, wiped state directory, `disableAllHooks`, and runs of
   commits with no ledger activity.

### Overwrite attempts

If a student's plugin ever offers a record for a sequence number the server
already holds, with different content, the server refuses it and files the
attempt in `attribution_record_conflicts` with both hashes and the offered body.

On an honest install that table stays empty forever, the plugin only ever
re-sends bytes it already sent. A row in it means the local ledger was edited
between two deliveries, and it is the least ambiguous evidence this system
produces: not "these numbers look odd" but "on this date they tried to change
record 41 from X to Y."

---

## What it catches, and what it does not

| Attack | Result | How |
|---|---|---|
| Edit a past ledger entry | **Does not work** | the server's copy is the one scored |
| Edit it *and* rebuild the whole hash chain | **Does not work** | same; and the two copies now disagree |
| Force-push to hide the edit | **Does not work** | a force-push cannot reach a database |
| Re-send an altered record | **Does not work** | refused, and filed as evidence |
| Delete records locally | **Does not work** | the server already has them |
| Gitignore or delete the ledger | Caught | records still arrive; the file's absence is noted |
| Uninstall or disable the plugin | Caught | commits grow, records stop |
| Wipe the local state directory | Caught | `baseline_reset` events |
| Never install it | Caught | no records at all |
| Work offline, never flush, submit | Caught | `records_never_delivered` warning |
| **Paste AI code from a browser** | **As `unattributed`, sometimes** | unexplained lines in the diff |
| **Retype AI code by hand** | **No** | identical to real work in a git diff |

The top block is the point of the redesign: those rows say *does not work*
rather than *caught*, because there is no longer an after-the-fact detection
step that could miss. The record was delivered before the student had any reason
to want it changed.

The bottom two rows are the irreducible limit, and the pasting row has a caveat
worth stating plainly. Pasted code lands as `unattributed` only if the plugin
never sees the file change while a session is open. Paste, then start a Claude
session, and the drift sweep will tag those lines `human`, clean credit for
code the student did not write. Nothing that looks at a git diff can distinguish
transcribed AI output from real work, and this tool does not pretend otherwise.
A high `unattributed` figure is a reason to ask a student to walk you through
their code. It is not a verdict, and the report says so in its own footer.

---

## How it works

```
PreToolUse   Write|Edit|MultiEdit|NotebookEdit   capture the file before the edit
PostToolUse  Write|Edit|MultiEdit|NotebookEdit   diff, attribute, append to ledger
             (then, async and debounced)         stream new records to the server
PreToolUse   Bash (git commit)                   checkpoint, stage .aiattr/
PostToolUse  Bash (git commit)                   record the resulting commit SHA
SessionStart                                     drift sweep, attestation, flush backlog
SessionEnd                                       final report, checkpoint, flush
```

### Delivery

The ledger on disk is the queue. It is append-only and ordered, so the only
thing that has to be remembered is how much of it the server has acknowledged:
one integer per repo, in `~/.claude/ai-attribution/outbox.json`.

That watermark advances **only** on an explicit acknowledgement naming the
sequence the server's copy now reaches. Nothing else is safe. A dropped record
is invisible and permanent; a duplicated one is discarded server-side and costs
nothing, so every ambiguous case resolves toward sending again.

Offline, sends fail and the watermark does not move. Records keep accumulating
in the ledger exactly as they would online, and the next session start delivers
the lot. A student can work on a plane for a week and lose nothing.

Each file carries a tag list parallel to its lines. On every observed change,
`difflib.SequenceMatcher` diffs before against after: lines reported `equal`
keep the tag they already had, and lines reported `insert` or `replace` take
the tag of whoever made the change. That carry-forward is what lets an AI
attribution survive any number of later edits to the same file, and what makes
a hand-rewritten function correctly flip back to `human`.

No hook can block. Failures are swallowed and every entry point exits 0, so a
bug here can never wedge a student's session. Set `AIATTR_DEBUG=1` to see
tracebacks.

### Layout

```
core/provenance.py    per-line tagging, the central algorithm
core/ledger.py        hash chain, append lock
core/outbox.py        delivery watermark: what the server has acknowledged
core/counting.py      raw vs significant lines, exclusion globs
core/repoutil.py      git helpers
core/report.py        student-facing roll-up
hooks/                the hook entry points, including stream.py
verifier/             report_cli.py (student), verify_repo.py (instructor)
worker/               submission-time verification, runs outside the site
tests/run_tests.py    regression tests, no framework needed
```

Run the tests with:

```bash
python3 tests/run_tests.py
```

## Limits worth knowing

- Comment detection is prefix-based per file extension. Block comments count as
  significant lines.
- Edits made outside a session are attributed at the next session start, so a
  student who never reopens Claude Code leaves them as `unattributed`.
- Ledger line-events can exceed a commit's net additions when a file is
  rewritten repeatedly before committing. The verifier caps the explained
  portion at what git shows rather than letting buckets exceed 100%.
- Headline totals are cumulative across all history, so they are unaffected by
  a ledger that lags a commit or two behind the work it describes. The
  per-commit table attributes records by timestamp and is diagnostic only; it
  can be approximate when many commits land within a few seconds of each other.
- Files over 50k lines or 2 MB are skipped; `SequenceMatcher` is quadratic in
  the worst case.
