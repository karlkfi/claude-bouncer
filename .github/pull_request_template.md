<!--
Body structure is free-form — use whatever headings fit the change (## What / ## How /
## Testing / ## Docs is the common shape). The one required section is the release note
at the bottom.
-->

## What

## Testing

## Release note

<!--
Written now, while you still have the context. At tag time the release notes under
`docs/releases/` are drafted from these, so this line is the note — not raw material for
one. Write it in the voice of a release bullet: what changed for someone running the hook,
not what the diff did.

  A `cd` with an unresolvable target no longer silently drops path tracking; later
  relative paths in the same command prompt instead of passing.

Leave `None` when nothing user-facing changed — a test, a refactor, a backlog row, a
docs-only edit. `None` is an answer, not a skip: it tells the release drafter this PR was
considered.
-->

None
