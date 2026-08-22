# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing` `retro`
**Next ID:** Q86

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q74"></a>Q74 | Escalate suppression in modes that pre-approve, so declining to vouch protects there | `security` | 🔲 | M | A withheld `allow` still runs under `auto`/`acceptEdits`/`bypassPermissions` ([matrix](permission-modes.md)). Escalating to `ask`/`deny` would block ~1,657 commands in `auto`, most routine heredocs — needs a narrower signal first. |
| <a id="Q75"></a>Q75 | Guard `cut` and `base64`, which read outside the root under an `allow` | `security` | 🔲 | S | Measured: `cat in.txt && cut -f1 /outside` returns `allow`, while `xxd`/`od`/`strings`/`wc` already ask. `cut` appears 1,090 times in the corpus. Add the two `SPEC` rows. |
| <a id="Q73"></a>Q73 | Extend the interpreter allow-suppression to the PowerShell tool | `security` | 🔲 | S | PowerShell still lets a clean guarded command vouch for interpreter code, unlike Bash since Q72: `Get-Content .\README.md; python3 -c '…'` returns `allow`. Port `interp_code_source` over. |
| <a id="Q76"></a>Q76 | Extend entry-operand resolution to the PowerShell tool | `parsing` | 🔲 | S | `Remove-Item`/`Move-Item` have a read/write `role` but no entry role, so removing a symlink is still judged by its target, unlike Bash. Port `ENTRY_OPERANDS` over ([plan](plan/entry-operands.md)). |
| <a id="Q77"></a>Q77 | Guard `unlink`, which is unchecked on any path | `security` | 🔲 | S | Measured: `unlink <outside>/x` defers, because `unlink` has no `SPEC` row while `rm` does. Same shape as `rm` (positional operands, no value-taking flags), so it is one `ALIASES` entry plus a fixture. |
| <a id="Q80"></a>Q80 | Guard `ln`, which creates a link to any path unchecked | `security` | 🔲 | S | Measured 2026-08-19: `ln -s /q167-src /q167-link` defers, while `cp` to the same path denies. `classify_ln` already parses the operands to stage links in a chain; nothing checks them. |
| <a id="Q81"></a>Q81 | Track `case` patterns so their `)` does not end a command substitution early | `security` `parsing` | 🔲 | S | Measured 2026-08-20 on the 169 fix: a `case` pattern's `)` inside `"$(…)"` ends the substitution early, so an odd-quote heredoc body there is still silent. Needs `case`/`esac` tracking. |
| <a id="Q85"></a>Q85 | Count the friction report's native-tool decisions, which only Bash reaches today | `infra` | 🔲 | S | Both passes filter on `PreToolUse:Bash` ([scope](development/measuring-friction.md)), so a guarded `Edit`/`Write`/`Read` is absent, sibling-checkout denies included. Measured 2026-08-21: 5 such blocks beside 80 Bash ones. |
| <a id="Q84"></a>Q84 | Offer a `supervise` posture that turns the guard's denies back into prompts | `security` `infra` | 🔲 | S | An operator building trust in the guard can't watch it work short of downgrading it. `hook-verdict` prescribes an opt-in `WORKSPACE_GUARD_POSTURE=supervise`; the default stays unsupervised. |
| <a id="Q79"></a>Q79 | Correct or re-measure [permission-modes.md](permission-modes.md), which says `ask` blocks in every mode | `docs` `retro` | 🔲 | S | Measured 2026-08-18: an `ask` the operator approves runs, so "block everywhere" is false on a literal read. The v1.10.0 notes cite it as why Q72 checks the path outright, so it is load-bearing. Reword, or re-run the forced-decision measurement. |
| <a id="Q78"></a>Q78 | Carry the `session-backlog` rename into two stale code comments | `docs` | 🔲 | S | Both name the `backlog` skill. `tests/test_shell_scripts.py:9` is repo-local, fix here. `scripts/lint-backlog.sh:5` is vendored from `karlkfi/claude-skills`, so fix it there first. |
| <a id="Q82"></a>Q82 | Reconcile the "no real sensitive paths in fixtures" rule with the tree | `tests` `docs` | 🔲 | S | Fixtures must name synthetic placeholders, not real sensitive paths. `tests/test_workspace_guard.py` uses `/etc/passwd` ~20x, `~/.ssh/id_rsa`, `$HOME/.aws/credentials`. Rename, or narrow the rule. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
| <a id="Q42"></a>Q42 | Catch a glob match that is itself a symlink out of the root | `security` | M | **Demand:** a glob-matched in-root name that points outside gets read silently. A glob resolves as the pattern, so `realpath` never sees the match. Closing it needs match enumeration. |
| <a id="Q47"></a>Q47 | Catch a `**` glob item that matches fewer segments than the pattern | `security` | M | **Demand:** a session runs `shopt -s globstar`. Verified: `docs/**` expands to `docs/` too, so a loop body's trailing `../` climbs above the root undetected. Issue 99's proxy needs fixed segments. |
| <a id="Q53"></a>Q53 | Grow the PowerShell coverage past the cmdlet table | `security` | M | **Demand:** a real outside read slips through. A .NET call, a native `.exe`, or an unlisted cmdlet is unchecked and silent; prompting on the unparsed tail was rejected as too noisy. See [Q51's known gaps](plan/q51-powershell-tool.md). |
| <a id="Q54"></a>Q54 | Quiet `$_` in a PowerShell `ForEach-Object` block | `parsing` | S | **Demand:** the friction report shows `$_` prompts accumulating. It reports as 'expand' like bash's `cat $f`, but carries far more of PowerShell's idiom. See [Q51's known gaps](plan/q51-powershell-tool.md). |
