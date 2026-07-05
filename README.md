# workspace-guard

**Path-aware bash permissions for Claude Code.**

[![release](https://img.shields.io/github/v/release/karlkfi/claude-workspace-guard)](https://github.com/karlkfi/claude-workspace-guard/releases) [![tests](https://img.shields.io/github/actions/workflow/status/karlkfi/claude-workspace-guard/tests.yml?branch=main&label=tests)](https://github.com/karlkfi/claude-workspace-guard/actions/workflows/tests.yml) [![License: MIT](https://img.shields.io/github/license/karlkfi/claude-workspace-guard.svg)](LICENSE) [![Claude Code plugin](https://img.shields.io/badge/Claude_Code-plugin-7e57c2)](#install)

> Stop approving every in-repo grep. Start catching the one that reads `/etc/passwd`.

You ask Claude to "find that auth error." It runs `grep -r token /var/log`. Or
`cat ~/.aws/credentials` while "checking the environment." Or pipes a file from
outside your repo into `jq`. The default `Bash(grep:*)` permission rules can't
tell these apart from the dozens of in-repo greps Claude runs every session —
they either trust every invocation or prompt on every one.

workspace-guard is a `PreToolUse` hook for `Bash` that parses the command, finds
its file arguments, and asks for confirmation only when a path resolves outside
your project root (`$CLAUDE_PROJECT_DIR`). In-repo reads and pure pipelines run
silently.

![Claude Code's permission prompt when grep targets a file outside the project root](docs/img/ask-prompt.png)

## Contents

- [What it does](#what-it-does)
- [Install](#install)
- [Upgrade](#upgrade)
- [How it works](#how-it-works)
- [Agent guidance: avoiding prompts](#agent-guidance-avoiding-prompts)
- [Configuration](#configuration)
- [Limitations](#limitations)
- [Companion plugin: branch-guard](#companion-plugin-branch-guard)
- [Design](#design)
- [Privacy](#privacy)
- [Contributing](#contributing)
- [License](#license)

## What it does

The hook produces one of four outcomes:

- **allow** — the command runs without a prompt.
- **ask** — Claude Code shows its standard permission prompt for the command
  (as above). You approve or reject.
- **deny** — the command is blocked with a constructive reason. This is the
  default for **host-wide temp** paths (`/tmp`, `/var/tmp`, `$TMPDIR`): they're
  shared across every session and worktree and live outside the project root, so
  instead of prompting, the hook steers you to a repo-local gitignored scratch
  dir (`./tmp/`). It's also the default for **writes into a sibling checkout of
  the same repo** when the session runs in a git worktree (see below).
  Configurable down to `ask`; see [Configuration](#configuration).
- **defer** — the hook stays silent; your normal permission settings apply.

Guarded commands: `grep` (and `egrep`, `fgrep`), `rg`, `sed`, `awk` (and
`gawk`, `mawk`), `jq`, `yq`, `cat`, `head`, `tail`, `sort`, `wc`, `diff`,
`file`, `hexdump`, plus the cat-shape readers `less`, `more`, `tac`, `rev`,
`nl`, `uniq`, `xxd`, `od`, `strings`, `cmp`, and `zcat`/`gzcat`/`bzcat`/`xzcat`.
On the write side: `cp`, `mv`, `tee`, `rm`, `dd`. These are the file-reading
and file-writing commands Claude reaches for most often; tools like `ls`,
`find`, and `xargs` aren't covered yet (see
[`docs/STATUS.md`](docs/STATUS.md)).

It also stays quiet for paths that aren't really "outside your project":
`/dev/null` and friends, and the session's **own** background-task output under
`/tmp/claude-<uid>/…` that the agent polls with `cat`/`tail`/`grep`. So
sessions that spawn and manage background work aren't spammed with prompts for
reading their own output — in real usage that one case accounted for ~37% of
all prompts. Read-only commands may also poll a **sibling** session's output
under the same project's scratch dir — the dispatcher-tails-workers pattern of
parallel dispatch. Writing into another session's scratch still asks, and a
different project's scratch still asks entirely.

| Command                              | Decision |
| ------------------------------------ | -------- |
| `grep foo ./src.txt`                 | allow    |
| `rg -g '*.py' foo ./src`             | allow    |
| `cat data.txt \| grep foo`           | allow    |
| `jq '.a/.b' data.json`               | allow    |
| `yq .foo data.yaml`                  | allow    |
| `sed 's/a/b/g' notes.md`             | allow    |
| `wc -l data.txt`                     | allow    |
| `sort -o sorted.txt data.txt`        | allow    |
| `diff a.txt b.txt`                   | allow    |
| `cp a.txt b.txt`                     | allow    |
| `mv a.txt b.txt`                     | allow    |
| `rm -rf ./build`                     | allow    |
| `dd if=./in of=./out bs=1M`          | allow    |
| `echo foo \| tee log.txt`            | allow    |
| `cat data.txt > /dev/null`           | allow    |
| `grep foo data.txt 2>/dev/null`      | allow    |
| `grep foo data.txt 2>&1`             | allow    |
| `cat <<<"/etc/foo"` (here-string)    | allow    |
| `cat ~/proj/notes.md` (root `~/proj`) | allow   |
| `cd "$(git rev-parse --show-toplevel)" && cat README.md` | allow |
| `cd "$(pwd)" && cat README.md`       | allow    |
| `tail /tmp/claude-501/…/<this-session>/…` (own task output) | allow |
| `tail /tmp/claude-501/<this-project>/<sibling-session>/…` (sibling read) | allow |
| `grep secret /etc/passwd`            | **ask**  |
| `jq '.x' /etc/hosts`                 | **ask**  |
| `yq -o json /etc/hosts`              | **ask**  |
| `wc --files0-from=/etc/list`         | **ask**  |
| `diff --from-file=/etc/hosts in.txt` | **ask**  |
| `mv .env ~/leaked`                   | **ask**  |
| `tee /etc/hosts`                     | **ask**  |
| `less /var/log/syslog`               | **ask**  |
| `cat ../../etc/passwd`               | **ask**  |
| `cat ~/.aws/credentials`             | **ask**  |
| `cat ~user/notes.md`                 | **ask**  |
| `cat $HOME/.ssh/id_rsa`              | **ask**  |
| `cd /etc && cat passwd`              | **ask**  |
| `cd "$(mktemp -d)" && cat x.txt`     | **ask**  |
| `LC_ALL=C cat /etc/passwd`           | **ask**  |
| `ln -s /etc/passwd link && cat link` | **ask**  |
| `ln /etc/passwd link && cat link`    | **ask**  |
| `cp x /tmp/claude-501/<this-project>/<sibling>/…` (sibling write) | **ask** |
| `cat /tmp/claude-501/<other-project>/…` (cross-project read) | **ask** |
| `cat /tmp/out` · `cat /var/tmp/x`    | **deny** |
| `sed -f /tmp/evil.sed notes.md`      | **deny** |
| `grep foo data.txt > /tmp/out.txt`   | **deny** |
| `sort -o /tmp/out.txt data.txt`      | **deny** |
| `cp ./secret.txt /tmp/exfil`         | **deny** |
| `rm -rf /tmp/foo`                    | **deny** |
| `dd if=./in of=/tmp/out`             | **deny** |
| `cd /tmp && cat in.txt > evil`       | **deny** |
| `cat ./tmp/out` (repo-local scratch) | allow    |
| `grep '/tmp' data.txt` (`/tmp` is the pattern) | allow |
| `cat /tmpfoo/x` (not under `/tmp`)   | **ask**  |
| `echo secret > /tmp/out`             | defer    |
| `ls /etc`                            | defer    |

Note the `jq` row: `.a/.b` is a jq program, not a filesystem path. The hook
knows the difference because it parses each command against a per-command spec
of which positions are programs, which are files, and which flags take values.
A naive string match would either miss real file arguments or false-positive on
program syntax.

The **deny** rows are **host-wide temp** paths — at or under `/tmp`, `/var/tmp`,
or `$TMPDIR` after symlink resolution. They're classified from the *same*
resolved file arguments the hook already extracts, so `/tmp` appearing only as
text (a grep pattern, a commit message, an `echo` string) is never matched. The
deny is the default and can be softened to `ask` or narrowed with an allowlist —
see [Configuration](#configuration). It applies only to the commands the hook
already guards: an unguarded command targeting `/tmp` (e.g. `echo secret > /tmp/out`)
still defers.

The **ask** rows assume an interactive or `default`-mode session. In full-auto
`bypassPermissions` mode (`--dangerously-skip-permissions`) those same paths
return `deny` instead — equally blocking, with recoverable feedback for the
agent. See [Configuration](#configuration).

### Worktree-aware sibling-checkout deny

When your session runs inside a **git worktree**, a write that lands in a
*sibling checkout of the same repo* — the primary checkout or another worktree —
is a distinct, high-consequence mistake: the edit silently lands on the wrong
branch (often `main`, or another session's in-flight branch). This is easy to do
by absolute path (`<repo>/cmd/main.go` instead of
`<repo>/.worktrees/mine/cmd/main.go`).

The hook treats "sibling checkout" as a recognized tier of the outside-workspace
check and **denies writes into it** — upgraded from the generic outside-workspace
`ask`, because a deny self-heals in one agent round trip instead of relying on a
human to notice and retype the path. The message names the offending checkout,
its checked-out branch, and the corrected path under your session's checkout
(same relative path). This applies to:

- **Bash writes and redirect targets** — `cp`/`mv`/`tee`/`rm`/`dd` operands and
  `> file` targets that resolve inside a sibling checkout.
- **The `Edit`, `Write`, `MultiEdit`, and `NotebookEdit` tools** — the one place
  the hook reaches beyond `Bash`. Its *only* active rule there is this
  sibling-checkout deny; every other edit defers to your normal permissions.

**Reads keep today's behavior** — reading a sibling checkout risks staleness,
not damage, so it stays an `ask`. Detection is a no-op when the session isn't in
a worktree, and a path in an *unrelated* git repo is never treated as a sibling
(it shares no git common-dir with your repo). For deliberate cross-checkout work,
`WORKSPACE_GUARD_OVERRIDE=<reason>` downgrades the deny to `ask`; see
[Configuration](#configuration).

## Install

Install on any Claude Code surface that runs plugin `PreToolUse` hooks — the
CLI, the IDE extensions, or **Claude Code for Claude Desktop**. Both methods add
the same marketplace and plugin.

**Claude Code (CLI or IDE extension)** — run the slash commands:

```
/plugin marketplace add karlkfi/claude-workspace-guard
/plugin install workspace-guard@workspace-guard
```

**Claude Code for Claude Desktop** — use the **Customize** tab:

1. Open the **Customize** tab and go to its plugins / marketplaces section.
2. Add `karlkfi/claude-workspace-guard` as a marketplace (the repo at
   `https://github.com/karlkfi/claude-workspace-guard.git`).
3. Find **workspace-guard** in that marketplace, install it, and make sure it's
   enabled.

After installing with either method:

- Requires `python3` on your PATH.
- Restart Claude Code so the hook is registered.
- **Won't fire where plugin `PreToolUse` hooks don't run.** Claude Cowork and
  Claude Desktop's *native* assistant don't run them yet, so the guard never
  fires in those
  ([anthropics/claude-code#45514](https://github.com/anthropics/claude-code/issues/45514)).

To verify, ask Claude to run `grep root /etc/passwd` — you should see a
permission prompt citing the outside-workspace path. Then ask it to `grep` a
file in your repo; it should run without prompting.

## Upgrade

workspace-guard installs from a GitHub marketplace, which Claude Code tracks at
the repository's default branch (`main`). It does **not** auto-update by default,
so a newer release won't reach you until you refresh the marketplace and
reinstall the plugin.

**Claude Code (CLI or IDE extension)** — run the slash commands:

```
/plugin marketplace update workspace-guard
/plugin uninstall workspace-guard@workspace-guard
/plugin install workspace-guard@workspace-guard
```

The first command re-fetches the marketplace manifest from the repo; the
reinstall picks up the new version. Refreshing the catalog alone does **not**
upgrade an already-installed plugin unless you've turned on auto-update for the
marketplace — hence the explicit reinstall.

**Claude Code for Claude Desktop** — in the **Customize** tab's plugins /
marketplaces section, refresh the `workspace-guard` marketplace, then reinstall
**workspace-guard** from it.

After upgrading either way:

- Run `/reload-plugins` to activate the updated hook without restarting, or
  restart Claude Code.
- Confirm the new version is live: the `/plugin` menu lists the installed
  version — compare it against the
  [latest release](https://github.com/karlkfi/claude-workspace-guard/releases).

## How it works

1. **Tokenize** the command with Python's `shlex` (POSIX mode, punctuation
   grouping) so quotes are respected and shell operators (`|`, `&&`, `>`, `;`)
   become their own tokens. Unquoted `#` comments are stripped first, keeping
   the newline that ends each one so the next line stays its own command group.
   A newline outside quotes is also a command
   separator — like `;` — so a guarded command on a line after another is
   classified on its own rather than merged into its neighbour. Heredoc body
   lines (everything between `<<TAG` and a line matching `TAG`, `<<-` included)
   are dropped so body content — HTML like `</div>`, a script, path-like text —
   is never parsed as commands or file arguments.
2. **Split** into simple commands on those operators and collect each redirect
   target (`> file`) into the command group it belongs to, so it's later
   resolved against that group's cwd (see step 5). The token after `<<`
   (heredoc delimiter) or `<<<` (here-string content) is skipped — it isn't a
   path. An fd number written before a redirect (`2>file`) and an
   fd-duplication or close (`2>&1`, `2>&-`) are recognised so the digit and the
   dup target don't leak as phantom file arguments; `>&file` (a redirect to a
   file, not a dup) still has its target checked.
3. **Strip** leading POSIX `NAME=VALUE` command-prefix assignments from each
   simple command (`LC_ALL=C cat …` → `cat …`) so the assignment doesn't mask
   the command-name lookup.
4. **Classify** each token using a per-command spec table that knows which flags
   take values (`grep -e PAT`), which flag-values are themselves files
   (`grep -f`, `jq --slurpfile`), and how many leading positionals are the
   program/pattern to skip. `dd` is handled separately because its operands are
   all `KEY=VALUE` pairs — `if=PATH` and `of=PATH` are the file operands; the
   rest (`bs=`, `count=`, `conv=`, `iflag=`, `oflag=`, …) are values, not paths.
5. **Track** cwd shifts across the chain. A `cd`/`pushd` in an earlier group
   re-roots relative file paths — including relative redirect targets — in
   later guarded groups (so `cd /etc && cat passwd` flags `passwd` as
   `/etc/passwd`, and `cd /tmp && cat in.txt > evil` flags `evil` as
   `/tmp/evil`). Two pure, deterministic command substitutions are recognised
   as `cd`/`pushd` targets and resolved from the tracked cwd instead of
   dropping tracking: `$(git rev-parse --show-toplevel)` (computed by walking
   up to the nearest `.git` entry — git is never executed) and `$(pwd)` (the
   identity). The whitelist is closed and matched on the exact
   whitespace-normalized token — there is no general `$( )` evaluation. When
   the new cwd can't be resolved at hook time — bare `cd`,
   `cd -`, `cd $HOME`, `popd`, any other substitution — later relative paths
   short-circuit to `ask`.
6. **Stage** symlinks *and* hard links created by an earlier `ln OUTSIDE LINK`
   in the chain (with or without `-s`). `LINK`'s resolved path is recorded so
   a later `cat LINK` is flagged — bash hasn't materialised the link yet at
   hook time, so a naive `realpath` would otherwise place `LINK` lexically
   inside the workspace and let it through.
7. **Resolve** every file argument against `$CLAUDE_PROJECT_DIR` with
   `realpath`, collapsing `../` and following symlinks. Anything that resolves
   outside the root yields `ask`; otherwise `allow`. A leading `~` or `~/…` is
   expanded to `$HOME` first (bash does this deterministically), so a home path
   inside the root is allowed instead of needlessly prompted. Tokens that bash
   would still expand unpredictably at runtime — `~user`/`~+`/`~-`, an unset
   `$HOME`, or a `$` that introduces an expansion (`$VAR`, `${VAR}`, `$(...)`,
   `$1`, `$?`) — short-circuit to
   `ask`, since `realpath` would otherwise lexically place them inside `cwd`.
   A `$` bash keeps literal — trailing (`foo$`) or before a non-name char
   (`a$.b`) — is treated as part of the filename and resolved normally.
   Well-known
   device paths (`/dev/null`, `/dev/stdin`, `/dev/stdout`, `/dev/stderr`,
   `/dev/zero`, `/dev/tty`, `/dev/random`, `/dev/urandom`, `/dev/fd/N`) are
   allowlisted and skip the workspace check.
8. **Allow** the current session's own Claude-managed scratch — and, for
   read-only commands, sibling sessions of the same project. Claude Code writes
   each background task's output to
   `/tmp/claude-<uid>/<encoded-project>/<session-uuid>/tasks/<id>.output`, and
   the agent reads it back with `cat`/`tail`/`grep`. Reading command output
   isn't the boundary this hook guards, so:
   - a path under `/tmp/claude-<uid>/` that carries the **current** session's id
     as a path segment is allowed for any guarded command (your own scratch);
     and
   - for **read-only** commands, a path under the current project's scratch dir
     (`/tmp/claude-<uid>/<encoded-project>/`) is allowed even when it belongs to
     a *different* session — the dispatcher-tails-workers pattern of parallel
     dispatch. The hook finds that project dir by scanning the temp root for the
     slug that holds the current `session_id`, so it never depends on Claude's
     undocumented slug encoding (which differs between a worktree and the main
     checkout); if it can't be located, the read simply keeps prompting.

   Writing into another session's scratch, and any access to a *different
   project's* scratch, still prompt. Because these allows match on the resolved
   `realpath` — and run *after* the `ln`-staging check — a symlink planted in
   the scratch dir that escapes the root is still flagged.
9. **Allow reads of Claude-owned project data.** For read-only commands (`cat`,
   `head`, `tail`, `grep`, `rg`, `sed`, `awk`, `jq`, `yq`, `diff`, `sort`,
   `wc`, `file`, `hexdump`, and their aliases), a path whose resolved
   `realpath` is under `~/.claude/projects/` is allowed silently. That
   directory is written exclusively by the Claude Code harness (session
   metadata, sub-agent data, workflow journals) and reading it back is not
   the boundary this hook guards. Write commands (`cp`, `mv`, `tee`, `rm`)
   are **not** exempt — they must still pass the workspace check. The
   exemption also does not apply to redirect targets, since the hook cannot
   verify redirect direction without running the command. Users can extend
   the list with `WORKSPACE_GUARD_READ_ALLOW_PREFIXES`; see
   [Configuration](#configuration).
10. **Deny** host-wide temp. After the steps above, any *remaining*
   outside-workspace file argument whose resolved `realpath` is at or under a
   host-temp root (`/tmp`, `/var/tmp`, `$TMPDIR`, all resolved first — so macOS's
   `/tmp → /private/tmp` and a `$TMPDIR` under `/var/folders/…` are caught) is
   reclassified from `ask` to `deny`, with a message steering to a repo-local
   gitignored scratch dir. Because this runs on the already-resolved file
   arguments, a `/tmp` that appears only as text (a grep pattern, an `echo`
   string) is never matched. The Claude-managed temp root from step 8 is
   excluded — another session's task output keeps its `ask` (or, for a
   same-project read, the step 8 allow) rather than this steer-to-`./tmp/` deny.
   The action, scratch-dir name, extra roots,
   and an allowlist escape hatch are all configurable; see
   [Configuration](#configuration).
11. **Deny** writes into a sibling checkout of the same repo. When the session
   root is inside a git worktree, the hook resolves the enclosing git checkout of
   each *write* path (walking up to the nearest `.git`, reading only tiny git
   metadata) and compares its shared `--git-common-dir` to the session's. A path
   inside a *different* checkout of the *same* repo (same common-dir, different
   root) is reclassified to `deny`, naming the checkout, its branch, and the
   corrected in-session path. Only writes upgrade — reads keep step 7's `ask`.
   A path in an unrelated repo has a different common-dir and stays a generic
   outside `ask`. The same rule is the sole active check on the `Edit`, `Write`,
   `MultiEdit`, and `NotebookEdit` tools. `WORKSPACE_GUARD_OVERRIDE=<reason>`
   downgrades it to `ask`; see [Configuration](#configuration).

## Agent guidance: avoiding prompts

When the hook prompts, its reason now tells the agent how to avoid the prompt
next time — naming the offending path and the fix (use an in-root path, drop a
`$VAR`/`~`, or read with the Read/Grep tools). But some habits avoid prompts
entirely, and the hook can't surface them because it *allows* those paths
silently — there's no prompt on which to attach advice.

Paste the block below into your project's `CLAUDE.md` (or `AGENTS.md`) so the
agent follows them from the start. They're framed as instructions to the agent:

```markdown
## Avoiding workspace-guard permission prompts

This repo uses workspace-guard, a hook that prompts before a guarded bash file
command (`grep`, `sed`, `awk`, `jq`, `cat`, `head`, `tail`, `cp`, `mv`, `rm`,
`tee`, `dd`, …) reads or writes a path outside the project root. To keep work
flowing, avoid triggering it:

- **Prefer the Read, Grep, and Glob tools over bash** `cat`/`grep`/`sed`/`head`/
  `tail`/`awk` for inspecting files. They're purpose-built, don't go through this
  hook, and are the right tool for reading and searching code.
- **Keep guarded file arguments inside the project root.** A path that resolves
  outside the root (including via `../` traversal) prompts every time.
- **Don't put `$VAR`, `$(...)`, or a `~user` prefix in a guarded file argument.**
  The hook can't expand them, so it treats them as outside the root and prompts —
  even when they'd resolve in-root. (A bare `~`/`~/…` *is* expanded to `$HOME`,
  so home-relative paths inside the root are fine.) Write the literal in-root
  path instead (e.g. `cat ./config/app.json`, not `cat "$HOME/proj/config/app.json"`).
- **Don't `cd` outside the project root**, and avoid bare `cd`, `cd -`, and
  `cd $HOME` — they lose the hook's working-directory tracking, so every later
  relative path in the same command prompts. Stay in the root, or `cd` into a
  subdirectory of it with a literal path.
  (`cd "$(git rev-parse --show-toplevel)"` and `cd "$(pwd)"` are fine — the
  hook resolves these two substitutions itself and tracking survives; any
  other `$(...)` target still drops it.)
- **Write temp files inside the project root, not `/tmp`.** Host-wide temp
  (`/tmp`, `/var/tmp`, `$TMPDIR`) is **denied** by default — not just prompted —
  because it's shared across sessions and worktrees and lives outside the root.
  Use a repo-local gitignored scratch dir like `./tmp/out.txt` instead. (Redirects
  and command output to `/dev/null`, `/dev/stdout`, `/dev/stderr`, and `/dev/fd/N`
  are exempt and never prompt. Reading back this session's *own* background-task
  output under `/tmp/claude-<uid>/…/<session>/…` is also exempt — that path is
  managed by Claude Code, not something you choose — as is *read-only* access to
  a sibling session's output under the same project's scratch dir.) Reading
  files under `~/.claude/projects/` (Claude Code's own session and sub-agent
  data) is also exempt for read-only commands.
- **Read dependency source from in-workspace vendored/pinned copies, not the
  global cache.** Out-of-tree caches (Go's `~/go/pkg/mod`, npm's `~/.npm`, pip's
  `~/.cache/pip`, cargo's `~/.cargo/registry`) are outside the project root, so
  every guarded read of them prompts. Vendor the source into the tree instead
  (e.g. `go mod vendor` → `vendor/`, npm's `node_modules/`) and read from there,
  or use the Read/Grep tools, which skip the hook entirely.
- **In a git worktree, edit only via this session's checkout — never another
  checkout's path.** A write (bash or `Edit`/`Write`) into the primary checkout
  or another worktree of the same repo is **denied**: it would land your change
  on the wrong branch. Use the same relative path under your session root. For
  deliberate cross-checkout work, set `WORKSPACE_GUARD_OVERRIDE=<reason>` to
  downgrade the deny to a prompt.
```

The plugin also ships a **`reduce-workspace-guard-prompts`** skill: ask Claude
"why am I getting so many permission prompts?" and it will diagnose the cause —
grounding itself in your real prompt history via the bundled
`scripts/friction-report.py` analyzer — and walk through these fixes.

For the "just show me the numbers" case, the **`/friction-report`** slash command
runs that analyzer directly and prints the ranked report — no diagnosis, no fixes.
It passes its arguments straight through to the script, so the same flags work:

```
/friction-report                                  # last 7 days, this project
/friction-report --since 24h --repo gateway
/friction-report --raw --top 20
```

## Configuration

The set of guarded commands lives in the `SPEC` and `ALIASES` tables at the top
of `scripts/bash-workspace-guard.py`. Add a row to guard another command.

### Host-wide temp (`/tmp`) deny

A guarded file argument that resolves at or under a host-temp root is **denied**
by default and steered to a repo-local gitignored scratch dir. Four environment
variables tune this — all read at hook time, so no restart is needed:

| Env var | Default | Effect |
| --- | --- | --- |
| `WORKSPACE_GUARD_TMP_ACTION` | `deny` | `deny` blocks host-temp paths; `ask` softens to a confirmation prompt. Any other value falls back to `deny`. |
| `WORKSPACE_GUARD_SCRATCH_DIR` | `tmp/` | The repo-local scratch dir named in the deny message. |
| `WORKSPACE_GUARD_TMP_ROOTS` | (empty) | Extra host-temp roots, `:`- or `,`-separated. **Additive** — it extends the built-in `/tmp`, `/var/tmp`, and `$TMPDIR`; it can't shrink them. |
| `WORKSPACE_GUARD_TMP_ALLOW` | (empty) | Allowlist of exact-prefix or glob paths (`:`/`,`-separated) that **escape** the deny — for the rare tool that genuinely needs `/tmp`. |

`WORKSPACE_GUARD_TMP_ALLOW` is the one knob that *loosens* the guard, so it's
empty by default and opt-in: an allowlisted host-temp path is allowed silently
rather than denied. Scope each entry tightly (an exact path or a narrow glob like
`/tmp/myapp-*`), since anything it matches bypasses the boundary. The deny itself
is the secure default — softening to `ask` (`WORKSPACE_GUARD_TMP_ACTION=ask`) is
the gentler way to keep a human in the loop.

### Allowed read prefixes

A set of path prefixes are always allowed for **read-only** guarded commands
(`cat`, `head`, `tail`, `grep`, `rg`, `sed`, `awk`, `jq`, `yq`, `diff`,
`sort`, `wc`, `file`, `hexdump`, and their aliases). Write commands (`cp`,
`mv`, `tee`, `rm`) and redirect targets are never exempt.

The built-in default is `~/.claude/projects/` (Claude Code's own session and
sub-agent data). You can extend it with additional prefixes:

| Env var | Default | Effect |
| --- | --- | --- |
| `WORKSPACE_GUARD_READ_ALLOW_PREFIXES` | (empty) | Extra read-exempt prefixes, `:`- or `,`-separated. **Additive** — it extends the built-in list. |

Each entry is run through `realpath` so platform symlinks resolve correctly.
Scope entries tightly: anything under a configured prefix is silently allowed
for read commands without a confirmation prompt.

### Sibling-checkout (worktree) deny

When the session runs in a git worktree, writes into a sibling checkout of the
same repo are **denied** (see
[Worktree-aware sibling-checkout deny](#worktree-aware-sibling-checkout-deny)).
One env var tunes this, read at hook time (no restart needed):

| Env var | Default | Effect |
| --- | --- | --- |
| `WORKSPACE_GUARD_OVERRIDE` | (empty) | When set to a non-empty reason string, downgrades the sibling-checkout deny to `ask` for deliberate cross-checkout work. The reason is echoed back in the prompt. |

`WORKSPACE_GUARD_OVERRIDE` is the one knob that *loosens* this guard, so it's
empty by default and opt-in. The deny is the secure default: it self-heals in one
agent round trip, whereas an approvable prompt invites the reflexive "yes" that
lands the change on the wrong branch. Scope the override to the moment you
actually need it (e.g. one command), not the whole session.

### Outside-workspace ask vs. deny

For outside-workspace paths the hook returns `ask` so you get a confirmation
prompt. In full-auto runs (`--dangerously-skip-permissions`, i.e.
`bypassPermissions` mode) it returns `deny` instead — equally blocking, but it
feeds the reason back to the agent so it can route around the path rather than
stall on a prompt no one can approve. To hard-block in *every* mode, drop the
`permission_mode` check and return `"deny"` unconditionally in the script's
final output.

## Limitations

- A leading `~`/`~/…` is expanded to `$HOME` (bash does this deterministically),
  so a home path inside the root is allowed. Tokens that bash would expand
  *unpredictably* at runtime — `~user`/`~+`/`~-`, an unset `$HOME`, or a `$`
  that introduces an expansion (`$VAR`, `${VAR}`, `$(...)`, `$1`, `$?`) — are
  still treated as outside-workspace. A `$` bash keeps literal (trailing, or
  before a non-name char like `.`/`/`) is part of the filename and resolved
  normally, so a `price$` or `a$.b` argument no longer prompts spuriously.
- Heredoc body lines are dropped before parsing, so path-like body content
  (`</div>`, `/title`) is never mistaken for a file argument. The `<<TAG`
  terminator is matched by an exact line; an unterminated body swallows to the
  end of the command (matching bash). A `<<` produced by unquoted arithmetic
  (`$((x<<2))`) can arm a spurious delimiter — if a newline follows, later
  tokens may be skipped and the hook defers (fail-safe, never a silent allow).
- `realpath` only follows symlinks for files that already exist; nonexistent
  paths are normalized lexically (fine for read-style commands).
- Redirect targets (`> file`) are only inspected when the command chain also
  contains a guarded command — the hook keys off guarded commands, so a bare
  redirect from an unguarded command (`echo secret > /tmp/out`) is not checked
  and defers to normal permissions. When a guarded command *is* present, the
  redirect target is resolved against the cwd of the command group it appears
  in, so a relative target tracks `cd`-shifts the same way file arguments do
  (`cd /tmp && cat in.txt > evil` flags `/tmp/evil`).
- Multi-source `ln a b destdir/` (3+ positionals, symbolic or hard) is not
  staged. The hook recognises the one- and two-positional forms only.
- The whitelisted `cd` substitutions are matched after quote stripping, so the
  *single*-quoted form (`cd '$(git rev-parse --show-toplevel)'`) — which bash
  treats literally, failing the `cd` unless a directory with that literal name
  exists — is indistinguishable from the double-quoted form and is tracked as
  if it substituted. The mis-track is safe: the resolved directory is derived
  from the already-tracked cwd (the repo toplevel is an ancestor checked
  against the same workspace boundary), so it never admits a path the
  double-quoted form wouldn't. The unquoted form tokenizes into separate
  groups and keeps today's untracked behavior.
- An all-digits token immediately before a redirect operator is treated as an
  fd prefix (`2>file`) and dropped. `shlex` discards the original spacing, so a
  guarded command reading a file literally *named* with digits right before a
  redirect (`cat 2 >out`, where `2` is a file) is indistinguishable from the fd
  form and won't be checked. Such a path resolves in-root (and is allowed)
  anyway except after a `cd` outside the root — a pathological combination.
- The current session's own Claude-managed task-output dir
  (`/tmp/claude-<uid>/…/<session>/…`) is allowed silently, scoped to the
  session via the hook's `session_id`. Read-only commands are additionally
  allowed on a *sibling* session's output under the same project's scratch dir
  (`/tmp/claude-<uid>/<encoded-project>/`), located by scanning the temp root
  for the slug directory that holds the current `session_id` — the
  dispatcher-tails-workers pattern. The `/tmp/claude-<uid>/` prefix is an
  undocumented Claude Code convention inferred from the UID; if Claude Code
  relocates the dir, these paths simply revert to `ask` (fail-safe — the allow
  never widens the boundary). A session with no `session_id` (older CLIs)
  disables both allows entirely.
- In non-interactive / headless runs there is no one to answer an `ask` prompt,
  so an `ask` still **blocks** the command (verified on CLI 2.1.159 — it does
  not silently allow). Under `--dangerously-skip-permissions`
  (`bypassPermissions`) the hook emits `deny` rather than `ask` for
  outside-workspace paths: equally blocking, but the agent receives the reason
  and can recover instead of stalling. See [Configuration](#configuration).
- The host-temp `deny` only upgrades paths the hook already extracts as file
  arguments — the same paths it would otherwise have prompted on. Command shapes
  the hook doesn't parse as file args still defer: a standalone `cd /tmp`, a
  temp-creating tool that isn't in the `SPEC` table (`mktemp -p /tmp`), and a
  redirect from an *unguarded* command (`go test > /tmp/log`). These are out of
  scope by design; guarding more of them is a tracked follow-up.
- The sibling-checkout `deny` classifies *write-context* file arguments — the
  same set the read-prefix exemption treats as writes: redirect targets, `dd`
  operands, and every operand of `cp`/`mv`/`tee`/`rm`. So a `cp` **source** or a
  `dd if=` reading *from* a sibling checkout is denied too, not just the
  destination. That's stricter than a pure "destination only" reading, in the
  secure direction, and recoverable with `WORKSPACE_GUARD_OVERRIDE`. Pure read
  commands (`cat`, `grep`, …) of a sibling are unaffected and keep their `ask`.
- Sibling detection reads git worktree metadata (`.git`, `commondir`, `HEAD`)
  under the offending path and the session root. Any read/parse failure — or a
  session that isn't in a worktree — falls back to the normal outside `ask`
  (fail-safe: the deny is never applied on uncertainty, so the boundary is never
  weakened). A main-checkout session is a deliberate no-op even when worktrees
  exist.

## Companion plugin: branch-guard

workspace-guard draws its boundary along the **filesystem**: it asks before a
guarded command reads or writes a path outside `$CLAUDE_PROJECT_DIR`. It says
nothing about *git history* — once a path is in-root, an in-root
`git commit && git push` to `main`, a `git reset --hard`, or a `git clean -fd`
runs without a second look. Those are exactly the operations that turn an
in-workspace edit into an unrecoverable one.

[**branch-guard**](https://github.com/karlkfi/claude-branch-guard) covers that
gap. It's a sibling plugin with the same secure-by-default, `ask`-based design,
but its axis is the **git branch** rather than the filesystem path. Its motto:
*"Let Claude commit and push all day on feature branches. Pause it at main."*
It parses pending `git`/`gh` commands (and blocks file edits when the repo is on
a protected branch), then:

- **asks** before committing or pushing to `main`/`master`, force-pushing, or
  running destructive commands (`reset --hard`, `clean -fd`, `branch -D`,
  `restore <path>`);
- **allows** read-only git, staging, branch creation, and commits/pushes on
  feature/worktree branches to run silently;
- **defers** unknown commands to your normal permission settings.

The two are complementary and run side by side — workspace-guard watches the
path boundary, branch-guard watches the history boundary. Install it the same
way you installed this one:

```
/plugin marketplace add karlkfi/claude-branch-guard
/plugin install branch-guard@claude-branch-guard
```

## Design

For the rationale behind the approach (why a hook, why `ask`, why a static
spec table, what alternatives were rejected), see [`docs/design.md`](docs/design.md).
Out-of-scope security observations from audits live in
[`docs/security-notes.md`](docs/security-notes.md).

## Privacy

The hook runs entirely on your machine and has no network access, telemetry,
or analytics. It reads the pending command (or edit target) and your project
path, decides in memory, and never opens version-controlled file contents or
writes anything to disk. To detect sibling worktree checkouts it reads a few
small git metadata files (`.git`, `commondir`, `HEAD`) locally. See
[`PRIVACY.md`](PRIVACY.md) for the full policy.

## Contributing

Bugs, ideas, and questions go in
[GitHub Issues](https://github.com/karlkfi/claude-workspace-guard/issues).
For the development backlog and how to add new guarded commands, see
[`docs/STATUS.md`](docs/STATUS.md).

## License

MIT — see [LICENSE](LICENSE).
