# Agent reference: Rendering the brand images

The repo's brand assets in `docs/img/` are generated from a small set of SVG
masters with [resvg](https://github.com/linebender/resvg) — a single static
binary, no browser required. This doc is the procedure for regenerating them and
the authoring gotchas that keep the SVGs renderer-portable.

resvg is a **dev-time tool**, not a runtime dependency of the plugin — the hook
itself is stdlib-only Python. Nothing here ships to users; it only regenerates
the committed rasters when a master changes.

## Source of truth

Edit the **SVG masters**; never hand-edit a generated raster. Each master maps
to one or more outputs:

| Master (edit this)     | Generated output(s)                                   | Used for                        |
| ---------------------- | ----------------------------------------------------- | ------------------------------- |
| `social-preview.svg`   | `social-preview.png`                                  | GitHub repo social preview      |
| `favicon.svg`          | `favicon-16.png`, `favicon-32.png`, `favicon-48.png`, `favicon.ico` | browser tab favicon (transparent) |
| `icon-tile.svg`        | `apple-touch-icon.png` (180), `icon-512.png`          | iOS / PWA icons (opaque tile)   |

`favicon.svg` and `icon-tile.svg` share the same shield-and-hourglass mark;
`icon-tile.svg` adds the opaque dark tile background that iOS and PWA contexts
require (they ignore transparency). The shield is the shared shape across the
sibling guards (prod-guard, workspace-guard, branch-guard); foreground-guard's
distinguishing glyph is the hourglass, standing for the session's main-thread
time — the resource this guard protects from foreground polls and under-timed
slow runs.

**Not generated:** hand-captured screenshots have no SVG master and are not part
of this pipeline — re-shoot them manually if the UI they show changes.

## Prerequisites

- `resvg` (tested with 0.47). Install with `brew install resvg` or
  `cargo install resvg`.
- `python3` (stdlib only) — used to pack `favicon.ico`.

## Regenerate everything

Run from `docs/img/`:

```sh
# Social preview. The SVG uses CSS system-font stacks (-apple-system,
# ui-monospace) that are not real font names; pass concrete installed
# families so resvg resolves them predictably. Substitute fonts present on
# your machine if these are missing.
resvg --sans-serif-family "Helvetica Neue" --monospace-family "Menlo" \
  social-preview.svg social-preview.png

# Transparent favicons, rendered natively at each target size.
for s in 16 32 48; do resvg -w $s -h $s favicon.svg favicon-$s.png; done

# Opaque tile icons. Render natively at the target size (do NOT render large
# and downscale) — the thin shield border softens under a resample pass.
resvg -w 180 -h 180 icon-tile.svg apple-touch-icon.png
resvg -w 512 -h 512 icon-tile.svg icon-512.png

# Pack favicon.ico from the three PNGs (PNG-in-ICO; supported by all modern
# browsers).
python3 - <<'PY'
import struct
sizes = [16, 32, 48]
pngs = [(s, open(f"favicon-{s}.png", "rb").read()) for s in sizes]
n = len(pngs)
header = struct.pack("<HHH", 0, 1, n)
entries, off = b"", 6 + 16 * n
for s, d in pngs:
    w = h = (0 if s >= 256 else s)
    entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(d), off)
    off += len(d)
with open("favicon.ico", "wb") as f:
    f.write(header); f.write(entries)
    for _, d in pngs:
        f.write(d)
print("packed favicon.ico")
PY
```

Verify the result: `file favicon.ico` should report three icons, and the
social preview should have no clipped text and a crisp shield-and-hourglass
mark.

## Authoring gotchas

These bite when editing the SVG masters:

- **Blur filters need a non-zero-area shape.** resvg drops a filter applied to a
  zero-area element (`Filters on zero-sized shapes are not allowed`). Apply a
  glow to a `<rect>` or `<path>` with a real bounding box, never a `<line>` —
  a vertical line has zero width, so its filter region is empty.
- **Render tile icons natively, never downscaled.** Rendering large and shrinking
  with a bicubic resample softens the ~2px shield border at 180px. `resvg -w 180`
  rasterizes at the target resolution and keeps the edge crisp.
- **System-font stacks don't resolve themselves.** `-apple-system`,
  `ui-monospace`, etc. are CSS keywords, not font names. Pass `--sans-serif-family`
  / `--monospace-family` so resvg picks a real face; otherwise it falls back to
  its default and metrics (and line widths) shift.
- **Leading whitespace in `<tspan>` collapses.** The social preview's inline
  `# comment` annotations follow their command with a gap built from `&#160;`
  (non-breaking space), not plain spaces — XML collapses leading/interior
  whitespace in text content, so plain spaces render inconsistently or vanish.
- **Keep the fix chips inside the panel.** The social preview's two rows each
  end in a green "fix" chip (`run it in the background`, `timeout 2m → 10m`)
  whose right edge sits at x≈1132, clearing the panel's right border at x≈1200.
  When relabeling a chip, widen the `<rect>` and re-check that edge — a longer
  label overflows the pill or the panel before it clips text.
- **Non-ASCII glyphs fall back fonts.** The `→` in the timeout chip and any
  arrows resvg can't find in the requested family emit a
  `Fallback from … to Arial Unicode MS` warning and render from a fallback
  face. That is harmless for a lone arrow, but keep decorative arrows as drawn
  `<path>`s (as the two transform arrows are) rather than text glyphs so their
  weight and color stay under your control.

## Publishing the social preview

GitHub does not accept SVG for repo social previews. Upload the PNG:
**repo → Settings → General → Social preview → upload `social-preview.png`**.
