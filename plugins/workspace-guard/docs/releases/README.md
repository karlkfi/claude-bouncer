# Release notes

One file per released tag, named `vX.Y.Z.md`. Each file **is** the body of the corresponding
GitHub Release — the repo is the source of truth, and github.com is a rendering of it.

Notes used to be typed straight into the web form, which meant the most-read prose the project
publishes never appeared in a diff and could not be reviewed, corrected, or grepped. Authoring
them here fixes that.

## The files are bodies, not documents

A file is the body byte for byte, terminator included: a body published without a final
newline is stored without one. In particular:

- **No `# vX.Y.Z` heading.** The tag name is already the Release title; adding one renders it
  twice. This is why these files open mid-thought instead of with a title like every other doc
  in the tree.
- **Absolute URLs, not repo-relative links.** The body renders on github.com outside any file
  context, so `[Limitations](README.md#limitations)` resolves to nothing. Link to a path under
  `blob/vX.Y.Z/` so it keeps pointing at the code that shipped.

Publishing is therefore a copy, and re-publishing is idempotent:

```bash
gh release edit 'workspace-guard/v1.10.1' --notes-file plugins/workspace-guard/docs/releases/v1.10.1.md
```

## Fix a mistake in the file, not on the website

Editing a body in the web UI puts it out of sync with the file, and the next `--notes-file`
publish silently reverts it. Correct the file, open a pull request (PR) like any other docs
change, and re-publish from it after merge.

To confirm a published body still matches its file:

```bash
diff <(gh release view v1.8.0 --repo karlkfi/claude-workspace-guard --json body --template '{{.body}}') docs/releases/v1.8.0.md
```

Every release described here was published from `karlkfi/claude-workspace-guard`, so the
`--repo` flag is what makes the lookup resolve at all. It drops once a release is cut from
this repo, where the tag is `workspace-guard/vX.Y.Z`.

Do not reach for `--json body --jq .body` here. `--jq` appends a newline
unconditionally, so it reports a body that ends without one as matching, and a body that ends
with one as carrying a stray blank line. Both readings are wrong and they point in opposite
directions. `--template '{{.body}}'`, which the script uses, returns the bytes.

The full runbook — version bump, tag, publish — is in
[`../development/release-process.md`](../development/release-process.md).
