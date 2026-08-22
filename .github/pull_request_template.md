<!--
Body structure is free-form — use whatever headings fit the change. The two required
sections are the verification and the release note.

This repo holds five plugins. Name the one you changed in the title, the way the
commit does: `feat(workspace-guard): …`, `fix(lib): …`. A change under `lib/` reaches
every guard, so say which ones you re-ran.
-->

## What

## Verification

<!--
Both directions, since both fail silently: the rule still fires on what it should, and
still stays quiet on what it should not.

Then the part that makes a green run mean something — what you broke, and what went red
when you broke it. A new assertion that has never failed is not yet evidence.
-->

## Release note

<!--
Answer with a note or with `None`. This section ships empty on purpose: leaving it empty
reads as unanswered at release time, not as "nothing to say".

Write a note when a hook behaves differently for the person running it — a command's
decision moves (starts prompting, stops prompting, starts denying), a new command or flag
is guarded, a message an operator reads changes, or a new env var or config surface
appears. One line, in the voice of a release bullet: what changed for that person, not
what the diff did. Open with `action required:` if upgrading needs a manual step.

  Unanchored `pkill` patterns now deny. Anchor the pattern to the project root, or set
  WORKSPACE_GUARD_OVERRIDE for a deliberate cross-workspace kill.

Answer `None` when no decision moves and nothing an operator sees changes: tests,
refactors, internal parsing cleanups that preserve every decision, docs, CI, backlog rows.
`None` means "no bullet in the release notes — fold this PR into the changelog link."

At tag time these lines are collected and become the notes under `docs/releases/`, so
write the note itself here rather than raw material for one.
-->

