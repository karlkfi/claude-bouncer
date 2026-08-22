#!/usr/bin/env python3
"""Rasterize the brand images from their SVG masters with resvg.

Six asset directories carry the same three masters -- the repo's own in
`docs/img/`, and one per plugin under `plugins/<name>/docs/img/`. Each master
maps to a fixed set of outputs, so the procedure is identical everywhere and
worth running from one place rather than six.

resvg is a dev-time tool, not a runtime dependency: the hooks are stdlib-only
Python and nothing here ships to users. It only regenerates the committed
rasters when a master changes.

  python3 scripts/render-images.py                 # every asset directory
  python3 scripts/render-images.py docs/img        # just one
  python3 scripts/render-images.py --force         # rebuild even if up to date

An output is skipped when it is newer than the master it comes from. resvg
output shifts between versions, so re-rendering an unchanged master rewrites
a raster nobody edited and puts renderer noise in the diff.

The SVGs name CSS system-font stacks (-apple-system, ui-monospace), which are
keywords rather than real families, so the concrete faces are passed on the
command line. Override them where those faces are missing:

  SANS_FAMILY="DejaVu Sans" MONO_FAMILY="DejaVu Sans Mono" \
    python3 scripts/render-images.py
"""
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANS = os.environ.get('SANS_FAMILY', 'Helvetica Neue')
MONO = os.environ.get('MONO_FAMILY', 'Menlo')

FAVICON_SIZES = (16, 32, 48)
# Tile icons render natively at each target size. Rendering large and
# downscaling softens the ~2px shield border under the resample pass.
TILE_SIZES = (('apple-touch-icon.png', 180), ('icon-512.png', 512))


def asset_dirs():
    dirs = [os.path.join(ROOT, 'docs', 'img')]
    plugins = os.path.join(ROOT, 'plugins')
    for name in sorted(os.listdir(plugins)):
        d = os.path.join(plugins, name, 'docs', 'img')
        if os.path.isdir(d):
            dirs.append(d)
    return [d for d in dirs if os.path.isdir(d)]


FORCE = False


def stale(master, target):
    if FORCE or not os.path.exists(target):
        return True
    return os.path.getmtime(master) > os.path.getmtime(target)


def resvg(master, target, args):
    if not stale(master, target):
        return False
    subprocess.run(['resvg'] + args + [master, target], check=True, cwd=ROOT)
    return True


def rel(path):
    return os.path.relpath(path, ROOT)


def pack_ico(directory):
    """Pack favicon.ico from the PNGs -- PNG-in-ICO, read by every modern browser."""
    pngs = []
    for size in FAVICON_SIZES:
        with open(os.path.join(directory, 'favicon-%d.png' % size), 'rb') as f:
            pngs.append((size, f.read()))
    out = struct.pack('<HHH', 0, 1, len(pngs))
    entries, offset = b'', 6 + 16 * len(pngs)
    for size, data in pngs:
        dim = 0 if size >= 256 else size
        entries += struct.pack('<BBBBHHII', dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    target = os.path.join(directory, 'favicon.ico')
    with open(target, 'wb') as f:
        f.write(out + entries + b''.join(d for _, d in pngs))
    print('  %s' % rel(target))


def render_dir(directory):
    print(rel(directory))
    wrote = False
    social = os.path.join(directory, 'social-preview.svg')
    if os.path.exists(social):
        target = os.path.join(directory, 'social-preview.png')
        if resvg(social, target, ['--sans-serif-family', SANS, '--monospace-family', MONO]):
            print('  %s' % rel(target))
            wrote = True

    favicon = os.path.join(directory, 'favicon.svg')
    if os.path.exists(favicon):
        redrew = False
        for size in FAVICON_SIZES:
            target = os.path.join(directory, 'favicon-%d.png' % size)
            if resvg(favicon, target, ['-w', str(size), '-h', str(size)]):
                print('  %s' % rel(target))
                redrew = True
        # The .ico is packed from the PNGs, so it only moves when they do.
        if redrew:
            pack_ico(directory)
            wrote = True

    tile = os.path.join(directory, 'icon-tile.svg')
    if os.path.exists(tile):
        for name, size in TILE_SIZES:
            target = os.path.join(directory, name)
            if resvg(tile, target, ['-w', str(size), '-h', str(size)]):
                print('  %s' % rel(target))
                wrote = True

    if not wrote:
        print('  up to date')


def main():
    global FORCE
    args = [a for a in sys.argv[1:] if a != '--force']
    FORCE = '--force' in sys.argv[1:]
    dirs = [os.path.join(ROOT, a) for a in args] if args else asset_dirs()
    for d in dirs:
        if not os.path.isdir(d):
            sys.stderr.write('no such directory: %s\n' % d)
            return 1
    for d in dirs:
        render_dir(d)
    return 0


if __name__ == '__main__':
    sys.exit(main())
