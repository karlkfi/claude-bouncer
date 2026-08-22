# Agent reference docs

Read the relevant doc before starting a task in that area. These pages are written for agents and contributors working *on* prod-guard; users of the plugin start at the [`README.md`](../../README.md) in the repository root.

| Doc | Covers |
|---|---|
| [release-process.md](release-process.md) | Cutting a release: the version string in its two files, the annotated tag, the GitHub Release, and the scoped direct-to-`main` exception. |
| [rendering-images.md](../../../../docs/development/rendering-images.md) | Regenerating the brand assets in `docs/img/` (social preview, favicon, icon) from their scalable vector graphics (SVG) masters. Lives at the repository root: every plugin's assets are rendered by the same procedure. |
| [skills.md](skills.md) | The globally installed agent skills this repo's docs invoke: what each is for, where it fires here, and why a page links this index rather than the private repo they come from. A contributor without them loses nothing; every rule is written out in this tree. |

The backlog is shared with the other four guards and lives at the repository root; editing it is covered by [`maintaining-backlog.md`](../../../../docs/development/maintaining-backlog.md).

Design rationale and the threat models live in [`docs/design.md`](../design.md); the plan documents for individual pieces of work live in [`docs/plan/`](../plan/).
