# workspace-guard

A Claude Code plugin that adds a **PreToolUse hook** for `Bash`. When a guarded
command (`grep`, `sed`, `jq`, `awk`, `cat`, `head`, `tail`) is about to read or
write a file **outside the current workspace**, Claude Code prompts you to
confirm. Commands that only touch files inside the workspace — or that operate on
pipes/stdin — run without interruption.

Unlike plain permission rules (`Bash(grep:*)`), which match the literal command
string and can't tell `grep foo.txt` from `grep /etc/passwd`, this hook tokenizes
the command with a real POSIX shell lexer, resolves each file argument against the
project root, and decides accordingly.

## What it does

| Command                              | Decision |
| ------------------------------------ | -------- |
| `grep foo ./src.txt`                 | allow    |
| `cat data.txt \| grep foo`           | allow    |
| `jq '.a/.b' data.json`               | allow (the `.a/.b` program is not mistaken for a path) |
| `sed 's/a/b/g' notes.md`             | allow    |
| `grep secret /etc/passwd`            | **ask**  |
| `jq '.x' /etc/hosts`                 | **ask**  |
| `sed -f /tmp/evil.sed notes.md`      | **ask**  |
| `grep foo data.txt > /tmp/out.txt`   | **ask** (redirect target outside workspace) |
| `cat ../../etc/passwd`               | **ask**  |
| `ls /etc`                            | defer (not a guarded command) |

"Defer" means the hook stays silent and your normal permission settings apply.

## Install

```
/plugin marketplace add karlkfi/workspace-guard
/plugin install workspace-guard@workspace-guard
```

Restart Claude Code so the hook is registered. Requires `python3` on your PATH.

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
4. **Resolve** every file argument against `$CLAUDE_PROJECT_DIR` (the workspace
   root) with `realpath`, collapsing `../` and following symlinks. Anything that
   resolves outside the root yields `ask`; otherwise `allow`.

## Configuration

The set of guarded commands lives in the `SPEC` and `ALIASES` tables at the top of
`scripts/bash-workspace-guard.py`. Add a row to guard another command. To switch
from prompting to hard-blocking, change `"ask"` to `"deny"` in the script's final
output.

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

## License

MIT — see [LICENSE](LICENSE).
