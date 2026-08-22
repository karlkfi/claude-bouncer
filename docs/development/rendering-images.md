# Agent reference: Rendering the brand images

Six directories hold brand assets, and all six are generated the same way: SVG
masters rasterized with [resvg](https://github.com/linebender/resvg), a single
static binary that needs no browser. `make images` runs the whole set.

| Directory | Covers |
| --- | --- |
| `docs/img/` | the repository itself |
| `plugins/<name>/docs/img/` | one plugin, for all five |

resvg is a dev-time tool, not a runtime dependency. The hooks are stdlib-only
Python and nothing here ships to users. The pipeline only regenerates the
committed rasters when a master changes.

## Source of truth

Edit the SVG masters. Never hand-edit a generated raster.

| Master (edit this) | Generated output(s) | Used for |
| --- | --- | --- |
| `social-preview.svg` | `social-preview.png` | GitHub social preview, README banner |
| `favicon.svg` | `favicon-16.png`, `favicon-32.png`, `favicon-48.png`, `favicon.ico` | browser tab favicon (transparent), README plugin table |
| `icon-tile.svg` | `apple-touch-icon.png` (180), `icon-512.png` (512) | iOS / PWA icons (opaque tile) |

`favicon.svg` and `icon-tile.svg` carry the same mark. The tile adds the opaque
dark background that iOS and PWA contexts require, because they ignore
transparency.

**Not generated:** `plugins/workspace-guard/docs/img/ask-prompt.png` is a
hand-captured screenshot of Claude Code's permission prompt, embedded in that
plugin's README. It has no SVG master. Re-shoot it manually if the prompt UI
changes.

## The marks

Every mark is the same shield in the same purple gradient, drawn from the same
88-unit path. Only the glyph inside it changes:

| Asset directory | Glyph | Stands for |
| --- | --- | --- |
| repo | velvet rope | the bouncer's line |
| workspace-guard | folder | the project root |
| branch-guard | git branch | the branch it will not push to |
| prod-guard | racked server stack | the production target |
| exit-status-guard | broken chain link | the status severed between gate and report |
| foreground-guard | hourglass | the main-thread time a foreground poll spends |

A glyph has to survive 16px. Test a new one at that size before committing to
it: detail that reads at 512px turns to noise in a favicon. Three candidates for
the repo mark were rendered at 16, 48, and 512 before one was picked, and the
two rejected ones failed at 48.

Keep about 7 units of clearance between the glyph and the shield outline, in the
88-unit space the mark is drawn in. That is what the four original glyphs hold:
the folder clears by 10.4, the servers by 9.1, the branch by 13.2, and the
hourglass by 7.2 at its tightest corner.

**Centre a diagonal glyph by its clearance, not on (36, 44).** The shield is
widest at the top and narrows to a point, so a glyph running lower-left to
upper-right has far less room at one end than the other. The chain link shipped
centred on the geometric middle and cleared the outline by 0.35 units at the
lower left while leaving 6.3 spare at the upper right — it read as shoved
against the left edge. Moving the centre to (38, 43) and shrinking the mark by
a ninth evens it out at 7.4 and 7.7. An upright glyph does not have this
problem, which is why only this one needed the offset.

## Prerequisites

- `resvg`. Install with `brew install resvg` or `cargo install resvg`.
- `python3` (stdlib only), which packs `favicon.ico`.

## Regenerate

```sh
make images                                # every directory
python3 scripts/render-images.py docs/img  # one directory
```

The masters name CSS system-font stacks (`-apple-system`, `ui-monospace`). Those
are keywords rather than font families, so the script passes concrete faces —
Helvetica Neue and Menlo — that resvg can resolve. Where those faces are
missing, name ones that are installed:

```sh
SANS_FAMILY="DejaVu Sans" MONO_FAMILY="DejaVu Sans Mono" make images
```

Then check the result. `file favicon.ico` should report three icons, and the
social preview should have no clipped text and a crisp mark.

## Render only what you changed

resvg output shifts between versions. Re-rendering the unchanged masters under
0.48.1 rewrote every social preview and every favicon above 16px that 0.47 had
produced. The change was 0.13% of pixel bytes in branch-guard's preview:
invisible on screen, and a diff in every review that follows.

The script skips any output that is newer than its master and prints
`up to date`, so the no-argument form is safe to run. `--force` rebuilds
regardless, and is the only way to pick up a resvg upgrade. `git status` after a
render is still the check: a raster whose master you did not touch should not
appear in it.

## Authoring gotchas

These bite when editing the masters:

- **Blur filters need a shape with area.** resvg drops a filter on a zero-area
  element and says `Filters on zero-sized shapes are not allowed`. A vertical
  `<line>` has zero width, so its filter region is empty. workspace-guard's
  fence glow is a blurred `<rect>` for exactly this reason.
- **Render tile icons natively, never downscaled.** Rendering large and
  shrinking with a bicubic resample softens the 2px shield border at 180px.
  `resvg -w 180` rasterizes at the target resolution and keeps the edge crisp.
- **Leading whitespace in `<tspan>` collapses.** XML collapses leading and
  interior whitespace in text content, so plain spaces render inconsistently or
  vanish. foreground-guard's inline `# comment` annotations build their gap from
  `&#160;` (non-breaking space) instead.
- **Keep the longest line inside the panel.** prod-guard's
  `PROD_GUARD_OVERRIDE=…` row is its widest, and at 23px Menlo the panel fits
  about 72 monospace characters. foreground-guard's two rows each end in a green
  fix chip whose right edge sits at x≈1132, clearing the panel border at x≈1200.
  Widen the `<rect>` when you relabel a chip, then re-render and confirm the last
  character clears the border.
- **Non-ASCII glyphs fall back to another face.** An arrow resvg cannot find in
  the requested family emits `Fallback from … to Arial Unicode MS` and renders
  from the fallback. That is harmless for a lone arrow. Keep decorative arrows as
  drawn `<path>`s so their weight and color stay under your control.

## Contrast

These are read on a dark background at whatever brightness the reader has set,
which is often low. Keep every piece of text at 7.5:1 against the page
(`#0d1117`), and body-sized text at about 12:1. WCAG AA asks for 4.5:1, and that
is not enough here: a subtitle at 6.15:1 was reported unreadable at quarter
brightness in a dark room.

| Role | Colour | Ratio |
| --- | --- | --- |
| Title, plugin names | `#e6edf3` | 16.0 |
| Subtitles, descriptions, command text | `#c9d1d9` | 12.3 |
| Footer URL, panel title bar | `#a2aab4` | 8.1 |
| allow / ask / deny chips | `#56d364` / `#e3b341` / `#ff7b72` | 9.8 / 9.7 / 7.5 |

Two colours sit below that floor on purpose. The wordmark accent `#a78bfa`
(6.95:1) is 58px extra-bold, where the threshold is 3:1. The `$` shell prompt
`#7e57c2` (3.63:1) carries no information and is meant to recede, exactly as a
prompt does in a real terminal.

## Publishing the social preview

GitHub does not accept SVG for repo social previews. Upload the PNG:
**repo → Settings → General → Social preview → upload
`docs/img/social-preview.png`.**

Only the repository has that slot. The five plugin previews are not uploaded
anywhere. They are the banners at the top of each plugin's README.
