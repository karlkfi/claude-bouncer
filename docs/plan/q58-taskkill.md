# Plan: extend the unanchored-kill deny to `taskkill` (Q58)

**Goal:** deny `taskkill` when nothing ties it to this workspace, closing the
last door the unanchored-kill deny leaves open on Windows — `taskkill /IM
node.exe` is the same host-wide kill as `killall node` and `Stop-Process -Name
node`, and today it walks past both frontends unchecked.

**Approach:** one classifier, `classify_taskkill`, shared by the Bash and
PowerShell frontends, feeding the existing `'kill'` offender category. Each
frontend runs its own anchor check over the operands the classifier picks out,
because their resolution rules differ (`kill_operand_anchored` vs
`ps_kill_operand_anchored`) and must not diverge on the same path.

Queue row: Q58, filed by [Q57's plan](q57-powershell-stop-process.md), whose
"`taskkill` is not covered" limitation this closes.

## Why `taskkill` needs its own classifier

It is a native Windows executable reachable from both shells, and its flag
grammar is neither `pkill`'s nor a cmdlet's:

- **Windows flag syntax.** `/IM`, and also `-IM` — `taskkill` accepts both
  prefixes. Under Git Bash a single leading `/` is path-mangled by MSYS
  (`/IM` becomes `C:/Program Files/Git/IM`), so the idiom there is `//IM`; all
  three spellings are recognized. Names are case-insensitive.
- **No positional operands.** Every selector is flagged, so a bare word is a
  syntax error rather than a pattern — the opposite of `pkill`.
- **`.exe` and case in the command word.** `TASKKILL.EXE` and `taskkill` are one
  command to both shells, so the command word is normalized before matching.

## The verdict each selector earns

| Selection | Verdict | Why |
|---|---|---|
| `/PID 1234` (literal digits) | defer | The same rewrite `kill <pid>` and `Stop-Process -Id 1234` already get. It is what the deny message recommends, not a hazard. |
| `/IM node.exe` | deny unless anchored | This is `killall`. An image name carries no path, so in practice nothing anchors it — but the check is uniform rather than special-cased, and a name can never anchor by construction. |
| `/FI "<filter>"` | deny unless anchored | A filter selects by image name, window title, user, status — none of which is a path. A filter that *does* name this workspace anchors, on the same rule as a `pkill -f` pattern. |
| `/PID $p` (expanded) | deny unless anchored | Same reasoning as `Stop-Process -Id $p.Id`: the hook cannot see what the shell will put there. |
| no selector at all | deny | `taskkill` with nothing selecting is not a kill this hook can vouch for, matching `pkill -u karl`. |

`/?` returns None — a help invocation kills nothing, so it defers rather than
collecting a deny for a no-op. `mktemp --version` sets the precedent.

## Why the scope is the command, not the statement

Q57 widened the PowerShell kill scope to the whole statement because
`Stop-Process` takes pipeline input, so its anchor legitimately sits upstream.
`taskkill` reads no pipeline: its selection is entirely in its own arguments.
Judging it over a statement would accept an anchor that cannot be feeding it —
`Get-Content <root>\list.txt | taskkill /IM node.exe` would read as anchored
while killing every checkout's `node.exe`. So `taskkill` is judged on its own
tokens, on both frontends, which also keeps the two verdicts identical for the
same command.

## Reused, not rebuilt

- `workspace_anchor_re` / `ctx.kill_anchor` — the identical component-with-a-
  separator rule, so `taskkill`, `pkill` and `Stop-Process` cannot reach
  different verdicts about the same path.
- The `'kill'` offender category, `decide`'s `cross_hit` deny, and
  `WORKSPACE_GUARD_OVERRIDE`. One category, one override, one message shape.
- `kill_operand_anchored` (bash) and `ps_kill_operand_anchored` (PowerShell),
  each applied to the operands `classify_taskkill` reports. The classifier
  returns operand **indices**, so each frontend keeps its own token
  representation — PowerShell needs the tokenizer's `expandable` flag, which a
  list of plain strings would drop.
- `SIGNAL_CMDS`, which `taskkill` joins so a clean guarded command in the same
  bash string can't launder it into a blanket `allow`.
- Q59's PowerShell counterpart of that suppression. `taskkill` is classified
  inside `ps_statement_kills` — on its own segment's tokens, so the anchor scope
  above still holds — because that is the one place answering "did this statement
  signal a process". Landing it anywhere else would have re-opened, for
  `taskkill`, the hole Q59 had just closed for `Stop-Process`.

New: `native_cmd_name`, the Windows-shaped command-word normalizer (basename,
lowercased, `.exe` dropped), and a third fix sentence in `build_kill_hint` —
`tasklist` + `taskkill /PID`, since neither `pgrep -fl` nor `Get-Process` is the
rewrite for this command.

## Deliberate limitations

- **`/FI "PID eq 1234"` denies** even though the pid is literal. Parsing filter
  expressions means a grammar per filter keyword; the rewrite (`/PID 1234`) is
  the first thing the message names.
- **A `/P` password with no value swallows the next token.** `taskkill /P /IM
  node.exe` reads `/IM` as the password and `node.exe` as a stray operand —
  which still denies, so the misparse costs nothing.
- **A `taskkill` inside a quoted wrapper** (`sh -c 'taskkill /IM node.exe'`) is
  one token to either tokenizer and is missed, the same as every other kill.

## Deliverables

- [x] `scripts/bash-workspace-guard.py` — `TASKKILL_CMDS` / `TASKKILL_CONSUME` /
      `TASKKILL_SELECTORS` / `TASKKILL_FLAG_RE`, `native_cmd_name`,
      `classify_taskkill`, `ps_taskkill_offenders`, `taskkill` in `SIGNAL_CMDS`,
      call sites in `_analyze_command` and `ps_statement_kills`, third fix branch
      in `build_kill_hint`.
- [x] Tests — unit (flag prefixes, case, `.exe`, selector classification, `/?`,
      value-flag consumption) + e2e on both frontends (`/IM` deny, `/FI` deny,
      bare `taskkill` deny, `/PID` defer, anchored filter defer, override
      downgrade, laundering through a clean `cat` / `Get-Content`). The PowerShell
      kill harness became `PowerShellKillFixture`, shared by the `Stop-Process`
      and `taskkill` suites.
- [x] `README.md` — decision-table rows on both tables, the kill section, How it
      works step 13, Limitations, agent-guidance bullet, PowerShell coverage.
- [x] `.claude-plugin/plugin.json` — nothing; `windows`, `pkill` and `process`
      are all already in the keyword list.
- [x] `docs/design.md` — why this kill's scope is the command, not the statement.
- [x] `docs/STATUS.md` — drop Q58, widen Q59 to name `taskkill`.
