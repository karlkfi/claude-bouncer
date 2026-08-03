# workspace-guard

**Path-aware bash permissions for Claude Code.**

[![release](https://img.shields.io/github/v/release/karlkfi/claude-workspace-guard)](https://github.com/karlkfi/claude-workspace-guard/releases) [![tests](https://img.shields.io/github/actions/workflow/status/karlkfi/claude-workspace-guard/tests.yml?branch=main&label=tests)](https://github.com/karlkfi/claude-workspace-guard/actions/workflows/tests.yml) [![License: MIT](https://img.shields.io/github/license/karlkfi/claude-workspace-guard.svg)](LICENSE) [![Claude Code plugin](https://img.shields.io/badge/Claude_Code-plugin-7e57c2)](#install)

> Stop approving every in-repo grep. Start catching the one that reads `/etc/passwd`.

You ask Claude to "find that auth error." It runs `grep -r token /var/log`. Or
`cat ~/.aws/credentials` while "checking the environment." Or pipes a file from
outside your repo into `jq`. The default `Bash(grep:*)` permission rules can't
tell these apart from the dozens of in-repo greps Claude runs every session —
they either trust every invocation or prompt on every one.

workspace-guard is a `PreToolUse` hook for `Bash` — and for Claude's native file
tools (`Read`, `Grep`, `Glob`, `Edit`, `Write`, …) — that parses the command,
finds its file arguments, and asks for confirmation only when a path resolves
outside your project root (`$CLAUDE_PROJECT_DIR`). In-repo reads and pure
pipelines run silently.

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
  default for **host-wide temp** paths (`/tmp`, `/var/tmp`, `$TMPDIR`, `%TMP%`): they're
  shared across every session and worktree and live outside the project root, so
  instead of prompting, the hook steers you to a repo-local gitignored scratch
  dir (`./tmp/`). It's also the default for **writes into a sibling checkout of
  the same repo** when the session runs in a git worktree (see below).
  Configurable down to `ask`; see [Configuration](#configuration).
- **defer** — the hook stays silent; your normal permission settings apply.

Guarded commands: `grep` (and `egrep`, `fgrep`), `rg`, `sed`, `awk` (and
`gawk`, `mawk`), `jq`, `yq`, `cat`, `head`, `tail`, `sort`, `wc`, `diff`,
`file`, `hexdump`, `uniq`, `xxd` (whose optional second positional is an
*output* file and is treated as a write), plus the cat-shape readers `less`,
`more`, `tac`, `rev`, `nl`, `od`, `strings`, `cmp`, and
`zcat`/`gzcat`/`bzcat`/`xzcat`.
On the write side: `cp`, `mv`, `tee`, `rm`, `dd`, and `mktemp` (whose default
location is host temp — see below). These are the file-reading and file-writing
commands Claude reaches for most often; tools like `ls`, `find`, and `xargs`
aren't covered yet (see [`docs/STATUS.md`](docs/STATUS.md)). A **redirect**
target (`> file`) is checked on *any* command, guarded or not — it's a write the
shell performs regardless of the command word.

The same outside-workspace check also runs on Claude's **native file tools**, so
the guard can't be sidestepped by switching from a bash command to the
equivalent tool — `Read`-ing `/etc/passwd` prompts exactly like `cat /etc/passwd`
would. `Read`, `Grep`, and `Glob` are treated as reads (they keep the self-read
exemptions below); `Edit`, `Write`, `MultiEdit`, and `NotebookEdit` are treated
as writes. See [Beyond Bash](#beyond-bash-native-file-tools).

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
| `cat > page.html <<'EOF'` … `</div>` … `EOF` (heredoc body) | allow |
| `cat ~/proj/notes.md` (root `~/proj`) | allow   |
| `cd "$(git rev-parse --show-toplevel)" && cat README.md` | allow |
| `cd "$(pwd)" && cat README.md`       | allow    |
| `tail /tmp/claude-501/…/<this-session>/…` (own task output) | allow |
| `tail /tmp/claude-501/<this-project>/<sibling-session>/…` (sibling read) | allow |
| `f=notes.md; cat $f`                 | allow    |
| `d=sub; cd $d && cat x.txt`          | allow    |
| `cp x "$(git rev-parse --show-toplevel)/backup/"` | allow |
| `cat "$(pwd)/notes.md"`              | allow    |
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
| `echo "$(cat /etc/passwd)"` (quoted subst read) | **ask** |
| `LC_ALL=C cat /etc/passwd`           | **ask**  |
| `until grep -q x /etc/passwd; do :; done` | **ask** |
| `if cat /etc/passwd; then :; fi`     | **ask**  |
| `f=/etc/passwd; cat $f`              | **ask**  |
| `f=$HOME/x; cat $f` (non-literal value) | **ask** |
| `C=cat; $C /etc/passwd`              | **ask**  |
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
| `echo secret > /tmp/out` (unguarded redirect) | **deny** |
| `cat <<'EOF' > /tmp/out` … `EOF` (heredoc, outside target) | **deny** |
| `cd /tmp && echo x > out.txt`        | **deny** |
| `mktemp` · `mktemp -d` · `mktemp -p /tmp x.XXXX` | **deny** |
| `mktemp /tmp/x.XXXX`                 | **deny** |
| `echo "$(mktemp -p /tmp x.XXXX)"` (quoted subst write) | **deny** |
| `cd "$(mktemp -d)" && cat x.txt`     | **deny** |
| `cat ./tmp/out` (repo-local scratch) | allow    |
| `grep '/tmp' data.txt` (`/tmp` is the pattern) | allow |
| `mktemp -p ./scratch x.XXXX` (repo-local) | allow |
| `TMPDIR=./scratch mktemp` (repo-local default) | allow |
| `mktemp -dp ./scratch x.XXXX` (clustered `-d -p`) | allow |
| `cat /tmpfoo/x` (not under `/tmp`)   | **ask**  |
| `ls > /etc/out.txt` (unguarded redirect, outside) | **ask** |
| `ls /etc` (unguarded, no redirect)   | defer    |
| `mktemp --version` (creates nothing) | defer    |
| `echo '$(mktemp -d)'` (single-quoted, no subst) | defer |

Note the `jq` row: `.a/.b` is a jq program, not a filesystem path. The hook
knows the difference because it parses each command against a per-command spec
of which positions are programs, which are files, and which flags take values.
A naive string match would either miss real file arguments or false-positive on
program syntax.

The **deny** rows are **host-wide temp** paths — at or under `/tmp`, `/var/tmp`,
`$TMPDIR`, or the platform's own temp dir (`%TMP%` on Windows) after symlink
resolution. They're classified from the *same*
resolved paths the hook already extracts, so `/tmp` appearing only as text (a
grep pattern, a commit message, an `echo` string) is never matched. The deny is
the default and can be softened to `ask` or narrowed with an allowlist — see
[Configuration](#configuration). Beyond guarded-command file arguments, two more
shapes reach host temp and are covered: a **redirect** target from *any* command
(`echo secret > /tmp/out`), and **`mktemp`**, whose default location is host temp
(a bare `mktemp`, `mktemp -d`, or `mktemp -t`/`-p /tmp` all write there) — an
explicit in-workspace target (`mktemp -p ./scratch …`) is allowed like any other
in-root write.

The **ask** rows assume an interactive or `default`-mode session. In full-auto
`bypassPermissions` mode (`--dangerously-skip-permissions`) those same paths
return `deny` instead — equally blocking, with recoverable feedback for the
agent. See [Configuration](#configuration).

### Beyond Bash: native file tools

The hook is registered for Claude's native file tools as well as `Bash`, and runs
the *same* path check on them — a native tool receives a structured path
argument, so there's no command to parse, just a path to resolve and classify.

| Tool call                                         | Decision |
| ------------------------------------------------- | -------- |
| `Read` an in-repo file                            | defer    |
| `Read` the session's own task output              | defer    |
| `Read` / `Grep` / `Glob` an outside path          | **ask**  |
| `Read` / `Grep` / `Glob` under `/tmp`, `/var/tmp` | **deny** |
| `Edit` / `Write` / `MultiEdit` an in-repo file    | defer    |
| `Edit` / `Write` / `MultiEdit` an outside path    | **ask**  |
| `Write` under `/tmp`, `/var/tmp`                   | **deny** |
| `Write` into a sibling checkout (in a worktree)   | **deny** |

The read/write split matters: `Read`, `Grep`, and `Glob` get the read-only
exemptions (the session's own and sibling workers' task output, and any
[`WORKSPACE_GUARD_READ_ALLOW_PREFIXES`](#configuration)), so the agent reading
back its own output is never prompted. `Edit`, `Write`, `MultiEdit`, and
`NotebookEdit` are writes, so they also pick up the sibling-checkout deny below.

A path the hook can't resolve without a shell — one containing `$VAR` or a
`~user` prefix — **defers** on these tools, since they don't shell-expand.
`Bash` remains the only surface with full command parsing (pipelines, redirects,
`cd` tracking, variable propagation); the native handlers are a straight
path-in, decision-out check.

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
- **The `Edit`, `Write`, `MultiEdit`, and `NotebookEdit` tools** — a write into a
  sibling checkout through these tools denies too (they are guarded as writes;
  see [Beyond Bash](#beyond-bash-native-file-tools)).

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

**Turn on auto-update while you're here (recommended).** This is a third-party
git marketplace, so Claude Code won't refresh it on its own — an install pins its
version until you act (see [Upgrade](#upgrade)). Install time is the decision
point: add the marketplace to `~/.claude/settings.json` with `autoUpdate: true`
so new releases reach you automatically.

```json
"extraKnownMarketplaces": {
  "workspace-guard": {
    "source": { "source": "git", "url": "https://github.com/karlkfi/claude-workspace-guard.git" },
    "autoUpdate": true
  }
}
```

Without this you're on the manual update path documented under
[Upgrade](#upgrade).

After installing with either method:

- Requires Python 3 on your PATH. The hook is launched through
  `scripts/run-python-hook.cmd`, which resolves an interpreter by trying `py -3`,
  `python`, then `python3` (on Windows) or `python3`, then `python` (elsewhere),
  so a working Python under any of those names is enough. If none of them runs,
  the guard reports the problem on stderr rather than failing silently.
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
the repository's default branch (`main`). Claude Code auto-updates **official
Anthropic marketplaces only** — a third-party git marketplace like this one does
**not** refresh on its own, so an install pins its version until you either turn
on auto-update or update manually. (Concretely: it's easy to sit on an old
release for weeks while fixes ship — a friction-cutting parsing fix can be
invisible to anyone still pinned to the version they first installed.)

### Recommended: turn on auto-update (set-and-forget)

Add the marketplace to `~/.claude/settings.json` with `autoUpdate: true`, then
restart Claude Code:

```json
"extraKnownMarketplaces": {
  "workspace-guard": {
    "source": { "source": "git", "url": "https://github.com/karlkfi/claude-workspace-guard.git" },
    "autoUpdate": true
  }
}
```

From then on Claude Code refreshes the marketplace and updates the plugin for
you — no per-release action. This is the same block shown under
[Install](#install); set it once at install time and you're done.

### Manual update

If you'd rather update on demand, refresh the marketplace and update the plugin
yourself.

**Claude Code (CLI or IDE extension)** — run the slash commands:

```
/plugin marketplace update workspace-guard
/plugin uninstall workspace-guard@workspace-guard
/plugin install workspace-guard@workspace-guard
```

The first command re-fetches the marketplace manifest from the repo; the
reinstall picks up the new version. Refreshing the catalog alone does **not**
upgrade an already-installed plugin — hence the explicit reinstall.

**Claude Code for Claude Desktop / headless** — Claude Desktop doesn't expose the
`/plugin` slash commands, but the `claude` CLI does the same thing and shares
Desktop's plugin state, so it works there and in any headless run:

```
claude plugin marketplace update workspace-guard
claude plugin update workspace-guard@workspace-guard
```

`claude plugin update` updates in place (no uninstall/reinstall needed); it
prints "restart required to apply".

After upgrading either way:

- Run `/reload-plugins` to activate the updated hook without restarting, or
  restart Claude Code.
- Confirm the new version is live: the `/plugin` menu lists the installed
  version — compare it against the
  [latest release](https://github.com/karlkfi/claude-workspace-guard/releases).

## How it works

1. **Tokenize** the command with Python's `shlex` (POSIX mode, punctuation
   grouping) so quotes are respected and shell operators (`|`, `&&`, `>`, `;`)
   become their own tokens. Heredoc body lines (everything between a `<<TAG`
   redirection and a line matching `TAG`, `<<-` included) are dropped from the
   raw command string *before* it reaches `shlex`, so body content — HTML like
   `</div>`, a script, prose with apostrophes, an unbalanced quote — is never
   tokenized as commands or file arguments and can't abort the parse; the
   `<<TAG` operator and any trailing `> file` redirect on the command line are
   kept. Unquoted `#` comments are stripped next, keeping the newline that ends
   each one so the next line stays its own command group.
   A newline outside quotes is also a command
   separator — like `;` — so a guarded command on a line after another is
   classified on its own rather than merged into its neighbour. `shlex` returns
   a run of adjacent operators as a single token (`(cd x); …` → `);`, `((cat …`
   → `((`); each such run is split by longest match back into its individual
   operators so the command boundary isn't lost and the guarded command inside
   a subshell or group is still classified.
2. **Split** into simple commands on those operators and collect each redirect
   target (`> file`) into the command group it belongs to, so it's later
   resolved against that group's cwd (see step 6). A redirect target is a write
   the shell performs regardless of the command word, so it's checked even when
   the command itself is unguarded — `echo secret > /tmp/out` and `ls >
   /etc/out.txt` are honored on their targets. The token after `<<`
   (heredoc delimiter) or `<<<` (here-string content) is skipped — it isn't a
   path. An fd number written before a redirect (`2>file`) and an
   fd-duplication or close (`2>&1`, `2>&-`) are recognised so the digit and the
   dup target don't leak as phantom file arguments; `>&file` (a redirect to a
   file, not a dup) still has its target checked.
3. **Strip** leading shell reserved words (`until grep …`, `if cat …`, `do
   tail …`, `! grep …`, `time cat …`) and then POSIX `NAME=VALUE` command-prefix
   assignments (`LC_ALL=C cat …` → `cat …`) from each simple command, in that
   order, so neither a keyword nor an inline assignment masks the command-name
   lookup.
4. **Resolve** literal in-command variable assignments (one-pass constant
   propagation). A standalone `NAME=value` or `export NAME=value` command
   whose value survives quote removal as a plain literal — non-empty, no `$`,
   backticks, glob characters, whitespace, or `:` — is remembered, and later
   plain `$NAME`/`${NAME}` uses in the *same* command string are substituted
   before the workspace check. So `SP=./out; tail -5 $SP/run.csv` resolves
   and allows instead of prompting as runtime-expanded, and
   `f=/etc/passwd; cat $f` prompts on the *resolved* path. This evaluates
   exactly the expansion bash will perform, using only text already inside
   the command; the substituted path still goes through every step below.
   Anything uncertain — a value built from another expansion, a variable
   later touched by `read`/`eval`/`declare`/`unset`, an assignment inside a
   subshell, pipeline segment, or backgrounded command — drops the variable
   and keeps today's runtime-expanded `ask`. As a side effect, a guarded
   command reached *through* a variable (`C=cat; $C file`) is now recognised
   and guarded too.

   A `for VAR in <list>` loop is resolved the same way when every item in the
   list is a plain literal: instead of poisoning `VAR`, the hook records its
   candidate value set, and a later `$VAR` in a file argument is expanded to
   *every* candidate and checked. All candidates in-root → allow; any candidate
   outside (or host-temp) → prompt, naming the offending resolved path. Since
   bash visits every value, one outside item taints the whole loop — which is
   exactly the decision the hook reaches. A glob item (`for f in docs/*.md`) is
   recorded as the pattern itself: `*`, `?` and `[…]` never match `/`, so every
   path bash expands the pattern into resolves against the same directory as
   the pattern does — which is already how a glob written straight into a file
   argument is treated, so `cat docs/*.md` and the loop over it now agree. A
   pattern that escapes (`../*.md`, `/etc/*.conf`) resolves outside and prompts
   like any other outside path.

   A list item may also be built from an *enclosing* loop's variable, so nested
   surveys resolve too: in `for d in docs/*; do for f in "$d"/*.md`, the inner
   variable binds one candidate per (outer candidate, item) pair — here the
   single pattern `docs/*/*.md`. That cross product is what bash actually
   visits, and each pair keeps the segment structure of every path it expands
   to, so the same reasoning applies at any nesting depth. An outside outer
   candidate carries into every inner candidate built from it and prompts,
   naming the resolved path. Two limits keep the work bounded as depth
   multiplies: a variable is poisoned rather than bound when its candidate set
   would exceed 256 values, and a file argument naming several loop variables
   keeps its runtime-expanded prompt when their cross product would. Both
   poison rather than truncate — a checked prefix would say nothing about the
   candidates past it.

   Lists with a non-literal item (a `$` the hook can't resolve, command
   substitution, or brace like `{a,b}` — brace-expanded by bash, so the literal
   string isn't a stand-in for the real paths), the `for VAR; do …` ("$@")
   form, the `for ((…))` arithmetic form, and a loop variable reassigned in the
   body all keep today's poison behavior.
5. **Classify** each token using a per-command spec table that knows which flags
   take values (`grep -e PAT`), which flag-values are themselves files
   (`grep -f`, `jq --slurpfile`), and how many leading positionals are the
   program/pattern to skip. `dd` is handled separately because its operands are
   all `KEY=VALUE` pairs — `if=PATH` and `of=PATH` are the file operands; the
   rest (`bs=`, `count=`, `conv=`, `iflag=`, `oflag=`, …) are values, not paths.
   `mktemp` is handled separately too, because its *default* location is host
   temp: a bare `mktemp`, `mktemp -d`, or `mktemp -t`/bare `--tmpdir` resolves to
   `$TMPDIR`/`/tmp`, while `-p DIR`/`--tmpdir=DIR` and a slashed template
   (`mktemp /tmp/x.XXXX`, `./x.XXXX`) name their own location. Short flags may be
   clustered — `-dp DIR` is decoded as `-d -p DIR`. The GNU/BSD `-t` arity split
   is sidestepped — `-t` lands in host temp on both. A literal inline
   `TMPDIR=<dir>` prefix relocates that default location, so `TMPDIR=./scratch
   mktemp` resolves in-workspace (allow); an explicit `-p`/`--tmpdir=` still wins
   over it, and a `$`-bearing value degrades to the host-temp default.
6. **Track** cwd shifts across the chain. A `cd`/`pushd` in an earlier group
   re-roots relative file paths — including relative redirect targets — in
   later guarded groups (so `cd /etc && cat passwd` flags `passwd` as
   `/etc/passwd`, and `cd /tmp && cat in.txt > evil` flags `evil` as
   `/tmp/evil`). A `cd`/`pushd` target that is a propagated literal variable
   (step 4) re-roots the same way (`d=sub; cd $d && cat x.txt`). Two pure,
   deterministic command substitutions are also recognised as `cd`/`pushd`
   targets and resolved from the tracked cwd instead of dropping tracking:
   `$(git rev-parse --show-toplevel)` (computed by walking up to the nearest
   `.git` entry — git is never executed) and `$(pwd)` (the identity). The
   whitelist is closed and matched on the exact whitespace-normalized token —
   there is no general `$( )` evaluation. When the new cwd can't be resolved at
   hook time — bare `cd`, `cd -`, `cd $HOME`, `popd`, any other substitution —
   later relative paths short-circuit to `ask`.
7. **Stage** symlinks *and* hard links created by an earlier `ln OUTSIDE LINK`
   in the chain (with or without `-s`). `LINK`'s resolved path is recorded so
   a later `cat LINK` is flagged — bash hasn't materialised the link yet at
   hook time, so a naive `realpath` would otherwise place `LINK` lexically
   inside the workspace and let it through.
8. **Resolve** every file argument against `$CLAUDE_PROJECT_DIR` with
   `realpath`, collapsing `../` and following symlinks. Anything that resolves
   outside the root yields `ask`; otherwise `allow`. A leading `~` or `~/…` is
   expanded to your home directory first (bash does this deterministically), so
   a home path inside the root is allowed instead of needlessly prompted. The
   home comes from the same lookup Claude Code itself uses, not from `$HOME`,
   which is unset on Windows. Tokens that bash would still expand unpredictably
   at runtime — `~user`/`~+`/`~-`, or a `$` that introduces an expansion
   (`$VAR`, `${VAR}`, `$(...)`, `$1`, `$?`) — short-circuit to
   `ask`, since `realpath` would otherwise lexically place them inside `cwd`.
   The two whitelisted pure substitutions from step 6
   (`$(git rev-parse --show-toplevel)`, `$(pwd)`) are the exception: when one
   *leads* a file operand or redirect target it is resolved against the tracked
   cwd first — the same value bash computes — and the remainder is concatenated
   verbatim (bash inserts no separator), so `cp x "$(git rev-parse --show-toplevel)/backup/"`
   classifies the real in-repo destination instead of asking. Any `$`/`~` left
   in the remainder, a non-whitelisted substitution, or an untracked cwd keeps
   the `ask`.
   A `$` bash keeps literal — trailing (`foo$`) or before a non-name char
   (`a$.b`) — is treated as part of the filename and resolved normally.
   Well-known
   device paths (`/dev/null`, `/dev/stdin`, `/dev/stdout`, `/dev/stderr`,
   `/dev/zero`, `/dev/tty`, `/dev/random`, `/dev/urandom`, `/dev/fd/N`) are
   allowlisted and skip the workspace check.
9. **Allow** the current session's own Claude-managed scratch — and, for
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

   `/tmp/claude-<uid>` is the POSIX root; on Windows there is no per-UID suffix
   because the temp dir is already per-user, so the root is `%LOCALAPPDATA%\Temp\claude`.

   Writing into another session's scratch, and any access to a *different
   project's* scratch, still prompt. Because these allows match on the resolved
   `realpath` — and run *after* the `ln`-staging check — a symlink planted in
   the scratch dir that escapes the root is still flagged.
10. **Allow reads of Claude-owned project data.** For read-only commands (`cat`,
   `head`, `tail`, `grep`, `rg`, `sed`, `awk`, `jq`, `yq`, `diff`, `sort`,
   `wc`, `file`, `hexdump`, and their aliases), a path whose resolved
   `realpath` is under `~/.claude/projects/` is allowed silently. That
   directory is written exclusively by the Claude Code harness (session
   metadata, sub-agent data, workflow journals) and reading it back is not
   the boundary this hook guards. Write commands (`cp`, `mv`, `tee`, `rm`)
   are **not** exempt — they must still pass the workspace check — and
   neither is a read command invoked with a write-mode flag (`sed -i` /
   `--in-place`, gawk `-i` / `--include`, `yq -i` / `--inplace`, `sort -o`
   / `--output`): any of these flips the whole invocation into write mode.
   The second positional of `uniq IN OUT` / `xxd IN OUT` is an output file
   and is checked as a write (the `IN` operand keeps the exemption).
   The exemption also does not apply to redirect targets, since the hook
   cannot verify redirect direction without running the command. Users can
   extend the list with `WORKSPACE_GUARD_READ_ALLOW_PREFIXES`; see
   [Configuration](#configuration).
11. **Deny** host-wide temp. After the steps above, any *remaining*
   outside-workspace file argument whose resolved `realpath` is at or under a
   host-temp root (`/tmp`, `/var/tmp`, `$TMPDIR`, and the platform's own temp
   dir — `%TMP%` on Windows, which the POSIX names don't cover — all resolved
   first, so macOS's `/tmp → /private/tmp` and a `$TMPDIR` under
   `/var/folders/…` are caught) is
   reclassified from `ask` to `deny`, with a message steering to a repo-local
   gitignored scratch dir. Because this runs on the already-resolved file
   arguments, a `/tmp` that appears only as text (a grep pattern, an `echo`
   string) is never matched. The Claude-managed temp root from step 9 is
   excluded — another session's task output keeps its `ask` (or, for a
   same-project read, the step 9 allow) rather than this steer-to-`./tmp/` deny.
   The action, scratch-dir name, extra roots,
   and an allowlist escape hatch are all configurable; see
   [Configuration](#configuration).
12. **Deny** writes into a sibling checkout of the same repo. When the session
   root is inside a git worktree, the hook resolves the enclosing git checkout of
   each *write* path (walking up to the nearest `.git`, reading only tiny git
   metadata) and compares its shared `--git-common-dir` to the session's. A path
   inside a *different* checkout of the *same* repo (same common-dir, different
   root) is reclassified to `deny`, naming the checkout, its branch, and the
   corrected in-session path. Only writes upgrade — reads keep step 8's `ask`.
   A path in an unrelated repo has a different common-dir and stays a generic
   outside `ask`. The same rule is the sole active check on the `Edit`, `Write`,
   `MultiEdit`, and `NotebookEdit` tools. `WORKSPACE_GUARD_OVERRIDE=<reason>`
   downgrades it to `ask`; see [Configuration](#configuration).
13. **Recurse into command substitutions.** A guarded command hidden in a
   `"$(…)"` or backtick `` `…` `` substitution — `echo "$(mktemp -p /tmp x)"`,
   `` x=`grep secret /etc/passwd` `` — isn't tokenized as its own command by the
   step-1 lexer (the metacharacters are inside quotes), so its file ops would be
   invisible. The hook scans the *raw* command for substitution bodies in
   unquoted or double-quoted context (single-quoted `'$(…)'` is a bash literal
   and is skipped; `$((…))` arithmetic has no command) and runs each body back
   through steps 1–13. Only *offenders* bubble up: a clean guarded command inside
   a substitution never turns a deferring outer command into an `allow` — this
   step can only add friction. (The bare unquoted `$(…)` form was already caught,
   because its `(`/`)` split the inner command into its own group.)

## Agent guidance: avoiding prompts

When the hook prompts, its reason now tells the agent how to avoid the prompt
next time — naming the offending path and the fix (use an in-root path, drop a
`$VAR`/`~`, or — for the false-positive categories — switch to the Read/Grep
tools). But some habits avoid prompts entirely, and the hook can't surface
them because it *allows* those paths silently — there's no prompt on which to
attach advice.

Paste the block below into your project's `CLAUDE.md` (or `AGENTS.md`) so the
agent follows them from the start. They're framed as instructions to the agent:

```markdown
## Avoiding workspace-guard permission prompts

This repo uses workspace-guard, a hook that prompts before a guarded bash file
command (`grep`, `sed`, `awk`, `jq`, `cat`, `head`, `tail`, `cp`, `mv`, `rm`,
`tee`, `dd`, …) reads or writes a path outside the project root. To keep work
flowing, avoid triggering it:

- **Prefer the Read, Grep, and Glob tools over bash** `cat`/`grep`/`sed`/`head`/
  `tail`/`awk` for inspecting files. They're purpose-built for reading and
  searching code, and their literal single-path inputs can't trigger the
  `$VAR`/`$(...)`, `cd`-tracking, or heredoc false positives. They are still
  guarded: a genuinely outside-workspace read prompts either way — that prompt
  is the boundary working as intended, so approve it rather than working
  around it.
- **Keep guarded file arguments inside the project root.** A path that resolves
  outside the root (including via `../` traversal) prompts every time.
- **Don't put `$VAR`, `$(...)`, or a `~user` prefix in a guarded file argument.**
  The hook can't expand them, so it treats them as outside the root and prompts —
  even when they'd resolve in-root. (A bare `~`/`~/…` *is* expanded to your home
  directory, so home-relative paths inside the root are fine. A variable assigned
  a plain literal path *earlier in the same command string* — `f=./config/app.json; cat $f`
  — is also resolved and doesn't prompt, as is a `for f in a b c` loop over a
  literal list, a `for f in docs/*.md` loop over an in-root glob, and a nested
  loop whose inner list is built from the outer variable
  (`for d in docs/*; do for f in "$d"/*.md`). A file operand or
  redirect target that *begins* with `$(git rev-parse --show-toplevel)` or
  `$(pwd)` — the same two whitelisted substitutions the `cd` tracker resolves —
  is resolved too, so `cp x "$(git rev-parse --show-toplevel)/backup/"` is fine.)
  Otherwise write the literal in-root path (e.g. `cat ./config/app.json`, not
  `cat "$HOME/proj/config/app.json"`).
- **Give `cd` a literal target** — bare `cd`, `cd -`, `cd $HOME`/`cd $VAR`,
  `popd`, and any unrecognized `$(...)` target lose the hook's
  working-directory tracking, so every later relative path in the same
  command prompts as unresolvable.
  (`cd "$(git rev-parse --show-toplevel)"` and `cd "$(pwd)"` are fine — the
  hook resolves these two substitutions itself and tracking survives.)
  A literal `cd` keeps tracking even when it leaves the root: later relative
  paths then resolve against the new directory and prompt as
  outside-workspace, naming the absolute path they land on — deliberate
  prompts, not lost tracking. Stay inside the root unless you mean to work
  outside it.
- **Write temp files inside the project root, not `/tmp`.** Host-wide temp
  (`/tmp`, `/var/tmp`, `$TMPDIR`, and `%TMP%` on Windows) is **denied** by
  default — not just prompted —
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

For the "just show me the numbers" case, the **`/workspace-guard:friction-report`**
slash command runs that analyzer directly and prints the ranked report — no
diagnosis, no fixes. The `workspace-guard:` prefix disambiguates it from the
identically-named commands shipped by the companion guards (prod-guard,
foreground-guard); if none of those are installed, the bare `/friction-report`
also resolves. It passes its arguments straight through to the script, so the
same flags work:

```
/workspace-guard:friction-report                  # last 7 days, this project
/workspace-guard:friction-report --since 24h --repo gateway
/workspace-guard:friction-report --raw --top 20
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
| `WORKSPACE_GUARD_TMP_ROOTS` | (empty) | Extra host-temp roots, separated by the platform list separator (`:` on POSIX, `;` on Windows) or a comma. **Additive** — it extends the built-in `/tmp`, `/var/tmp`, `$TMPDIR`, and the platform temp dir (`%TMP%` on Windows); it can't shrink them. |
| `WORKSPACE_GUARD_TMP_ALLOW` | (empty) | Allowlist of exact-prefix or glob paths (same separators) that **escape** the deny — for the rare tool that genuinely needs `/tmp`. |

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
`mv`, `tee`, `rm`), redirect targets, read commands carrying a
write-mode flag (`sed -i`, gawk `-i inplace`, `yq -i`, `sort -o`), and the
positional output file of `uniq IN OUT` / `xxd IN OUT` are never exempt.

The built-in default is `~/.claude/projects/` (Claude Code's own session and
sub-agent data). You can extend it with additional prefixes:

| Env var | Default | Effect |
| --- | --- | --- |
| `WORKSPACE_GUARD_READ_ALLOW_PREFIXES` | (empty) | Extra read-exempt prefixes, separated by the platform list separator (`:` on POSIX, `;` on Windows) or a comma. **Additive** — it extends the built-in list. |

Each entry is run through `realpath` so platform symlinks resolve correctly.
Scope entries tightly: anything under a configured prefix is silently allowed
for read commands without a confirmation prompt.

A relative entry — including a Windows path written `/tmp/x` with no drive
letter, which names a directory on whichever drive is current — is resolved
against the tool's working directory, the same base the command's own file
arguments resolve against. This applies to every configured path: the host-temp
roots and allowlist above, and these prefixes.

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

- A leading `~`/`~/…` is expanded to your home directory (bash does this
  deterministically), so a home path inside the root is allowed. Tokens that
  bash would expand *unpredictably* at runtime — `~user`/`~+`/`~-`, or a `$`
  that introduces an expansion (`$VAR`, `${VAR}`, `$(...)`, `$1`, `$?`) — are
  still treated as outside-workspace. A `$` bash keeps literal (trailing, or
  before a non-name char like `.`/`/`) is part of the filename and resolved
  normally, so a `price$` or `a$.b` argument no longer prompts spuriously.
- Heredoc body lines are dropped from the raw command string before parsing, so
  path-like body content (`</div>`, `/title`), prose, or an unbalanced quote in
  the body is never mistaken for a file argument and never aborts the parse (a
  real outside-workspace redirect on the `<<TAG` command line is still checked).
  The terminator is matched by an exact line (`<<-` allows leading tabs); an
  unterminated body swallows to the end of the command (matching bash). A `<<`
  inside quotes, inside a `#` comment, or produced by arithmetic (`$((x<<2))`,
  `((x<<2))`) is not a heredoc and never arms a delimiter. Command
  substitutions *inside* a heredoc body are still analyzed even when a quoted
  delimiter (`<<'EOF'`) would stop bash expanding them — a conservative extra
  `ask`, never a missed one.
- Literal variable propagation is deliberately narrow. Only standalone
  `NAME=value` / `export NAME=value` assignments whose value is a plain
  literal after quote removal (non-empty; no `$`, backticks, glob characters,
  whitespace, or `:`) are propagated, and only into plain `$NAME`/`${NAME}`
  uses. On Windows a leading drive prefix (`C:\`, `c:/`) is exempt from the `:`
  rule — otherwise every absolute path there is impure and propagation never
  fires; a second `:` anywhere in the value still rejects it, and on
  Linux/macOS `C:/x` is just a directory named `C:` so the rule is unchanged.
  Parameter-expansion operators (`${f:-x}`, `${f%.*}`), arrays, values
  built from other expansions, and variables later touched by
  `read`/`eval`/`declare`/`unset` or assigned inside a subshell,
  pipeline segment, or backgrounded command all keep the runtime-expanded
  `ask`. A heredoc (`<<`) anywhere in the command, or anything that may set
  `IFS`, disables propagation for that command entirely (conservative — a
  heredoc adds redirection state, and a changed `IFS` re-splits every later
  expansion, so a value checked as one word can reach the command as several).
  Setting `IFS` counts whether it is a plain `IFS=`/`export IFS=` assignment,
  an assigning builtin naming it (`declare`, `local`, `typeset`, `readonly`,
  `read`, `printf -v`, `for IFS in …`), or an `eval`/`source` that could set it
  out of sight. Only `unset IFS` is exempt: bash falls back to the default
  splitting the guard already assumes.
  Uncertainty always falls back to `ask` — propagation only ever adds allows for
  expansions bash performs deterministically.
- `for VAR in <list>` loop resolution is equally narrow. The candidate set is
  recorded only when *every* list item is a plain literal or a glob (the
  assignment purity test minus the glob metacharacters, plus a brace `{a,b}` is
  rejected because a for-list item — unlike an assignment RHS — is
  brace-expanded). A later `$VAR` in a file argument is then checked against all
  candidates; one outside item taints the loop. Lists with any non-literal item,
  the `for VAR; do …` ("$@") form, the `for ((…))` arithmetic form, and a loop
  variable reassigned inside the body all keep today's behavior. As with
  assignments, this only ever adds allows for the exact values bash iterates.
- A glob item stands for its whole expansion rather than being enumerated, so a
  loop over one is exactly as strong as the same glob written into a file
  argument directly — no more and no less. In particular, `realpath` can't see
  through a *matched* name that is itself a symlink out of the workspace, since
  the pattern is resolved instead of the files: `for f in docs/*.md; do cat
  "$f"; done` treats a `docs/link.md` → `/etc/passwd` symlink the same way
  `cat docs/*.md` already does. Under `shopt -s globstar` a `**` matches a
  *variable* number of path segments, including zero — `docs/**` expands to
  `docs/` as well as `docs/sub/b.md` — so a trailing `../` in the loop body can
  climb one level higher at runtime than the pattern shows, and a read just
  outside the root can be allowed. Globstar is off by default in bash; with it
  on, prefer an explicit pattern over `**` in a loop list.
- A nested loop's list may be built from the outer loop's variable
  (`for d in docs/*; do for f in "$d"/*.md`), and the inner variable binds the
  cross product. A candidate set larger than 256 values poisons the variable
  instead, so a loop over very long literal lists keeps today's prompt. The
  same 256 cap applies to a file argument that names several loop variables at
  once: under three nested loops of 256 literals, `cat $a/$b/$c` stands for
  16.7 million paths, so it keeps the runtime-expanded prompt rather than being
  enumerated. Enumerating it ran the hook past two minutes — and because Claude
  Code treats a failed `PreToolUse` hook as a non-blocking error, a guard that
  never answers enforces nothing at all.
- `realpath` only follows symlinks for files that already exist; nonexistent
  paths are normalized lexically (fine for read-style commands).
- Redirect targets (`> file`) are inspected on *every* command, guarded or not —
  a redirect is a write the shell performs regardless of the command word, so
  `echo secret > /tmp/out` (host temp → deny) and `ls > /etc/out.txt` (outside →
  ask) are both honored on their targets. The target is resolved against the cwd
  of the command group it appears in, so a relative target tracks `cd`-shifts the
  same way file arguments do (`cd /tmp && cat in.txt > evil`, and equally
  `cd /tmp && echo x > out.txt`, flag `/tmp/…`). A redirect target that is
  in-workspace, or a *positional* argument of an unguarded command (`ls /etc`),
  still defers — only the redirect write itself is added to the check.
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
  undocumented Claude Code convention inferred from the UID (on Windows, from
  the per-user temp dir instead — there is no UID); if Claude Code
  relocates the dir, these paths simply revert to `ask` (fail-safe — the allow
  never widens the boundary). A session with no `session_id` (older CLIs)
  disables both allows entirely.
- In non-interactive / headless runs there is no one to answer an `ask` prompt,
  so an `ask` still **blocks** the command (verified on CLI 2.1.159 — it does
  not silently allow). Under `--dangerously-skip-permissions`
  (`bypassPermissions`) the hook emits `deny` rather than `ask` for
  outside-workspace paths: equally blocking, but the agent receives the reason
  and can recover instead of stalling. See [Configuration](#configuration).
- The host-temp `deny` covers guarded-command file arguments, redirect targets
  from any command (`go test > /tmp/log`), a `cd` into host temp followed by a
  relative write, `mktemp` (its default location is host temp), and — as of the
  substitution recursion (step 13) — guarded commands inside `"$(…)"`/backtick
  bodies (`echo "$(mktemp -d)"`, `` x=`mktemp` ``). A literal inline
  `TMPDIR=<dir>` prefix is honored for `mktemp`'s default location
  (`TMPDIR=./scratch mktemp` → allow; a `$`-bearing value can't be trusted and
  degrades to the host-temp default), and combined `mktemp` short flags
  (`-dp DIR`) are decoded precisely (`-d -p DIR`). One shape remains out of
  scope and still defers: an inline `TMPDIR=/tmp cmd` that only redirects a
  *non-`mktemp` tool's* own internal temp location (not a path the hook parses).
- Command-substitution recursion (step 13) scans the raw command for
  quote-context fidelity, so single-quoted `'$(…)'` is correctly skipped. Its
  edges degrade to *defer* (never a silent allow): a substitution body resolves
  relative paths against the command's starting cwd, not a cwd set by an earlier
  in-chain `cd` (`cd /x && echo "$(cat f)"` judges `f` against the start cwd); a
  body that itself contains a quoted operator character (`echo "$(grep ")" f)"`)
  can mis-tokenize on re-parse and defer; and backtick nesting via `` \` `` or a
  no-space `$((…))`-shaped subshell isn't decoded. Process substitution
  `<(…)`/`>(…)` is only ever unquoted and is already caught by the subshell
  split.
- The sibling-checkout `deny` classifies *write-context* file arguments — the
  same set the read-prefix exemption treats as writes: redirect targets, `dd`
  operands, every operand of `cp`/`mv`/`tee`/`rm`, every operand of a read
  command carrying a write-mode flag (`sed -i`, gawk `-i inplace`, `yq -i`,
  `sort -o`), and the positional output file of `uniq IN OUT` / `xxd IN OUT`.
  So a `cp` **source** or a
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
