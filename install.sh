#!/usr/bin/env bash
# Installs the ai-attribution CLI and hooks without requiring Claude Code.
#
# Claude Code students get this plugin through `claude plugin install`, which
# also registers hooks/hooks.json so Claude Code fires it automatically. No
# other agent has an equivalent registration step -- `aiattr install-hooks
# <tool>` does that job for them instead -- so this script's only job is
# getting the files onto disk at a fixed, predictable path. Fixed matters for
# the same reason core/paths.py keeps the data directory fixed: a path that
# can move silently orphans a student's setup later.
#
# Usage:  curl -fsSL https://raw.githubusercontent.com/K-man1/b2b-hook/main/install.sh | sh
#
# Safe to re-run: it replaces whatever was at the install directory, which is
# how updating works -- run it again to pick up a newer version.

set -eu

REPO_URL="${AIATTR_REPO_URL:-https://github.com/K-man1/b2b-hook}"
BRANCH="${AIATTR_BRANCH:-main}"
INSTALL_DIR="${AIATTR_INSTALL_DIR:-$HOME/.ai-attribution/plugin}"

for bin in curl tar; do
  command -v "$bin" >/dev/null 2>&1 || {
    echo "error: $bin is required and was not found on PATH." >&2
    exit 1
  }
done

PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "error: no python3 found. Install Python 3 first: https://python.org" >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading ai-attribution ($BRANCH)..."
curl -fsSL "$REPO_URL/archive/refs/heads/$BRANCH.tar.gz" | tar xz -C "$tmp"

# GitHub's tarball extracts to a single "<repo>-<branch>/" directory, whatever
# its exact name -- found rather than hardcoded so a fork under a different
# name still works with AIATTR_REPO_URL overridden.
extracted="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [ -z "$extracted" ] || [ ! -f "$extracted/cli/aiattr.py" ]; then
  echo "error: download did not produce a usable plugin directory." >&2
  exit 1
fi

mkdir -p "$(dirname "$INSTALL_DIR")"
rm -rf "$INSTALL_DIR"
mv "$extracted" "$INSTALL_DIR"

echo "Installed to $INSTALL_DIR"
echo
echo "Add this to your shell profile (~/.zshrc or ~/.bashrc), then restart your shell:"
echo "  alias aiattr='$PYTHON \"$INSTALL_DIR/cli/aiattr.py\"'"
echo
echo "Then connect it to your account (your dashboard shows this with your key filled in):"
echo "  aiattr configure --key YOUR_KEY --endpoint YOUR_ENDPOINT"
echo
echo "Then wire up your AI coding app:"
echo "  aiattr install-hooks <tool>     (run 'aiattr install-hooks list' to see options)"
