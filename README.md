# workspace-guard

**Path-aware bash permissions for Claude Code.**

[![License: MIT](https://img.shields.io/github/license/karlkfi/claude-workspace-guard.svg)](LICENSE) [![Claude Code plugin](https://img.shields.io/badge/Claude_Code-plugin-7e57c2)](#install)

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

## What it does

The hook produces one of three outcomes:

- **allow** — the command runs without a prompt.
- **ask** — Claude Code shows its standard permission prompt for the command
  (as above). You approve or reject.
- **defer** — the hook stays silent; your normal permission settings apply.

Guarded commands: `grep` (and `egrep`, `fgrep`, `rg`), `sed`, `awk` (and
`gawk`, `mawk`), `jq`, `cat`, `head`, `tail`. These are the file-reading
commands Claude reaches for most often; tools like `ls`, `find`, and `xargs`
aren't covered yet (see [`docs/STATUS.md`](docs/STATUS.md)).

| Command                              | Decision |
| ------------------------------------ | -------- |
| `grep foo ./src.txt`                 | allow    |
| `cat data.txt \| grep foo`           | allow    |
| `jq '.a/.b' data.json`               | allow    |
| `sed 's/a/b/g' notes.md`             | allow    |
| `cat data.txt > /dev/null`           | allow    |
| `grep secret /etc/passwd`            | **ask**  |
| `jq '.x' /etc/hosts`                 | **ask**  |
| `sed -f /tmp/evil.sed notes.md`      | **ask**  |
| `grep foo data.txt > /tmp/out.txt`   | **ask**  |
| `cat ../../etc/passwd`               | **ask**  |
| `ls /etc`                            | defer    |

Note the `jq` row: `.a/.b` is a jq program, not a filesystem path. The hook
knows the difference because it parses each command against a per-command spec
of which positions are programs, which are files, and which flags take values.
A naive string match would either miss real file arguments or false-positive on
program syntax.

## Install

```
/plugin marketplace add karlkfi/claude-workspace-guard
/plugin install workspace-guard@workspace-guard
```

- Requires `python3` on your PATH.
- Restart Claude Code so the hook is registered.

To verify, ask Claude to run `grep root /etc/passwd` — you should see a
permission prompt citing the outside-workspace path. Then ask it to `grep` a
file in your repo; it should run without prompting.

## How it works

1. **Tokenize** the command with Python's `shlex` (POSIX mode, punctuation
   grouping) so quotes are respected and shell operators (`|`, `&&`, `>`, `;`)
   become their own tokens.
2. **Split** into simple commands on those operators and pull redirect targets
   (`> file`) aside as files to check.
3. **Classify** each token using a per-command spec table that knows which flags
   take values (`grep -e PAT`), which flag-values are themselves files
   (`grep -f`, `jq --slurpfile`), and how many leading positionals are the
   program/pattern to skip.
4. **Resolve** every file argument against `$CLAUDE_PROJECT_DIR` with
   `realpath`, collapsing `../` and following symlinks. Anything that resolves
   outside the root yields `ask`; otherwise `allow`. Well-known device paths
   (`/dev/null`, `/dev/stdin`, `/dev/stdout`, `/dev/stderr`, `/dev/zero`,
   `/dev/tty`, `/dev/random`, `/dev/urandom`, `/dev/fd/N`) are allowlisted and
   skip the workspace check.

## Configuration

The set of guarded commands lives in the `SPEC` and `ALIASES` tables at the top
of `scripts/bash-workspace-guard.py`. Add a row to guard another command. To
switch from prompting to hard-blocking, change `"ask"` to `"deny"` in the
script's final output.

## Limitations

- Command substitution (`grep x $(cat list)`) and variable-expanded paths
  (`grep x $VAR`) are not visible before execution.
- `realpath` only follows symlinks for files that already exist; nonexistent
  paths are normalized lexically (fine for read-style commands).
- In non-interactive / headless runs there is no one to answer an `ask` prompt,
  so it effectively blocks.

## Design

For the rationale behind the approach (why a hook, why `ask`, why a static
spec table, what alternatives were rejected), see [`docs/design.md`](docs/design.md).
Out-of-scope security observations from audits live in
[`docs/security-notes.md`](docs/security-notes.md).

## Contributing

Bugs, ideas, and questions go in
[GitHub Issues](https://github.com/karlkfi/claude-workspace-guard/issues).
For the development backlog and how to add new guarded commands, see
[`docs/STATUS.md`](docs/STATUS.md).

## License

MIT — see [LICENSE](LICENSE).
