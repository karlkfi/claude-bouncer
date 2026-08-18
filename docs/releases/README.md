# Release notes

One file per tagged release, holding the published GitHub Release body
**verbatim** — no front matter, and no title heading, because the Releases page
renders the tag as the page `<h1>` and a `# vX.Y.Z` would duplicate it.

Publish, or republish after an edit, with:

```bash
gh release edit vX.Y.Z --notes-file docs/releases/vX.Y.Z.md
```

Authoring here rather than in the web form is what makes each fix a diff and
each published body reproducible from a commit. The invariant is that this file
matches the published body — so an edit to the notes lands as a PR and is then
republished, never typed into the Release.

These files target GitHub's comment-flavour renderer, where a single newline
becomes a `<br>`. Do not hard-wrap paragraphs or list items; keep each on one
line however long it gets. In-page anchors do not work in a release body, since
headings there carry no `id` — refer to a section by name in bold instead.
