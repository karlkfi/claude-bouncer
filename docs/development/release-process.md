# Agent reference: Cutting a release

Method — when a release is worth cutting, how to draft and verify notes, how
to check the artifacts — is the `github-oss-release` skill. This file is what
the skill cannot know: where this repo keeps a version, what a tag is called
here, and which of the five guards a change reaches.

## A release is one plugin

Nothing releases "claude-bouncer". Each guard carries its own version,
inherited from the repository it came from, and a release moves one of them.
A user who installed `prod-guard` and nothing else should see an update when
`prod-guard` moves and never because `foreground-guard` did.

So a change under `plugins/<name>/` releases that plugin. A change under
`lib/` reaches all five and releases none of them.

## Tag names carry the plugin

`<plugin>/vX.Y.Z` — `workspace-guard/v1.10.1`, `prod-guard/v2.5.2`. Every
query filters by prefix:

```
git tag --list 'workspace-guard/v*' --sort=-v:refname | head -1
```

`-v:refname` orders the version part correctly inside a prefix, so 1.10.1
sorts above 1.9.0 and the first line is the previous release of that guard.

A bare `vX.Y.Z` tag names no plugin and must never be created here. The
retired repositories hold those tags — `karlkfi/claude-workspace-guard`
v1.10.0 and its four siblings — and they keep resolving where they are. This
repo's first tag for a guard continues that plugin's numbering instead of
restarting it.

GitHub percent-encodes the slash in a release URL, so a link copied from the
browser reads `releases/tag/workspace-guard%2Fv1.10.1`. Nothing here has been
published under the scheme yet — open the first one and confirm before pasting
it into an announcement.

## Version levels, read off what shipped

SemVer, per plugin. For a guard the deciding question is not what the diff
touched. It is whether the set of decisions moved, in either direction.

**patch — the same decisions, better.** Reason wording, false positives inside
a rule that already claimed the ground, parser hardening no operator sees,
docs, packaging. branch-guard 1.4.2 replaced the entire hook launcher and
shipped as a patch: `branch-guard.py` was byte-identical and no verdict moved.
workspace-guard 1.7.2 closed two write-guard gaps as a patch, because the
write boundary already claimed those directories and only the implementation
had lagged.

**minor — the boundary moves.** A newly guarded command, a new surface
(workspace-guard 1.8.0 Windows, 1.9.0 process kills, 1.10.0 the script an
interpreter is told to run), a new configuration variable, or a prompt
withdrawn (branch-guard 1.5.0). New denials ship as minors here:
workspace-guard 1.2.0 made host-wide temp paths deny by default. A guard's
contract is which questions it asks, so asking a new one is added behaviour
rather than a break.

**major — the installed thing changes identity, or existing configuration
stops meaning what it did.** exit-status-guard 2.0.0 renamed the plugin and
its marketplace, so the upgrade took four manual commands and could not
deliver itself. A removed override variable and an inverted default verdict
(prod-guard's `feat(verdict)!: deny by default`) are the same class.

foreground-guard is pre-1.0 (0.5.1). Its surface is unstable by definition, so
a break rides in a minor — state it in the notes rather than breaking quietly.

## A `lib/` change is up to five releases

`make sync-check` keeps the vendored copies identical on `main`. That is
agreement, not delivery: a user gets the fixed parser only in the plugins
whose version moved. Decide which guards the change moves a decision for, and
cut each one. Say which suites you re-ran, as for any `lib/` change.

## The version string lives in three places

Per plugin, and all three must agree:

- `plugins/<name>/.claude-plugin/plugin.json` → `"version"`
- `.claude-plugin/marketplace.json` → the entry whose `"name"` is that plugin.
  Not `plugins[0]`: the five entries are ordered workspace-guard,
  branch-guard, prod-guard, exit-status-guard, foreground-guard, and
  `plugins[0]` is what the retired repositories' runbooks all said.
- `README.md` → the version column of the **The guards** table. This is the
  one that gets forgotten, and it is the reason the bump is no longer a pure
  two-line commit.

Nothing gates the agreement. Check it before and after the bump:

```
python3 - <<'PY'
import json, re, pathlib
mk = json.load(open('.claude-plugin/marketplace.json'))
readme = pathlib.Path('README.md').read_text()
for entry in mk['plugins']:
    name = entry['name']
    pj = json.load(open(f'plugins/{name}/.claude-plugin/plugin.json'))['version']
    row = re.search(r'\[%s\]\([^)]*\) \| ([0-9.]+)' % re.escape(name), readme)
    versions = {entry['version'], pj, row.group(1) if row else 'MISSING'}
    print(('ok  ' if len(versions) == 1 else 'DIFF'), name, sorted(versions))
PY
```

## Delivery comes from `main`, not from the tag

Claude Code installs a plugin from the marketplace clone at `main`, and
`claude plugin update` compares the version *string* in the marketplace entry.
The version is therefore live for anyone who updates the marketplace the
moment the release pull request (PR) merges. The tag and the GitHub Release
are the record — the notes, the changelog chain — not the delivery.

Two consequences. A wrong version in `marketplace.json` ships and a tag typo
does not, so the manifest is the line to re-read. And a merge that bumps a
version without a tag behind it is a silent release: keep the bump in the
release PR and tag as soon as it lands.

## Steps

1. **Pick the plugin and the level.** The previous tag is
   `git tag --list '<plugin>/v*' --sort=-v:refname | head -1`. For a plugin's
   first release from this repo there is none, and the previous version is the
   retired repository's last tag.

2. **Harvest the notes** (below), draft the body, and put it in
   `plugins/<name>/docs/releases/vX.Y.Z.md`.

3. **Open one release PR** carrying the notes file and all three version
   locations. Commit and title in the repo's convention:
   `chore(release): <plugin> X.Y.Z`. Answer the `## Release note` block with
   `None` — a version bump moves no decision.

   This is one PR, not the notes-then-bump split the retired repositories
   used, and there is no direct-to-`main` push any more. That exception
   existed to keep a two-line bump off a merge commit. The bump is not two
   lines now that the README table is in it, and five plugins share this
   `main`.

4. **Merge, then tag the squash commit.** `tests.yml` runs on push to `main`,
   so the commit you are about to tag gets its own run. Tag a green one.

   ```
   git switch main && git pull --ff-only
   git tag -a 'workspace-guard/v1.10.1' -m 'workspace-guard 1.10.1' <sha>
   git push origin 'workspace-guard/v1.10.1'
   ```

   Two commands, never chained. branch-guard asks on any tag publish under
   `strict` — `refs/tags/…`, a bare name, and `--tags` all ask — and a denied
   `git tag … && git push …` loses the tag creation along with the push. Under
   `dontAsk` and `bypassPermissions` that ask converts to a deny and retrying
   will not help (`NON_INTERACTIVE_MODES`, `plugins/branch-guard/hooks/branch-guard.py`).
   `auto` prompts, so it finishes.

5. **Publish from the file.**

   ```
   gh release create 'workspace-guard/v1.10.1' \
     --title 'workspace-guard 1.10.1' \
     --latest=false \
     --notes-file plugins/workspace-guard/docs/releases/v1.10.1.md
   ```

   `--latest=false` on every release here. The repository-level Latest badge
   points at one release, and whichever plugin it names is the wrong answer
   for the other four. The title drops the slash because it reads better in
   the list. The tag keeps it.

## Harvesting the notes

Every PR body ends with a `## Release note` block — see
[`.github/pull_request_template.md`](../../.github/pull_request_template.md) —
holding one line written while the author still had the change in their head.
Those lines are the notes, not raw material for them.

The range needs both ends narrowed: a prefixed tag, and the paths that reach
this plugin, because `main` carries four others.

```
prev="$(git tag --list 'workspace-guard/v*' --sort=-v:refname | head -1)"
for pr in $(git log --oneline "$prev"..HEAD -- plugins/workspace-guard lib \
            | grep -oE '\(#[0-9]+\)' | grep -oE '[0-9]+'); do
  printf '#%s  ' "$pr"
  gh pr view "$pr" --json body --jq .body | python3 scripts/release-note.py
done
```

`scripts/release-note.py` is the same extractor the `release-note` CI check
runs on every PR, so the harvest and the gate cannot drift into disagreeing
about what counts as an answer. Four outcomes:

| output | meaning | what to do |
|---|---|---|
| a note line | the hook behaves differently for someone running it | it is the bullet — edit for the release's voice, do not rewrite from the diff |
| `None` | no decision moved, nothing an operator sees changed | fold into the changelog link |
| `!! UNANSWERED` | the section is there and empty | read the diff, and ask how it merged — the `release-note` check should have blocked it |
| `!! NO SECTION` | no section at all | the PR predates the template, or it was dropped. Reconstruct from the diff |

`None` is a claim about the notes rather than about the code. A `None` sitting
beside a diff that moves a decision is the one failure CI cannot catch, and it
is visible at tag time.

## PR numbers restarted at #1

The five histories came with the move, so `git log` reaches every original
commit. The numbers in their subjects did not: `(#175)` is workspace-guard's,
`(#103)` is branch-guard's, and in a body published from this repo GitHub
auto-links a bare `#175` to claude-bouncer#175 — a different PR, or none yet.

Write full URLs to the retired repository for anything merged before the move,
and resolve any bare `#N` you keep. The boundary per plugin is its subtree
commit, `Add 'plugins/<name>/' from commit …`. Everything reachable from its
second parent came from the old repository.

## Where the notes live

`plugins/<name>/docs/releases/vX.Y.Z.md`, one file per tag, holding the body
verbatim with no title heading. workspace-guard, branch-guard, and
exit-status-guard have the directory and a README stating the convention.
prod-guard and foreground-guard have neither: copy
[`plugins/workspace-guard/docs/releases/README.md`](../../plugins/workspace-guard/docs/releases/README.md)
and fix its paths when you cut their first release.

Close the body with the compare link, both ends prefixed:

```
**Full Changelog**: https://github.com/karlkfi/claude-bouncer/compare/workspace-guard/v1.10.0...workspace-guard/v1.10.1
```

The link 404s until the tag is pushed, which is expected while the release PR
is open. Open it once after publishing — a compare between two slash-bearing
refs is worth seeing resolve rather than assuming.

For a plugin's first release from this repo there is no previous tag here, and
a compare cannot span repositories. Drop the line and point at the last
release of the retired one instead:

```
Previous release: https://github.com/karlkfi/claude-workspace-guard/releases/tag/v1.10.0
```

## The two release scripts do not run here

Both came from their own repositories and neither survived the move. Fix
before use. Do not run either as it stands.

- `plugins/foreground-guard/scripts/cut-release.sh` reads
  `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` from the
  repository root, bumps `plugins[0]` (workspace-guard's entry), targets
  `karlkfi/claude-foreground-guard`, runs the root `tests/` suite — the shared
  parser, not foreground-guard's — pushes the bump straight to `main`, and
  tags `vX.Y.Z`.
- `plugins/branch-guard/scripts/verify-release-notes.sh` derives each tag from
  a notes filename, so it looks up `v1.9.0`, which no longer exists. What it
  does is still right: diff every published body against the file it was
  published from.

## Anti-patterns

- **Bumping one or two of the three version locations.** The README row is the
  one that gets missed, and nothing fails when it does.
- **A bare `vX.Y.Z` tag.** It claims all five plugins and collides with the
  next guard to release.
- **Tagging before the release PR merges,** or merging it and leaving the tag
  for later. The merge is the delivery. The tag records what was delivered.
- **Releasing all five because `lib/` changed.** Release the guards whose
  decisions moved.
- **A bare `#NNN` in notes for anything merged before the move.** It links to
  the wrong repository's PR numbering.
- **`--latest`.** It hands the repository's Latest badge to one of five
  plugins.
- **Writing the body anywhere but the notes file.** An inline `--notes`, a
  `--generate-notes`, or a fix typed into the web form all ship prose nobody
  reviewed, and the next `--notes-file` publish reverts the fix silently.
