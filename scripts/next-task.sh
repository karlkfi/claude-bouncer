#!/usr/bin/env bash
#
# next-task.sh — print a kickoff prompt for the top ready (🔲) Queue row.
#
# Usage:
#   claude -n "$(scripts/next-task.sh --title)" "$(scripts/next-task.sh)"
#     -> session named "QN: <title>", prompted with the full kickoff
#   scripts/next-task.sh [--title] [path/to/STATUS.md]   # just print
#
# Naming the session after the Q-ID keeps `claude --resume` history and
# after-the-fact metrics readable. The session still re-verifies the pick
# (open PRs, blockers); if the row turns out to be in flight, it takes the
# next one (rename with /rename if that happens).

set -euo pipefail

MODE=prompt
if [[ "${1:-}" == "--title" ]]; then
    MODE=title
    shift
fi

if [[ -n "${1:-}" ]]; then
    FILE="$1"
else
    FILE="$(git rev-parse --show-toplevel)/docs/STATUS.md"
fi

if [[ ! -f "$FILE" ]]; then
    printf 'next-task: file not found: %s\n' "$FILE" >&2
    exit 2
fi

awk -F'|' -v mode="$MODE" '
function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
/^## Queue/ { q = 1; next }
/^## /      { q = 0 }
q && /^\|/ {
    id = $2
    gsub(/<[^>]*>/, "", id)
    gsub(/[[:space:]]/, "", id)
    if (id !~ /^Q[0-9]+$/) next
    st = trim($5)
    if (st != "🔲") next
    item = trim($3)
    gsub(/\]\([^)]*\)/, "", item)   # [title](link) -> title
    gsub(/\[/, "", item)
    notes = trim($7)
    if (mode == "title")
        printf "%s: %s\n", id, item
    else
        printf "%s: %s — take this item from the top of the Queue in docs/STATUS.md and work it per the repo backlog process: run gh pr list first, verify any blockers, do the work, then delete the row in its own isolated docs(status) commit. Notes: %s\n", id, item, notes
    found = 1
    exit
}
END { if (!found) { print "next-task: no ready (🔲) row in the Queue" > "/dev/stderr"; exit 1 } }
' "$FILE"
