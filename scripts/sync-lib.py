#!/usr/bin/env python3
"""Copy the shared modules into every plugin, or verify the copies are current.

A Claude Code plugin is COPIED into `~/.claude/plugins/cache/<marketplace>/
<plugin>/<version>/` at install time, and the docs are explicit that a path
climbing out of the plugin root ("../shared-utils") will not resolve there,
because nothing outside the plugin directory is copied. A symlink to a sibling
IS dereferenced for a git-hosted marketplace, but it is skipped for
`--plugin-dir` and local-path installs -- the two ways this repo gets tested
before release, so the shape that breaks would be the shape nobody exercises
until users hit it.

So each plugin carries its own copy, and this script is what keeps the copies
honest: `--check` is a CI gate that fails when one has drifted from the root.
Edit the module under `lib/`; never a vendored copy.

  python3 scripts/sync-lib.py            # write the copies
  python3 scripts/sync-lib.py --check    # verify, exit 1 on drift
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ('bouncer_parse.py', 'bouncer_grants.py')
PLUGINS = os.path.join(ROOT, 'plugins')

BANNER = (
    '# ---------------------------------------------------------------------\n'
    '# VENDORED COPY -- do not edit. Source: lib/%s\n'
    '# Regenerate with: python3 scripts/sync-lib.py\n'
    '# ---------------------------------------------------------------------\n'
)


def plugin_names():
    return sorted(d for d in os.listdir(PLUGINS)
                  if os.path.isdir(os.path.join(PLUGINS, d)))


def vendored_text(module):
    source = os.path.join(ROOT, 'lib', module)
    if not os.path.exists(source):
        raise SystemExit(
            'sync-lib: declared module lib/%s does not exist. Add it, or drop '
            'it from MODULES.' % module)
    with open(source) as f:
        return (BANNER % module) + f.read()


def target_for(name, module):
    return os.path.join(PLUGINS, name, 'lib', module)


def main():
    check = '--check' in sys.argv[1:]
    stale = []
    for module in MODULES:
        want = vendored_text(module)
        for name in plugin_names():
            target = target_for(name, module)
            current = None
            if os.path.exists(target):
                with open(target) as f:
                    current = f.read()
            if current == want:
                continue
            if check:
                stale.append('%s/%s' % (name, module))
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'w') as f:
                f.write(want)
            print('synced %s' % os.path.relpath(target, ROOT))

    if check:
        if stale:
            sys.stderr.write(
                'shared module is stale in: %s\n'
                'Run: python3 scripts/sync-lib.py\n' % ', '.join(stale))
            return 1
        print('shared modules (%d) in sync across %d plugins'
              % (len(MODULES), len(plugin_names())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
