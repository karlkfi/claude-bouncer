#!/usr/bin/env bash
#
# branch-guard: a Claude Code PreToolUse hook.
#
# Auto-approves git commits on non-protected branches (e.g. claude/*, feature
# branches) and prompts (ask) before commits or file edits that target a
# protected branch (main/master). Emits no decision for anything else, so the
# normal permission flow applies.
set -euo pipefail

PROTECTED_BRANCH_REGEX='^(main|master)$'

# emit DECISION REASON  ->  prints the PreToolUse hook decision JSON.
emit() {
  local decision="$1" reason="$2"
  reason="${reason//\\/\\\\}"   # escape backslashes for JSON
  reason="${reason//\"/\\\"}"   # escape double quotes for JSON
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"%s","permissionDecisionReason":"%s"}}\n' \
    "$decision" "$reason"
}

main() {
  local input tool branch
  input="$(cat)"
  tool="$(printf '%s' "$input" | jq -r '.tool_name // empty')"

  case "$tool" in
    Bash)
      local cmd
      cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"
      # Only weigh in on `git commit`; defer on everything else.
      if ! printf '%s' "$cmd" | grep -Eq 'git[^&|;]*[[:space:]]commit([[:space:]]|$)'; then
        exit 0
      fi
      branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
      ;;
    Edit|Write|MultiEdit)
      local file dir
      file="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')"
      [[ -n "$file" ]] || exit 0
      dir="$(dirname "$file")"
      branch="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
      ;;
    *)
      exit 0
      ;;
  esac

  # Could not resolve a branch (not a repo / detached HEAD) -> defer.
  [[ -n "$branch" ]] || exit 0

  if [[ "$branch" =~ $PROTECTED_BRANCH_REGEX ]]; then
    emit ask "Targets protected branch '$branch' — confirm before proceeding."
    exit 0
  fi

  # Non-protected branch: auto-approve commits; defer edits to the normal flow.
  if [[ "$tool" == "Bash" ]]; then
    emit allow "Commit on non-protected branch '$branch' — auto-approved."
  fi
  exit 0
}

main "$@"
