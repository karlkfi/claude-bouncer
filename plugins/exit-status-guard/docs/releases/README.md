# Release notes

One file per tagged release, holding the published GitHub Release body
**verbatim** — no front matter, and no title heading, because the Releases page
renders the tag as the page `<h1>` and a `# vX.Y.Z` would duplicate it.

Publish, or republish after an edit, with:

```bash
gh release edit 'exit-status-guard/v2.0.1' --notes-file plugins/exit-status-guard/docs/releases/v2.0.1.md
```

A body published from `karlkfi/claude-exit-status-guard` is corrected
there instead, with `--repo` and its bare `vX.Y.Z` tag.

Authoring here rather than in the web form is what makes each fix a diff and
each published body reproducible from a commit. The invariant is that this file
matches the published body — so an edit to the notes lands as a PR and is then
republished, never typed into the Release.

These files target GitHub's comment-flavour renderer, where a single newline
becomes a `<br>`. Do not hard-wrap paragraphs or list items; keep each on one
line however long it gets. In-page anchors do not work in a release body, since
headings there carry no `id` — refer to a section by name in bold instead.

## Where the contents come from

The `release-note` block in `.github/pull_request_template.md`, filled in when
the PR is opened. Collect the blocks since the previous tag and the change list
is already written; what is left is ordering it by what a reader needs first,
and writing the danger banner and the upgrade steps, which no per-PR note can
supply.

Reconstructing that list at tag time instead — from commit subjects, PR titles,
and diffs — is what v1.0.1 had to do, and it is the expensive half of cutting a
release. It also under-reports: commit subjects name what was changed, not what
it does to someone on the previous version.

## Verifying

From the repository root — every plugin, or just the ones named as
arguments:

```bash
scripts/verify-release-notes.sh exit-status-guard
```

A release cut from this repository is tagged `exit-status-guard/vX.Y.Z`.
Notes files predating the move were published from
`karlkfi/claude-exit-status-guard` as a bare `vX.Y.Z` and still resolve
there, so the script checks each file against whichever of the two
published it.

The comparison is byte-exact, so it also catches a trailing newline
appearing or disappearing. Do not hand-roll it with
`--json body --jq .body`: `--jq` appends a newline unconditionally, so it
reports a body that ends without one as matching and a body that ends
with one as carrying a stray line. Both readings are wrong and they point
in opposite directions. The script uses `--template '{{.body}}'`, which
returns the bytes.

The full runbook — version bump, tag, publish — is in
[`docs/development/release-process.md`](../../../../docs/development/release-process.md).
Unlike its four siblings, exit-status-guard carries no per-plugin pointer file,
so this link goes to the repository root.
