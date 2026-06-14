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

The hook produces one of three outcomes:

- **allow** — the command runs without a prompt.
- **ask** — Claude Code shows its standard permission prompt for the command
  (as above). You approve or reject.
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
all prompts. It's scoped to the current session, so another session's scratch
still asks.

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
| `tail /tmp/claude-501/…/<this-session>/…` (own task output) | allow |
| `grep secret /etc/passwd`            | **ask**  |
| `jq '.x' /etc/hosts`                 | **ask**  |
| `yq -o json /etc/hosts`              | **ask**  |
| `sed -f /tmp/evil.sed notes.md`      | **ask**  |
| `grep foo data.txt > /tmp/out.txt`   | **ask**  |
| `sort -o /tmp/out.txt data.txt`      | **ask**  |
| `wc --files0-from=/etc/list`         | **ask**  |
| `diff --from-file=/etc/hosts in.txt` | **ask**  |
| `cp ./secret.txt /tmp/exfil`         | **ask**  |
| `mv .env ~/leaked`                   | **ask**  |
| `rm -rf /tmp/foo`                    | **ask**  |
| `tee /etc/hosts`                     | **ask**  |
| `dd if=./in of=/tmp/out`             | **ask**  |
| `less /var/log/syslog`               | **ask**  |
| `cat ../../etc/passwd`               | **ask**  |
| `cat ~/.aws/credentials`             | **ask**  |
| `cat ~user/notes.md`                 | **ask**  |
| `cat $HOME/.ssh/id_rsa`              | **ask**  |
| `cd /etc && cat passwd`              | **ask**  |
| `LC_ALL=C cat /etc/passwd`           | **ask**  |
| `ln -s /etc/passwd link && cat link` | **ask**  |
| `ln /etc/passwd link && cat link`    | **ask**  |
| `echo secret > /tmp/out`             | defer    |
| `ls /etc`                            | defer    |

Note the `jq` row: `.a/.b` is a jq program, not a filesystem path. The hook
knows the difference because it parses each command against a per-command spec
of which positions are programs, which are files, and which flags take values.
A naive string match would either miss real file arguments or false-positive on
program syntax.

The **ask** rows assume an interactive or `default`-mode session. In full-auto
`bypassPermissions` mode (`--dangerously-skip-permissions`) those same paths
return `deny` instead — equally blocking, with recoverable feedback for the
agent. See [Configuration](#configuration).

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
   become their own tokens. A newline outside quotes is also a command
   separator — like `;` — so a guarded command on a line after another is
   classified on its own rather than merged into its neighbour.
2. **Split** into simple commands on those operators and pull redirect targets
   (`> file`) aside as files to check. The token after `<<` (heredoc
   delimiter) or `<<<` (here-string content) is skipped — it isn't a path. An
   fd number written before a redirect (`2>file`) and an fd-duplication or
   close (`2>&1`, `2>&-`) are recognised so the digit and the dup target don't
   leak as phantom file arguments; `>&file` (a redirect to a file, not a dup)
   still has its target checked.
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
   re-roots relative file paths in later guarded groups (so
   `cd /etc && cat passwd` flags `passwd` as `/etc/passwd`). When the new cwd
   can't be resolved at hook time — bare `cd`, `cd -`, `cd $HOME`, `popd` —
   later relative paths short-circuit to `ask`.
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
   `$HOME`, or any `$` (variables and command substitutions) — short-circuit to
   `ask`, since `realpath` would otherwise lexically place them inside `cwd`.
   Well-known
   device paths (`/dev/null`, `/dev/stdin`, `/dev/stdout`, `/dev/stderr`,
   `/dev/zero`, `/dev/tty`, `/dev/random`, `/dev/urandom`, `/dev/fd/N`) are
   allowlisted and skip the workspace check.
8. **Allow** the current session's own Claude-managed scratch. Claude Code
   writes each background task's output to
   `/tmp/claude-<uid>/<encoded-project>/<session-uuid>/tasks/<id>.output`, and
   the agent reads it back with `cat`/`tail`/`grep`. Reading your own
   command output isn't the boundary this hook guards, so a path whose resolved
   `realpath` is under `/tmp/claude-<uid>/` **and** carries the current
   session's id as a path segment is allowed silently. The scope is
   per-session, not the whole temp root: another session's or project's task
   output (which can contain secrets) still prompts. Because the match is on
   the resolved `realpath`, a symlink planted in the scratch dir that escapes
   the root is still flagged.

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
- **Write temp files inside the project root, not `/tmp`.** Use a path like
  `./.tmp/out.txt` rather than `/tmp/out.txt`. (Redirects and command output to
  `/dev/null`, `/dev/stdout`, `/dev/stderr`, and `/dev/fd/N` are exempt and never
  prompt. Reading back this session's *own* background-task output under
  `/tmp/claude-<uid>/…/<session>/…` is also exempt — that path is managed by
  Claude Code, not something you choose.)
- **Read dependency source from in-workspace vendored/pinned copies, not the
  global cache.** Out-of-tree caches (Go's `~/go/pkg/mod`, npm's `~/.npm`, pip's
  `~/.cache/pip`, cargo's `~/.cargo/registry`) are outside the project root, so
  every guarded read of them prompts. Vendor the source into the tree instead
  (e.g. `go mod vendor` → `vendor/`, npm's `node_modules/`) and read from there,
  or use the Read/Grep tools, which skip the hook entirely.
```

The plugin also ships a **`reduce-workspace-guard-prompts`** skill: ask Claude
"why am I getting so many permission prompts?" and it will diagnose the cause
from recent prompts and walk through these fixes.

## Configuration

The set of guarded commands lives in the `SPEC` and `ALIASES` tables at the top
of `scripts/bash-workspace-guard.py`. Add a row to guard another command.

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
  *unpredictably* at runtime — `~user`/`~+`/`~-`, an unset `$HOME`, or any
  unquoted `$` (variables and command substitutions) — are still treated as
  outside-workspace. This is the secure-by-default choice: a literal filename
  containing `$` will get an `ask` prompt rather than slip through.
- `realpath` only follows symlinks for files that already exist; nonexistent
  paths are normalized lexically (fine for read-style commands).
- Redirect targets (`> file`) are only inspected when the command chain also
  contains a guarded command — the hook keys off guarded commands, so a bare
  redirect from an unguarded command (`echo secret > /tmp/out`) is not checked
  and defers to normal permissions. When a guarded command *is* present, the
  redirect target is resolved against the original cwd, not any `cd`-shifted
  cwd: a relative redirect after `cd /etc` is still checked against the
  original workspace cwd, and absolute redirect targets are caught.
- Multi-source `ln a b destdir/` (3+ positionals, symbolic or hard) is not
  staged. The hook recognises the one- and two-positional forms only.
- An all-digits token immediately before a redirect operator is treated as an
  fd prefix (`2>file`) and dropped. `shlex` discards the original spacing, so a
  guarded command reading a file literally *named* with digits right before a
  redirect (`cat 2 >out`, where `2` is a file) is indistinguishable from the fd
  form and won't be checked. Such a path resolves in-root (and is allowed)
  anyway except after a `cd` outside the root — a pathological combination.
- The current session's own Claude-managed task-output dir
  (`/tmp/claude-<uid>/…/<session>/…`) is allowed silently, scoped to the
  session via the hook's `session_id`. The `/tmp/claude-<uid>/` prefix is an
  undocumented Claude Code convention inferred from the UID; if Claude Code
  relocates the dir, these paths simply revert to `ask` (fail-safe — the allow
  never widens the boundary). A session with no `session_id` (older CLIs)
  disables the allow entirely.
- In non-interactive / headless runs there is no one to answer an `ask` prompt,
  so an `ask` still **blocks** the command (verified on CLI 2.1.159 — it does
  not silently allow). Under `--dangerously-skip-permissions`
  (`bypassPermissions`) the hook emits `deny` rather than `ask` for
  outside-workspace paths: equally blocking, but the agent receives the reason
  and can recover instead of stalling. See [Configuration](#configuration).

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
or analytics. It reads the pending Bash command and your project path, decides
in memory, and never opens file contents or writes anything to disk. See
[`PRIVACY.md`](PRIVACY.md) for the full policy.

## Contributing

Bugs, ideas, and questions go in
[GitHub Issues](https://github.com/karlkfi/claude-workspace-guard/issues).
For the development backlog and how to add new guarded commands, see
[`docs/STATUS.md`](docs/STATUS.md).

## License

MIT — see [LICENSE](LICENSE).
