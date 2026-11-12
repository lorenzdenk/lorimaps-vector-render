#!/usr/bin/env bash
# Publishes the logs directory to the branch "render-logs" (readable without logging in).
# usage: push_logs.sh <logs dir> <status text> [branch]
set -e
src="$1"; status="$2"; branch="${3:-render-logs}"
tmp=$(mktemp -d)
cp -r "$src"/. "$tmp"/ 2>/dev/null || true
cd "$tmp"
echo "$status" > STATUS
git init -q -b "$branch"
git config user.name "github-actions"
git config user.email "github-actions@users.noreply.github.com"
git add -A && git commit -qm "$status"
git push -q -f "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}" "$branch"
rm -rf "$tmp"
