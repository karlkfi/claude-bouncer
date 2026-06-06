#!/usr/bin/env bash
#
# Pipe-tests for hooks/branch-guard.py. Spins up a throwaway git repo under
# tmp/, exercises each tool/branch combination, and asserts the emitted
# permissionDecision. Requires python3, jq, and git on PATH.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOK="$REPO_ROOT/hooks/branch-guard.py"
WORK="$REPO_ROOT/tmp/test-repo"

# Keep tests hermetic regardless of the caller's shell.
unset BRANCH_GUARD_PUSH_POLICY

pass=0
fail=0

cleanup() {
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

setup_repo() {
  rm -rf "$WORK"
  mkdir -p "$WORK"
  git -C "$WORK" init -q -b main
  git -C "$WORK" config user.name "Test"
  git -C "$WORK" config user.email "test@example.com"
  printf 'hello\n' > "$WORK/file.txt"
  git -C "$WORK" add -A
  git -C "$WORK" commit -q -m "init"
  git -C "$WORK" branch claude/x
}

# decision_for PAYLOAD CWD [ENV_KV] -> echoes the permissionDecision, or "none".
# ENV_KV is an optional `NAME=value` passed into the hook's environment.
decision_for() {
  local payload="$1" cwd="$2" env_kv="${3:-}" out
  out="$( cd "$cwd" && printf '%s' "$payload" | env ${env_kv} python3 "$HOOK" )"
  if [[ -z "$out" ]]; then
    printf 'none'
  else
    printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision'
  fi
}

check() {
  local name="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    printf 'ok   - %s (%s)\n' "$name" "$actual"
    pass=$((pass + 1))
  else
    printf 'FAIL - %s: expected %s, got %s\n' "$name" "$expected" "$actual"
    fail=$((fail + 1))
  fi
}

setup_repo

# 1. git commit on a non-protected branch -> allow
git -C "$WORK" checkout -q claude/x
check "commit on claude/x -> allow" allow \
  "$(decision_for '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}' "$WORK")"

# 2. git commit on main -> ask
git -C "$WORK" checkout -q main
check "commit on main -> ask" ask \
  "$(decision_for '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}' "$WORK")"

# 3. non-commit bash -> no decision
check "git status -> none" none \
  "$(decision_for '{"tool_name":"Bash","tool_input":{"command":"git status"}}' "$WORK")"
check "ls -> none" none \
  "$(decision_for '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}' "$WORK")"

# 3b. all-git chain containing a commit on a feature branch -> allow
git -C "$WORK" checkout -q claude/x
check "add && commit on claude/x -> allow" allow \
  "$(decision_for '{"tool_name":"Bash","tool_input":{"command":"git add -A && git commit -m x"}}' "$WORK")"

# 3c. commit chained with a NON-git command on a feature branch -> defer
#     (the bug the python port fixes: the trailing command must not ride along
#     into an auto-approve).
check "commit && rm on claude/x -> none" none \
  "$(decision_for '{"tool_name":"Bash","tool_input":{"command":"git commit -m x && rm -rf foo"}}' "$WORK")"

# 3d. same mixed chain on main -> ask (commit targets a protected branch)
git -C "$WORK" checkout -q main
check "commit && rm on main -> ask" ask \
  "$(decision_for '{"tool_name":"Bash","tool_input":{"command":"git commit -m x && rm -rf foo"}}' "$WORK")"

# 3e. env-prefixed / global-flag commit still detected on main -> ask
check "env-prefixed commit on main -> ask" ask \
  "$(decision_for "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"GIT_AUTHOR_NAME=x git -C $WORK commit -m y\"}}" "$WORK")"

# 3f. commit substring that is NOT a git commit invocation -> defer
check "git log --grep=commit -> none" none \
  "$(decision_for '{"tool_name":"Bash","tool_input":{"command":"git log --grep=commit"}}' "$WORK")"

# 4. edit of a file whose repo is on main -> ask
git -C "$WORK" checkout -q main
check "edit on main -> ask" ask \
  "$(decision_for "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\"$WORK/file.txt\"}}" "$REPO_ROOT")"

# 5. edit of a file whose repo is on a non-protected branch -> no decision
git -C "$WORK" checkout -q claude/x
check "write on claude/x -> none" none \
  "$(decision_for "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$WORK/file.txt\"}}" "$REPO_ROOT")"

# 6. unknown tool / missing file_path -> no decision
check "unknown tool -> none" none \
  "$(decision_for '{"tool_name":"Read","tool_input":{"file_path":"/etc/hosts"}}' "$REPO_ROOT")"
check "edit missing file_path -> none" none \
  "$(decision_for '{"tool_name":"Edit","tool_input":{}}' "$REPO_ROOT")"

# ---------------------------------------------------------------------------
# Push guard. Run from the worktree on the feature branch unless noted.
git -C "$WORK" checkout -q claude/x

# JSON payload helpers.
push()      { printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' "$1"; }
push_mode() { printf '{"tool_name":"Bash","tool_input":{"command":"%s"},"permission_mode":"%s"}' "$1" "$2"; }

PROT='BRANCH_GUARD_PUSH_POLICY=protected'
OFF='BRANCH_GUARD_PUSH_POLICY=off'

# 7. strict is the default: auto-approve a push of the worktree's own branch
#    (including force pushes); ask for anything else.
check "[default=strict] bare push -> allow" allow \
  "$(decision_for "$(push 'git push')" "$WORK")"
check "[default=strict] push origin HEAD -> allow" allow \
  "$(decision_for "$(push 'git push origin HEAD')" "$WORK")"
check "[default=strict] push origin claude/x -> allow" allow \
  "$(decision_for "$(push 'git push origin claude/x')" "$WORK")"
check "[default=strict] force-push worktree branch -> allow" allow \
  "$(decision_for "$(push 'git push --force origin claude/x')" "$WORK")"
check "[default=strict] force-push (-f) bare -> allow" allow \
  "$(decision_for "$(push 'git push -f')" "$WORK")"
check "[default=strict] commit && push (worktree) -> allow" allow \
  "$(decision_for "$(push 'git commit -m x && git push')" "$WORK")"
check "[default=strict] push origin main -> ask" ask \
  "$(decision_for "$(push 'git push origin main')" "$WORK")"
check "[default=strict] push origin feature-y -> ask" ask \
  "$(decision_for "$(push 'git push origin feature-y')" "$WORK")"
check "[default=strict] push origin HEAD:other -> ask" ask \
  "$(decision_for "$(push 'git push origin HEAD:other')" "$WORK")"
check "[default=strict] force-push to main -> ask" ask \
  "$(decision_for "$(push 'git push -f origin main')" "$WORK")"
check "[default=strict] push --all -> ask" ask \
  "$(decision_for "$(push 'git push --all origin')" "$WORK")"
check "[default=strict] push && rm (worktree) -> none" none \
  "$(decision_for "$(push 'git push && rm -rf foo')" "$WORK")"

# 8. protected policy: ask only on a protected target; never auto-approve.
check "[protected] push origin main -> ask" ask \
  "$(decision_for "$(push 'git push origin main')" "$WORK" "$PROT")"
check "[protected] push origin HEAD:main -> ask" ask \
  "$(decision_for "$(push 'git push origin HEAD:main')" "$WORK" "$PROT")"
check "[protected] delete main (:main) -> ask" ask \
  "$(decision_for "$(push 'git push origin :main')" "$WORK" "$PROT")"
check "[protected] worktree-branch push -> none" none \
  "$(decision_for "$(push 'git push origin HEAD')" "$WORK" "$PROT")"
check "[protected] push other feature branch -> none" none \
  "$(decision_for "$(push 'git push origin feature-y')" "$WORK" "$PROT")"

# 9. off policy: pushes are not guarded.
check "[off] push origin main -> none" none \
  "$(decision_for "$(push 'git push origin main')" "$WORK" "$OFF")"

# 10. non-interactive ('auto') modes: a would-be ask becomes deny (fail safe),
#     while allow and defer are unaffected.
check "[auto] push origin main -> deny" deny \
  "$(decision_for "$(push_mode 'git push origin main' 'auto')" "$WORK")"
check "[bypassPermissions] push origin main -> deny" deny \
  "$(decision_for "$(push_mode 'git push origin main' 'bypassPermissions')" "$WORK")"
check "[auto] worktree-branch push -> allow (unchanged)" allow \
  "$(decision_for "$(push_mode 'git push' 'auto')" "$WORK")"
check "[acceptEdits] push origin main -> ask (human present)" ask \
  "$(decision_for "$(push_mode 'git push origin main' 'acceptEdits')" "$WORK")"

# 11. non-interactive mode also converts a commit-on-protected ask to deny.
git -C "$WORK" checkout -q main
check "[auto] commit on main -> deny" deny \
  "$(decision_for "$(push_mode 'git commit -m x' 'auto')" "$WORK")"
git -C "$WORK" checkout -q claude/x

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
