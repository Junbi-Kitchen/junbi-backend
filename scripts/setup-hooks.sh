#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/setup-hooks.sh
# Run once after cloning to install git hooks for this repo.
#
# Usage:
#   cd gook-backend
#   bash scripts/setup-hooks.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_SRC="$REPO_ROOT/git-hooks"
HOOKS_DST="$REPO_ROOT/.git/hooks"

echo "Installing git hooks for $(basename "$REPO_ROOT")..."

for hook in "$HOOKS_SRC"/*; do
  name="$(basename "$hook")"
  dst="$HOOKS_DST/$name"

  cp "$hook" "$dst"
  chmod +x "$dst"
  echo "  ✓ $name installed → .git/hooks/$name"
done

echo ""
echo "Done. Hooks active:"
echo "  pre-push  — blocks push if no worklogs/ entry in the commits"
echo ""
echo "To bypass in a true emergency: SKIP_WORKLOG_CHECK=1 git push"
