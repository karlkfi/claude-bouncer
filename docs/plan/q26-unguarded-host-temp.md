# Plan: Q26 — extend host-temp `deny` to currently-unguarded shapes

## Goal

Close the three host-temp leak shapes the original host-temp `deny` left out of
scope ([host-temp-deny.md](host-temp-deny.md), README Limitations): a redirect
from an **unguarded** command (`echo secret > /tmp/out`), the same after a `cd`
into host-temp (`cd /tmp && echo x > out.txt`), and the temp-creating tool
`mktemp` (whose *default* target is host-temp). Confirmed empirically: all three
currently emit nothing and defer to normal permissions.

## Scope decisions (confirmed with user)

- **Redirect scope = all redirect targets, always.** A redirect is a shell-level
  write the hook already resolves; it should be checked regardless of the command
  word. So `echo x > /tmp/x` → **deny** (host-temp) and `echo x > /outside` →
  **ask** (outside). This closes host-temp *and* plain-outside redirect leaks and
  is the cleanest mental model, at the cost of new prompts for benign outside
  redirects from unguarded commands (the secure direction).
- **mktemp handled in this PR** (not deferred) via a dedicated classifier.

## Approach

Two independent changes, both reusing the existing resolver/classifier
(`check_file` → `classify_outside`) so host-temp/outside/sibling tiers, the
exemptions, and the reason strings all come for free.

### A. Ungate redirect targets

Redirect targets are already collected per-group and run through `check_file`
into `outside`; the *only* thing discarding them for unguarded commands is the
`if not guarded: return` gate in `handle_bash`. Change it to
`if not outside and not guarded: return` — emit a block whenever there's a real
offender, but still only emit the explicit `allow` when a guarded command is
present (so an in-workspace redirect from an unguarded command keeps deferring).
Via the existing per-group cwd tracking, this also fixes `cd /tmp && echo …`.

### B. `mktemp` classifier

`mktemp` can't be a plain `SPEC` row: its *default* location is host-temp, and
`-t`'s arity diverges GNU (no arg) vs BSD (`-t prefix`). Add `classify_mktemp`
(sibling of `classify_dd`/`classify_ln`) returning the path(s) it will create,
then run them through `check_file` (write context). The default-location cases
return `default_temp_dir()` (`$TMPDIR` or `/tmp`) — a concrete path so the normal
host-temp check fires *and* the rare in-workspace `$TMPDIR` is correctly allowed.

Flag handling (explicit, never inferred):
- `-p DIR` / `-pDIR` / `--tmpdir=DIR` → DIR is the target directory.
- bare `--tmpdir` (GNU optional-arg) / `-t` → default host-temp location.
- `-V`/`--version`/`--help` → creates nothing → return None (defer).
- `-d`/`-u`/`-q`/`--suffix=` / unknowns → no path arg. `-u` (dry-run) still
  classifies its path (intent is host-temp) — secure-by-default.
- slashed template (`mktemp /tmp/x.XXXX`, `./x.XXXX`) names its own location;
  bare-name template (or none) uses the default location.

Exotic getopt forms (combined short flags `-dp DIR`, inline `TMPDIR=…` prefix)
degrade toward the host-temp default (`deny`), never a silent allow — documented.

## Match / no-match (tests)

DENY (host-temp): `echo x > /tmp/out`, `printf … > /tmp/x`, `ls > /tmp/x`,
`cd /tmp && echo x > out.txt`, `mktemp`, `mktemp -d`, `mktemp -t p`,
`mktemp -p /tmp x.XXX`, `mktemp /tmp/x.XXX`.
ASK (outside): `ls > /etc/out.txt`, `echo x > ~/notes.txt`.
ALLOW / defer (unchanged): `echo x > ./out.txt` (defer), `ls /etc` (defer),
`cd /etc` (defer), `mktemp -p ./scratch x.XXX` (allow), `mktemp ./x.XXX` (allow),
`mktemp --version` (defer).

## Steps

1. Script: gate change; `default_temp_dir` + `classify_mktemp` + wiring. 
2. Tests: repurpose `test_only_redirect_no_guarded_command_defers`; add
   unguarded-redirect E2E; `ClassifyMktempTests` unit + mktemp E2E deny/allow.
3. Docs: README decision table, host-temp paragraph, How-it-works steps 2/5,
   Limitations (redirect + standalone-cd resolved, mktemp added, residual
   limits), guarded-commands list; plugin.json keywords.
4. STATUS: delete the Q26 Deferred row (isolated commit).
5. Full suite; commit (code+tests, docs, STATUS each isolated as appropriate).

## Out of scope (residual, documented as Limitations)

- Inline `TMPDIR=/x cmd` redirecting a tool's internal temp location (the
  original plan's separate `TMPDIR=/tmp cmd` shape).
- Commands buried inside command substitution (`cd $(mktemp -d)` internals).
- Full getopt short-flag combining for `mktemp`.
