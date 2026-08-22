#!/usr/bin/env bash
#
# Diffs every plugins/<name>/docs/releases/vX.Y.Z.md against the body GitHub
# currently serves for that release. The files are what the runbook publishes
# from, so the copy that can drift is the published one: an edit made in the
# browser changes it and leaves no diff anywhere. Requires gh on PATH and
# authenticated, so this is not part of `make check`, which is hermetic and
# offline.
#
# A release cut from this repo is tagged <plugin>/vX.Y.Z. Everything older
# shipped from the plugin's own repository as a bare vX.Y.Z and still resolves
# there, so each file is checked against whichever venue published it. See
# docs/development/release-process.md.
#
# Usage: scripts/verify-release-notes.sh [--dry-run] [plugin ...]
#        default: every plugin under plugins/
#        --dry-run  print what would be checked and query nothing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY_RUN=0
plugins=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; $d'
      exit 0 ;;
    -*) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    *)  plugins+=("$1"); shift ;;
  esac
done

if [[ ${#plugins[@]} -eq 0 ]]; then
  for dir in "$REPO_ROOT"/plugins/*/; do
    [[ -d "$dir" ]] || continue
    plugins+=("$(basename "$dir")")
  done
fi
for plugin in "${plugins[@]}"; do
  [[ -d "$REPO_ROOT/plugins/$plugin" ]] || {
    printf 'no such plugin: %s\n' "$plugin" >&2
    exit 2
  }
done

# (plugin, version) pairs, gathered before anything is queried so an empty run
# is caught once rather than per plugin.
rows=()
for plugin in "${plugins[@]}"; do
  for notes in "$REPO_ROOT/plugins/$plugin/docs/releases"/v*.md; do
    [[ -e "$notes" ]] || continue
    rows+=("$plugin"$'\t'"$(basename "$notes" .md)")
  done
done

# An empty list would otherwise report "0 differ" and exit 0, which reads as
# a clean check rather than a check that never ran.
if [[ ${#rows[@]} -eq 0 ]]; then
  printf 'no release notes found under plugins/*/docs/releases\n' >&2
  exit 1
fi

if [[ $DRY_RUN -eq 1 ]]; then
  for row in "${rows[@]}"; do
    IFS=$'\t' read -r plugin version <<<"$row"
    printf 'plugins/%s/docs/releases/%s.md\t%s/%s\t%s\tkarlkfi/claude-%s\n' \
      "$plugin" "$version" "$plugin" "$version" "$version" "$plugin"
  done
  exit 0
fi

if ! command -v gh >/dev/null 2>&1; then
  printf 'gh is not on PATH; cannot read the published bodies\n' >&2
  exit 1
fi

# Host-wide temp is shared across worktrees, so concurrent runs would collide.
WORK="$REPO_ROOT/tmp/verify-release-notes.$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

# One listing decides every file's venue. Probing each tag instead would read a
# transient gh failure as "not published here" and quietly check the wrong
# repository.
if ! HERE_TAGS="$(gh release list --limit 500 --json tagName --jq '.[].tagName')"; then
  printf 'could not list this repository'"'"'s releases\n' >&2
  exit 1
fi

pass=0
fail=0
for row in "${rows[@]}"; do
  IFS=$'\t' read -r plugin version <<<"$row"
  notes="$REPO_ROOT/plugins/$plugin/docs/releases/$version.md"
  out="$WORK/$plugin-$version"

  gh_args=(release view)
  if printf '%s\n' "$HERE_TAGS" | grep -Fxq "$plugin/$version"; then
    label="$plugin/$version"
    gh_args+=("$plugin/$version")
  else
    label="$plugin $version (karlkfi/claude-$plugin)"
    gh_args+=("$version" --repo "karlkfi/claude-$plugin")
  fi

  # --template, not --jq: `--jq .body` appends a newline unconditionally, so it
  # reports a difference on every release whose body already ends with one.
  # Redirect straight to a file — routing the body through a shell variable
  # would strip its trailing newlines and defeat the byte-exact comparison.
  if ! gh "${gh_args[@]}" --json body --template '{{.body}}' \
      > "$out.published" 2>"$out.err"; then
    printf 'FAIL - %s: could not read the published release\n' "$label"
    sed 's/^/         /' "$out.err" >&2
    fail=$((fail + 1))
    continue
  fi
  if diff -q "$out.published" "$notes" >/dev/null; then
    printf 'ok   - %s\n' "$label"
    pass=$((pass + 1))
  else
    printf 'FAIL - %s: published body differs from plugins/%s/docs/releases/%s.md\n' \
      "$label" "$plugin" "$version"
    diff "$out.published" "$notes" | sed 's/^/         /' || true
    fail=$((fail + 1))
  fi
done

printf '\n%d match, %d differ\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
