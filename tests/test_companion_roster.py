"""The five plugin READMEs carry one companion roster, and it must agree.

Each `## Companion plugins` section lists the plugin's siblings in the
marketplace's order, then pr-sentinel, with each bullet worded the same in
every README that carries it (Q89). That uniformity is what makes the roster
greppable, and nothing else enforces it: adding a guard means editing five
sections by hand, and missing one fails no other gate (Q104).

The guard set and its order come from `.claude-plugin/marketplace.json`, so a
guard added there fails here until every README lists it.
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = '## Companion plugins'
END = 'They run side by side'
TAIL = 'pr-sentinel'
BULLET_NAME = re.compile(r'^- \[\*\*([a-z0-9-]+)\*\*\]')


def guards():
    with open(os.path.join(ROOT, '.claude-plugin', 'marketplace.json')) as f:
        return [p['name'] for p in json.load(f)['plugins']]


def roster(text):
    """[(name, bullet text)] from the companion section, or None without one.

    A bullet runs from its `- [**name**]` line through the indented lines
    under it, kept as written so a re-wrap counts as a difference.
    """
    start = text.find(START)
    if start < 0:
        return None
    end = text.find(END, start)
    if end < 0:
        return None
    bullets = []
    for line in text[start:end].splitlines():
        m = BULLET_NAME.match(line)
        if m:
            bullets.append([m.group(1), line])
        elif bullets and line.startswith('  '):
            bullets[-1][1] += '\n' + line
    return [(name, body) for name, body in bullets]


def problems(rosters, order):
    """Every disagreement among `{plugin: roster}`, as readable lines."""
    out = []
    for plugin, entries in rosters.items():
        if entries is None:
            out.append(f'{plugin}: no {START!r} section ending at {END!r}')
            continue
        want = [g for g in order if g != plugin] + [TAIL]
        got = [name for name, _ in entries]
        if got != want:
            out.append(f'{plugin}: lists {got}, expected {want}')
    texts = {}
    for plugin, entries in rosters.items():
        for name, body in entries or ():
            texts.setdefault(name, {}).setdefault(body, []).append(plugin)
    for name, variants in texts.items():
        if len(variants) > 1:
            out.append(f'{name}: worded {len(variants)} ways, in '
                       + '; '.join(', '.join(v) for v in variants.values()))
    return out


def readme_rosters(order):
    out = {}
    for plugin in order:
        with open(os.path.join(ROOT, 'plugins', plugin, 'README.md'),
                  encoding='utf-8') as f:
            out[plugin] = roster(f.read())
    return out


class CompanionRosterTests(unittest.TestCase):

    def test_the_five_rosters_agree(self):
        order = guards()
        self.assertEqual([], problems(readme_rosters(order), order))


class CheckerControls(unittest.TestCase):
    """The check above passes on a clean tree, which a check that reads nothing
    would too. Each of these breaks one README's roster and demands a finding."""

    def setUp(self):
        self.order = guards()
        self.clean = readme_rosters(self.order)

    def test_the_clean_tree_has_a_roster_in_every_readme(self):
        for plugin, entries in self.clean.items():
            with self.subTest(plugin=plugin):
                self.assertEqual(len(self.order), len(entries))

    def test_a_swapped_order_is_found(self):
        broken = dict(self.clean)
        broken['workspace-guard'] = list(reversed(self.clean['workspace-guard']))
        self.assertTrue(any(p.startswith('workspace-guard: lists')
                            for p in problems(broken, self.order)))

    def test_a_reworded_bullet_is_found(self):
        broken = dict(self.clean)
        entries = list(self.clean['branch-guard'])
        name, body = entries[0]
        entries[0] = (name, body.replace('boundary', 'border', 1))
        broken['branch-guard'] = entries
        self.assertTrue(any(p.startswith(f'{name}: worded 2 ways')
                            for p in problems(broken, self.order)))

    def test_a_guard_added_to_the_marketplace_is_found(self):
        found = problems(self.clean, self.order + ['sixth-guard'])
        self.assertEqual(len(self.order), len(found))
        self.assertTrue(all('sixth-guard' in p for p in found))

    def test_a_missing_section_is_found(self):
        self.assertIsNone(roster('# readme\n\nno companions here\n'))
        broken = dict(self.clean, **{'prod-guard': None})
        self.assertIn(f"prod-guard: no {START!r} section ending at {END!r}",
                      problems(broken, self.order))


if __name__ == '__main__':
    unittest.main()
