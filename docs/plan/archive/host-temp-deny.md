# Plan: host-temp `deny` → repo-local scratch

## Goal

When a guarded Bash file command targets a host-wide temp dir (`/tmp`,
`/var/tmp`, or a `$TMPDIR` that resolves outside the worktree), **deny** it
(configurable to `ask`) with a constructive message steering the user to a
repo-local gitignored scratch dir (`./tmp/`). This upgrades the prior `/tmp`
*prompt* to a configurable *deny*.

## Approach

Reuse the existing path extractor and resolver — do **not** re-implement path
matching or substring-match `/tmp`. After `check_file` has resolved a token to
an outside-workspace `realpath`, add one classification step: if the resolved
path is at/under a host-temp root (and not under the Claude-managed temp root),
emit a new offender category `'hosttemp'` instead of `'outside'`. The decision
logic then denies host-temp offenders (default) regardless of permission mode.

Because classification happens on the *resolved file-path arguments the hook
already extracts*, this automatically avoids text/pattern false positives
(commit messages, grep patterns, echo strings aren't path args) and handles the
macOS `/tmp → /private/tmp` symlink and `$TMPDIR → /var/folders/...` (resolve
first, classify second).

## Key decisions

- **Default ON, `deny`.** Secure-by-default: `deny` is stricter than the prior
  `ask`. Opt down to `ask` via env.
- **Scope = the same commands the hook already guards.** Currently-unguarded
  shapes (`cd /tmp` alone, `mktemp -p /tmp`, `go test > /tmp/log`,
  `TMPDIR=/tmp cmd`) still **defer** — they were never extracted as file args.
  Recorded as a deferred Queue follow-up (Q26).
- **Claude-managed temp root excluded.** `/tmp/claude-<uid>/…` keeps its
  existing behavior: current-session scratch is allowed; another session's is
  `ask` (cross-session leak decision for a human), NOT host-temp `deny` (whose
  "use ./tmp/" message would be wrong there).
- **No file-content reads.** Concrete scratch-dir naming uses `os.path.isdir`
  (a stat, same class as the `realpath` the hook already does). PRIVACY's
  "does not open or read the contents of any file" stays true.

## Config (env, secure-by-default)

| Env var | Default | Meaning |
|---|---|---|
| `WORKSPACE_GUARD_TMP_ACTION` | `deny` | `deny` or `ask` for host-temp paths |
| `WORKSPACE_GUARD_TMP_ROOTS` | (empty) | extra host-temp roots, `:`/`,` separated, additive |
| `WORKSPACE_GUARD_TMP_ALLOW` | (empty) | allowlist of exact-prefix/glob paths that escape the deny (documented trade-off) |
| `WORKSPACE_GUARD_SCRATCH_DIR` | `tmp/` | scratch dir name named in the message |

## Match / no-match (tests)

DENY: `cat /tmp/out`, `rm "/tmp/x"`, `cat /var/tmp/x`, `sort -o /tmp/o in`,
redirect `> /tmp/log` (with a guarded command), `cd /tmp && cat in > evil`,
a `$TMPDIR`-rooted path.
NOT-DENY (allow or ask, untouched): repo-local `./tmp/...`, relative `tmp/...`,
`foo/tmp/bar`, `~/tmp` (ask, outside), `/tmpfs`/`/tmpfoo` (ask, different path),
URL `https://host/tmp/x`, `/tmp` as non-path text (commit msg / grep pattern).

## Steps

1. Script: host-temp helpers + `check_file` classification + decision + reason. ✅
2. Tests: new `HostTempDenyTests`; swap incidental `/tmp` in feature tests to a
   non-host-temp outside path so they keep testing their concern. ✅
3. Docs: README decision table, What-it-does, Configuration, Limitations,
   agent-guidance; PRIVACY env-var note. ✅
4. STATUS: deferred Q26 follow-up (isolated commit). ✅
5. Run full suite; commit.
</content>
</invoke>
