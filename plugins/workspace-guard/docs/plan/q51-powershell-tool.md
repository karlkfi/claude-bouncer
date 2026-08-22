# Q51 — guard the PowerShell tool on Windows

**Status: done.** Filed by Q44's validation pass; see
[`q44-windows-validation.md`](archive/q44-windows-validation.md) finding 2.

All acceptance criteria below are met. What shipped, beyond them:

- **`Set-Location` / `Push-Location` tracking.** Not in the criteria, but
  without it `Set-Location C:\out; Get-Content secrets.txt` resolves the
  relative operand against the session cwd and allows it silently. Anything the
  hook can't follow drops tracking and reports relative operands as
  'untracked', matching the bash `cd` handling.
- **Two-pass parameter binding.** PowerShell binds by name first and fills the
  remaining positional slots afterwards. A single left-to-right pass gave
  `Select-String -Pattern foo <file>` slot 0 (-Pattern) and never checked the
  file. Positional slots are therefore parameter *names*, not roles, so a
  name-bound parameter closes its slot.
- **`consume` enumerated in full per row.** An undeclared value-taking
  parameter shifts every later operand: `Set-Content -Encoding UTF8 C:\out\x`
  would bind `UTF8` as the target and the real path as `-Value`.

Known gaps, deliberately left (README's Limitations says so):

- Anything off the cmdlet table — a .NET call such as
  `[IO.File]::ReadAllText(…)`, a native `.exe` — is unchecked and silent.
- No symlink staging (`New-Item -ItemType SymbolicLink`); step 7 of the bash
  pipeline has no PowerShell equivalent.
- `$_` inside a `ForEach-Object` block reports as 'expand' and prompts, the
  same as bash's `cat $f`. Consistent, but `$_` is far more load-bearing in
  PowerShell idiom, so this may prove noisy in practice.

## Goal

Stop native-Windows sessions from running shell commands this plugin never sees.

## The gap

Claude Code has two shell tools. Which one a Windows session gets is decided by
whether Git for Windows happens to be installed:

| configuration | shell tool | guarded today |
|---|---|---|
| Windows, no Git for Windows | `PowerShell` | **no** |
| Windows, Git for Windows | `Bash` (Git Bash) | yes |
| Windows, either, `CLAUDE_CODE_USE_POWERSHELL_TOOL=1` | `PowerShell` | **no** |
| macOS / Linux / WSL | `Bash` | yes |

`hooks/hooks.json` matches `Bash`, the edit tools, and the read tools. `main()`
dispatches on `tool_name` and has no `PowerShell` branch. So in the first and
third rows the guard is installed, reports itself as active, and checks no shell
command at all. The native file tools stay guarded throughout, which is what
makes this easy to miss: `Read` and `Write` still prompt, so the plugin looks
like it is working.

Anthropic's documentation calls Git for Windows "optional" and says the
PowerShell tool is "rolling out progressively" where Git Bash *is* installed —
so this is not an exotic configuration, and a session can move into it without
the user changing anything.

## Why this isn't a one-line matcher addition

Adding `PowerShell` to the `hooks.json` matcher would route the command into
`handle_bash`, which tokenizes with `shlex` in POSIX mode. PowerShell is not a
POSIX shell, and the differences all fall in the unsafe direction:

- The escape character is a backtick, not a backslash; `shlex` reads
  `C:\Users\x` as escapes and yields `C:Usersx`, which resolves *inside* the
  workspace. That is a silent allow, and it is the common case, not an edge.
- Reads and writes are cmdlets (`Get-Content`, `Set-Content`, `Out-File`,
  `Add-Content`, `Copy-Item`, `Remove-Item`) with their own parameter grammar
  (`-Path`, `-LiteralPath`, `-Destination`), plus aliases that collide with the
  `SPEC` table's names (`cat`, `type`, `gc` for `Get-Content`; `sc` for
  `Set-Content`).
- Argument-mode vs expression-mode parsing, `@()`/`$()` subexpressions, and
  `&`/`.` invocation have no `SPEC` analogue.

So this needs its own `SPEC`-equivalent table, not a reuse of the Bash one — and
the repo's rule against aliasing a tool onto a row whose flag set diverges (see
Q3 on `rg`) applies with force here.

## Posture — settled

**Parse a known subset; defer on everything else.** This keeps the guard's
standing rule (defer on uncertainty, so normal permissions apply) rather than
carving out a stricter one for a shell the parser is weakest in.

The rejected alternative was `ask` on anything unparsed. It is the stricter
reading, and for a plugin whose job is friction at the boundary it was a real
candidate — but early on the table covers little, so nearly every command would
prompt, and a guard that noisy gets disabled or blanket-allowed. Zero coverage
with the plugin switched off is worse than partial coverage with it on.

The consequence to be honest about in `README.md`: the unparsed tail stays
unguarded, and that gap is invisible to the user.

## Wiring facts

Both were unknown when this was filed. Neither should be re-derived.

- **The matcher name is `PowerShell`.** The tools reference states its table
  holds "the exact strings you use in permission rules, subagent tool lists, and
  hook matchers", and lists `PowerShell` there.
- **The command arrives as `tool_input.command`,** the same field Bash uses,
  alongside `timeout`, `description`, and `run_in_background`. The hooks
  reference does not document the PowerShell tool's input schema at all; this
  comes from strings in the installed binary (2.1.220), where
  `Cannot destructure property 'command' from null or undefined value` sits with
  `PowerShellTool: exec spawn failed:`. Filed upstream as
  [anthropics/claude-code#83647](https://github.com/anthropics/claude-code/issues/83647);
  if that lands, cite the docs instead of the binary.

That second one is source inspection, not an end-to-end run, and the repo treats
those differently for good reason. So the handler must **not** treat a missing
command field as ordinary uncertainty: silently deferring there is
indistinguishable from the bug it would be hiding, and reproduces exactly the
failure `run-python-hook.cmd` exists to prevent — a guard that reports itself
active and enforces nothing. A `PowerShell` call whose command can't be read is
a wiring failure and should `ask`, with a reason that says so.

## Acceptance criteria

- A `PowerShell` matcher in `hooks/hooks.json` and a `PowerShell` branch in
  `main()` that never routes a PowerShell command through the POSIX tokenizer.
- A `PowerShell` call with no readable command field emits `ask`, not silence.
- `Get-Content`/`Set-Content`/`Out-File` and their aliases reach the same
  outside-workspace verdict as `cat`/`tee` do under Bash, with native paths
  (`C:\Users\…`) surviving tokenization intact.
- Fixtures for the backslash-escape case above, asserting no silent allow.
- README's Limitations entry on Windows shell coverage updated or removed.
