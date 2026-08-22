#!/usr/bin/env python3
"""Verify the CI path filters still cover every plugin, and every job.

`.github/workflows/tests.yml` classifies each pull request's diff and skips the
jobs whose plugin it did not touch. That makes a missing filter the most
expensive kind of defect: the gate goes green by SKIPPING rather than by
passing, so `main` ends up green on evidence it never gathered. Nothing else
notices -- a skipped job and a passing job read the same in a checks list.

Three assertions, each closing a hole that opens silently:

  1. Every directory under plugins/ has a filter named after it. A sixth guard
     added to the marketplace would otherwise ship with its suite never run.
  2. Every filter names its own plugin directory and pulls in the shared
     anchor, so a lib/ change still reaches all five vendored copies.
  3. Every job that works inside a plugin carries that plugin's `if:` guard.
     An unguarded job runs on every pull request, which is only wasteful; a job
     guarded by the WRONG plugin skips when its own code changes, which is not.

Prints one line per assertion and exits 1 on any failure.

  python3 scripts/path-filter-check.py

`read_workflow()` and the three `check_*` functions are the importable half.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, '.github', 'workflows', 'tests.yml')
PLUGINS = os.path.join(ROOT, 'plugins')

# The anchor every plugin filter pulls in, and the one path it must carry: an
# edit to the workflow re-runs everything, because the filters are in it.
SHARED_ANCHOR = '*shared'
WORKFLOW_PATH = '.github/workflows/tests.yml'

JOB_RE = re.compile(r'^  ([a-z0-9][a-z0-9-]*):$')
FILTER_RE = re.compile(r'^            ([a-z0-9_]+):')
OUTPUT_RE = re.compile(r"needs\.changes\.outputs\.([a-z0-9_]+)")


def plugin_names():
    return sorted(d for d in os.listdir(PLUGINS)
                  if os.path.isdir(os.path.join(PLUGINS, d)))


def filter_name(plugin):
    """A filter is its plugin's name -- an expression cannot spell a hyphen."""
    return plugin.replace('-', '_')


def read_workflow(path=WORKFLOW):
    """The filter definitions and the job blocks, as plain text.

    Hand-parsed rather than through a YAML library: the tests run on the
    stdlib alone, and the two shapes this needs are fixed by the file's own
    indentation.
    """
    with open(path) as f:
        lines = f.read().splitlines()

    filters, current, jobs, job = {}, None, {}, None
    in_filters = False
    for line in lines:
        if line.strip() == 'filters: |':
            in_filters = True
            continue
        job_match = JOB_RE.match(line)
        if job_match:
            in_filters, current = False, None
            job = job_match.group(1)
            jobs[job] = []
            continue
        if in_filters:
            filter_match = FILTER_RE.match(line)
            if filter_match:
                current = filter_match.group(1)
                filters[current] = []
            elif current and line.strip().startswith('- '):
                filters[current].append(line.strip()[2:].strip().strip("'"))
            continue
        if job:
            jobs[job].append(line)
    return filters, jobs


def check_every_plugin_has_a_filter(filters, plugins):
    return ['no filter named %r for plugins/%s' % (filter_name(p), p)
            for p in plugins if filter_name(p) not in filters]


def check_filters_are_complete(filters, plugins):
    problems = []
    shared = filters.get('shared', [])
    if WORKFLOW_PATH not in shared:
        problems.append('the shared anchor does not list %s, so editing the '
                        'filters would not re-run the jobs they gate'
                        % WORKFLOW_PATH)
    for p in plugins:
        entries = filters.get(filter_name(p))
        if entries is None:
            continue                      # already reported by assertion 1
        own = 'plugins/%s/**' % p
        if own not in entries:
            problems.append('filter %r does not name %s' % (filter_name(p), own))
        if SHARED_ANCHOR not in entries:
            problems.append('filter %r does not pull in %s, so a lib/ change '
                            'would skip it' % (filter_name(p), SHARED_ANCHOR))
    return problems


def check_jobs_are_guarded(jobs, plugins):
    """A job working inside plugins/<name> must be gated on that plugin."""
    problems = []
    for job, body in sorted(jobs.items()):
        text = '\n'.join(body)
        worked_on = sorted({p for p in plugins if 'plugins/%s' % p in text})
        if not worked_on:
            continue
        guards = set(OUTPUT_RE.findall(text))
        want = {filter_name(p) for p in worked_on}
        if not guards:
            problems.append('job %r works in %s and carries no `if:` guard'
                            % (job, ', '.join(worked_on)))
        elif guards != want:
            problems.append('job %r works in %s but is gated on %s'
                            % (job, ', '.join(worked_on),
                               ', '.join(sorted(guards))))
    return problems


def main():
    filters, jobs = read_workflow()
    plugins = plugin_names()
    failed = False
    for label, problems in (
            ('every plugin has a filter',
             check_every_plugin_has_a_filter(filters, plugins)),
            ('every filter is complete',
             check_filters_are_complete(filters, plugins)),
            ('every plugin job is guarded',
             check_jobs_are_guarded(jobs, plugins))):
        print('%-32s %s' % (label, 'ok' if not problems else 'FAILED'))
        for problem in problems:
            print('  %s' % problem)
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
