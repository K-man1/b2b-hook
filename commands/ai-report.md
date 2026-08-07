---
description: Show what share of this repo was written by AI vs. by you
---

Run the attribution report for the current repository and show the user its
output verbatim:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" "${CLAUDE_PLUGIN_ROOT}/verifier/report_cli.py"
```

Present the table as-is. Do not recompute, estimate, or adjust any of the
numbers yourself, and do not add your own guess about how much of the code you
wrote. The whole point of this tool is that the figures come from recorded
file diffs rather than from a model's recollection.

If the user asks why a bucket looks wrong, the useful things to explain are:

- `unobserved` covers lines the plugin never saw written: either already on
  disk when tracking started, or added while no session was running.
- Edits made outside a Claude session land in `human` only after the next
  session start, when the drift sweep runs.
- The plugin cannot tell hand-written code from code pasted in from elsewhere.
  Both changed the file while nothing was watching, so both count as `human`.
  If the user asks whether pasted code is detected, say plainly that it is not.
