# Release notes

One file per released tag, named `vX.Y.Z.md`. Each file **is** the body of the corresponding
GitHub Release — the repo is the source of truth, and github.com is a rendering of it.

Notes used to be typed straight into the web form, which meant the most-read prose the project
publishes never appeared in a diff and could not be reviewed, corrected, or grepped. Authoring
them here fixes that.

## The files are bodies, not documents

A file is the body byte for byte, terminator included. Every body ends with a final newline, so
every file here does; a body published without one would be stored without one. In particular:

- **No `# vX.Y.Z` heading.** The tag name is already the Release title; adding one renders it
  twice. This is why these files open mid-thought instead of with a title like every other doc
  in the tree.
- **Absolute URLs, not repo-relative links.** The body renders on github.com outside any file
  context, so `[Limitations](README.md#limitations)` resolves to nothing. Link to a path under
  `blob/vX.Y.Z/` so it keeps pointing at the code that shipped.

Publishing is therefore a copy, and re-publishing is idempotent:

```bash
gh release edit 'prod-guard/v2.5.2' --notes-file plugins/prod-guard/docs/releases/v2.5.2.md
```

## Fix a mistake in the file, not on the website

Editing a body in the web UI puts it out of sync with the file, and the next `--notes-file`
publish silently reverts it. Correct the file, open a pull request (PR) like any other docs
change, and re-publish from it after merge. A body published from
`karlkfi/claude-prod-guard` is corrected there, with `--repo` and its bare `vX.Y.Z` tag.

## Verifying

From the repository root — every plugin, or just the ones named as arguments:

```bash
scripts/verify-release-notes.sh prod-guard
```

A release cut from this repository is tagged `prod-guard/vX.Y.Z`. Notes files predating the
move were published from `karlkfi/claude-prod-guard` as a bare `vX.Y.Z` and still resolve
there, so the script checks each file against whichever of the two published it.

The comparison is byte-exact, so it also catches a trailing newline appearing or disappearing.
Do not hand-roll it with `--json body --jq .body`: `--jq` appends a newline unconditionally, so
it reports a body that ends without one as matching and a body that ends with one as carrying a
stray line. Both readings are wrong and they point in opposite directions. The script uses
`--template '{{.body}}'`, which returns the bytes.

The full runbook — version bump, tag, publish — is in
[`../development/release-process.md`](../development/release-process.md).
