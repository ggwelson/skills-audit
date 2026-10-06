#!/usr/bin/env bash
# Regenerate docs/screenshots from the demo home. Needs Google Chrome (or set CHROME).
set -euo pipefail
cd "$(dirname "$0")/.."

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
PYTHONPATH=src python3 -m skills_audit --demo --out "$out" >/dev/null
chrome=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
mkdir -p docs/screenshots

# shot NAME THEME WIDTH,HEIGHT HASH
# Some Chrome installs keep running after writing the file, so wait for it and then stop Chrome.
shot() {
  local file="docs/screenshots/$1.png" dark=()
  [ "$2" = dark ] && dark=(--force-dark-mode)
  rm -f "$file"
  "$chrome" --headless=new --hide-scrollbars --force-device-scale-factor=2 ${dark[@]+"${dark[@]}"} \
    --no-first-run --no-default-browser-check --window-size="$3" --user-data-dir="$out/profile-$1" \
    --screenshot="$file" "file://$out/skills-browser.html#$4" >/dev/null 2>&1 &
  local pid=$!
  for _ in $(seq 1 120); do
    [ -s "$file" ] && break
    sleep 0.5
  done
  sleep 1
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  [ -s "$file" ] || { echo "no screenshot for $1" >&2; return 1; }
  echo "$file"
}

kit="claude-plugin:writing-kit@acme-tools"
overview="select=$kit,claude-skill:~/.claude/skills/pr-triage,shared-skill:~/.agents/skills/sql-helper,cleanup:agents-lock-stale&open=$kit&focus=$kit%7C3"
shot overview-dark dark 1440,900 "$overview"
shot overview-light light 1440,900 "$overview"
shot removal-steps dark 1440,900 "agent=shared&open=shared-skill:~/.agents/skills/react-best-practices,shared-skill:~/.agents/skills/sql-helper&select=shared-skill:~/.agents/skills/react-best-practices"
