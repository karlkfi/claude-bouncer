#!/usr/bin/env python3
"""Verify every install instruction in a README names this marketplace.

  .claude-plugin/marketplace.json  -> "name", "owner".name, the plugin list
  README.md, plugins/*/README.md   -> what a reader is told to install

The five plugins were published from five repositories before they were one,
and the pass that repointed the READMEs (43643f4) moved the fenced `/plugin`
lines and nothing else. Twenty-three sites survived it: four release badges,
five Claude Desktop steps, three "latest release" links, branch-guard's
local-install key and checkout path, and nine `extraKnownMarketplaces` keys
naming the plugin rather than the marketplace -- so following the auto-update
instructions declared a second marketplace nobody had installed from.

Against that tree this reports twenty-two of them. The one it does not is the
`~/workspace/claude-branch-guard` checkout path, which carries no owner and no
marketplace name; a reader may clone anywhere, so there is nothing here to
compare it against. The key beside it is checked, and that is what made the
block wrong.

A link checker cannot find any of it. The retired repositories are still
served, so every stale link resolves and the page renders correctly -- the
reader is simply sent somewhere that will never publish again. What separates
the stale text from the correct text is agreement with the manifest, which is
a file in the tree, so this reads no network.

Prints one line per README and exits 1 on any disagreement.

  python3 scripts/install-ref-check.py

Dated notes under plugins/*/docs/releases/ are deliberately out of scope: they
record what shipped at that version, and rewriting them would make the record
wrong.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETPLACE = os.path.join(ROOT, '.claude-plugin', 'marketplace.json')

# An add, an install or an update names where the reader is going, so the
# marketplace has to be this one. An uninstall or a remove names where they
# have been -- exit-status-guard's 2.0.0 migration block tells a `pipe-guard`
# user to remove `pipe-guard@pipe-guard`, and it is correct.
ADD_RE = re.compile(r'(?:^|[`\s/])(?:claude )?plugin marketplace add\s+(\S+)')
UPDATE_RE = re.compile(r'(?:^|[`\s/])(?:claude )?plugin marketplace update\s+(\S+)')
INSTALL_RE = re.compile(r'(?:^|[`\s/])(?:claude )?plugin install\s+\S+?@(\w[\w-]*)')
# A JSON key under extraKnownMarketplaces, and the marketplace half of an
# enabledPlugins entry. Both are read by Claude Code as marketplace names.
KEY_RE = re.compile(r'"extraKnownMarketplaces"\s*:\s*\{\s*"([^"]+)"')
ENABLED_RE = re.compile(r'"enabledPlugins"\s*:\s*\{\s*"\S+?@([^"]+)"')

# A reference the repository keeps on purpose, as (path, text, why). Checked
# in both directions: an entry that matches nothing is an error, so an
# exemption cannot outlive the line it was written for.
EXCLUSIONS = [
    ('plugins/prod-guard/README.md',
     'slug that merely contains `prod`, e.g. `karlkfi/claude-prod-guard`',
     'an example of the matching rules, not an install instruction'),
    ('README.md',
     'published from `karlkfi/claude-workspace-guard` and its four',
     'the migration section, which has to name what it is migrating from'),
]


def manifest():
    with open(MARKETPLACE) as f:
        return json.load(f)


def expected():
    """The marketplace name, the repo slug, and the retired slugs it replaced."""
    m = manifest()
    name = m['name']
    owner = m['owner']['name']
    slug = '%s/%s' % (owner, name)
    retired = set('%s/claude-%s' % (owner, p['name']) for p in m['plugins'])
    return name, slug, retired


def readmes():
    """The root README and each plugin's, in report order."""
    found = ['README.md']
    plugins = os.path.join(ROOT, 'plugins')
    for d in sorted(os.listdir(plugins)):
        path = os.path.join('plugins', d, 'README.md')
        if os.path.exists(os.path.join(ROOT, path)):
            found.append(path)
    return found


def findings(text, name, slug, retired, waived):
    """(line number, what is wrong) for one README, exclusions applied."""
    out = []
    lines = text.split('\n')
    for n, line in enumerate(lines, 1):
        if any(w in line for w in waived):
            continue
        for old in sorted(retired):
            if old in line:
                out.append((n, '%s is a retired repository' % old))
        for got in ADD_RE.findall(line):
            if got.strip('`') != slug:
                out.append((n, 'marketplace add %s, expected %s' % (got, slug)))
        for pattern in (UPDATE_RE, INSTALL_RE):
            for got in pattern.findall(line):
                if got.strip('`') != name:
                    out.append((n, 'names marketplace %s, expected %s'
                                % (got, name)))

    # A settings key and its enabledPlugins entry sit on the line after the
    # brace in a pretty-printed block, so these two are read over the whole
    # file and mapped back to a line rather than matched line by line.
    for pattern in (KEY_RE, ENABLED_RE):
        for m in pattern.finditer(text):
            if m.group(1) == name:
                continue
            n = text.count('\n', 0, m.end(1)) + 1
            if any(w in lines[n - 1] for w in waived):
                continue
            out.append((n, 'names marketplace %s, expected %s'
                        % (m.group(1), name)))
    return sorted(out)


def main():
    name, slug, retired = expected()

    unmatched = list(EXCLUSIONS)
    bad = []
    for path in readmes():
        with open(os.path.join(ROOT, path)) as f:
            text = f.read()
        waived = []
        for entry in list(unmatched):
            if entry[0] == path and entry[1] in text:
                waived.append(entry[1])
                unmatched.remove(entry)
        found = findings(text, name, slug, retired, waived)
        for n, why in found:
            bad.append('%s:%d: %s' % (path, n, why))
        print('%-4s %s' % ('DIFF' if found else 'ok', path))

    sys.stdout.flush()
    if unmatched:
        sys.stderr.write(
            'exclusion matches nothing, so it is stale: %s\n'
            % ', '.join('%s (%s)' % (e[0], e[2]) for e in unmatched))
        return 1
    if bad:
        sys.stderr.write('\n'.join(bad) + '\n')
        sys.stderr.write(
            "install instructions must name the marketplace in "
            ".claude-plugin/marketplace.json (%s, at %s). An uninstall or a "
            "marketplace remove is exempt -- it names what the reader is "
            "getting rid of.\n" % (name, slug))
        return 1
    print('install references agree across %d README(s), %d exclusion(s) '
          'held back' % (len(readmes()), len(EXCLUSIONS)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
