"""Core logic for the AI attribution plugin. Stdlib only, no dependencies."""

# Lives here rather than in hooks/_common.py so that core/ never has to import
# upward into hooks/ to find it. Every other module in core/ is importable on
# its own; the version string should not be the one thing that drags the hook
# layer in behind it.
VERSION = "0.7.0"
