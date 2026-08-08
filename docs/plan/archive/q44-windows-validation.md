# Q44 — validate the guard against a real Windows install

**Status: done.** Windows CI now runs the suite in both environments the guard
has to be correct in, and the MSYS conversion the guard silently depends on is
asserted rather than assumed. The two defects validation turned up are filed as
Q51 (unguarded PowerShell tool) and Q52 (MSYS path forms).

## Goal

Establish what the guard actually does on native Windows, in the environment it
actually runs in, and close the gaps that turns up.

## What was unverified

The Q44 row named two assumptions. Both are now settled, and the second one
turned out to hide a third gap that matters more than either.

### 1. The `Bash` tool on native Windows is Git Bash — confirmed

From the Claude Code setup documentation: installing Git for Windows "enables
the Bash tool by providing Git Bash", and `CLAUDE_CODE_GIT_BASH_PATH` points at
`bash.exe` when it can't be found. So the POSIX tokenizer this guard is built
around is parsing for the right shell — as long as that shell is the one
running.

### 2. There is a second shell tool, and it is unguarded — new

Git for Windows is *optional*. Without it there is no `Bash` tool at all:
Claude Code runs shell commands through the **`PowerShell` tool**. With Git Bash
installed, the same tool is "rolling out progressively" alongside `Bash`, and
`CLAUDE_CODE_USE_POWERSHELL_TOOL=1`/`0` opts in or out.

`hooks/hooks.json` matches `Bash`, the edit tools, and the read tools. There is
no `PowerShell` matcher, and `main()` has no `PowerShell` branch. On a native
Windows install without Git for Windows — a documented, supported configuration
— this plugin does not see shell commands at all and enforces nothing.

That is a secure-by-default hole rather than a parsing bug, so it is tracked
separately and does not ship in this change.

### 3. MSYS path forms are misread — confirmed, and bounded

Confirmed, with the important qualifier that it cannot produce a silent allow.
See finding 4.

## Approach

There is no Windows box in this session, so ground truth comes from the
`windows-latest` CI runner, the same method Q39 used. The important correction
is *which shell CI uses*.

`unittest-windows` runs `python scripts/skip-ceiling.py` with no `shell:` key,
so it runs under the runner's default `pwsh`. The guard's Windows behaviour has
therefore only ever been observed in a PowerShell environment — not the Git Bash
one the `Bash` tool actually provides. `claude-branch-guard` already runs its
suite with `shell: bash` for exactly this reason, and its harness carries
explicit MSYS handling (`MINGW*|MSYS*|CYGWIN*` -> native path conversion).

This matters beyond tidiness. Q39 finding 6 concluded "`$HOME` is unset on
Windows", and Q43 built the `resolved_home()`/`expand_tilde()` design on top of
it. That is a `pwsh` fact. MSYS *sets* `HOME` — and typically to `/c/Users/…`,
the leading-slash form `ntpath` does not consider absolute.

So: land a temporary probe job under `shell: bash`, read real values out of it,
and write assertions from what it measured rather than from what seems likely.

## Findings

All values below are quoted from the probe job on `windows-latest`
(`MSYSTEM=MINGW64`, Git Bash 5.3.15, Python 3.13, checkout on `D:`).

### 1. `shell: bash` on the runner is genuinely Git Bash

```
shell: C:\Program Files\Git\bin\bash.EXE --noprofile --norc -e -o pipefail {0}
GNU bash, version 5.3.15(1)-release (x86_64-pc-cygwin)
```

So the runner is a usable stand-in for the shell Claude Code's Bash tool
provides. It is not a stand-in for a Claude Code *session* — see Out of scope.

### 2. MSYS converts path-shaped variables — the hypothesis this refutes

The concern going in was that Git Bash sets `HOME` to an MSYS path, which
`ntpath` does not consider absolute, so `resolved_home()` would return None and
Q43's tilde expansion would quietly stop working. That does not happen. MSYS
rewrites path-shaped variables when it execs a native binary:

| variable | in the shell | as native Python sees it |
|---|---|---|
| `HOME` | `/c/Users/runneradmin` | `C:\Users\runneradmin` |
| `TMP` / `TEMP` | `/tmp` | `C:\Users\RUNNER~1\AppData\Local\Temp` |

Every environment-derived path therefore lands correctly:

```
resolved_home()       = C:\Users\runneradmin
claude_tmp_root()     = C:\Users\runneradmin\AppData\Local\Temp\claude
claude_projects_dir() = C:\Users\runneradmin\.claude\projects
host_temp_roots(cwd)  = {'D:\tmp', 'C:\Users\runneradmin\AppData\Local\Temp',
                         'D:\var\tmp'}
expand_tilde('~/x')   = 'C:\Users\runneradmin\x'
```

This is load-bearing and invisible, so `MsysEnvironmentTests` now asserts it.
If MSYS ever stopped converting, `resolved_home()` would return None and the
real temp directory would drop out of `host_temp_roots()` — a `deny` tier
downgraded to `ask` — with nothing else failing.

### 3. The two Windows CI jobs model two different processes

`unittest-windows` runs under the runner's default `pwsh`. That is the correct
model for the *hook* process: `run-python-hook.cmd` is launched by cmd.exe, and
`$HOME` is unset there, which is what Q39 finding 6 and Q43 were reasoning
about. It is the wrong model for the *shell* whose commands the hook parses.

Since the environment-reading helpers have to be correct in both, this change
adds `unittest-windows-gitbash` rather than switching the existing job over.
The suite passes in both: `Ran 860 tests … OK (skipped=3)` under Git Bash,
the same skip set as `pwsh`.

### 4. MSYS path forms in command text — real divergence, no silent allow

Nothing converts paths written *inside* a command; the guard resolves them with
`ntpath` against the tool's cwd, and Git Bash resolves them through its own
mount table. They disagree:

| written in the command | Git Bash reads | the guard resolves |
|---|---|---|
| `/tmp/x` | `C:\Users\RUNNER~1\AppData\Local\Temp\x` | `D:\tmp\x` |
| `/c/Users/foo` | `C:\Users\foo` | `D:\c\Users\foo` |
| `/etc/passwd` | `C:\Program Files\Git\etc\passwd` | `D:\etc\passwd` |

The boundary still holds, and the reason is structural rather than lucky: a
leading-slash path resolves under `ntpath` to `<drive>\…`, which is inside the
project only if the project root *is* the drive root. So a path the guard reads
as in-workspace cannot be one Git Bash sends outside, and the failure direction
is over-prompting, never a silent allow.

Two real defects remain, both filed rather than fixed here (Q52):

- The confirmation prompt names a path the command will never touch. Asking the
  user to approve a read of `D:\etc\passwd` when the command reads
  `C:\Program Files\Git\etc\passwd` degrades the one moment the guard exists
  for.
- MSYS-form configuration entries silently match nothing.
  `WORKSPACE_GUARD_READ_ALLOW_PREFIXES=/c/Users/me/shared` resolves to
  `D:\c\Users\me\shared` — the same class of dead knob Q39 finding 1 fixed for
  `/tmp`.

`/tmp` is the exception that stays correct, by coincidence worth writing down:
the file argument and the built-in host-temp root are both `/tmp`, so both
resolve to the same nonexistent `<drive>\tmp` and the comparison matches. The
`deny` fires on a path that isn't the one being written, but it fires.

## Out of scope

- The `PowerShell` tool gap (item 2 above) — separate row and PR.
- Fixing the MSYS path-form divergence (finding 4) — Q52.
- Anything requiring a Windows desktop with Claude Code installed: the runner
  gives a real Git Bash and a real `ntpath`, but not a real Claude Code session,
  so hook wiring on Windows stays verified by the config tests only. What is
  still unverified after this change is narrow: that Claude Code passes `cwd`
  and `CLAUDE_PROJECT_DIR` in native form, and that `${CLAUDE_PLUGIN_ROOT}` in
  `hooks.json` expands to a path cmd.exe accepts.
