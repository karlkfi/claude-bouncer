# Agent reference: Cutting a release

A release is three artifacts that must agree: the **version string** (in two files), an **annotated git tag**, and a **GitHub Release**. This doc is the checklist for producing all three consistently. Releases are the one place where a commit lands on `main` without a PR — that exception is deliberate and scoped to the version bump only (see §The direct-to-main exception).

## The fast path: `scripts/cut-release.sh`

`scripts/cut-release.sh` automates every step below with the same guardrails, and refuses each anti-pattern in §Anti-patterns (dirty tree, non-fast-forward `main`, one-sided/backwards bump, red tests, pre-existing tag). Run it from a checkout level with `origin/main`:

```
scripts/cut-release.sh --dry-run minor   # preview the plan + generated notes, mutate nothing
scripts/cut-release.sh minor             # or patch | major | an explicit X.Y.Z
```

It bumps both files, commits, pushes to `main`, tags, and publishes the Release — pausing once for confirmation and once (in `$EDITOR`) to curate auto-generated notes. Choosing the bump level is still yours (see step 3). The rest of this doc is the manual procedure it encodes — the reference when the script can't run or you need to do a step by hand.

## The version string lives in exactly two files

Both must be bumped together and kept identical:

- `.claude-plugin/plugin.json` → `"version"`
- `.claude-plugin/marketplace.json` → `plugins[0].version`

Nothing else in the repo encodes the version (no README badge, no `__version__`). If you add a third location, add it here too. The two files staying in lockstep is enforced by `WiringTests.test_plugin_and_marketplace_agree` in `tests/test_foreground_guard.py`, so a one-sided bump fails the suite. To confirm the current value before bumping:

```
grep -rn '"version"' .claude-plugin/
```

## Steps

1. **Start from a fresh `main`.** Releases must include everything merged. Rebase the worktree:

   ```
   git fetch origin main && git rebase origin/main
   ```

2. **Run the full test suite — it must be green.**

   ```
   python3 -m unittest discover tests
   ```

3. **Bump both version files** to the new `X.Y.Z`. Patch (`Z`) for fixes, docs, and packaging; minor (`Y`) for a new guarded form (a `BUILTIN_WATCH` row, a Class B registry addition) or a change to the hook surface; major (`X`) for a default-behavior change (e.g. a class defaulting to `deny`). Most releases are patch.

4. **Commit the bump alone** — no other changes in this commit:

   ```
   git commit -am "chore(release): bump version to X.Y.Z"
   ```

5. **Push the bump straight to `main`** (see §The direct-to-main exception):

   ```
   git push origin HEAD:main
   ```

6. **Tag the bump commit** with an annotated tag whose message is just the version:

   ```
   git tag -a vX.Y.Z -m "vX.Y.Z" <bump-commit-sha>
   git push origin vX.Y.Z
   ```

7. **Create the GitHub Release** on that tag, marked latest:

   ```
   gh release create vX.Y.Z --title "vX.Y.Z" --latest --notes-file <notes.md>
   ```

   See §Release notes for the body format.

## The direct-to-main exception

Feature and fix work goes through PRs; the release bump does **not**. The bump commit is pushed directly to `main` and then tagged. This matches the sibling guard plugins' release flow and keeps the tag pointing at a commit that exists on `main` with no merge-commit indirection.

This is the *only* sanctioned direct-to-main push. It is narrow by design: a two-line version bump with no logic. Anything bundled with substantive code would need a PR — so keep the bump commit pure. The standing rules still hold: never force-push `main`, and never bundle unrelated changes into the bump.

## Release notes

The body is a curated document, not a generated changelog. Every release since v0.4.0 uses the same skeleton, and the latest body is the template:

```
gh release view "$(git tag --sort=-v:refname | head -1)" --json body --jq .body
```

The sections, in order:

- **Tagline** — one line saying what the plugin does, for the reader who arrived from a search result.
- **Highlights** — the changes worth upgrading for (three or four), each a paragraph opening with a bold claim and linking into the tagged README (`blob/vX.Y.Z/...`), never `main`.
- **Upgrading** — what an existing install must or may do, and an explicit "Nothing is deprecated or removed in this release" line when that is true (it usually is).
- **Configuration surface** — config keys added, removed, or changed in meaning since the previous release; state "no keys were added or removed" when measured true.
- **Everything since v\<PREV\>** — a count, then a bullet per PR: `* <title> by @<author> in <PR-url>`.
- **Validation** — receipts, not adjectives: the test count and Python versions with a link to the CI run on the release commit, the count at the previous release for contrast, what the new tests cover, and the never-`allow` invariant. Ends with a `<details>` fold titled "What the suite does not assert".
- **Security** — written even when there is nothing to report: no advisory, stdlib-only with no telemetry, a PRIVACY.md link pinned to the tag, and the productivity-guard-not-security-boundary framing.
- **Installing this release** — the `/plugin` commands, the `> [!NOTE]` callout that third-party marketplaces don't auto-update, and the sibling-guards paragraph explaining why the hook never emits `allow`.
- A trailing **Full changelog:** line linking the `v<PREV>...vX.Y.Z` compare.

Two rendering rules, because GitHub renders release bodies with comment-flavour GFM: never hard-wrap a paragraph (a single newline becomes `<br>`), and in-page anchors don't work — refer to sections by name.

`scripts/cut-release.sh` generates only the seed of this — the theme line, the feat/fix PR bullets, and the changelog link. Expand it to the full skeleton in the `$EDITOR` pause, or write the body first and pass `--notes-file`. To enumerate what shipped since the last tag:

```
git log --oneline v<PREV>..HEAD
```

## Anti-patterns to watch for

- **Bumping only one of the two version files.** They must stay identical; a mismatch ships a marketplace listing that disagrees with the installed plugin, and fails `test_plugin_and_marketplace_agree`.
- **Routing the bump through a PR.** The established flow is direct-to-main; a PR adds a merge commit the tag then has to point around.
- **Bundling code or docs into the bump commit.** That turns the sanctioned direct-to-main push into an unsanctioned one. Land everything else first, then bump.
- **Tagging before pushing the bump.** Push `main` first, then tag the commit that's now on `main`, so the tag is never orphaned on a branch.
- **Skipping the GitHub Release.** A tag without a Release breaks the "Full Changelog" chain and the Latest marker; every prior tag has a matching Release.
