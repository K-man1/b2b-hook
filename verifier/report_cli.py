"""Student-facing report. Backs the /ai-report command.

Reads only local snapshot state, so it shows the three observed buckets and
says nothing about `unattributed`, which requires git reconciliation.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import paths, report  # noqa: E402


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    root = paths.repo_root(start)
    if not root:
        print("Not inside a git repository, so there is nothing to report.")
        print("This plugin only tracks files in a git work tree, because the")
        print("repo is how the ledger reaches your instructor.")
        return 0
    print(report.format_text(report.build(root, paths.repo_id(root))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
