"""Student-facing report. Backs the /ai-report command.

Reads local snapshot state only. Nothing here talks to the server, and the
numbers it prints are the same ones the machine reports.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import paths, report, repoutil  # noqa: E402


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    root = repoutil.repo_root(start)
    if not root:
        print("Not inside a git repository, so there is nothing to report.")
        print("This plugin only tracks files in a git work tree, because a")
        print("repository is the unit you pick when you submit a project.")
        return 0
    print(report.format_text(report.build(root, paths.repo_id(root))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
