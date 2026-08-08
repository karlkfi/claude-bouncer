# Agent reference: Cutting a release

A release is four artifacts that must agree: the **version string** (in two files), a **notes file** under [`docs/releases/`](../releases/), an **annotated git tag**, and a **GitHub Release**. This doc is the checklist for producing all four consistently. Releases are the one place where a commit lands on `main` without a PR — that exception is deliberate and scoped to the version bump only (see §The direct-to-main exception).

## The version string lives in exactly two files

Both must be bumped together and kept identical:

- `.claude-plugin/plugin.json` → `"version"`
- `.claude-plugin/marketplace.json` → `plugins[0].version`

Nothing else in the repo encodes the version (no README badge, no `__version__`). If you add a third location, add it here too. To confirm before bumping:

```
grep -rn '"version"' .claude-plugin/
```

## Pre-flight: the session must be able to push

**Check this before drafting anything.** Steps 7 and 8 push to `main` and push a tag, and branch-guard confirms any push whose target isn't the worktree branch. Under `auto`/`dontAsk` there is nobody to answer, so both are denied outright — a release started in the wrong mode gets all the way through notes, review, and merge before it discovers it cannot finish.

Run the release interactively, or plan to hand steps 7–9 to a terminal. Everything up to and including the notes pull request (PR) works in any mode; only the last three steps need the prompt.

## Steps

1. **Start from a fresh `main`.** Releases must include everything merged. Rebase the worktree:

   ```
   git fetch origin main && git rebase origin/main
   ```

2. **Run the full test suite — it must be green.**

   ```
   python3 -m unittest discover tests
   ```

3. **Pick `X.Y.Z`.** Patch (`Z`) for fixes, docs, and packaging; minor (`Y`) for new guarded commands or hook surface; major (`X`) for a default-behavior change. Most releases are patch. The notes file is named after it, so the choice comes first.

4. **Write the notes to `docs/releases/vX.Y.Z.md` and land them through a pull request (PR).** See §Release notes for drafting the body and [`docs/releases/README.md`](../releases/README.md) for what belongs in the file. Landing them before the bump is what puts a tag's own notes inside the tagged commit, and it is the only step that gets the prose reviewed.

5. **Bump both version files** to `X.Y.Z`.

6. **Commit the bump alone** — no other changes in this commit:

   ```
   git commit -am "chore(release): bump version to X.Y.Z"
   ```

7. **Push the bump straight to `main`** (see §The direct-to-main exception):

   ```
   git push origin HEAD:main
   ```

8. **Tag the bump commit** with an annotated tag whose message is just the version:

   ```
   git tag -a vX.Y.Z -m "vX.Y.Z" <bump-commit-sha>
   git push origin vX.Y.Z
   ```

   This push and the one in step 7 both need an interactive permission mode (see §Pre-flight); retrying under `auto` won't help. Create the tag in its own command: chained as `git tag … && git push …`, the deny takes out the `git tag` too and the tag never gets created.

9. **Create the GitHub Release** on that tag, marked latest, from the notes file:

   ```
   gh release create vX.Y.Z --title "vX.Y.Z" --latest --notes-file docs/releases/vX.Y.Z.md
   ```

   Never pass `--notes` inline or leave the body to the web form — that is the habit this file layout exists to break. To correct a published body, fix the file, land the fix, and re-publish with `gh release edit vX.Y.Z --notes-file docs/releases/vX.Y.Z.md`.

## The direct-to-main exception

Feature and fix work goes through PRs; the release bump does **not**. The bump commit is pushed directly to `main` and then tagged. This matches every prior release (`v1.0.0`, `v1.0.1`, `v1.0.2`) and keeps the tag pointing at a commit that exists on `main` with no merge-commit indirection.

This is the *only* sanctioned direct-to-main push. It is narrow by design: a two-line version bump with no logic. Anything bundled with substantive code would need a PR — so keep the bump commit pure. The standing rules still hold: never force-push `main`, and never bundle unrelated changes into the bump.

## Release notes

Every published body is in [`docs/releases/`](../releases/), so read a real one rather than guessing. [`v1.0.2.md`](../releases/v1.0.2.md) is the shape of a routine patch; [`v1.8.0.md`](../releases/v1.8.0.md) is the shape of a large release — grouped sections, a `> [!IMPORTANT]` callout for a behavior change an upgrader will feel, and a closing line stating what was validated.

The invariants:

- A one-line intro summarizing the release theme (e.g. "Patch release: a parsing hardening fix and docs improvements.").
- A bullet per notable PR: `* <title> by @<author> in <PR-url>`. Curate — highlight user-facing changes; routine chores can be folded into the changelog link.
- A trailing `**Full Changelog**: https://github.com/karlkfi/claude-workspace-guard/compare/v<PREV>...vX.Y.Z` line. It 404s until the tag is pushed; that is expected while the notes PR is in review.

### Start from the notes their authors wrote

Every PR body ends with a `## Release note` block — see [`.github/pull_request_template.md`](../../.github/pull_request_template.md) — holding one line written at PR time, while the author still had the change in their head. Harvest those first. They are the notes, not raw material for them:

```
for pr in $(git log --oneline v<PREV>..HEAD | grep -oE '\(#[0-9]+\)' | grep -oE '[0-9]+'); do
  note=$(gh pr view "$pr" --json body --jq .body \
    | awk '/^## Release note/{f=1;next} /^## /{f=0} f' \
    | awk '/<!--/{c=1} !c; /-->/{c=0}' | grep -v '^[[:space:]]*$')
  printf '#%s  %s\n' "$pr" "${note:-(no release-note block)}"
done
```

The second `awk` drops the template's instructions, which stay in the body as an HTML comment even when the author fills the section in.

Read the two empty answers differently. `None` is an answer: the author weighed it and nothing is user-facing, so fold that PR into the changelog link. `(no release-note block)` is a gap — nobody was asked, or nobody answered — so reconstruct that one from its diff, the way every release before this template was written.

To enumerate what shipped since the last tag, and to catch anything the harvest missed:

```
git log --oneline v<PREV>..HEAD
```

For a first draft in the bullet shape, generate the notes into the file directly. This endpoint only computes a body — it creates and modifies nothing, so it is safe to run before the tag exists:

```
gh api repos/karlkfi/claude-workspace-guard/releases/generate-notes \
  -f tag_name=vX.Y.Z -f previous_tag_name=v<PREV> --jq .body > docs/releases/vX.Y.Z.md
```

Then write the intro, prune the bullets, and group them. Do not reach for `gh release create --generate-notes`: it publishes an unreviewed body and leaves no file behind.

## Anti-patterns to watch for

- **Bumping only one of the two version files.** They must stay identical; a mismatch ships a marketplace listing that disagrees with the installed plugin.
- **Routing the bump through a PR.** The established flow is direct-to-main; a PR adds a merge commit the tag then has to point around.
- **Bundling code or docs into the bump commit.** That turns the sanctioned direct-to-main push into an unsanctioned one. Land everything else first, then bump.
- **Tagging before pushing the bump.** Push `main` first, then tag the commit that's now on `main`, so the tag is never orphaned on a branch.
- **Skipping the GitHub Release.** A tag without a Release breaks the "Full Changelog" chain and the Latest marker; every prior tag has a matching Release.
- **Writing the body anywhere but the file.** An inline `--notes`, a `--generate-notes`, or a paste into the web form all ship prose nobody reviewed and leave `docs/releases/` missing a version. Same for fixing a typo on github.com: the file is the source of truth, so the next `--notes-file` publish reverts the fix silently.
- **Bundling the notes PR into the bump commit.** The notes are ordinary docs and go through review; the bump stays a pure two-line change. Land the notes first, then bump.
