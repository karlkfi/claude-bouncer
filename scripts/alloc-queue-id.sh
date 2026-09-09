#!/usr/bin/env bash
# Allocate a backlog Q-ID by claiming a ref on the shared remote.
#
# Creating a ref that does not yet exist is a compare-and-swap against the
# remote, so two sessions asking at the same instant are handed different IDs
# with no lock, no lease file and nothing to release when a session dies.
#
# Three properties this depends on. Changing any one of them silently hands two
# sessions the same ID, which is the failure the mechanism exists to remove.
#
#   1. THE CLAIM OBJECT MUST BE UNIQUE PER CLAIMANT. Every form of `git push`
#      short-circuits when the ref already points at the object being pushed:
#      it sends nothing, prints "Everything up-to-date" and exits 0. A shared
#      sentinel object therefore reports success to the loser of every race.
#      Each claim here writes its own blob, so a second claimant is always
#      pushing a different object at the ref.
#   2. THE PUSH MUST CARRY `--force-with-lease=<ref>:`. The empty expectation
#      means "this ref must not exist", checked by the receiving end, and a
#      violation exits 1 with `stale info`. Without it the rejection relies on
#      the claim not being a fast-forward, which holds for unrelated blobs and
#      stops holding the moment anyone points a claim at a commit.
#   3. THE CLAIM MUST NOT POINT AT A BRANCH TIP. A ref is a GC root, so claims
#      anchored to `claude/*` tips would pin every squash-orphaned branch
#      history forever. A blob retains itself and nothing else.
#
# Usage: alloc-queue-id.sh [--remote <name>] [--store <dir>] [--table <file>]
#                          TITLE [TITLE...]
#
#   One ID per title. The title is mandatory because this is the one point
#   every filed row passes through, which makes it the only place a
#   near-duplicate check can sit.
#
#   --remote  remote holding the claims (default: origin)
#   --store   per-item store to read the local floor from (default: docs/queue)
#   --table   legacy STATUS.md to read the local floor from instead
set -euo pipefail

REF_NS='refs/queue-ids'
# Bounds the advance-on-collision walk. Exceeding it means a very large
# concurrent burst or a bug; either way, failing beats spinning.
MAX_ATTEMPTS=25

remote=origin
store=
table=
titles=()

die() {
	printf 'alloc-queue-id: %s\n' "$*" >&2
	exit 1
}

while [[ $# -gt 0 ]]; do
	case "$1" in
	--remote) remote="${2:?--remote needs a value}"; shift 2 ;;
	--store) store="${2:?--store needs a value}"; shift 2 ;;
	--table) table="${2:?--table needs a value}"; shift 2 ;;
	-h | --help)
		awk '/^# Usage:/,/^set -euo/ { sub(/^#[[:space:]]?/, ""); print }' "$0" | sed '$d'
		exit 0
		;;
	-*) die "unknown option: $1" ;;
	*) titles+=("$1"); shift ;;
	esac
done

[[ ${#titles[@]} -gt 0 ]] || die "give one title per ID; see --help"

root="$(git rev-parse --show-toplevel)" || die "not in a git repository"
[[ -n "$store" || -n "$table" ]] || store="$root/docs/queue"

# The floor is the highest ID anyone has ever taken, from three sources that
# each under-report on their own: the remote's claims miss IDs filed before the
# namespace existed, the working tree misses every completed item, and history
# misses IDs claimed but not yet filed.
highest_local() {
	{
		if [[ -n "$store" ]]; then
			git -C "$root" log --diff-filter=A --name-only --pretty=format: -- "$store" 2>/dev/null || true
			ls "$store" 2>/dev/null || true
		fi
		if [[ -n "$table" ]]; then
			git -C "$root" log -p --pretty=format: -- "$table" 2>/dev/null || true
			cat "$table" 2>/dev/null || true
		fi
	} | grep -oE '\bQ[0-9]+\b' | grep -oE '[0-9]+' | sort -n | tail -1
}

highest_claimed() {
	git ls-remote "$remote" "$REF_NS/*" 2>/dev/null |
		grep -oE 'Q[0-9]+$' | grep -oE '[0-9]+' | sort -n | tail -1
}

# A title close to one already filed is usually a duplicate rather than a new
# item. Reported, never blocking: only a reader can tell a genuine sibling from
# a re-file, and refusing here would put a judgement call in a script.
warn_near_duplicate() {
	local title="$1" existing
	existing="$( { [[ -n "$store" ]] && grep -h '^# ' "$store"/Q*.md 2>/dev/null; } || true)"
	[[ -n "$existing" ]] || return 0
	local key
	key="$(printf '%s' "$title" | tr '[:upper:]' '[:lower:]' | tr -cs '[:alnum:]' '\n' |
		awk 'length($0) > 4' | sort -u)"
	[[ -n "$key" ]] || return 0
	while IFS= read -r line; do
		local overlap total
		total="$(printf '%s\n' "$key" | wc -l | tr -d ' ')"
		overlap="$(printf '%s\n' "$key" |
			grep -cFf <(printf '%s' "$line" | tr '[:upper:]' '[:lower:]' |
				tr -cs '[:alnum:]' '\n' | awk 'length($0) > 4' | sort -u) 2>/dev/null || true)"
		if [[ -n "$overlap" && "$overlap" -gt 0 ]] &&
			[[ $((overlap * 2)) -ge "$total" ]]; then
			printf 'alloc-queue-id: near-duplicate of an existing item: %s\n' \
				"${line#\# }" >&2
		fi
	done <<<"$existing"
}

claim() {
	# Separate statements: bash expands every word of a `local` before running
	# it, so `local n="$1" ref="...$n"` would read n while it is still unset.
	local n="$1"
	local ref="$REF_NS/Q$n"
	local blob
	# Unique per claimant and per attempt: property 1 above.
	blob="$(printf 'queue-id claim Q%s %s %s\n' "$n" "$$" "${EPOCHREALTIME:-$RANDOM}" |
		git -C "$root" hash-object -w --stdin)"
	git -C "$root" push --force-with-lease="$ref:" --quiet \
		"$remote" "${blob}:${ref}" 2>/dev/null
}

floor_local="$(highest_local || true)"
floor_claimed="$(highest_claimed || true)"
floor=0
for candidate in "${floor_local:-0}" "${floor_claimed:-0}"; do
	[[ "$candidate" -gt "$floor" ]] && floor="$candidate"
done

for title in "${titles[@]}"; do
	warn_near_duplicate "$title"
	won=
	for ((attempt = 0; attempt < MAX_ATTEMPTS; attempt++)); do
		floor=$((floor + 1))
		if claim "$floor"; then
			won="Q$floor"
			break
		fi
	done
	[[ -n "$won" ]] || die "no ID claimed in $MAX_ATTEMPTS attempts; is $remote reachable?"
	printf '%s\n' "$won"
done
