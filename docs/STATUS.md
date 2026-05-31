# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

## Conventions

**Status:** ✅ done · ▶ started · 🔲 ready · 🚫 blocked · 💤 deferred
**Size:** S = one session · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing`

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md) for the full rules. Short version:
- **Starting an S item:** complete it, delete the row.
- **Starting an M/L item:** create or update a plan doc under `docs/plan/`; delete the row here when done. (Skip the `▶ Started` marker unless you have a specific reason — the open PR is the in-flight signal.)
- **New item identified:** append it to the Queue with the next unused ID. Batch audit-discovery items in one commit.
- **`Last touched:` is one line, date only.** Do not append session narrative.

Last touched: 2026-05-31

---

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q1"></a>Q1 | Add tests for `bash-workspace-guard.py` SPEC/parsing | `tests` | 🔲 | S | Fixture-based unit tests covering each `SPEC` row, `prog_suppressed_by`, `--opt=val` inline values, redirect capture, `realpath` traversal. The SPEC table is load-bearing and currently untested. |
| <a id="Q2"></a>Q2 | Allowlist common safe non-workspace paths | `parsing` | 🔲 | S | `/dev/null`, `/dev/stdin`, `/dev/stdout`, `/dev/fd/N`, `/dev/zero` currently trigger `ask`. Add a small allowlist of well-known device/FD paths that bypass the outside-workspace check. |
| <a id="Q3"></a>Q3 | Reconcile `rg` alias to `grep` | `parsing` `bug` | 🔲 | S | `ripgrep` has a different flag set (`-g`, `-t`, `--type`, `--type-add`, etc.). Treating it as `grep` means unknown flags are zero-arg, so `rg -g '*.py' PAT path` mis-parses `'*.py'` as a positional. Either add a dedicated `rg` SPEC or drop the alias. |
| <a id="Q4"></a>Q4 | Heredoc / here-string false positives | `parsing` | 🔲 | S | `<<` / `<<<` capture the next shlex token as a "file" candidate. Lexical resolution usually lands inside the workspace, but `<<<"/etc/foo"` would falsely flag. Skip the token after `<<`/`<<<` from path checks. |
| <a id="Q5"></a>Q5 | Tilde and `$VAR` path expansions silently allowed | `security` `parsing` | 🔲 | S | Verified: `cat ~/.ssh/id_rsa` and `cat $HOME/.aws/credentials` resolve lexically inside `cwd` and return `allow`; bash expands at runtime to outside-workspace files. Treat any path token beginning with `~` or containing an unquoted `$` as outside-workspace. |
| <a id="Q6"></a>Q6 | Inline env-var assignments bypass guard | `security` `parsing` | 🔲 | S | Verified: `LC_ALL=C cat /etc/passwd` tokenizes with the assignment as `tokens[0]`, so `SPEC.get(name)` returns `None` and the hook defers entirely. Skip leading `NAME=VALUE` tokens (POSIX command-prefix assignments) before the `SPEC` lookup. |
| <a id="Q7"></a>Q7 | `cd`/`pushd` earlier in chain shifts runtime cwd | `security` `parsing` | 🔲 | S | Verified: `cd /etc && cat passwd` returns `allow`; paths resolve against the hook-input `cwd` but bash `cd`s before `cat` runs. Detect a `cd`/`pushd` token in an earlier group of the same chain and either re-root subsequent guarded groups or downgrade their decision to `ask`. |
| <a id="Q8"></a>Q8 | Symlink TOCTOU within a chained command | `security` | 🔲 | M | Verified: `ln -s /etc/passwd link && cat link` returns `allow` because `link` doesn't exist at hook time, so `realpath` returns the lexical in-workspace path. Add `ln`-staging awareness (track new symlink names whose targets are outside-workspace and propagate to later groups) or treat any guarded group that follows an `ln -s` in the same chain as `ask`. |
| <a id="Q9"></a>Q9 | Extend `SPEC` with common read-side cat-family commands | `parsing` | 🔲 | S | Add `less`, `more`, `wc`, `sort`, `uniq`, `tac`, `rev`, `nl`, `zcat`/`gzcat`/`bzcat`/`xzcat`, `xxd`, `od`, `hexdump`, `strings`, `file`, `diff`, `cmp` — all use the same positional-file shape as `cat`/`head`/`tail`. `sort` needs `-o FILE` in `file_flags`. Update README decision table. |
| <a id="Q10"></a>Q10 | Add `yq` as a sibling SPEC to `jq` | `parsing` | 🔲 | S | `yq` (kislyuk and mikefarah variants) mirrors `jq`'s shape: program positional, `-f`/`--from-file` for script files. Add a dedicated row rather than aliasing — flag sets diverge enough that alias risks Q3-style mis-parsing. |
| <a id="Q11"></a>Q11 | Investigate guarding write/mutation commands (`cp`, `mv`, `rm`, `ln`, `tee`, `dd`) | `parsing` `security` | 🔲 | M | Higher blast radius than current read-side set, but different threat model and `SPEC` shape (`dd` uses `if=`/`of=`, `ln`/`cp`/`mv` have source-vs-dest semantics, `rm -rf` is irreversible). Needs a plan doc under `docs/plan/` covering tokenization, decision policy (`ask` vs `deny`), and README framing before implementation. |
