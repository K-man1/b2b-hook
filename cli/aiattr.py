"""Student-facing CLI.

    python3 cli/aiattr.py status
    python3 cli/aiattr.py configure --key KEY --endpoint URL --student-id ID
    python3 cli/aiattr.py projects
    python3 cli/aiattr.py ignore /path/to/personal-repo
    python3 cli/aiattr.py sync

`configure` is what the website's "Install to Claude Code" flow calls after the
plugin is installed, to bind this machine to a student account.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, registry  # noqa: E402

VERSION = "0.5.0"


def cmd_status(_args):
    cfg = config.load()
    key = cfg.get("api_key") or ""
    print("AI attribution client {}".format(VERSION))
    print("  data dir   : {}".format(os.path.dirname(config.config_path())))
    print("  student id : {}".format(cfg.get("student_id") or "(not set)"))
    print("  endpoint   : {}".format(cfg.get("endpoint") or "(not set)"))
    # Never print the key. Enough to confirm one is present.
    print("  api key    : {}".format(
        "set, ending " + key[-4:] if len(key) >= 4 else "(not set)"))
    print("  reporting  : {}".format(
        "on" if config.sync_enabled() else "off (tracking locally only)"))
    ignored = cfg.get("ignore") or []
    print("  ignored    : {}".format(len(ignored)))
    for p in ignored:
        print("      {}".format(p))
    rows = registry.projects()
    print("  projects   : {}".format(len(rows)))

    # Undelivered records are the one piece of state a student may need to act
    # on: everything else here is inert configuration, but a backlog means the
    # server does not yet have evidence of work they have already done.
    if config.sync_enabled():
        from core import outbox, paths
        behind = []
        for row in rows:
            root = row.get("path")
            if not root:
                continue
            _pending, backlog = outbox.unsent(row["id"], paths.ledger_path(root))
            if backlog:
                behind.append((row.get("name") or "?", backlog))
        if behind:
            print("  undelivered:")
            for name, n in behind:
                print("      {:<24} {} record(s)".format(name[:24], n))
            print("      run `aiattr.py flush` inside the repo to send them")
        else:
            print("  undelivered: none")
    return 0


def cmd_configure(args):
    cfg = config.load()
    if args.key:
        cfg["api_key"] = args.key
    if args.endpoint:
        cfg["endpoint"] = args.endpoint.rstrip("/")
    if args.student_id:
        cfg["student_id"] = args.student_id
    if args.disable_sync:
        cfg["sync"] = False
    if args.enable_sync:
        cfg["sync"] = True
    path = config.save(cfg)
    print("Wrote {}".format(path))
    return cmd_status(args)


def cmd_projects(args):
    rows = registry.projects()
    if not rows:
        print("No tracked projects yet.")
        print("Open a git repository in Claude Code and it will appear here.")
        return 0
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    print("{:<24} {:<22} {:>7} {:>7} {:>7}".format(
        "project", "last activity", "ai", "human", "unobs"))
    print("-" * 72)
    for r in rows:
        t = r.get("totals") or {}
        def g(k):
            return (t.get(k) or {}).get("raw", 0)
        print("{:<24} {:<22} {:>7} {:>7} {:>7}".format(
            (r.get("name") or "?")[:24],
            (r.get("last_activity") or "")[:19],
            g("ai"), g("human"), g("unobserved")))
    print()
    print("Totals are what this machine observed. Your instructor's numbers")
    print("come from verifying the ledger you pushed, not from this list.")
    return 0


def cmd_ignore(args):
    cfg = config.load()
    ignore = list(cfg.get("ignore") or [])
    target = os.path.realpath(os.path.expanduser(args.path))
    if args.remove:
        ignore = [p for p in ignore
                  if os.path.realpath(os.path.expanduser(p)) != target]
        print("No longer ignoring {}".format(target))
    else:
        if target not in ignore:
            ignore.append(target)
        print("Ignoring {}".format(target))
        print("No attribution will be recorded there, and no ledger written.")
    cfg["ignore"] = ignore
    config.save(cfg)

    # Purge any existing index entry too. Stopping future tracking is not
    # enough on its own: the stale entry would keep being reported, so opting
    # out of a personal repo would still send its name and remote to the
    # server.
    if not args.remove:
        from core import outbox as _outbox
        from core import paths as _paths
        rid = _paths.repo_id(target)
        if registry.remove(rid):
            print("Removed it from your project index as well.")
        _outbox.forget(rid)
    return 0


def cmd_flush(args):
    """Send any ledger records the server has not acknowledged yet.

    Mostly a diagnostic: the hooks do this on their own. It exists so a student
    who worked offline can confirm delivery before submitting, rather than
    having to trust that a background hook fired.
    """
    if not config.sync_enabled():
        print("Reporting is not configured. Run `configure` first.")
        return 1

    from core import outbox, paths

    root = paths.repo_root(os.path.abspath(args.path or os.getcwd()))
    if not root:
        print("Not inside a git repository.")
        return 1

    rid = paths.repo_id(root)
    _pending, backlog = outbox.unsent(rid, paths.ledger_path(root))
    print("{}: {} record(s) not yet delivered".format(
        os.path.basename(root), backlog))
    if not backlog:
        return 0

    hook = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "hooks", "stream.py")
    payload = json.dumps({"cwd": root, "session_id": "cli"})
    import subprocess
    subprocess.run([sys.executable, hook, "flush"],
                   input=payload, text=True, timeout=120)

    _pending, remaining = outbox.unsent(rid, paths.ledger_path(root))
    if remaining:
        print("{} still undelivered. The server may be unreachable.".format(remaining))
        return 1
    print("All records delivered.")
    return 0


def cmd_sync(_args):
    if not config.sync_enabled():
        print("Reporting is not configured. Run `configure` first.")
        return 1
    hooks = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "hooks", "sync.py")
    os.execv(sys.executable, [sys.executable, hooks])


def main():
    ap = argparse.ArgumentParser(prog="aiattr")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="show configuration and tracking state")

    c = sub.add_parser("configure", help="bind this machine to a student account")
    c.add_argument("--key")
    c.add_argument("--endpoint")
    c.add_argument("--student-id")
    c.add_argument("--disable-sync", action="store_true")
    c.add_argument("--enable-sync", action="store_true")

    p = sub.add_parser("projects", help="list tracked repositories")
    p.add_argument("--json", action="store_true")

    i = sub.add_parser("ignore", help="stop tracking a repository")
    i.add_argument("path")
    i.add_argument("--remove", action="store_true", help="resume tracking it")

    sub.add_parser("sync", help="report the project index now")

    f = sub.add_parser("flush", help="deliver any undelivered ledger records")
    f.add_argument("path", nargs="?", help="repository (default: current directory)")

    args = ap.parse_args()
    handlers = {
        "status": cmd_status, "configure": cmd_configure,
        "projects": cmd_projects, "ignore": cmd_ignore, "sync": cmd_sync,
        "flush": cmd_flush,
    }
    if args.cmd not in handlers:
        ap.print_help()
        return 1
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
