# branch-guard

A Claude Code plugin that lets commits flow freely on feature branches while
keeping a human in the loop for anything that touches a protected branch.

It installs a single `PreToolUse` hook that:

- **auto-approves `git commit`** when the current branch is *not* protected
  (e.g. `claude/*` or any feature branch), and
- **prompts you (`ask`)** before a `git commit` **or** a file edit
  (`Edit`/`Write`/`MultiEdit`) that targets a **protected branch** (`main` or
  `master`).

Everything else is left untouched — the hook stays silent and the normal Claude
Code permission flow applies.

## Why

In a worktree-based workflow you want zero friction committing to throwaway
`claude/*` branches, but a deliberate pause before you commit to — or edit files
checked out on — `main`/`master`. branch-guard encodes exactly that policy so
you don't have to approve every routine feature-branch commit by hand, yet can't
silently mutate a protected branch.

The edit check resolves the branch of **the file's own repository**
(`git -C <dir-of-file>`), not the session's working directory. That deliberately
catches edits made through a checkout sitting on `main` (for example a parent
repo path) even while your session's cwd is a feature-branch worktree.

## Behavior

| Tool call | Current branch | Decision |
|---|---|---|
| `git commit …` (Bash) | non-protected (e.g. `claude/x`) | **allow** (auto) |
| `git commit …` (Bash) | `main` / `master` | **ask** |
| any other Bash command | any | no decision (defer) |
| `Edit` / `Write` / `MultiEdit` | file's repo on `main` / `master` | **ask** |
| `Edit` / `Write` / `MultiEdit` | file's repo on non-protected branch | no decision (defer) |
| any other tool | any | no decision (defer) |

"No decision" means the hook emits nothing and Claude Code applies its usual
permission rules.

### Known limitation

The hook guards `git commit` (via Bash) and edits made through the
`Edit`/`Write`/`MultiEdit` tools. It does **not** intercept file mutations done
through other Bash commands — e.g. `sed -i`, `>` redirects, or `rm` — on a
protected branch.

## Requirements

- `jq` and `git` on `PATH`.

## Test

```bash
./test/run.sh
```

Spins up a throwaway git repo under `tmp/` and asserts the decision for each
tool/branch combination.

## Activation

The plugin self-hosts as a single-plugin marketplace, so you can install it
straight from this directory.

### Interactive

```text
/plugin marketplace add ~/workspace/claude-branch-guard
/plugin install branch-guard@claude-branch-guard
```

(CLI equivalents: `claude plugin marketplace add ~/workspace/claude-branch-guard`
then `claude plugin install branch-guard@claude-branch-guard`.)

### Via settings.json

Add to `~/.claude/settings.json` (all projects) or a project's
`.claude/settings.json` (this project only):

```json
{
  "extraKnownMarketplaces": {
    "claude-branch-guard": {
      "source": { "source": "directory", "path": "~/workspace/claude-branch-guard" }
    }
  },
  "enabledPlugins": {
    "branch-guard@claude-branch-guard": true
  }
}
```

To point at a remote instead of a local path, replace the `source` with a
`github`/`git`/`url` form (see the Claude Code plugin-marketplace docs).
