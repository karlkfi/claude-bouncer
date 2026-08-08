# workspace-guard

**Workspace-boundary shell permissions for Claude Code.**

[![release](https://img.shields.io/github/v/release/karlkfi/claude-workspace-guard)](https://github.com/karlkfi/claude-workspace-guard/releases) [![tests](https://img.shields.io/github/actions/workflow/status/karlkfi/claude-workspace-guard/tests.yml?branch=main&label=tests)](https://github.com/karlkfi/claude-workspace-guard/actions/workflows/tests.yml) [![License: MIT](https://img.shields.io/github/license/karlkfi/claude-workspace-guard.svg)](LICENSE) [![Claude Code plugin](https://img.shields.io/badge/Claude_Code-plugin-7e57c2)](#install)

> Stop approving every in-repo grep. Start catching the one that reads `/etc/passwd`.

You ask Claude to "find that auth error." It runs `grep -r token /var/log`. Or
`cat ~/.aws/credentials` while "checking the environment." Or pipes a file from
outside your repo into `jq`. The default `Bash(grep:*)` permission rules can't
tell these apart from the dozens of in-repo greps Claude runs every session —
they either trust every invocation or prompt on every one.

workspace-guard is a `PreToolUse` hook for the shell tools (`Bash` and
`PowerShell`) — and for Claude's native file tools (`Read`, `Grep`, `Glob`,
`Edit`, `Write`, …) — that parses the command, finds its file arguments, and
asks for confirmation only when a path resolves outside your project root
(`$CLAUDE_PROJECT_DIR`). In-repo reads and pure pipelines run silently. A path is
the main way out of that boundary but not the only one, so the same rule covers
`pkill` and `Stop-Process`, which reach another checkout's processes by pattern
instead.

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
  the same repo** when the session runs in a git worktree, and for a
  **process kill that names no path in this workspace** — whether the pattern is
  the kill's own (`pkill -f`) or reached it through a `pgrep` or a `ps` pipeline
  (all below). Configurable down to `ask`; see [Configuration](#configuration).
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

Process kills are guarded too, though they touch no file: `pkill`/`killall`,
PowerShell's `Stop-Process`, and Windows' `taskkill` signal a process by
*pattern* or by *name*, which reaches every checkout on the host — and so does a
`kill` fed pids by a `pgrep` or by a `ps` pipeline, whatever it filters with. See
[Unanchored process-kill deny](#unanchored-process-kill-deny).

The same outside-workspace check also runs on Claude's **native file tools**, so
the guard can't be sidestepped by switching from a bash command to the
equivalent tool — `Read`-ing `/etc/passwd` prompts exactly like `cat /etc/passwd`
would. `Read`, `Grep`, and `Glob` are treated as reads (they keep the self-read
exemptions below); `Edit`, `Write`, `MultiEdit`, and `NotebookEdit` are treated
as writes. See [Beyond Bash](#beyond-bash-native-file-tools).

It also stays quiet for paths that aren't really "outside your project":
`/dev/null` and friends, and the session's **own** Claude-managed scratch tree
under `/tmp/claude-<uid>/…/<session>/…` — both the background-task output the
agent polls with `cat`/`tail`/`grep` and the `scratchpad/` dir Claude Code
points the session at for temp files. That whole tree is exempt for **reads and
writes alike**: Claude Code chose the path and cleans it up, so a throwaway that
shouldn't outlive the session belongs there. Sessions that spawn and manage
background work therefore aren't spammed with prompts for reading their own
output — in real usage that one case accounted for ~37% of all prompts.
Read-only commands may also poll a **sibling** session's output under the same
project's scratch dir — the dispatcher-tails-workers pattern of parallel
dispatch. Writing into another session's scratch still asks, and a different
project's scratch still asks entirely.

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
| `cat > doc.md <<'EOF'` … `$(cat /etc/x)` … `EOF` (literal body) | allow |
| `cat ~/proj/notes.md` (root `~/proj`) | allow   |
| `cat /c/proj/notes.md` (Git Bash, root `C:\proj`) | allow |
| `cd "$(git rev-parse --show-toplevel)" && cat README.md` | allow |
| `cd "$(pwd)" && cat README.md`       | allow    |
| `tail /tmp/claude-501/…/<this-session>/…` (own task output) | allow |
| `echo x > /tmp/claude-501/…/<this-session>/scratchpad/f` (own scratch write) | allow |
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
| `cat > doc.md <<EOF` … `$(cat /etc/x)` … `EOF` (expanded body) | **ask** |
| `cat > doc.md <<EOF` … `don't` … `$(cat /etc/x)` … `EOF` (apostrophe first) | **ask** |
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
| `pkill -f ginkgo` · `pkill -f "make check"` | **deny** |
| `pkill -f "<sibling-worktree>/bin/x"` | **deny** |
| `pkill -u karl` (no pattern at all)  | **deny** |
| `killall node`                       | **deny** |
| `pkill -f "<this-root>/.build/ginkgo"` (anchored) | defer |
| `kill $(pgrep -f ginkgo)` · `pgrep -f ginkgo \| xargs -r kill` | **deny** |
| `ps -eo pid,command \| grep ginkgo \| awk '{print $1}' \| xargs kill` | **deny** |
| `ps -eo pid,command \| awk '/ginkgo/ {print $1}' \| xargs kill` | **deny** |
| `ps -eo pid= \| xargs kill` (no filter at all) | **deny** |
| `kill $(ps -eo pid= \| head -1)`     | **deny** |
| `for p in $(pgrep -f ginkgo); do kill $p; done` | **deny** |
| `kill -0 -s 9 $(pgrep -f ginkgo)` (`-s 9` overrides the `-0`) | **deny** |
| `pgrep -f ginkgo \| xargs -0 kill` (`-0` is xargs' NUL flag) | **deny** |
| `pgrep -f "<this-root>/bin/x" \| xargs -r kill` (anchored) | defer |
| `taskkill //IM node.exe` · `taskkill //FI "IMAGENAME eq node.exe"` | **deny** |
| `taskkill` (no selector at all)      | **deny** |
| `taskkill //PID 1234` · `taskkill /?` | defer   |
| `ps aux \| grep "<this-root>/bin/x" \| awk '{print $1}' \| xargs kill` | defer |
| `kill 1234` · `kill -0 1234` · `kill $pid` | defer |
| `while kill -0 $(pgrep -f ginkgo)` (sends no signal) | defer |
| `./run.sh & p=$!; kill $p; ps -p $p` (ps consumes a pid) | defer |
| `pgrep -fl ginkgo` · `pgrep -f ginkgo; kill 1234` | defer |
| `cat in.txt && kill 1234` (clean read, but signals) | defer |
| `sh -c 'cat /etc/hosts'` · `timeout 5 bash -c 'cat /etc/hosts'` | **ask** |
| `sh -c 'pkill -f ginkgo'` (kill inside a body) | **deny** |
| `docker exec c sh -c 'cat /var/lib/x'` · `ssh h sh -c '…'` | defer |
| `cat in.txt; sh -c 'cat in.txt'` (clean body, still no vouch) | defer |
| `ps aux \| grep ginkgo` (no kill in the string) | allow |
| `cat in.txt; bash --version` (shell, no `-c` body) | allow |
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

### The PowerShell tool

Claude Code ships two shell tools, and a Windows session gets `PowerShell`
rather than `Bash` whenever Git for Windows isn't installed — as does any
session with `CLAUDE_CODE_USE_POWERSHELL_TOOL=1`. Both are hooked, but
PowerShell gets its own parser and its own table. It isn't a POSIX shell: its
escape character is a backtick, so a native path run through the POSIX tokenizer
comes out as `C:Usersbobx` — a name that resolves *inside* the project root.
Reads and writes are cmdlets with their own parameter grammar, and several of
their aliases (`cat`, `rm`, `cp`, `mv`, `tee`, `sc`) collide with POSIX command
names that take entirely different flags.

Guarded cmdlets: `Get-Content`, `Select-String`, `Import-Csv`, `Import-Clixml`,
`Set-Content`, `Add-Content`, `Out-File`, `Tee-Object`, `Export-Csv`,
`Export-Clixml`, `Copy-Item`, `Move-Item`, `Remove-Item`, `Rename-Item`, and
their aliases. Output redirects (`>`, `>>`, `2>`) are checked on any command.
`Stop-Process` (and `kill`, `spps`) is guarded as well, as a process kill, as is
Windows' own `taskkill`.

| Command                                        | Decision |
| ---------------------------------------------- | -------- |
| `Get-Content .\src.txt`                        | allow    |
| `Select-String foo .\notes.md`                 | allow    |
| `Set-Content .\out.txt "hi"`                   | allow    |
| `Set-Location docs; Get-Content note.md`       | allow    |
| `Set-Content out.txt @'` … `'@` (literal body) | allow    |
| `Get-Content C:\Users\bob\.aws\credentials`    | **ask**  |
| `Get-Content -LiteralPath C:\out\x`            | **ask**  |
| `Set-Content -Encoding UTF8 C:\out\x "hi"`     | **ask**  |
| `Out-File -FilePath C:\out\x`                  | **ask**  |
| `Copy-Item .\in.txt C:\out\x`                  | **ask**  |
| `Get-Content -Path in.txt,C:\out\x` (array)    | **ask**  |
| `Set-Location C:\out; Get-Content secret.txt`  | **ask**  |
| `Write-Output hi > C:\out\x` (redirect)        | **ask**  |
| `Write-Output "$(Get-Content C:\out\x)"`       | **ask**  |
| `Get-Content $env:USERPROFILE\x`               | **ask**  |
| `Stop-Process -Name node`                      | **deny** |
| `Get-Process node \| Stop-Process`              | **deny** |
| `Stop-Process -Id $p.Id`                       | **deny** |
| `Get-Process \| Where-Object { $_.Path -like '<this-root>\*' } \| Stop-Process` | defer |
| `taskkill /IM node.exe`                        | **deny** |
| `Stop-Process -Id 1234` · `taskkill /PID 1234` | defer    |
| `Get-Content .\in.txt; Stop-Process -Id 1234`  | defer    |
| `Get-Content .\in.txt; taskkill /PID 1234`     | defer    |
| `Get-ChildItem C:\out` (not a guarded cmdlet)  | defer    |
| `Get-Content "unterminated` (unparseable)      | defer    |

Parameters bind the way PowerShell binds them: by name first — including
unambiguous prefixes (`-Pat` is `-Path`) and the colon form (`-Path:C:\x`) —
and only then into whatever positional slots are left, so
`Select-String -Pattern foo C:\x` puts the file in `-Path` however the two are
ordered. `Set-Location` and `Push-Location` are followed so a later relative
operand resolves against the right directory; anything the hook can't follow
(a bare `cd`, a `$var` target, `Pop-Location`) drops tracking and prompts on
relative operands rather than guessing.

`Stop-Process` and `taskkill` are guarded as process kills rather than as file
operations — see [Unanchored process-kill deny](#unanchored-process-kill-deny)
for the rule and the rewrites each deny recommends.

A cmdlet that isn't in the table is **not checked**, and neither is a .NET call
or a native `.exe`. See [Limitations](#limitations) for why that's the posture
and what it costs.

### Beyond Bash: native file tools

The hook is registered for Claude's native file tools as well as the shell
tools, and runs the *same* path check on them — a native tool receives a
structured path argument, so there's no command to parse, just a path to resolve
and classify.

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
`~user` prefix — **defers** on these tools, since they don't shell-expand. Full
command parsing (pipelines, redirects, location tracking) belongs to the shell
tools; the native handlers are a straight path-in, decision-out check.

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

### Unanchored process-kill deny

Every check above is about *paths*. `pkill -f` addresses a process by **pattern**
instead, which is a write to another session's work through the one door the
workspace boundary can't see:

```
pkill -f "make check"      # every checkout on this host running make check
pkill -f ginkgo            # every checkout on this host running ginkgo
```

Run several worktrees of one repo in parallel and a bare-program pattern reaches
all of them. Measured across one developer's session transcripts: 38 `pkill`
targets, 2 of which carried anything identifying the worktree that started them.
One of the other 36 did the damage — a load harness cleaned up with
`pkill -f 'make scripts-test'` killed the verification run in its own worktree
and recorded a passing suite as a failure.

So `pkill` and `killall` are **denied** unless some operand **anchors** the
pattern to this workspace — the project root's directory name appearing as a
whole path component, with a path separator on at least one side:

```
pkill -f "issue-125-a1b2/.build/ginkgo"   # anchored -> defer
pkill -f "/home/k/ws/repo/bin/server"     # anchored -> defer
pkill -f ginkgo                           # -> deny
pkill -f issue-125-a1b2                   # -> deny (bare word, no separator)
killall node                              # -> deny (a process name can't anchor)
pkill -u karl                             # -> deny (no pattern at all)
```

A bare word doesn't count however distinctive it looks: the pattern is a
substring match against a command line, not a path, and the hook can't judge
whether a given word excludes a sibling. Component bounds treat `-`, `.` and `_`
as name characters, so a root named `repo` does not anchor inside
`repo-branch1`. A token still carrying an unresolved expansion never anchors
either — bash decides at runtime where `$HOME/repo/bin` lands, so the `/repo/` in
it proves nothing.

**Deny rather than ask**, because 36 of those 38 kills would have raised a
prompt: an `ask` on nearly every kill trains reflexive approval, which is the
failure it exists to prevent. The two rewrites the message names cost nothing in
the normal case — run `pgrep -fl <pattern>` and kill the pid(s) you meant, or put
the workspace path in the pattern. For a deliberate cross-workspace kill,
`WORKSPACE_GUARD_OVERRIDE=<reason>` downgrades it to `ask`.

An **anchored** kill *defers* rather than emitting `allow`: it's out of this
hook's scope, and an `allow` would short-circuit your own permission settings on
a destructive command. `kill <pid>` is untouched — killing by pid is the rewrite
the deny recommends, not a hazard.

#### Kills fed by a pattern, not by a name

The same blind kill dodges a rule that keys on the *command* by deriving pids
from a pattern instead:

```
kill $(pgrep -f ginkgo)
pgrep -f ginkgo | xargs -r kill
ps -eo pid,command | grep ginkgo | grep -v grep | awk '{print $1}' | xargs -r kill
for p in $(pgrep -f ginkgo); do kill $p; done
```

Every one of these kills exactly what `pkill -f ginkgo` kills. The third was the
worst case: `grep` and `awk` are clean guarded commands, so the hook used to emit
its blanket `allow` for the whole string *including the kill* — actively
green-lighting it rather than merely missing it.

Two rules close that. First, **a clean guarded command never speaks for a kill**:
if anything in the command string signals a process, the hook emits nothing
instead of `allow`, so your own permission settings still get their say. Second,
the **source that produced the pids is checked as if its pattern were the kill's
own** — and a launderable kill whose sources all fail the anchor test is denied
with the same message and the same override.

What counts as *launderable* is the narrow part: a `kill` whose operands are all
literal pids or job specs was demonstrably not fed by a pattern, so it stays out
of the rule even when it shares a command string with a `pgrep`.

```
pgrep -f ginkgo | xargs -r kill              # -> deny
pgrep -f "<this-root>/bin/x" | xargs kill    # anchored -> defer
pgrep -f ginkgo; kill 1234                   # literal pid -> defer
kill $pid                                    # no pattern in the string -> defer
```

A **`kill -0` sends no signal** — it's the liveness probe behind a wait loop — so
where its pids came from doesn't matter either. Both spellings are exempt, `-0`
and the POSIX `-s 0`/`-n 0`. A *second* signal selector forfeits the exemption,
because which one wins depends on how it's spelled: a later `-s`/`-n` overrides
an earlier bare spec, while a later bare spec is read as a pid instead. Rather
than model that per shell, the hook takes the exemption only when there's nothing
to arbitrate.

```
while kill -0 $(pgrep -f ginkgo); do sleep 1; done   # -> defer
pgrep -f ginkgo | xargs kill -0                      # -> defer
kill -0 -s 9 $(pgrep -f ginkgo)                      # -s 9 wins -> deny
pgrep -f ginkgo | xargs -0 kill                      # xargs' NUL flag -> deny
```

The pid sources are `pgrep`'s pattern operands and **`ps` itself** — not the
command filtering it. A `ps` feeding a kill is a pid source whose selection the
hook can't read, so it denies whatever the filter is, and denies with no filter
at all:

```
ps -eo pid,command | awk '/ginkgo/ {print $1}' | xargs kill   # -> deny
ps -eo pid,command | sed -n '/ginkgo/s/.*//p' | xargs kill    # -> deny
ps -eo pid= | xargs kill                                      # -> deny
kill $(ps -eo pid= | head -1)                                 # -> deny
```

A **`grep` in the same pipeline is readable**, so its pattern can still anchor
one: `ps aux | grep "<this-root>/bin/x" | awk '{print $1}' | xargs kill` defers.
That is the rewrite to reach for. An awk program is not read even when it looks
anchored — reading one would be unsafe rather than merely imprecise, because an
inverting program (`awk '!/<this-root>/ {print $1}'`) would scan as anchored
while killing every *other* checkout.

The pids have to be able to reach the kill: a `ps` counts in the kill's own
pipeline, or inside a command substitution the kill consumes. So the everyday
debugging idiom is untouched, because its `ps` consumes a pid rather than
producing one:

```
./run.sh & pid=$!; kill $pid; ps -p $pid    # -> defer
```

An **exclusion** can't anchor a pipeline: `grep -v` removes the pids it matches
rather than selecting them, so `ps … | grep ginkgo | grep -v "<this-root>/skip"`
still denies — what reaches the kill is every *other* checkout's `ginkgo`. A
pattern the hook can't read (`grep -f patterns.txt`) can't clear a pipeline
either; it reports as unreadable rather than as absent.

Provenance here is co-occurrence within one pipeline or command string, not
dataflow: the hook doesn't prove the pids the kill receives came from the source
it found. The literal-pid rule removes the case where that would matter, and
`WORKSPACE_GUARD_OVERRIDE=<reason>` covers the remainder.

#### `sh -c` bodies

A shell `-c` operand is a whole command string inside one token. Two separate
things happen to it.

**It is checked, when this host is the one that runs it.** The body goes back
through the hook as its own command string, so it gets the same answer it would
written unwrapped — wrapping something in `sh -c` doesn't exempt it.

```
sh -c 'cat /etc/hosts'                  # -> ask   (same as `cat /etc/hosts`)
sh -c 'echo x > /etc/hosts'             # -> ask
sh -c 'pkill -f ginkgo'                 # -> deny  (unanchored kill)
cd /etc && sh -c 'cat hosts'            # -> ask   (the `cd` ran first)
```

This covers `sh`, `bash`, `zsh`, `dash` and `ksh`, under the wrappers that
actually appear — `timeout 5 bash -c …`, `xargs -I{} sh -c …`,
`find … -exec sh -c … \;`, `env FOO=1 sh -c …`, `nohup`, `nice`, `stdbuf`.

**It never earns the string an `allow`.** That holds whether or not the body was
checked, because plenty of bodies don't run here at all — a path inside a
container is not a path on this disk — and a body the hook declines to read is
exactly the case where vouching is indefensible.

```
docker exec c sh -c 'cat /var/lib/x'    # -> defer (a container path, unchecked)
ssh host sh -c 'cat /etc/hosts'         # -> defer (another machine)
cat in.txt; sh -c 'cat in.txt'          # -> defer (clean body, still no vouch)
cat in.txt; bash --version              # allow    (no -c body at all)
```

The unchecked list is everything that isn't a local wrapper: container runtimes
(`docker`, `podman`, `kubectl exec`, …), `ssh`, `sudo`, and anything else the
hook doesn't recognize. Naming the local wrappers rather than the remote ones is
deliberate — a runtime nobody has heard of yet reads as remote, and its body is
left alone rather than judged against paths it never touches. See
[Limitations](#limitations).

#### PowerShell: `Stop-Process`

The same rule, adapted to a cmdlet with three ways to name its target:

```
Stop-Process -Name node                     # -> deny (this is `killall`)
Get-Process node | Stop-Process             # -> deny (nothing scopes the pipeline)
Stop-Process -Id $p.Id                      # -> deny (the hook can't see the pid)
Stop-Process -Id 1234                       # defer  (by pid, the safe rewrite)
Get-Process | Where-Object { $_.Path -like '<this-root>\*' } | Stop-Process   # defer
```

`-Id` with literal pids is the `kill <pid>` case and is untouched. `-Name` is
denied outright and *cannot* be rescued by an anchor: a process name carries no
path, so nothing written around it scopes it to this workspace. Everything else —
`-InputObject`, or processes arriving over the pipeline — is anchored by the rest
of the **statement**, which is why the `Where-Object` filter two segments
upstream counts. A statement ends at `;`, a newline, `&&`, `||` or `&`; `|`,
parentheses and script-block braces all stay inside it.

Covering the pipeline form is what keeps the rule honest. Guard only `-Name` and
the deny teaches the agent to reach for `Get-Process node | Stop-Process`
instead — the same host-wide kill, one keystroke further away.

A clean cmdlet in the same string doesn't speak for the kill either. A
`Stop-Process` or a `taskkill` anywhere in the string — including one inside a
`$(…)` body — suppresses the blanket `allow` a `Get-Content` would otherwise
earn, so `Get-Content .\in.txt; Stop-Process -Id 1234` emits nothing and your own
permission settings decide. That covers the kills this rule leaves alone, by
literal pid or anchored: they were never the hook's to green-light.

#### Windows: `taskkill`

`taskkill` is the same kill again, and it reaches both shells — Git Bash spawns
it as a native executable, PowerShell as a native command — so it is checked in
both:

```
taskkill /IM node.exe                       # -> deny (this is `killall`)
taskkill /FI "IMAGENAME eq node.exe"        # -> deny (a filter names no path)
taskkill                                    # -> deny (nothing selects)
taskkill /PID $p                            # -> deny (the hook can't see the pid)
taskkill /PID 1234                          # defer  (by pid, the safe rewrite)
taskkill /?                                 # defer  (kills nothing)
```

Flags bind whichever prefix you write — `/IM`, `-IM`, or the `//IM` that Git
Bash's path mangling requires — and their names are case-insensitive, as is the
command word (`TASKKILL.EXE` is `taskkill`). The rewrite the deny names is this
command's own: `tasklist /FI "IMAGENAME eq <name>"` to find the process, then
`taskkill /PID <pid>` to kill just that one.

Unlike `Stop-Process`, a `taskkill` is judged on **its own arguments**, not on
the whole statement. It reads no pipeline — its selection is entirely in its own
flags — so an anchor written upstream of it isn't what picks the processes, and
counting one would clear a kill it has nothing to do with.

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

These steps describe the `Bash` frontend. The `PowerShell` tool has its own
tokenizer, cmdlet table, and path resolution — backslash is a path character
there, not an escape (see [The PowerShell tool](#the-powershell-tool)) — and
rejoins this one at the classification steps (9–13), so both reach a decision
through the same boundary rules and produce the same reasons. Symlink staging
(step 7) has no PowerShell equivalent yet.

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
   On Windows a leading-slash path is read through Git Bash's mount table
   before `realpath`, since that is what the shell will do with it: `/c/…` is
   the C: drive, `/tmp` is `%TMP%`, `/bin` is `<git>\usr\bin`, and anything
   else sits under the Git install dir. Without it the guard named a directory
   on whichever drive happened to be current. The native `Read`/`Edit` tools
   are *not* the shell and keep the drive-relative reading they themselves use.
   Well-known
   device paths (`/dev/null`, `/dev/stdin`, `/dev/stdout`, `/dev/stderr`,
   `/dev/zero`, `/dev/tty`, `/dev/random`, `/dev/urandom`, `/dev/fd/N`) are
   allowlisted and skip the workspace check.
9. **Allow** the current session's own Claude-managed scratch — and, for
   read-only commands, sibling sessions of the same project. Claude Code writes
   each background task's output to
   `/tmp/claude-<uid>/<encoded-project>/<session-uuid>/tasks/<id>.output`, and
   the agent reads it back with `cat`/`tail`/`grep`. The same `<session-uuid>`
   dir holds the `scratchpad/` Claude Code hands the session for temp files.
   Neither is the boundary this hook guards, so:
   - a path under `/tmp/claude-<uid>/` that carries the **current** session's id
     as a path segment is allowed for any guarded command, **read or write** —
     this check runs before the read/write split, so the session's whole scratch
     tree is writable, not just readable; and
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
   `/var/folders/…` are caught; on Windows a command's own `/tmp/x` resolves to
   `%TMP%` too, so it lands on the same root) is
   reclassified from `ask` to `deny`, with a message steering to a repo-local
   gitignored scratch dir — and, when the session's `scratchpad/` from step 9 is
   on disk, naming that too as the place for a throwaway that shouldn't outlive
   the session. Because this runs on the already-resolved file
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
13. **Deny** a process kill with no workspace anchor. `pkill` and `killall`
   address a process by pattern rather than by path, so they reach every checkout
   on the host. Value-taking flags come off (`-u karl`, `--signal TERM`), `--`
   ends options, and a signal flag (`-9`, `-TERM`) is skipped like any other
   flag; what remains are the pattern operands. Unless one of them contains the
   project root's directory name as a whole path component *with a path
   separator on at least one side*, the command is `deny`ed with the two safe
   rewrites. Both misparse directions land on `deny`, so a flag table that
   lags an implementation's options costs friction, never a hole. An anchored
   kill emits nothing — it **defers** rather than `allow`ing, leaving your own
   permission settings in charge of a destructive command.
   `WORKSPACE_GUARD_OVERRIDE=<reason>` downgrades the deny to `ask`. PowerShell's
   `Stop-Process` runs the same check with its own selection rules — the anchor is
   looked for across the whole statement there, since the anchored form is a
   `Where-Object` filter upstream in the pipeline. Windows' `taskkill` runs it in
   *both* frontends, on its own arguments only: it reads no pipeline, so nothing
   upstream selects what it kills. A literal `/PID` defers, `/IM` and `/FI` deny,
   and `/?` kills nothing so it defers too. See
   [Unanchored process-kill deny](#unanchored-process-kill-deny).
   The same step covers a kill fed by a pattern rather than named by one, in the
   Bash tool. Any signalling command in the string (`kill`, `pkill`, `killall`,
   `taskkill`, directly or as an `xargs` command word) suppresses the blanket
   `allow` a clean guarded command would otherwise earn, so a `grep` can never
   carry an `xargs kill` past your permission settings — `allow` speaks for the
   whole string. A shell `-c` body suppresses it the same way: it arrives as one
   token, and even once step 15 has read it the hook has no basis to vouch for a
   construct it reads at one remove. A `Stop-Process` or a `taskkill` suppresses
   the PowerShell `allow` too,
   including one this step had no cause to deny and one written inside a `$(…)`
   body. The pid *sources* then go through the anchor test above: `pgrep`'s
   pattern operands, and **`ps`** — which is the source in a pipeline, rather
   than whatever filters it. A `ps` contributes an unreadable pattern that can
   never anchor, so the pipeline is caught whatever the filter is (`awk`, `sed`,
   `cut`) and with no filter at all; a `grep` in the same pipeline is readable,
   so its pattern can still anchor. A `kill` whose operands aren't all literal
   pids, with no anchoring pattern, is `deny`ed as one. `ps` counts only where
   its pids can reach the kill — the same pipeline, or a substitution the kill
   consumes — so `run & p=$!; kill $p; ps -p $p` is untouched. An inverting
   `grep -v` and a pattern read from a `-f` file contribute nothing that can
   anchor.
14. **Recurse into command substitutions.** A guarded command hidden in a
   `"$(…)"` or backtick `` `…` `` substitution — `echo "$(mktemp -p /tmp x)"`,
   `` x=`grep secret /etc/passwd` `` — isn't tokenized as its own command by the
   step-1 lexer (the metacharacters are inside quotes), so its file ops would be
   invisible. The hook scans the *raw* command for substitution bodies in
   unquoted or double-quoted context (single-quoted `'$(…)'` is a bash literal
   and is skipped; `$((…))` arithmetic has no command) and runs each body back
   through steps 1–15. Heredoc bodies leave the command line first and are
   scanned on their own: a quoted-delimiter body is literal to bash and is
   dropped, an unquoted one is scanned with quoting turned off, because bash
   applies none inside it — so the apostrophe in a `don't` there is text, not
   the start of a quoted run that would hide a live `$(…)` after it. Only
   *offenders* bubble up: a clean guarded command inside
   a substitution never turns a deferring outer command into an `allow` — this
   step can only add friction. (The bare unquoted `$(…)` form was already caught,
   because its `(`/`)` split the inner command into its own group.) Nesting is
   followed 25 levels deep; see Limitations for what the bound costs.
15. **Recurse into shell `-c` bodies.** A body is an ordinary command string that
   happened to arrive inside one token, so it runs back through steps 1–15 and
   its offenders fold in — but only when this host is what executes it. The
   group's command word has to be a shell or a local wrapper (`timeout`, `env`,
   `xargs`, `find`, `nohup`, `nice`, `ionice`, `stdbuf`, `setsid`, `time`);
   anything else is treated as remote and the body is left alone, because the
   paths in `docker exec c sh -c '…'` or `ssh h sh -c '…'` are not this
   filesystem's. The `-c` must be the shell's own option, found in the option run
   right after the shell word — otherwise a `bash run.sh | grep -c '^FAIL'` would
   hand the tokenizer a grep pattern and get offenders invented out of it. Each
   body resolves against its group's cwd, so a preceding `cd` moves it; once the
   hook has lost track of the cwd the body is skipped, since a relative path
   would otherwise resolve against a stale directory and read as in-workspace.
   Like step 14 this only *adds* friction: the body's own `guarded` is dropped,
   so a body reading nothing but workspace files still leaves the string
   deferring. The 25-level nesting bound covers these too.

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
  Use a repo-local gitignored scratch dir like `./tmp/out.txt` instead — or, for
  a throwaway that shouldn't outlive the session, this session's *own*
  Claude-managed tree under `/tmp/claude-<uid>/…/<session>/…`, including the
  `scratchpad/` dir Claude Code points you at. That tree is exempt for **reads
  and writes both** — Claude Code chose the path and cleans it up. (Redirects
  and command output to `/dev/null`, `/dev/stdout`, `/dev/stderr`, and `/dev/fd/N`
  are exempt and never prompt. Only *read-only* access extends to a sibling
  session's output under the same project's scratch dir; writing there still
  asks.) Reading files under `~/.claude/projects/` (Claude Code's own session
  and sub-agent data) is also exempt for read-only commands.
- **Read dependency source from in-workspace vendored/pinned copies, not the
  global cache.** Out-of-tree caches (Go's `~/go/pkg/mod`, npm's `~/.npm`, pip's
  `~/.cache/pip`, cargo's `~/.cargo/registry`) are outside the project root, so
  every guarded read of them prompts. Vendor the source into the tree instead
  (e.g. `go mod vendor` → `vendor/`, npm's `node_modules/`) and read from there,
  or use the Read/Grep tools, whose literal single-path inputs avoid the
  `$VAR`/`$(...)` false positives — they are still guarded, so a read that
  genuinely lands outside the root still prompts.
- **In a git worktree, edit only via this session's checkout — never another
  checkout's path.** A write (bash or `Edit`/`Write`) into the primary checkout
  or another worktree of the same repo is **denied**: it would land your change
  on the wrong branch. Use the same relative path under your session root. For
  deliberate cross-checkout work, set `WORKSPACE_GUARD_OVERRIDE=<reason>` to
  downgrade the deny to a prompt.
- **Never kill a process by an unanchored pattern.** `pkill`/`killall` match
  every checkout on the host, so `pkill -f "make check"` or `pkill -f ginkgo`
  kills whatever another session is running — both are **denied**. Run
  `pgrep -fl <pattern>` first and kill the pid(s) you meant, or put the project
  root's directory name in the pattern as a path component with a separator
  (`pkill -f "<root-dirname>/.build/ginkgo"`), which is allowed through. A bare
  word doesn't count as an anchor, nor does a pattern with an unexpanded `$VAR`
  in it. In PowerShell the same rule applies to
  `Stop-Process`: `-Name` and an unfiltered `Get-Process … | Stop-Process` are
  denied, `Stop-Process -Id <literal pid>` is never blocked, and the anchored
  form is `Get-Process | Where-Object { $_.Path -like '<root>\*' } |
  Stop-Process`. On Windows, `taskkill /IM <name>` and `taskkill /FI <filter>`
  are denied from either shell; find the process with
  `tasklist /FI "IMAGENAME eq <name>"` and kill it with `taskkill /PID <pid>`,
  which is never blocked. For a deliberate cross-workspace kill, set
  `WORKSPACE_GUARD_OVERRIDE=<reason>`.
- **Routing the same pattern through pids doesn't help.**
  `pgrep -f ginkgo | xargs kill`, `kill $(pgrep -f ginkgo)`, and
  `ps … | grep ginkgo | awk '{print $1}' | xargs kill` are denied on the same
  pattern rule — the anchor is what's missing, not the spelling. Nor does
  changing the filter: `ps` is the pid source, so `ps … | awk '/ginkgo/ {print
  $1}' | xargs kill`, the `sed`/`cut` spellings, and `ps -eo pid= | xargs kill`
  with no filter at all are denied too. To scope a `ps` pipeline, put the
  project root's directory name in a **`grep`** stage — that one the hook reads.
  Note that a `grep -v` exclusion never anchors a pipeline, because what reaches
  the kill is everything it did *not* match. `kill <literal pid>`,
  `kill -0 <pid>`, and killing your own backgrounded child
  (`cmd & pid=$!; kill $pid`, even alongside a `ps -p $pid`) are never blocked —
  those are the rewrites, and they stay out of the rule even in a string that
  also runs a `pgrep`.
- **Don't wrap work in `sh -c '…'` to get it past the hook.** The body is read
  back through the same rules, so it gets the same answer it would unwrapped —
  and a body carries the extra cost that it can never earn the silent `allow` a
  clean guarded command gets. Wrapping trades an allow for a prompt and gains
  nothing. Write the command directly.
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
by default and steered to the two destinations that are allowed: a repo-local
gitignored scratch dir, and this session's own `scratchpad/` (named only when it
exists on disk). Four environment
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

A relative entry is resolved against the tool's working directory, the same base
the command's own file arguments resolve against. On Windows an entry with a
leading slash is first read the way Git Bash reads it, so `/c/Users/me/shared`
means `C:\Users\me\shared` — as it does in a command — rather than a `c` folder
on whichever drive is current. Both rules apply to every configured path: the
host-temp roots and allowlist above, and these prefixes.

### Cross-workspace denies

Two denies fire on work that reaches past this session's own checkout: a write
into a sibling checkout of the same repo (see
[Worktree-aware sibling-checkout deny](#worktree-aware-sibling-checkout-deny)),
and a process kill with no workspace anchor (see
[Unanchored process-kill deny](#unanchored-process-kill-deny)). One env var tunes
both, read at hook time (no restart needed):

| Env var | Default | Effect |
| --- | --- | --- |
| `WORKSPACE_GUARD_OVERRIDE` | (empty) | When set to a non-empty reason string, downgrades the sibling-checkout deny and the unanchored-kill deny to `ask`, for work that deliberately reaches another checkout. The reason is echoed back in the prompt. |

`WORKSPACE_GUARD_OVERRIDE` is the one knob that *loosens* this guard, so it's
empty by default and opt-in. The denies are the secure default: they self-heal in
one agent round trip, whereas an approvable prompt invites the reflexive "yes"
that lands the change on the wrong branch, or kills the wrong process. Scope the
override to the moment you actually need it (e.g. one command), not the whole
session.

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
  substitutions *inside* a heredoc body follow bash's own rule: a quoted
  delimiter (`<<'EOF'`, `<<"EOF"`, `<<\EOF`, or any partly quoted word) makes
  the body literal, so a `$(…)` there is documentation and is ignored; an
  unquoted `<<EOF` body is expanded by bash and is still analyzed — on its own,
  with quoting off, since a heredoc body has none.
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
  `ask`. Anything that may set `IFS` disables propagation for that command
  entirely (conservative — a changed `IFS` re-splits every later expansion, so
  a value checked as one word can reach the command as several). A heredoc
  (`<<`) does not: its body is dropped before parsing, so a body line shaped
  like an assignment never reaches the map, and a value assigned before the
  heredoc still resolves after it.
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
- Command substitutions nested more than 25 deep stop being recursed into, for
  the same reason the loop cap exists: unbounded recursion exhausts the
  interpreter stack, and a hook that dies mid-decision is one Claude Code treats
  as a non-blocking error — enforcing nothing. In bash the bound is on the
  recursion, not on the analysis: the lexer does not track quote nesting through
  `$(…)`, so an inner command past the cap generally still surfaces in an outer
  level's tokens and is flagged there. PowerShell masks each `$(…)`/`@(…)` out of
  the text it tokenizes, so there is no outer level to catch it: a subexpression
  nested past the cap goes unanalyzed, and the command defers rather than
  earning an `allow` on the strength of a body the guard never read.
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
- The current session's own Claude-managed scratch tree
  (`/tmp/claude-<uid>/…/<session>/…` — task output *and* `scratchpad/`) is
  allowed silently for reads and writes alike, scoped to the session via the
  hook's `session_id`. Read-only commands are additionally
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
  can mis-tokenize on re-parse and defer; and backtick nesting via
  `` \` `` or a no-space `$((…))`-shaped subshell isn't decoded. Process substitution
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
- The unanchored-kill deny covers `pkill`, `killall`, and a pattern-fed `kill`
  in the Bash tool, `Stop-Process` in the PowerShell tool, and `taskkill` in
  both. A kill routed through something the hook doesn't parse — a script that
  kills on your behalf — is checked in **neither**. `Get-Process -Id 1234 |
  Stop-Process` denies even though the pid is literal — it's bound on the
  upstream cmdlet, which the kill rule doesn't read; write `Stop-Process -Id 1234`.
  `taskkill /FI "PID eq 1234"` denies for the same shape of reason: filter
  expressions aren't parsed, so write `taskkill /PID 1234`.
  Anchoring is also *lexical*, which cuts both ways: a process
  started with a relative command line (`make check`) carries no path for `-f` to
  match, so it can't be anchored at all and the answer there is `pgrep -fl` plus
  a kill by pid; and a pattern naming an unrelated directory that happens to
  share this root's basename reads as anchored. What the rule does reliably
  exclude is a *sibling* checkout, whose directory name differs by construction.
  A pattern naming a worktree nested under the project root (`.claude/worktrees/*`)
  counts as in-workspace, consistent with the boundary the rest of the hook
  draws.
- A pattern-fed kill's pid sources are `pgrep` and `ps`, matched by command
  word — a wrapper (`busybox ps`, a shell function, a script that kills on your
  behalf) is not one. Because the source is `ps` and not the stage reading it,
  the filter can be anything; the cost is that an *anchored* filter the hook
  can't read doesn't clear the pipeline either, so
  `ps … | awk '/<this-root>\/x/ {print $1}' | xargs kill` denies. Anchor with a
  `grep`, or use `WORKSPACE_GUARD_OVERRIDE`. (Reading an `awk` program was
  rejected as unsafe, not merely imprecise: an inverting `!/<this-root>/` program
  would scan as anchored while killing every other checkout.) Provenance is
  co-occurrence within one pipeline or command string rather than dataflow, so a
  string that both searches by pattern and kills an unrelated non-literal pid is
  denied though nothing was laundered; the override covers it.
- **A shell `-c` body is only analyzed when this host runs it.** `sh -c
  '<body>'` — and the `bash`, `zsh`, `dash`, `ksh` spellings under the local
  wrappers (`timeout 5 bash -c …`, `xargs -I{} sh -c …`,
  `find … -exec sh -c … \;`) — is read back through the same rules, so a kill, an
  outside read or a redirect inside one is caught. Under anything else it is not:
  a container runtime, `ssh`, `sudo`, or a wrapper the hook doesn't recognize
  leaves the body unparsed, because the paths in `docker exec … sh -c 'cat
  /var/lib/…'` are the container's rather than this disk's, and guessing wrong
  there means blocking a path that was never touched. The body is also skipped
  once the hook has lost track of the cwd (after a `cd -` or a `cd "$VAR"`),
  where a relative path in it would resolve against a stale directory. In all
  those cases the string still *defers* — a body never earns the blanket `allow`,
  analyzed or not.
- **The PowerShell tool is guarded for a known set of cmdlets, and only those.**
  Claude Code ships two shell tools. Which one a Windows session gets depends on
  whether Git for Windows is installed — without it there is no Bash tool and
  shell commands run through PowerShell, as they also do wherever
  `CLAUDE_CODE_USE_POWERSHELL_TOOL=1` is set. Both are hooked. PowerShell is not
  a POSIX shell, though, so it gets its own parser and its own table rather than
  the bash one — see [The PowerShell tool](#the-powershell-tool): `Get-Content`,
  `Select-String`, `Import-Csv`, `Import-Clixml`, `Set-Content`, `Add-Content`,
  `Out-File`, `Tee-Object`, `Export-Csv`, `Export-Clixml`, `Copy-Item`,
  `Move-Item`, `Remove-Item`, `Rename-Item`, their aliases (`cat`, `type`, `gc`,
  `sls`, `sc`, `ac`, `cp`, `mv`, `rm`, `del`, …), output redirects, and
  `Stop-Process` and `taskkill` as process kills.
  Everything else — a cmdlet not on that list, a .NET call such as
  `[IO.File]::ReadAllText(…)`, a native `.exe` — is **not checked**, and the
  session gets no signal that it wasn't. The alternative was to prompt on
  everything unparsed, which at this table size would prompt on nearly every
  command; a guard that noisy gets switched off, and zero coverage is worse than
  partial. Expect this list to grow. The native file tools (`Read`, `Grep`,
  `Glob`, `Edit`, `Write`) are guarded in full either way.
- **A custom Git Bash mount is read as an ordinary directory.** On Windows the
  hook resolves a leading-slash path through the mount table Git for Windows
  ships (`/c/…` is the C: drive, `/tmp` is `%TMP%`, `/bin` is `<git>\usr\bin`,
  anything else hangs off the Git install dir). A mount you added in
  `/etc/fstab`, and MSYS's virtual paths like `/proc`, are not in that table and
  fall through to the last rule, so the prompt names a path under the Git
  install dir instead. The same happens for every non-drive path if Git Bash
  can't be located at all (the hook looks at `CLAUDE_CODE_GIT_BASH_PATH`, then
  `bash` and `git` on `PATH`) — it keeps the older drive-relative reading
  rather than guessing. Either way the effect is a prompt naming a path that
  isn't quite the one being opened, never a missed one.

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
