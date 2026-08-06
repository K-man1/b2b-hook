#!/usr/bin/env bash
# Find a working Python 3 and exec the hook with it.
#
# Students run this on machines we cannot inspect, so interpreter discovery has
# to be defensive. In particular, on Windows + Git Bash `python3` usually
# resolves to the Microsoft Store stub, which exits 49 silently in a non-TTY
# subprocess. Probing each candidate with `-c ""` makes the stub fail the probe
# and fall through to a real interpreter.
#
# Order: python3 (macOS/Linux) -> python (python.org on Windows) -> py -3.
#
# Usage:  bash py.sh /path/to/hook.py [args...]

# PEP 540. Without it, Windows Python picks cp1252 for filesystem encoding and
# crashes on any path containing CJK/Arabic/Hebrew characters. Must be exported
# before the interpreter starts.
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

SCRIPT="$1"
shift

for candidate in "python3" "python" "py -3"; do
  # Word splitting on "py -3" is intentional here.
  # shellcheck disable=SC2086
  if $candidate -c "" >/dev/null 2>&1 &&
     $candidate -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    exec $candidate "$SCRIPT" "$@"
  fi
done

# No usable interpreter. Exit 0 regardless: this plugin must never block a
# student's session. The resulting absence of ledger records is itself the
# signal the instructor's verifier picks up.
exit 0
