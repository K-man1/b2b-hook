"""Who wrote the code: the agent identity carried on every record.

Until now this was a constant. `heartbeat.EDITOR` was the string `claude-code`
and nothing else was possible, because nothing else could fire the hooks. Once
a second agent can, the identity stops being a property of the build and starts
being a property of the individual edit: a student may run Claude Code and
Cursor against the same repo in the same afternoon, and two heartbeats for the
same file must not claim to come from the same tool.

Two separate names live in each entry, and conflating them is the mistake this
module exists to prevent:

  `editor`  goes on the Hackatime heartbeat. It has to be a string Hackatime's
            own parser recognises, because their AI_AGENT_PRODUCTS list is what
            decides whether a row is understood as agent-written at all. We do
            not get to invent these.

  `label`   is for humans reading a report. It can say whatever is clearest.

`verified` records whether the `editor` string was actually read out of the
Hackatime source or merely looks right. That distinction is load-bearing and is
the reason it is stored rather than assumed: an unrecognised product name does
not fail loudly, it silently lands the row under a name their parser does not
associate with an AI agent, and the attribution quietly stops counting. A wrong
guess here is invisible, so it is written down as a guess.
"""

import os

DEFAULT = "claude-code"

# Confirmed present in WakatimeUserAgentParser::AI_AGENT_PRODUCTS by reading the
# hackclub/hackatime tree. Anything marked verified=False is our best guess at
# the slug and must be checked against that list before it is trusted; see the
# module docstring for why a bad guess is silent rather than loud.
AGENTS = {
    "claude-code": {"editor": "claude-code", "label": "Claude Code", "verified": True},
    "codex": {"editor": "codex", "label": "Codex CLI", "verified": True},
    "cursor": {"editor": "cursor", "label": "Cursor", "verified": True},
    "windsurf": {"editor": "windsurf", "label": "Windsurf", "verified": True},
    "github-copilot": {"editor": "copilot", "label": "GitHub Copilot", "verified": True},

    "gemini-cli": {"editor": "gemini-cli", "label": "Gemini CLI", "verified": False},
    "qwen-code": {"editor": "qwen-code", "label": "Qwen Code", "verified": False},
    "opencode": {"editor": "opencode", "label": "opencode", "verified": False},
    "goose": {"editor": "goose", "label": "Goose", "verified": False},
    "amp": {"editor": "amp", "label": "Amp", "verified": False},
    "github-copilot-cli": {"editor": "copilot-cli", "label": "Copilot CLI", "verified": False},
    "cline": {"editor": "cline", "label": "Cline", "verified": False},
    "roo-code": {"editor": "roo-code", "label": "Roo Code", "verified": False},
    "cody": {"editor": "cody", "label": "Cody", "verified": False},
    "trae": {"editor": "trae", "label": "Trae", "verified": False},
    "antigravity": {"editor": "antigravity", "label": "Antigravity", "verified": False},
    "kiro": {"editor": "kiro", "label": "Kiro", "verified": False},
    "qoder": {"editor": "qoder", "label": "Qoder", "verified": False},
    "devin": {"editor": "devin", "label": "Devin", "verified": False},
}

# Deliberately absent: vscodium. It does not write code, so it cannot author a
# line. It is an editor that other agents run inside, and the agent is what this
# records. Listing it would invite a heartbeat claiming an editor authored
# something, which is exactly the claim we cannot support.

UA = "{} ai-attribution-wakatime/{}"


def resolve(slug):
    """Identity for an agent slug. Unknown slugs degrade, they do not raise.

    A hook firing for an agent added after this table was written should still
    produce a record. Losing the edit entirely would be worse than filing it
    under a product name Hackatime may not recognise, because the ledger is also
    read by the Back to Basics server, which does not care about their parser.
    """
    slug = (slug or DEFAULT).strip().lower()
    known = AGENTS.get(slug)
    if known:
        return dict(known, slug=slug)
    return {"slug": slug, "editor": slug, "label": slug, "verified": False}


def current(payload=None):
    """The agent this hook invocation is running on behalf of.

    Resolution order is explicit-beats-ambient: a payload field wins over the
    environment, because one shell can host several agents and an exported
    variable outlives the tool that set it. The default keeps every existing
    Claude Code install reporting exactly what it reported before.
    """
    if payload:
        named = payload.get("aiattr_agent") or payload.get("agent")
        if named:
            return resolve(named)
    return resolve(os.environ.get("AIATTR_AGENT") or DEFAULT)
