# Agent reference: Rendering the brand images

The repo's brand assets in `docs/img/` are generated from a small set of SVG
masters with [resvg](https://github.com/linebender/resvg) — a single static
binary, no browser required. This doc is the procedure for regenerating them and
the authoring gotchas that keep the SVGs renderer-portable.

## Source of truth

Edit the **SVG masters**; never hand-edit a generated raster. Each master maps
to one or more outputs:

| Master (edit this)     | Generated output(s)                                   | Used for                        |
| ---------------------- | ----------------------------------------------------- | ------------------------------- |
| `social-preview.svg`   | `social-preview.png`                                  | GitHub repo social preview      |
| `favicon.svg`          | `favicon-16.png`, `favicon-32.png`, `favicon-48.png`, `favicon.ico` | browser tab favicon (transparent) |
| `icon-tile.svg`        | `apple-touch-icon.png` (180), `icon-512.png`          | iOS / PWA icons (opaque tile)   |

`favicon.svg` and `icon-tile.svg` share the same shield-and-server-stack mark;
`icon-tile.svg` adds the opaque dark tile background that iOS and PWA contexts
require (they ignore transparency). The shield is the shared shape across the
sibling guards (workspace-guard, branch-guard); prod-guard's distinguishing
glyph is the racked server stack, standing for the production infrastructure
target.

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
social preview should have no clipped text and a crisp shield-and-server-stack
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
- **Keep the longest terminal line inside the panel.** The social preview's
  third row (the `PROD_GUARD_OVERRIDE=…` command) is the widest; at 23px Menlo
  the panel fits ~72 monospace chars. When editing a command, re-render and
  confirm the last character clears the panel's right border rather than
  clipping.

## Publishing the social preview

GitHub does not accept SVG for repo social previews. Upload the PNG:
**repo → Settings → General → Social preview → upload `social-preview.png`**.
