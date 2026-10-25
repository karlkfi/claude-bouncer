---
reaches: opt-in-channel
gate: tag
reviewers: solo
merge: agent
---

# Merge posture

What a merge to `main` costs here, declared rather than derived. A dispatching
session reads it to route each pull request to a reviewer; nothing else
consumes it.

## reaches — opt-in-channel

`.claude-plugin/marketplace.json` lists all five guards with a relative
`source`, so Claude Code installs them from the marketplace clone at `main`:
a merge propagates without a tag, and CI cannot see that it does. Two things
bound how far. Adding the marketplace is an explicit step, so nobody receives
these guards by default. And an existing install moves only when
`claude plugin update` sees a new version string in the marketplace entry
(`docs/development/release-process.md`, "Delivery comes from `main`"), which
plugin auto-update never triggers — `autoUpdate: true` is persisted and nothing
consumes it (`anthropics/claude-code#73673`). A merge that bumps no version
reaches fresh installs and nobody else.

## gate — tag

The expensive human read belongs at the release: one plugin at a time, driven
by `/release`, where the notes are written and the version bump decides what
existing installs receive. Strictly the delivery is the release pull request's
merge, not the tag that follows it, since the version string in
`marketplace.json` is what `claude plugin update` compares. The runbook keeps
the two in one cut — bump in the release PR, tag the squash commit as soon as
it lands — so the release is the one place to read, and an ordinary merge is
not it.

## reviewers — solo

One maintainer. `main` has no classic branch protection and no CODEOWNERS file.
Its one ruleset, `protect-default`, blocks deletion and force-push and requires
linear history, and requires no review, so no mechanism holds a merge open for
a second reader. Every agent session on the maintainer's machine authenticates
as `karlkfi`, so a single PR author is one *account*; it is the absence of a
review requirement that carries this key, not the author count.

The checker below reports "2 distinct PR authors" and calls that a
disagreement. The second is `app/dependabot`, with one merged PR (`#32`) against
263 from `karlkfi`, as of 2026-10-25. A bot that opens its own PRs spends no
colleague's review, so the disagreement is expected and does not move this key.

## merge — agent

A standing grant: the dispatching session merges a pull request once it is
green and reviewed, with no `human-review` exceptions. This records practice
already in place — orchestrators here have been told to merge their own batches
— rather than starting it. Over 2026-09-10 to 2026-10-25, 103 agent merges
landed with no regression traced to them, and the p90 wait from ready to merged
fell from 23 hours for a maintainer merge to 27 minutes for an agent one
(measured by the dispatching session, not re-taken here).

What bounds an agent merge is the two keys above: it reaches fresh installs of
an opt-in marketplace, and an existing install moves only at a release the
maintainer cuts. `make check`, the per-plugin CI jobs, and the
`## Release note` gate on the pull request body all still have to pass.

## Re-taking the readings

    python3 ~/.claude/skills/session-orchestrator/scripts/merge-posture.py \
      karlkfi/claude-bouncer

It reports branch protection, CODEOWNERS, distinct authors, merge velocity, and
which workflows publish on a merge as against on a tag, then checks them
against this file. Exit 1 is a disagreement or a missing declaration. It sees
CI and nothing else, so its silence about what a merge reaches is not
agreement — that key is the one this file exists to supply.
