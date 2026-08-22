# Plan: multi-tool support — guard native Read/Grep/Glob and widen Edit/Write

**Goal:** close the coverage gap where the guard fires on Bash file commands
(`cat`/`grep`/`sed`/…) but is silent when Claude reads or writes the *same*
outside-workspace path through its native tools (`Read`, `Grep`, `Glob`,
`Edit`, `Write`, `MultiEdit`, `NotebookEdit`).

**Approach:** the parsing problem that dominates the Bash path does not exist
for native tools — they hand the hook a structured `tool_input` with explicit
path fields. So this is mostly a *refactor*: lift the decision core out of
`handle_bash`'s closures into module-level functions taking an explicit context,
then add two thin handlers that resolve one path and reuse that core. No second
tokenizer, no new decision semantics.

## Why this shape

- **One decision core, no divergence.** The outside/host-temp/sibling
  classification and the deny-vs-ask decision live once and are shared by Bash
  and the native handlers. A native `Read` of `/etc/passwd` gets *exactly* the
  same verdict as `cat /etc/passwd`; they can never drift.
- **Secure-by-default, additive only.** Every slice only *adds* friction on
  outside-workspace access — it never loosens an existing allow. Extending
  coverage strengthens the boundary, so it is aligned with the plugin's
  security principle (adding friction needs no sign-off; removing it does).
- **The self-read exemptions carry over unchanged.** The read-prefix
  (`~/.claude/projects/`) and session-tmp / sibling-session-scratch allows —
  which killed ~37% of Bash prompts — apply to native reads through the same
  `classify_outside`, so widening to `Read` does not reintroduce that spam.

## The gap being closed

| Tool | `tool_input` field | Read/Write | Before | After |
|---|---|---|---|---|
| `Read` | `file_path` | read | unguarded | outside → `ask` (self-read exempt) |
| `Grep` | `path` | read | unguarded | outside → `ask` (self-read exempt) |
| `Glob` | `path` | read | unguarded | outside → `ask` (self-read exempt) |
| `Edit`/`Write`/`MultiEdit` | `file_path` | write | sibling-deny only | full outside/host-temp/sibling check |
| `NotebookEdit` | `notebook_path` | write | sibling-deny only | full outside/host-temp/sibling check |

`Read` is the highest-value slice: it is a straight bypass of the plugin's
headline promise, and the deny messages literally tell the agent to "read the
file with the Read/Grep/Glob tools instead of bash" — so today's guidance routes
around the guard.

## Refactor (the load-bearing part)

Extract from `handle_bash`'s closures into module-level functions, preserving
Bash behavior exactly:

- `path_is_outside(rp, proj)` — was the `is_outside` closure.
- `build_context(data)` — one `Ctx` bundling `proj`, `cwd`, `session_id`,
  `session_tmp_root`, `session_proj_dir`, `tmp_roots`, `tmp_allow`, `tmp_action`,
  `read_prefixes`, `session_wt`, `sib_override`. Built once per invocation by
  every handler.
- `classify_outside(rp, ctx, is_read)` — given a **resolved** realpath, return
  `(category, detail)` or `None`. This is the body of `check_file` from the
  session-tmp allow onward (session-tmp → read-prefix → sibling-session →
  sibling-checkout → host-temp → outside). Bash-only concerns (`ln` staging,
  the `expand`/`untracked` categories) stay in `handle_bash`.
- `decide(offenders, ctx, bypass)` — the final deny-vs-ask block from
  `handle_bash` (host-temp deny, sibling deny, `bypassPermissions` deny), plus
  `build_reason`.

`handle_bash` keeps its tokenizer, group loop, `resolve_token`, `stage_ln`, and
a thin `check_file` that does `ln`-staging + `expand`/`untracked`, then delegates
to `classify_outside`; the final block calls `decide`. No behavior change — the
existing e2e suite is the regression gate.

## New handlers

- `resolve_native_path(raw, cwd)` — `expand_tilde`, then defer (return `None`)
  on a leftover `~`/`$` (native tools do not shell-expand; matches the existing
  `handle_edit` posture), else `realpath` against `cwd`.
- `handle_read_tool(data)` — for `Read`/`Grep`/`Glob`: pull
  `file_path`/`path`, resolve, `classify_outside(..., is_read=True)`; `None` →
  defer, else `decide` + `emit`.
- `handle_edit(data)` — widen from the sibling-only rule to the full check:
  resolve `file_path`/`notebook_path`, `classify_outside(..., is_read=False)`,
  `None` → defer, else `decide` + `emit`. The sibling-deny path is now just one
  category `classify_outside` returns.

## Dispatch + wiring

- `main()`: `Read`/`Grep`/`Glob` → `handle_read_tool`; the four edit tools →
  `handle_edit`; everything else (incl. absent `tool_name`) → `handle_bash`.
- `hooks/hooks.json`: add a `PreToolUse` matcher `Read|Grep|Glob` pointing at
  the same script.

## Scope / deliberate limitations

- **MCP tools (`mcp__*`) are out of scope.** They have arbitrary schemas with no
  universal path field; a generic parser cannot find their paths. Per-server
  coverage would be a separate, opt-in effort.
- **`WebFetch`/`WebSearch` are out of scope.** They cross a *network-egress*
  boundary, not the filesystem boundary this plugin guards.
- **Widened Edit/Write is a behavior change**, not just new coverage: a
  non-sibling outside `Write` that previously deferred now `ask`s (or `deny`s for
  host-temp). This is the intended strengthening; the self-read exemptions keep
  it from prompting on the agent's own scratch.

## Deliverables

- [ ] `scripts/bash-workspace-guard.py` — refactor (`path_is_outside`,
      `build_context`/`Ctx`, `classify_outside`, `decide`), `resolve_native_path`,
      `handle_read_tool`, widened `handle_edit`, `main()` dispatch.
- [ ] `hooks/hooks.json` — `Read|Grep|Glob` matcher.
- [ ] Tests — e2e for native reads (outside `ask`, inside defer, self-read /
      read-prefix / session-tmp exempt) and native writes (outside `ask`,
      host-temp `deny`, sibling `deny`); plus a regression check that the Bash
      suite is unchanged.
- [ ] `README.md` — "What it does" guarded-tools paragraph, decision-table rows,
      the "one place beyond Bash" wording, How-it-works note.
- [ ] `.claude-plugin/plugin.json` — keywords (`read`, `tools`).
- [ ] `docs/STATUS.md` — Queue item (isolated commit).
