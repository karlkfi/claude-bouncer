# Release notes

One file per stable tag, `vX.Y.Z.md`, holding the body of that GitHub Release verbatim. Notes are
authored here and published from here, so the body that ships is reviewable in a diff and
reproducible from a commit.

## The invariant is "matches the published body", not "frozen at the tag"

A file here tracks what the Release **currently says**, not what it said the day it was tagged. So
a correction to an already-published body — a broken link, a wrong spec count, a clarification
someone asked for — is expected to change the file too, in the same change that changes the
Release. The two are one artifact stored twice.

That makes the browser's "Edit release" box the thing to avoid: it changes one copy and leaves no
diff. Re-publish from the file instead:

```bash
gh release edit 'branch-guard/v1.9.1' --notes-file plugins/branch-guard/docs/releases/v1.9.1.md
```

A body published from `karlkfi/claude-branch-guard` is corrected there instead, with `--repo`
and its bare `vX.Y.Z` tag.

## Format

No front matter and no `# vX.Y.Z` title heading — the Releases page renders the tag as the page
h1, so a title in the body duplicates it. The file starts with the first line of the body.

See the [release runbook](../development/release-process.md) for which sections a body carries and
when each one applies.

## Verifying

From the repository root — every plugin, or just the ones named as arguments:

```bash
scripts/verify-release-notes.sh branch-guard
```

A release cut from this repository is tagged `branch-guard/vX.Y.Z`. Notes files predating the
move were published from `karlkfi/claude-branch-guard` as a bare `vX.Y.Z` and still resolve
there, so the script checks each file against whichever of the two published it.

The comparison is byte-exact, so it also catches a trailing newline appearing or disappearing.
Do not hand-roll it with `--json body --jq .body`: `--jq` appends a newline unconditionally, so
it reports a body that ends without one as matching and a body that ends with one as carrying a
stray line. Both readings are wrong and they point in opposite directions. The script uses
`--template '{{.body}}'`, which returns the bytes.

Every body ends with a final newline, and so does every file here: the four published without
it — `v1.1.0`, `v1.2.0`, `v1.3.0`, `v1.3.1` — were re-published to add it rather than stored
short to match, so this convention and git's are the same rule. An editor that drops the final
byte shows up here. That is the check working: the file no longer matches what is published, and
the fix is to re-publish it.
