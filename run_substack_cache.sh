#!/bin/zsh
# Entry point for the launchd job that refreshes substack_cache/ from a
# residential IP. Installed as ~/Library/LaunchAgents/com.robpaine.feeds-substack-cache.plist
# pointing at this script inside a permanent clone of the repo.
#
# Self-heals the checkout: prefers main once substack_cache.py has landed
# there; until then runs from whatever branch is checked out (the PR branch).

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"

git fetch -q origin
if git ls-tree -r --name-only origin/main | grep -qx "substack_cache.py"; then
  current=$(git branch --show-current)
  if [ "$current" != "main" ]; then
    git switch -q main
  fi
fi
git pull -q --rebase

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q PyYAML
fi

exec .venv/bin/python substack_cache.py
