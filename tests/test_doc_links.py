"""Every relative link in a tracked Markdown file must resolve.

The subtree merge that brought the five plugin repos together rebased 111
Markdown files under `plugins/<name>/` without touching their link bodies, so
`../../.github/pull_request_template.md` -- correct while workspace-guard was
its own repo -- came to mean `plugins/workspace-guard/.github/...`, which does
not exist. That one was found by hand and fixed in 801deb2. Nothing would have
found the next one: a dead relative link renders as ordinary text on
github.com and no suite reads docs.

Anchors are checked too, and are the half that rots without a merge to blame:
renaming a heading breaks every link into it from a file the rename never
touched.
"""
import os
import re
import subprocess
import unittest
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `[x](y)`, tolerating <y> and a "title". The lookbehind drops `![x](y)`:
# a missing image is a rendering bug, not a broken reference.
INLINE = re.compile(r'(?<!!)\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+"[^"]*")?\s*\)')
REFDEF = re.compile(r'^ {0,3}\[[^\]]+\]:\s*<?(\S+?)>?\s*$', re.M)
SCHEME = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')
FENCE = re.compile(r'^\s*(?:```|~~~)')
HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')


def tracked_markdown():
    """git, not a walk: `.claude/worktrees/` is gitignored and holds whole
    checkouts, so an rglob here would grade eight stale branches as well."""
    out = subprocess.run(['git', 'ls-files', '-z', '*.md'], cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if out.returncode != 0:
        raise RuntimeError('git ls-files failed: %s'
                           % out.stderr.decode('utf-8', 'replace').strip())
    return [p for p in out.stdout.decode('utf-8').split('\0') if p]


def strip_fences(text):
    """Blank fenced blocks, keeping line numbers so offsets still report."""
    lines, fenced = [], False
    for line in text.split('\n'):
        if FENCE.match(line):
            fenced = not fenced
            lines.append('')
        else:
            lines.append('' if fenced else line)
    return '\n'.join(lines)


def strip_code(text):
    """Also blank code spans and HTML comments, for reading links.

    Docs here quote bad links on purpose -- the release-notes README cites
    `[Limitations](README.md#limitations)` as the mistake to avoid -- so a
    checker that reads inside a code span reports the example as the bug.

    Headings do not get this treatment: github.com keeps the text inside a
    span when it slugs '## Break-glass: `BRANCH_GUARD_OVERRIDE`', and
    blanking it here silently invalidates every anchor into a heading that
    names a flag or a command -- nine of them in this repo.
    """
    blank = lambda m: '\n' * m.group(0).count('\n')
    text = re.sub(r'<!--.*?-->', blank, strip_fences(text), flags=re.S)
    return re.sub(r'(`+)(?!`).*?\1(?!`)', blank, text, flags=re.S)


def links(text):
    """(line number, target) for every non-external link."""
    found = []
    for pattern in (INLINE, REFDEF):
        for m in pattern.finditer(text):
            target = unquote(m.group(1))
            if SCHEME.match(target):
                continue
            found.append((text.count('\n', 0, m.start()) + 1, target))
    return sorted(found)


def slugs(text):
    """Heading anchors as github.com generates them: lowercased, punctuation
    dropped, spaces hyphenated, collisions suffixed `-1`, `-2`."""
    seen, out = {}, set()
    for line in strip_fences(text).split('\n'):
        m = HEADING.match(line)
        if not m:
            continue
        title = m.group(2)
        title = re.sub(r'`([^`]*)`', r'\1', title)
        title = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', title)
        title = re.sub(r'[*~]', '', title)
        slug = re.sub(r'[^\w\- ]', '', title.lower()).replace(' ', '-')
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else '%s-%d' % (slug, n))
    return out


def broken(paths, exists, read):
    """Every link in `paths` that does not resolve, as printable lines."""
    heads, bad = {}, []
    for path in paths:
        for line, target in links(strip_code(read(path))):
            where = '%s:%d -> %s' % (path, line, target)
            body, _, anchor = target.partition('#')
            dest = path if not body else os.path.normpath(
                os.path.join(os.path.dirname(path), body))
            if body:
                if dest.startswith('..'):
                    bad.append(where + '   (climbs out of the repository)')
                    continue
                if not exists(dest):
                    bad.append(where + '   (no such file)')
                    continue
            # Only Markdown has headings for an anchor to aim at; a link into
            # a script, an image or a directory is resolved by its path alone.
            if not anchor or not dest.endswith('.md'):
                continue
            if dest not in heads:
                heads[dest] = slugs(read(dest))
            if anchor.lower() not in heads[dest]:
                bad.append(where + '   (no such heading)')
    return bad


def exists_in_repo(path):
    return os.path.exists(os.path.join(ROOT, path))


def read_in_repo(path):
    with open(os.path.join(ROOT, path), encoding='utf-8') as f:
        return f.read()


def reachable_pages(readme, inside, exists, read):
    """Markdown pages a reader reaches from `readme` without leaving `inside`."""
    seen, queue = set(), [readme]
    while queue:
        path = queue.pop()
        if path in seen or not path.endswith('.md') or not exists(path):
            continue
        seen.add(path)
        for _, target in links(strip_code(read(path))):
            body = target.partition('#')[0]
            if not body:
                continue
            dest = os.path.normpath(os.path.join(os.path.dirname(path), body))
            if dest.startswith(inside + os.sep):
                queue.append(dest)
    return seen


def escaping(paths, inside, read):
    """Relative links on `paths` landing outside `inside`, as printable lines."""
    bad = []
    for path in sorted(paths):
        for line, target in links(strip_code(read(path))):
            body = target.partition('#')[0]
            if not body:
                continue
            dest = os.path.normpath(os.path.join(os.path.dirname(path), body))
            if not dest.startswith(inside + os.sep):
                bad.append('%s:%d -> %s' % (path, line, target))
    return bad


PLUGINS = ('workspace-guard', 'branch-guard', 'prod-guard',
           'exit-status-guard', 'foreground-guard')


class PluginReaderLinkTests(unittest.TestCase):
    """A page a plugin's README reaches must not link out of that plugin.

    Claude Code copies a plugin into
    `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, so someone who
    installed the guard has that directory and nothing above it -- the same
    reason `lib/bouncer_parse.py` is vendored into each plugin. A relative link
    climbing out resolves while you browse the repository and dead-ends for the
    reader it was written for, which is why `DocLinkTests` above passes on one.

    The criterion is reachability from the README, not which directory a page
    sits in: foreground-guard's README links a page under `docs/development/`,
    so that page is graded too. What is exempt is everything a README never
    reaches -- CLAUDE.md and the release runbooks -- because those are read in
    a checkout, where a relative link is the one that follows the branch you
    are on rather than jumping to `main`.
    """

    def test_no_reader_facing_page_links_out_of_its_plugin(self):
        bad = []
        for name in PLUGINS:
            inside = os.path.join('plugins', name)
            bad += escaping(
                reachable_pages(os.path.join(inside, 'README.md'), inside,
                                exists_in_repo, read_in_repo),
                inside, read_in_repo)
        self.assertEqual([], bad, 'reader-facing page links out of its '
                         'plugin; use an absolute URL:\n  ' + '\n  '.join(bad))

    def test_the_check_can_fail(self):
        """Pins the walk as well as the finding: an absolute URL is invisible
        to `links`, and a page the README never reaches is never graded."""
        docs = {
            'plugins/p/README.md': ('[d](docs/design.md)\n'
                                    '[c](../../docs/queue/README.md)\n'),
            'plugins/p/docs/design.md': ('[b](../../../docs/queue/README.md)\n'
                                         '[ok](https://example.invalid/x)\n'),
            'plugins/p/CLAUDE.md': '[unreached](../../docs/queue/README.md)\n',
        }
        pages = reachable_pages('plugins/p/README.md', 'plugins/p',
                                lambda p: p in docs, docs.__getitem__)
        self.assertEqual({'plugins/p/README.md', 'plugins/p/docs/design.md'},
                         pages)
        bad = escaping(pages, 'plugins/p', docs.__getitem__)
        self.assertEqual(2, len(bad), bad)
        self.assertNotIn('CLAUDE.md', ' '.join(bad))


class DocLinkTests(unittest.TestCase):
    def test_every_relative_link_resolves(self):
        bad = broken(tracked_markdown(), exists_in_repo, read_in_repo)
        self.assertEqual([], bad, 'broken relative links:\n  '
                         + '\n  '.join(bad))

    def test_the_check_can_fail(self):
        """A link checker that silently matched nothing would pass forever.

        Each line of the fixture pins one behaviour: a path that has moved out
        from under a link, an anchor that resolves, one whose heading was
        renamed away, one whose heading names a flag -- code span and
        underscore both surviving into the slug, which two earlier drafts of
        `slugs` broke -- and a link quoted in a code span, which is an example
        rather than a reference.
        """
        docs = {
            'a/b/doc.md': ('[t](../../.github/pull_request_template.md)\n'
                           '[o](../ok.md#a-heading)\n'
                           '[x](../ok.md#renamed-away)\n'
                           '[c](../ok.md#a-flag_name)\n'
                           '`[q](../nope.md)`\n'),
            'a/ok.md': '# A heading\n\n## A `FLAG_NAME`\n',
        }

        bad = broken(['a/b/doc.md'], lambda p: p in docs, docs.__getitem__)
        self.assertEqual(2, len(bad), bad)
        self.assertIn('no such file', bad[0])
        self.assertIn('no such heading', bad[1])


if __name__ == '__main__':
    unittest.main()
