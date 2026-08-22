---
description: Cut a release for one plugin, or for every plugin with changes worth releasing
argument-hint: "[plugin] [major|minor|patch]  — no argument surveys all five"
---

Cut a release. `docs/development/release-process.md` is the authority: read it
before deviating from anything below, and do not restate it back to the user.

`$ARGUMENTS` is empty (survey all five), a plugin name, or a plugin name and a
level. A level given here is the user's call — do not talk them out of it, but
say so if the evidence points elsewhere.

## 1. Get the facts

```
python3 scripts/release.py status --notes $ARGUMENTS
```

Each plugin comes back with its version, its delta since its own last tag, the
`## Release note` line each author wrote, and a verdict of `release`, `hold`, or
`none`. **The verdict is a starting point, not the answer.** It reads commit
types and harvested notes; it cannot read a diff. A `None` sitting beside a
change that moves a decision is the one failure CI cannot catch, so read the
commits under any `hold` before accepting it.

## 2. Choose the level, from what the change did to the guard's decisions

The runbook's axis, not a generic SemVer table: did the set of decisions move,
in either direction? Patch is the same decisions, better. Minor is the boundary
moving, including new denials. Major is identity or configuration breaking —
a rename, a removed override, an inverted default verdict.

State the level per plugin with the evidence you chose it from, then confirm
the whole plan with the user before writing anything. For a multi-plugin cut,
one release PR carries every plugin, and each gets its own tag afterwards.

## 3. Write the notes

One file per tag at `plugins/<name>/docs/releases/vX.Y.Z.md`, holding the body
verbatim with no title heading. Read a shipped one first — the three plugins
with a `docs/releases/` directory have thirty between them. Harvested notes
are the bullets, not raw material for them.

Anything merged before the consolidation needs a full URL rather than a bare
`#NNN`: PR numbers restarted at #1 here, so `#175` links to the wrong repo's
pull request.

Put the draft through `deslop` and `verify-claims` before opening the PR. These
notes are the most-read prose the project ships, and a published body is
corrected only by fixing the file, landing it, and re-publishing.

## 4. Bump and open one PR

```
python3 scripts/release.py bump <plugin> <level>
```

That writes all three version locations and refuses if they disagree first. Do
it for each plugin in the cut, then open one PR carrying the notes files and
the bumps. Title `chore(release): <plugin> X.Y.Z`, or naming the set. Answer
the `## Release note` block with `None`.

Stop here and hand back. The merge is the delivery — Claude Code compares the
version string in `marketplace.json`, so the release goes live to anyone who
updates the marketplace the moment the PR lands.

## 5. Tag each plugin, after the merge

```
python3 scripts/release.py tag <plugin>
```

It verifies the notes file exists, that the tag is free locally and on origin,
and that HEAD is `origin/main` — then creates the annotated tag and prints the
two publish commands. Run them separately, never chained: branch-guard asks on
a tag push, and a denied `git tag && git push` loses the tag creation with it.
Both need an interactive permission mode; under `dontAsk` or `bypassPermissions`
the ask becomes a deny and retrying will not help.

Verify each published body against its file before announcing, and never retag
a published tag — supersede it with a higher patch.
