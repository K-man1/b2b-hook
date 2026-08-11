"""Student-facing CLI.

    python3 cli/aiattr.py status
    python3 cli/aiattr.py configure --key KEY --endpoint URL --student-id ID
    python3 cli/aiattr.py configure --enable-hackatime
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

from core import adapters, agents, config, registry  # noqa: E402

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VERSION = "0.6.0"


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

    # Reported separately from `reporting` because the two are independent
    # channels: this one goes to the student's own Hackatime account using the
    # key already in ~/.wakatime.cfg, and works even with no course server set.
    from core import heartbeat
    if heartbeat.enabled():
        print("  hackatime  : on (agent edits sent as 'ai coding')")
    elif (cfg.get("hackatime") or {}).get("enabled"):
        print("  hackatime  : on, but no key found in ~/.wakatime.cfg")
    else:
        print("  hackatime  : off")
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
            _pending, backlog = outbox.unsent(row["id"],
                                              paths.ledger_path(row["id"]))
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
    if args.enable_hackatime or args.disable_hackatime:
        ht = dict(cfg.get("hackatime") or {})
        ht["enabled"] = bool(args.enable_hackatime)
        cfg["hackatime"] = ht
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
    print("Totals are what this machine observed while Claude Code was open.")
    print("Lines it never saw written show as 'unobs' and are neither yours")
    print("nor AI's as far as this tool knows.")
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
        print("No attribution will be recorded there, and nothing reported.")
    cfg["ignore"] = ignore
    config.save(cfg)

    # Purge any existing index entry too. Stopping future tracking is not
    # enough on its own: the stale entry would keep being reported, so opting
    # out of a personal repo would still send its name and remote to the
    # server.
    if not args.remove:
        from core import heartbeat as _heartbeat
        from core import outbox as _outbox
        from core import paths as _paths
        rid = _paths.repo_id(target)
        if registry.remove(rid):
            print("Removed it from your project index as well.")
        _outbox.forget(rid)
        # Undelivered Hackatime buckets are the same hazard as a stale index
        # entry: they still carry the project name and its line counts, and
        # would be sent on the next flush.
        if _heartbeat.forget(rid):
            print("Discarded its undelivered Hackatime heartbeats.")
        # Drop the local record stream too. Leaving it would keep undelivered
        # records for a repo the student just said is none of our business.
        try:
            os.unlink(_paths.ledger_path(rid))
        except OSError:
            pass
    return 0


def _merge_json_hooks(path, new_hooks):
    """Add hook entries without clobbering a config file the tool already owns.

    Cursor, Codex, Gemini CLI etc. all use their hooks.json/settings.json for
    more than hooks, so this is a merge, not an overwrite. Merge key is the
    exact command string: re-running install-hooks after a plugin update must
    not pile up duplicate entries, but a genuinely different command (the
    plugin moved, or the student edited the file by hand) is left alone rather
    than guessed about.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            existing = json.load(fh)
    except (OSError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    merged_hooks = dict(existing.get("hooks") or {})
    for event, entries in new_hooks.get("hooks", {}).items():
        current = list(merged_hooks.get(event) or [])
        for entry in entries:
            if entry not in current:
                current.append(entry)
        merged_hooks[event] = current
    existing.update({k: v for k, v in new_hooks.items() if k != "hooks"})
    existing["hooks"] = merged_hooks
    return existing


def _merge_bare_hooks(path, events):
    """Same merge as _merge_json_hooks, for files where the hooks object is the
    whole file rather than a "hooks" key inside it (Devin's hooks.v1.json)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            existing = json.load(fh)
    except (OSError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    for event, entries in events.items():
        current = list(existing.get(event) or [])
        for entry in entries:
            if entry not in current:
                current.append(entry)
        existing[event] = current
    return existing


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def cmd_install_hooks(args):
    if args.tool == "list":
        print("Claude Code is wired in automatically; nothing to install for it.")
        print()
        for slug in adapters.known():
            spec = adapters.spec_for(slug)
            if spec is None:
                how = "not supported: " + adapters.UNSUPPORTED[slug]
            elif spec.get("user"):
                how = "every project, via ~/{}".format(spec["user"])
            elif spec.get("user_dirs"):
                how = "every project, via " + " and ".join(
                    "~/" + d for d in spec["user_dirs"])
            elif slug in adapters.SCRIPT:
                how = "per project: script files in {}".format(spec["dir"])
            else:
                how = "per project: {}  (no user-level config exists)".format(
                    spec["config"][0])
            if spec and spec.get("unverified"):
                how += "  (schema unverified -- check after install)"
            label = (spec or {}).get(
                "label", adapters.UNSUPPORTED_LABELS.get(slug, slug))
            print("  {:<20} {}".format(slug, label))
            print("      {}".format(how))
        return 0

    slug = agents.resolve(args.tool)["slug"]

    if slug in adapters.UNSUPPORTED:
        print("{}: {}".format(slug, adapters.UNSUPPORTED[slug]))
        return 1

    spec = adapters.spec_for(slug)
    if spec is None:
        print("Unknown agent '{}'. Run `install-hooks list` to see what's "
              "supported.".format(args.tool))
        return 1

    # Machine-wide unless the student asked for one repo, or the tool has no
    # user-level config to write to. Defaulting to machine-wide is the whole
    # point: a per-repo install has to be remembered for every new project,
    # and the one that gets forgotten is silently untracked.
    user_path = adapters.user_scope_path(spec)
    if args.project is None and adapters.has_user_scope(spec):
        path, scope, root = user_path, "global", None
    else:
        root = args.project or os.getcwd()
        scope = "project"

        # A project-scoped install pointed at the home directory is always a
        # mistake -- either install.sh passed --tool for a tool that has no
        # machine-wide setting, or the student ran this before cd-ing anywhere.
        # Writing there would create a config no tool ever reads while
        # reporting success, so refuse and say where it does belong.
        if os.path.realpath(root) == os.path.realpath(os.path.expanduser("~")):
            print("{} has no machine-wide hook config, so this has to be "
                  "installed per project.".format(spec.get("label", slug)))
            print("Your home directory is not a project. cd into the folder "
                  "you build in, then run:")
            print("  aiattr install-hooks {}".format(slug))
            return 1

        # SCRIPT tools have a directory of files, not one config path.
        path = None if slug in adapters.SCRIPT else os.path.join(
            root, spec["config"][0])

    if slug in adapters.SCRIPT:
        if scope == "global":
            hook_dirs = adapters.user_scope_dirs(spec)
        else:
            hook_dirs = [os.path.join(root or os.getcwd(), spec["dir"])]
        for hook_dir in hook_dirs:
            os.makedirs(hook_dir, exist_ok=True)
            for event_name, verb in spec["events"].items():
                script_path = os.path.join(hook_dir, event_name)
                with open(script_path, "w", encoding="utf-8") as fh:
                    fh.write("#!/usr/bin/env bash\n")
                    fh.write('exec bash "{}" "{}" {} --agent {}\n'.format(
                        os.path.join(PLUGIN_ROOT, "hooks", "py.sh"),
                        os.path.join(PLUGIN_ROOT, "hooks", "agent_hook.py"),
                        verb, slug))
                os.chmod(script_path, 0o755)
                print("Wrote {}".format(script_path))
        if scope == "global" and len(hook_dirs) > 1:
            print("Written to every location {} is documented to read, "
                  "because its own docs and its runtime disagree.".format(
                      spec.get("label", slug)))
        _scope_note(spec, scope)
        return 0

    if slug in adapters.CLAUDE_SHAPED:
        new_hooks = adapters.build_claude_shaped(spec, PLUGIN_ROOT, slug)
    else:
        new_hooks = spec["build"](PLUGIN_ROOT, slug)

    # One tool (Devin) wants the hooks object bare in its project file and
    # wrapped everywhere else. Unwrap only for the file that asks for it.
    if scope == "project" and spec.get("project_unwrapped"):
        _write_json(path, _merge_bare_hooks(path, new_hooks.get("hooks", {})))
    else:
        _write_json(path, _merge_json_hooks(path, new_hooks))
    print("Wrote {}".format(path))

    # Some tools gate hooks behind a setting. Writing the config without
    # flipping it produces a setup that looks finished and records nothing.
    flag = spec.get("feature_flag")
    if flag and scope == "global":
        key, _section = flag
        cfg = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
        result = adapters.enable_codex_hooks(cfg)
        if result == "added":
            print("Enabled {} in {}".format(key, cfg))
        elif result == "manual":
            print("NOTE: {} is present but not set to true in {}.".format(key, cfg))
            print("      Hooks will not run until it is. Set it by hand.")

    if spec.get("unverified"):
        print("Note: {}'s exact hook payload fields are not published. "
              "If edits from it never show up in `aiattr.py status`, "
              "run it once with AIATTR_DEBUG=1 set and check stderr.".format(slug))
    _scope_note(spec, scope)
    return 0


def _scope_note(spec, scope):
    """Say which projects this install actually covers, every time.

    Silence here is how a student ends up believing a per-repo install is
    machine-wide and only finds out weeks later that most of their work was
    never recorded.
    """
    if scope == "global":
        print("This covers every project on this machine. Nothing to repeat.")
    elif spec.get("user"):
        print("Installed for this project only. Drop --project to cover every "
              "project instead.")
    else:
        print("{} has no user-level hook config, so this covers this project "
              "only -- run it again in each repo you build in.".format(
                  spec.get("label", "This tool")))


def cmd_flush(args):
    """Send any ledger records the server has not acknowledged yet.

    Mostly a diagnostic: the hooks do this on their own. It exists so a student
    who worked offline can confirm delivery before submitting, rather than
    having to trust that a background hook fired.
    """
    if not config.sync_enabled():
        print("Reporting is not configured. Run `configure` first.")
        return 1

    from core import outbox, paths, repoutil

    root = repoutil.repo_root(os.path.abspath(args.path or os.getcwd()))
    if not root:
        print("Not inside a git repository.")
        return 1

    rid = paths.repo_id(root)
    _pending, backlog = outbox.unsent(rid, paths.ledger_path(rid))
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

    _pending, remaining = outbox.unsent(rid, paths.ledger_path(rid))
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
    c.add_argument("--enable-hackatime", action="store_true",
                   help="send the AI/human line split to your Hackatime "
                        "account, using the key in ~/.wakatime.cfg")
    c.add_argument("--disable-hackatime", action="store_true")

    p = sub.add_parser("projects", help="list tracked repositories")
    p.add_argument("--json", action="store_true")

    i = sub.add_parser("ignore", help="stop tracking a repository")
    i.add_argument("path")
    i.add_argument("--remove", action="store_true", help="resume tracking it")

    sub.add_parser("sync", help="report the project index now")

    f = sub.add_parser("flush", help="deliver any undelivered ledger records")
    f.add_argument("path", nargs="?", help="repository (default: current directory)")

    h = sub.add_parser("install-hooks",
                       help="wire up an agent other than Claude Code")
    h.add_argument("tool", help="agent slug (e.g. cursor, codex), or 'list'")
    h.add_argument("--project", nargs="?", const=".", default=None,
                   help="install into one repo instead of machine-wide "
                        "(default: this directory)")

    args = ap.parse_args()
    handlers = {
        "status": cmd_status, "configure": cmd_configure,
        "projects": cmd_projects, "ignore": cmd_ignore, "sync": cmd_sync,
        "flush": cmd_flush, "install-hooks": cmd_install_hooks,
    }
    if args.cmd not in handlers:
        ap.print_help()
        return 1
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
