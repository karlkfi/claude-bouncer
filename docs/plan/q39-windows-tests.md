# Q39 — make the remaining Windows test failures pass

**Status: done.** Windows runs the suite with 0 failures and 0 errors. The
ratchet in `scripts/windows-ratchet.py` and its baseline are retired;
`unittest-windows` is an ordinary gating job. The remaining Windows skips are
`$HOME`-driven and wait on Q43 (see finding 6).

## Goal

Get `python -m unittest discover tests` to pass on Windows, and retire the
ratchet in favour of an ordinary gating job.

## Approach

There was no Windows box in this session, so the only ground truth was the
`unittest-windows` continuous integration (CI) job, which prints the full
`unittest` output. Work proceeded in rounds: read the failure list from CI, fix
a category, push, re-read. Every claim below is quoted from a CI run.

Per case, decide whether the **fixture** or the **parser** is wrong. A fixture
that hard-codes a POSIX path is fixture noise; a parser that resolves a
configured root on a different drive than the path it compares it against is a
real bug that misfires on a real Windows install.

## Findings

Starting point: `failures=56, errors=3` across 59 distinct tests.

### 1. Configured paths resolved against a different base — 30 tests (parser)

`host_temp_roots()` resolved `/tmp` with `os.path.realpath`, which uses the
**hook process's** cwd. File arguments resolve against the **tool's** cwd. On
POSIX both are absolute and the bases never matter. On Windows a leading slash
is drive-relative — `ntpath.isabs('/tmp')` is `False` since Python 3.13 — so the
root landed on the hook process's drive (`D:\tmp`, the checkout) while the file
argument landed on the tool's (`C:\tmp`, the fixture workspace). The comparison
silently never matched:

```
AssertionError: 'ask' != 'deny' : expected 'deny' for 'cat /tmp/q-hosttemp-out';
got 'ask' (reason: "Outside-workspace path(s): /tmp/q-hosttemp-out ->
C:\tmp\q-hosttemp-out. ...")
```

A host-temp `deny` degraded to a plain `ask`, and `WORKSPACE_GUARD_TMP_ALLOW`
stopped matching anything at all — a knob that silently does nothing.

Fix: `resolve_from(base, raw)` joins a non-absolute path to the base before
`realpath`, applied to the host-temp roots, the allowlist, and the read-allow
prefixes. POSIX behaviour is unchanged (the join is a no-op on an absolute
path).

### 2. Native paths interpolated unquoted into commands — 22 tests (fixture)

`f"cat {abs_in}"` with `abs_in = C:\...\in.txt` hands the hook a command whose
backslashes are shell escapes. The POSIX tokenizer eats them — exactly as bash
does — so the path arrived as `C:...in.txt` and resolved wherever that mangled
name landed, usually inside the workspace. Tests expecting `ask` got `allow`;
tests expecting `allow` passed for the wrong reason.

Fix: an `sh()` helper (`shlex.quote`) at every site that interpolates a native
path, which is how a real command names such a path. A no-op on POSIX paths.

### 3. POSIX-shaped literals in helper unit tests — 4 tests (fixture)

`path_at_or_under("/tmp/x", "/tmp")` compares on `os.sep`; its callers always
pass realpaths, which carry the platform separator. `_split_pathlist` splits on
`os.pathsep`, which is `;` on Windows — `:` is part of a drive letter, so the
parser is right and the fixture was POSIX-specific.

### 4. POSIX-shaped assertion strings — 3 tests (fixture)

Two reason assertions hard-coded `/q85-fake-outside/notes.txt` where the hook
correctly reports `C:\q85-fake-outside\notes.txt`. `offender_display`'s
absolute-token branch was fed a leading-slash path, which `ntpath` does not
consider absolute — so it took the relative branch, correctly.

### 5. Windows temp dir was not a host-temp root — 1 test (parser)

The last failure standing. `HOST_TEMP_DEFAULT_ROOTS` lists only the POSIX names,
and Windows has no `$TMPDIR`. Its host-wide temp dir is `%TMP%` — the one
directory this tier exists to catch — and it was not a root at all, so a scratch
write there got a plain `ask`. Found by
`test_glob_item_escaping_body_path_flagged`, whose fixture climbs out of a
`tempfile` workspace into its parent: host temp on every platform, recognised as
such off Windows only.

Fix: append `tempfile.gettempdir()` on Windows. This makes Windows agree with
Linux, where `gettempdir()` is `/tmp` and already a root — so the behaviour is
the one the green Linux matrix already verifies.

### 6. `$HOME` is unset on Windows — 4 tests (deferred to Q43)

All 3 errors were `KeyError: 'HOME'`, plus one failure asserting tilde
expansion. These four now skip on Windows, matching the `skipTest("HOME not
set")` sites the suite already had.

Q40 landed on `main` while this branch was in flight and fixed
`claude_projects_dir()` to resolve the home directory with `expanduser`, which
retired a large block of those skips. It did **not** touch `expand_tilde()`,
which still reads `$HOME` only — so `cat ~/x` keeps its `~` on Windows and the
hook defers where bash would expand. That is Q43, and it is what the remaining
`$HOME`-driven skips (including these four) are waiting on. Skips are pointed at
Q43 rather than Q40 for that reason.

## Out of scope

Windows correctness beyond the test suite was not attempted — see the Deferred
Queue row on validating against a real Windows install. In particular the guard
does not understand MSYS/Git Bash path forms (`/c/Users/…`), which `ntpath`
resolves to `<drive>\c\Users\…`. That direction only ever over-prompts, never
silently allows.
