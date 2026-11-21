"""Strip credentials out of prompt text before it is hashed or sealed.

Students paste things into prompts: an error log with a token in the URL, a
.env file, "why doesn't this key work: sk-...". Sealing protects that text on
the way to the server, but the server and its reviewers still read it once it
is opened, and nobody needs a student's live Stripe key to judge whether a
turn was real work.

High-precision patterns only. A false negative leaks a key the student
pasted themselves; a false positive destroys part of the evidence a reviewer
relies on. So each pattern matches a format with a distinctive prefix or
shape, plus one guarded `name = value` form for secrets assigned in code.

Redaction happens before the prompt is hashed, so the commitment in the
ledger is to the redacted text: the server can verify it without the secret
ever having existed on its side.
"""

import re

_PATTERNS = (
    ("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.S)),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("api_key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("plugin_key", re.compile(r"\bb2b_[A-Za-z0-9]{16,}\b")),
)

# `password = "hunter2hunter2"`, `API_KEY: abc...`, `token=...`. The name is
# kept, only the value goes, so a reviewer can still see what the student was
# asking about.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b([A-Z0-9_\-]*(?:password|passwd|secret|api[_\-]?key|access[_\-]?token|auth[_\-]?token)[A-Z0-9_\-]*)
    (\s*[:=]\s*["']?)
    ([^\s"']{8,})
    """)


def redact(text):
    """Return (redacted_text, count). Never raises on odd input."""
    if not text:
        return text or "", 0
    count = 0
    for kind, pattern in _PATTERNS:
        text, n = pattern.subn("[redacted:{}]".format(kind), text)
        count += n

    hits = []

    def _value(match):
        # Already caught by a format pattern above: leave it, count it once.
        if match.group(3).startswith("[redacted:"):
            return match.group(0)
        hits.append(1)
        return match.group(1) + match.group(2) + "[redacted:assigned_secret]"

    text = _ASSIGNMENT.sub(_value, text)
    return text, count + len(hits)
