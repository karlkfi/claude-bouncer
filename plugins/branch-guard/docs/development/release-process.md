# Agent reference: Cutting a release

The runbook is repo-wide now:
[`docs/development/release-process.md`](../../../../docs/development/release-process.md).

Releasing one guard touches files outside its own directory — the marketplace
manifest and the README version table both live at the repository root, and the
tag names the plugin (`branch-guard/vX.Y.Z`) because five version lines share one tag
namespace. There is nothing left that is true of branch-guard alone.

What used to be here described the release this plugin cut from its own
repository: a bare `vX.Y.Z` tag, a version in `plugins[0]` of a manifest
beside the plugin, and a bump pushed straight to `main`. None of the three
holds after the move. Read the root runbook rather than restoring this file
from history.
