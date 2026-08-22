#!/usr/bin/env python3
"""Report, bump, and tag a plugin release.

`docs/development/release-process.md` is the runbook and the authority. This
script is the half of it that has to hold every time: a version string lives in
three files per plugin, five plugins share one tag namespace, and `main` carries
four other plugins' commits between any two of one plugin's releases. None of
that survives being done from memory.

What is deliberately NOT here is the judgment. Which level a release gets, and
whether a delta is worth a tag at all, are read off what the commits did to the
guard's decisions -- `status` lays out the evidence for that call and stops.

  python3 scripts/release.py status                 # every plugin: delta and verdict
  python3 scripts/release.py status workspace-guard --notes
  python3 scripts/release.py bump workspace-guard minor
  python3 scripts/release.py tag workspace-guard    # after the release PR merges
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETPLACE = os.path.join(ROOT, '.claude-plugin', 'marketplace.json')
README = os.path.join(ROOT, 'README.md')

# Conventional-commit types that reach someone running the guard. A release
# whose whole delta sits outside this set is internal churn, which the runbook
# answers with "wait" rather than "cut a small one".
USER_FACING_TYPES = ('feat', 'fix', 'perf', 'revert')


def die(msg):
    sys.stderr.write('release: %s\n' % msg)
    raise SystemExit(1)


def git(*args):
    r = subprocess.run(['git', '-C', ROOT] + list(args),
                       capture_output=True, text=True)
    if r.returncode != 0:
        die('git %s failed: %s' % (' '.join(args), r.stderr.strip()))
    return r.stdout


def plugin_names():
    with open(MARKETPLACE) as f:
        return [p['name'] for p in json.load(f)['plugins']]


# --------------------------------------------------------------- the version

def plugin_json(name):
    return os.path.join(ROOT, 'plugins', name, '.claude-plugin', 'plugin.json')


def marketplace_pattern(name):
    # Entries list "name" before "version", so anchoring on the name and taking
    # the next version reaches that plugin's entry and no other.
    return re.compile(r'("name"\s*:\s*"%s".*?"version"\s*:\s*")([^"]+)(")'
                      % re.escape(name), re.S)


def readme_pattern(name):
    return re.compile(r'(\[%s\]\([^)]*\)\s*\|\s*)([0-9][^\s|]*)' % re.escape(name))


def read_file(path):
    with open(path) as f:
        return f.read()


def locations(name):
    """The three files, each as (label, path, current value or None)."""
    pj = read_file(plugin_json(name))
    pj_m = re.search(r'"version"\s*:\s*"([^"]+)"', pj)
    mk = read_file(MARKETPLACE)
    mk_m = marketplace_pattern(name).search(mk)
    rd = read_file(README)
    rd_m = readme_pattern(name).search(rd)
    return [
        ('plugin.json', plugin_json(name), pj_m.group(1) if pj_m else None),
        ('marketplace.json', MARKETPLACE, mk_m.group(2) if mk_m else None),
        ('README.md', README, rd_m.group(2) if rd_m else None),
    ]


def current_version(name):
    """The agreed version, or None when the three disagree or one is missing."""
    found = set(v for _, _, v in locations(name))
    return found.pop() if len(found) == 1 and None not in found else None


def write_version(name, new):
    edits = [
        (plugin_json(name), re.compile(r'("version"\s*:\s*")([^"]+)(")'), 2),
        (MARKETPLACE, marketplace_pattern(name), 2),
        (README, readme_pattern(name), 2),
    ]
    for path, pattern, group in edits:
        text = read_file(path)
        out, count = pattern.subn(
            lambda m: m.group(1) + new + (m.group(3) if m.lastindex == 3 else ''),
            text, count=1)
        if count != 1:
            die('%s: expected one version for %s, matched %d'
                % (os.path.relpath(path, ROOT), name, count))
        with open(path, 'w') as f:
            f.write(out)


def next_version(cur, spec):
    if re.match(r'^\d+\.\d+\.\d+$', spec):
        target = spec
    else:
        major, minor, patch = (int(p) for p in cur.split('.'))
        if spec == 'major':
            target = '%d.0.0' % (major + 1)
        elif spec == 'minor':
            target = '%d.%d.0' % (major, minor + 1)
        elif spec == 'patch':
            target = '%d.%d.%d' % (major, minor, patch + 1)
        else:
            die("level must be major, minor, patch, or an explicit X.Y.Z, not '%s'" % spec)
    if tuple(int(p) for p in target.split('.')) <= tuple(int(p) for p in cur.split('.')):
        die('%s is not greater than the current %s' % (target, cur))
    return target


# ----------------------------------------------------------------- the delta

def previous_tag(name):
    out = git('tag', '--list', '%s/v*' % name, '--sort=-v:refname')
    tags = out.split()
    return tags[0] if tags else None


def boundary_commit(name):
    """Where a plugin's history joins this repo, for a first release.

    Before its first tag here there is nothing to diff against: the plugin's
    own history reaches back into the repository it came from, and its last
    release was cut there.
    """
    out = git('log', '--format=%H', '-1', '--fixed-strings',
              '--grep', "Add 'plugins/%s/'" % name)
    return out.strip() or None


def delta(name, since):
    args = ['log', '--format=%H\t%s', '--no-merges']
    if since:
        args.append('%s..HEAD' % since)
    args += ['--', 'plugins/%s' % name, 'lib']
    return [tuple(line.split('\t', 1))
            for line in git(*args).splitlines() if '\t' in line]


def commit_type(subject):
    m = re.match(r'^([a-z]+)(\([^)]*\))?!?:', subject)
    return m.group(1) if m else None


def harvest(subjects):
    """The `## Release note` line each author wrote, per referenced PR.

    Uses `scripts/release-note.py`'s own extractor rather than a second copy of
    the rule, so the harvest and the `release-note` CI check cannot drift into
    disagreeing about what counts as an answer.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'release_note', os.path.join(ROOT, 'scripts', 'release-note.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    notes = []
    for number in sorted(set(n for s in subjects
                             for n in re.findall(r'\(#(\d+)\)', s)), key=int):
        r = subprocess.run(['gh', 'pr', 'view', number, '--json', 'body', '--jq', '.body'],
                           capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            notes.append((number, '!! UNREADABLE'))
            continue
        notes.append((number, module.extract(r.stdout)))
    return notes


def verdict(commits, notes):
    """release / hold / none -- a starting point for the call, not the call.

    `hold` is the answer the runbook gives internal churn: refactors, tests, CI
    and docs reach people without a tag. It is a claim about this delta, and a
    `None` sitting beside a diff that moves a decision is the failure CI cannot
    catch, so `status` prints the commits underneath it either way.
    """
    if not commits:
        return 'none'
    if any(note not in ('None', '!! UNANSWERED', '!! NO SECTION', '!! UNREADABLE')
           for _, note in notes):
        return 'release'
    if any(commit_type(subject) in USER_FACING_TYPES for _, subject in commits):
        return 'release'
    return 'hold'


# --------------------------------------------------------------- subcommands

def cmd_status(args):
    names = args.plugins or plugin_names()
    report = []
    for name in names:
        tag = previous_tag(name)
        since = tag or boundary_commit(name)
        commits = delta(name, since)
        notes = harvest([s for _, s in commits]) if args.notes else []
        report.append({
            'plugin': name,
            'version': current_version(name),
            'locations': [(label, value) for label, _, value in locations(name)],
            'previous_tag': tag,
            'since': since,
            'first_release_here': tag is None,
            'commits': [{'sha': sha[:9], 'subject': subject} for sha, subject in commits],
            'notes': [{'pr': pr, 'note': note} for pr, note in notes],
            'verdict': verdict(commits, notes),
        })

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    for item in report:
        version = item['version'] or 'DISAGREE %s' % item['locations']
        print('%-18s %-9s %-8s %s' % (
            item['plugin'], version, item['verdict'],
            'first release here (since %s)' % item['since'][:9]
            if item['first_release_here'] else 'since %s' % item['previous_tag']))
        for commit in item['commits']:
            print('    %s  %s' % (commit['sha'], commit['subject']))
        for note in item['notes']:
            print('    #%-8s %s' % (note['pr'], note['note']))
    if not args.notes:
        print('\nnotes not harvested: re-run with --notes (needs gh) to read the'
              ' `## Release note` block each author wrote')
    return 0


def cmd_bump(args):
    cur = current_version(args.plugin)
    if cur is None:
        die('the three version locations disagree for %s: %s -- fix before bumping'
            % (args.plugin, locations(args.plugin)))
    new = next_version(cur, args.level)
    write_version(args.plugin, new)
    if current_version(args.plugin) != new:
        die('post-bump check failed for %s -- the tree is half-written, inspect it'
            % args.plugin)
    print('%s %s -> %s (plugin.json, marketplace.json, README.md)'
          % (args.plugin, cur, new))
    print('notes: plugins/%s/docs/releases/v%s.md' % (args.plugin, new))
    return 0


def cmd_tag(args):
    version = current_version(args.plugin)
    if version is None:
        die('the three version locations disagree for %s: %s'
            % (args.plugin, locations(args.plugin)))
    tag = '%s/v%s' % (args.plugin, version)

    notes = os.path.join(ROOT, 'plugins', args.plugin, 'docs', 'releases',
                         'v%s.md' % version)
    if not os.path.exists(notes):
        die('no notes at %s -- the body is published from the file'
            % os.path.relpath(notes, ROOT))
    if git('tag', '--list', tag).strip():
        die('%s already exists locally' % tag)
    if subprocess.run(['git', '-C', ROOT, 'ls-remote', '--exit-code', '--tags',
                       'origin', 'refs/tags/%s' % tag],
                      capture_output=True, text=True).returncode == 0:
        die('%s already exists on origin -- never retag, supersede with a higher patch' % tag)

    head = git('rev-parse', 'HEAD').strip()
    if head != git('rev-parse', 'origin/main').strip():
        die('HEAD is not origin/main -- the release PR has to merge first, and the'
            ' tag goes on the commit that landed')

    git('tag', '-a', tag, '-m', '%s %s' % (args.plugin, version), head)
    print('created %s at %s' % (tag, head[:9]))
    print('\nPublish (separately, never chained -- branch-guard asks on a tag push,'
          '\nand a denied `git tag && git push` loses the tag creation too):\n')
    print('  git push origin %s' % tag)
    print("  gh release create '%s' --title '%s %s' --latest=false --notes-file %s"
          % (tag, args.plugin, version, os.path.relpath(notes, ROOT)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='release.py', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command')

    p_status = sub.add_parser('status', help='per-plugin delta since its last release')
    p_status.add_argument('plugins', nargs='*')
    p_status.add_argument('--notes', action='store_true',
                          help='harvest each PR\'s `## Release note` block (needs gh)')
    p_status.add_argument('--json', action='store_true')
    p_status.set_defaults(func=cmd_status)

    p_bump = sub.add_parser('bump', help='write all three version locations')
    p_bump.add_argument('plugin')
    p_bump.add_argument('level', help='major | minor | patch | X.Y.Z')
    p_bump.set_defaults(func=cmd_bump)

    p_tag = sub.add_parser('tag', help='tag the merged bump and print the publish commands')
    p_tag.add_argument('plugin')
    p_tag.set_defaults(func=cmd_tag)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    known = plugin_names()
    for name in ([args.plugin] if getattr(args, 'plugin', None) else getattr(args, 'plugins', [])):
        if name not in known:
            die("unknown plugin '%s' -- the marketplace lists: %s" % (name, ', '.join(known)))
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
