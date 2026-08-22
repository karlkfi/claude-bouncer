#!/usr/bin/env python3
"""Verify a plugin's version string is identical in all three places it lives.

  plugins/<name>/.claude-plugin/plugin.json  -> "version"
  .claude-plugin/marketplace.json            -> the entry named <name>
  README.md                                  -> the "The guards" table row

The marketplace entry is the one Claude Code compares on `claude plugin
update`, so a bump that misses it ships nothing at all while the README
announces a new version. A bump that misses the README leaves the front page
wrong, which nothing else in the repo would notice.

Prints one line per plugin and exits 1 on any disagreement.

  python3 scripts/version-check.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETPLACE = os.path.join(ROOT, '.claude-plugin', 'marketplace.json')
README = os.path.join(ROOT, 'README.md')
PLUGINS = os.path.join(ROOT, 'plugins')

MISSING = '-'

# A row of the README's guards table: the plugin's linked name, then the
# version cell. Anchored on the link so prose tables elsewhere cannot match.
ROW_RE = re.compile(
    r'^\|\s*\[(?P<name>[^\]]+)\]\([^)]*\)\s*\|\s*(?P<version>[^|\s]+)\s*\|',
    re.M)


def plugin_names():
    return sorted(d for d in os.listdir(PLUGINS)
                  if os.path.isdir(os.path.join(PLUGINS, d)))


def read_json(path):
    with open(path) as f:
        return json.load(f)


def plugin_versions(names):
    out = {}
    for name in names:
        path = os.path.join(PLUGINS, name, '.claude-plugin', 'plugin.json')
        if os.path.exists(path):
            out[name] = read_json(path).get('version', MISSING)
    return out


def marketplace_versions():
    if not os.path.exists(MARKETPLACE):
        return {}
    entries = read_json(MARKETPLACE).get('plugins', [])
    return dict((e.get('name'), e.get('version', MISSING)) for e in entries)


def readme_versions():
    if not os.path.exists(README):
        return {}
    with open(README) as f:
        text = f.read()
    return dict((m.group('name'), m.group('version'))
                for m in ROW_RE.finditer(text))


def main():
    names = plugin_names()
    sources = (
        ('plugin.json', plugin_versions(names)),
        ('marketplace.json', marketplace_versions()),
        ('README.md', readme_versions()),
    )

    width = max([len(n) for n in names] + [0])
    bad = []
    for name in names:
        found = [(label, table.get(name, MISSING)) for label, table in sources]
        versions = set(v for _, v in found)
        if len(versions) == 1 and MISSING not in versions:
            print('ok   %-*s  %s' % (width, name, found[0][1]))
            continue
        bad.append(name)
        print('DIFF %-*s  %s' % (width, name,
                                 '  '.join('%s=%s' % f for f in found)))

    if bad:
        sys.stderr.write(
            'version strings disagree for: %s\n'
            'Bump all three: plugins/<name>/.claude-plugin/plugin.json, '
            '.claude-plugin/marketplace.json, and the README guards table.\n'
            % ', '.join(bad))
        return 1
    print('versions agree across %d plugins' % len(names))
    return 0


if __name__ == '__main__':
    sys.exit(main())
