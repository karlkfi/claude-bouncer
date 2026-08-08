<!--
Body structure is free-form — use whatever headings fit the change (## What / ## How /
## Testing / ## Docs is the common shape). The one required section is the release note
at the bottom.
-->

## What

## Testing

## Release note

<!--
Answer with a note or with `None`. This section ships empty on purpose: leaving it empty
reads as unanswered at release time, not as "nothing to say".

Write a note when the hook behaves differently for the person running it — a command's
decision moves (starts prompting, stops prompting, starts denying), a new command or flag
is guarded, a message an operator reads changes, or a new env var or config surface
appears. One line, in the voice of a release bullet: what changed for that person, not
what the diff did.

  Unanchored `pkill` patterns now deny. Anchor the pattern to the project root, or set
  WORKSPACE_GUARD_OVERRIDE for a deliberate cross-workspace kill.

Answer `None` when no decision moves and nothing an operator sees changes: tests,
refactors, internal parsing cleanups that preserve every decision, docs, CI, backlog rows.
`None` means "no bullet in the release notes — fold this PR into the changelog link."

At tag time these lines are collected and become the notes under `docs/releases/`, so
write the note itself here rather than raw material for one.
-->

