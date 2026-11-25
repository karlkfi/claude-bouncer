#!/usr/bin/env python3
"""Report where branch-guard friction accumulates, from session transcripts.

Read-only analyzer. Claude Code records each hook decision, with the call that
triggered it, in the session transcripts under ``~/.claude/projects/**/*.jsonl``.
This re-reads those records and ranks branch-guard's prompts and denials by
category, and each category maps to one fix. It adds no telemetry and writes
nothing (see PRIVACY.md).

How a transcript records a decision, and why a denial has to be recovered from
an error result, is in ``lib/bouncer_friction.py``.

Usage:
    python3 scripts/friction-report.py                 # last 7 days
    python3 scripts/friction-report.py --since 24h
    python3 scripts/friction-report.py --since 2026-06-01 --repo gateway
    python3 scripts/friction-report.py --plugin all --top 20
    python3 scripts/friction-report.py --json           # machine-readable
"""
import argparse
import collections
import glob
import importlib.util
import json
import os
import re
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'lib'))
from bouncer_friction import (                                # noqa: E402
    Call, in_window, parse_since, read_events, stale_install)

THIS_GUARD = 'branch-guard'

# The tools hooks/hooks.json registers the hook on, which is what the friction
# rate divides by. A silent defer leaves no record, so decisions cannot be the
# denominator.
GUARDED_TOOLS = ('Bash', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit')

# Every ask and deny the hook emits opens with the cause its classifier wrote,
# so each pattern is anchored there: a branch name quoted later in the reason
# cannot move it. `test/test_friction_report.py` reads the causes out of the
# hook and fails on one that matches no category, or more than one. Wordings
# the hook no longer emits are kept where transcripts still hold them.
CATEGORY_PATTERNS = {
    'protected-work': re.compile(
        r"Targets protected branch '|Push targets protected branch '"
        r"|`git [^`]+` onto a protected branch"
        r"|`git rebase` on a protected branch"
        r"|`git pull` may merge or rebase onto protected branch"
        r"|`git reset --hard` on protected branch"
        r"|Stash operation on a protected branch"),
    'protected-ref': re.compile(
        r"(?:Deleting|Renaming) protected branch '"
        r"|(?:Copying|Renaming) a branch onto protected branch '"
        r"|`git [^`]+` (?:moves|resets) protected branch '"),
    'cross-push': re.compile(
        r"Push targets '[^']*', not the worktree branch"
        r"|Push sends local branch|Push targets multiple branches"
        r"|Push uses a wildcard refspec"),
    'tag-push': re.compile(
        r"Push publishes every local tag"
        r"|Push targets '[^']*', a tag or other non-branch ref"),
    'stale-base': re.compile(
        r"'[^']*' has moved since this branch left it"),
    'unpinned-gh': re.compile(
        r"`gh [^`]+` names .* that exists in every repository"),
    'unpublished-tip': re.compile(
        r"`git branch -D` force-deletes '[^']*', whose tip isn't reachable"
        r"|`git reset --hard` moves branch '[^']*', whose tip isn't reachable"
        r"|`git [^`]+` moves existing branch '[^']*', whose current tip isn't"
        r"|Deleting/renaming a git branch"),
    'unnamed-target': re.compile(
        r"`git [^`]+`(?:/`--copy`)? names no branch"
        r"|`git branch -D` force-deletes a branch, and "
        r"|`git branch -D` force-deletes '[^']*', and "
        r"|`git [^`]+` can move an existing branch pointer, and "
        r"|`git [^`]+` moves existing branch '[^']*', and "
        r"|`git reset --hard` moves branch '[^']*', and "
        r"|`git reset --hard` discards changes\b"
        r"|This command acts on a repository this guard can't name"),
    'uncommitted-work': re.compile(
        r"`git restore` discards working-tree changes"
        r"|`git (?:switch|checkout -f)` would discard changes"
        r"|`git reset --hard` discards uncommitted changes"
        r"|`git clean` deletes untracked files"),
    'stash-drop': re.compile(r"Dropping stashed changes"),
    'worktree-force': re.compile(r"`git worktree remove --force` deletes"),
    'worktree-admin': re.compile(
        r"(?:Pruning or moving|Removing/moving) a git worktree"),
    'worktree-grant': re.compile(r"`git worktree add` creates a second"),
    'tag-delete': re.compile(r"Deleting a git tag"),
    'git-config': re.compile(r"Writing git config"),
    'history-rewrite': re.compile(
        r"Rewriting the reflog|`git [^`]+` can rewrite or prune history"),
    'remote-delete': re.compile(r"`gh [^`]+` (?:deletes|disables) "),
}

# One fix per category: what stops the verdict, not a way past it.
CATEGORY_HINT = {
    'protected-work':   'work on a feature branch or worktree and land it by '
                        'pull request',
    'protected-ref':    'leave the shared branch alone and pick another name',
    'cross-push':       'push the worktree branch to its own name, or name '
                        'the destination in --force-with-lease=<branch>:<sha>',
    'tag-push':         'push one tag by name, and expect one prompt per '
                        'release',
    'stale-base':       'git fetch && git rebase <base> before pushing',
    'unpinned-gh':      'add --repo <owner>/<name>',
    'unpublished-tip':  'push the branch first, or check with gh pr view '
                        'that it already merged',
    'unnamed-target':   'name an existing branch, and a literal directory in '
                        'cd or git -C',
    'uncommitted-work': 'commit the work to a branch first',
    'stash-drop':       'apply the stash and commit it before dropping it',
    'worktree-force':   'clean or commit inside it, then git worktree remove '
                        'without --force; use the session scratchpad for '
                        'throwaway checkouts',
    'worktree-admin':   'remove your own worktree by path with git worktree '
                        'remove',
    'worktree-grant':   'approve once per checkout, or unset '
                        'BRANCH_GUARD_WORKTREE_GRANTS',
    'tag-delete':       'leave the tag for the user to delete',
    'git-config':       'set it for one command with git -c key=value',
    'history-rewrite':  'skip it; git expires and repacks on its own',
    'remote-delete':    'close or archive instead, or leave the deletion to '
                        'the user',
}

# The guard leaves its allows unprefixed, so one recorded without a script path
# can be attributed only by its own wording.
ALLOW_TEXT = re.compile(
    r"Safe git/gh operation on branch '.*' — auto-approved\.\Z"
    r"|Safe read-only git/gh operation — auto-approved\.\Z"
    r"|.* — BRANCH_GUARD_OVERRIDE is set \(.*\), so branch-guard allowed it\.\Z",
    re.S)
LIFTED = ' — BRANCH_GUARD_OVERRIDE is set ('
OVERRIDE_VAR = 'BRANCH_GUARD_OVERRIDE'

# confirm() in the hook denies instead of asking where nobody can answer.
NO_PROMPT = 'has no way to prompt for confirmation'

DEFAULT_PLUGINS_DIR = os.path.expanduser('~/.claude/plugins')


def own_label(decision, reason):
    return (THIS_GUARD if decision == 'allow' and ALLOW_TEXT.match(reason)
            else None)


def category_of(reason):
    """The friction category of one reason, or 'other'."""
    cause = reason[len(THIS_GUARD) + 2:] \
        if reason.startswith(THIS_GUARD + ': ') else reason
    for cat, rx in CATEGORY_PATTERNS.items():
        if rx.match(cause):
            return cat
    return 'other'


_hook = None


def has_override(command):
    """Whether the guard would read a break-glass prefix in this command. Asked
    of the hook itself, since the variable's name in a grep, a heredoc or a
    commit message arms nothing."""
    global _hook
    if OVERRIDE_VAR not in command:
        return False
    if _hook is None:
        spec = importlib.util.spec_from_file_location(
            'branch_guard', os.path.join(ROOT, 'hooks', 'branch-guard.py'))
        _hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_hook)
    try:
        tokens = _hook.tokenize(_hook.strip_heredocs(command))
    except ValueError:
        return False                # the hook defers on a command it can't lex
    return _hook.override_reason(_hook.command_segments(tokens)) is not None


def scan(events, plugin, cutoff, repo):
    """(decisions, calls, survey) meeting the filters. The survey records how
    far each filter got, so an empty result can name the one that emptied it."""
    decisions, calls = [], []
    survey = {'labels': collections.Counter(), 'plugin_hits': 0,
              'repo_hits': 0, 'latest': None}
    for event in events:
        if isinstance(event, Call):
            if event.tool in GUARDED_TOOLS and in_window(event, cutoff, repo):
                calls.append(event)
            continue
        survey['labels'][event.plugin] += 1
        if plugin not in ('all', event.plugin):
            continue
        survey['plugin_hits'] += 1
        if repo and repo not in event.cwd:
            continue
        survey['repo_hits'] += 1
        if survey['latest'] is None or event.ts > survey['latest']:
            survey['latest'] = event.ts
        if in_window(event, cutoff, repo):
            decisions.append(event)
    return decisions, calls, survey


def explain_empty(survey, plugin, since, repo):
    """Lines naming which filter emptied the result. Call only when it is."""
    if not survey['labels']:
        return ["No guard decisions in the scanned transcripts at all (no "
                "PreToolUse hook has run,",
                "or the transcript root is wrong)."]
    if not survey['plugin_hits']:
        found = ', '.join('%s (%d)' % kv for kv in survey['labels'].most_common())
        return ["--plugin %r matched no guard in the scanned transcripts." % plugin,
                "  Guards found: " + found]
    if not survey['repo_hits']:
        return ["--repo %r matched no cwd among the %d decisions."
                % (repo, survey['plugin_hits']),
                "  It is a plain substring match on the recorded cwd."]
    return ["--since %s excluded all %d matching decisions."
            % (since, survey['repo_hits']),
            "  The most recent is %s; use --since all for no limit."
            % survey['latest'].strftime('%Y-%m-%d')]


def build_report(decisions, calls, plugin=THIS_GUARD):
    count = collections.Counter
    r = {'total': len(decisions), 'decisions': count(), 'plugins': count(),
         'categories': {'ask': count(), 'deny': count()}, 'tools': count(),
         'commands': count(), 'errors': count(), 'no_prompt_denies': 0,
         'calls': None, 'sessions': None, 'sessions_stopped': None,
         'overrides': None}
    stopped = set()
    overridden = {c.call_id for c in calls
                  if c.tool == 'Bash' and has_override(c.command)}
    lifted = refused = 0
    for d in decisions:
        r['decisions'][d.decision] += 1
        r['plugins'][d.plugin] += 1
        if d.decision == 'error':
            r['errors']['%s: %s' % (d.plugin, d.reason)] += 1
        if d.plugin == THIS_GUARD and d.decision == 'allow' and LIFTED in d.reason:
            lifted += 1
        if d.decision not in ('ask', 'deny'):
            continue
        stopped.add(d.session)
        cat = category_of(d.reason) if d.plugin == THIS_GUARD else 'other'
        r['categories'][d.decision][cat] += 1
        r['tools'][d.tool or '?'] += 1
        if d.plugin == THIS_GUARD:
            refused += d.call_id in overridden
            if d.decision == 'deny' and NO_PROMPT in d.reason:
                r['no_prompt_denies'] += 1
        if d.command:
            r['commands'][' '.join(d.command.split())[:100]] += 1
    # Both describe branch-guard alone, so they are absent, not zero, anywhere
    # else: another guard runs on other tools and reads another variable.
    if plugin == THIS_GUARD:
        sessions = {c.session for c in calls} | stopped
        r.update(calls=len(calls), sessions=len(sessions),
                 sessions_stopped=len(stopped),
                 overrides={'prefixed': len(overridden), 'lifted': lifted,
                            'refused': refused})
    return r


COVERAGE = ("A silent defer leaves no transcript record, so allow and defer "
            "are floors. A deny is read back off the error the blocked call "
            "handed back. Decisions carry no guard version, so a window "
            "spanning releases mixes them.")


def print_text(r, top, plugin=THIS_GUARD, stale=None, notes=()):
    if stale:
        print("!  %s %s installed, %s available in the marketplace clone."
              % (stale['plugin'], stale['installed'], stale['available']))
        print("   A newer release may already reduce the friction below. "
              "Update with:")
        print("     /plugin marketplace update %s && /reload-plugins\n"
              % stale['marketplace'])
    label = 'guard' if plugin == 'all' else plugin
    if not r['total']:
        print("No %s decisions found for the given filters." % label)
        for line in notes:
            print(line)
        return
    stops = r['decisions']['ask'] + r['decisions']['deny']
    join = ', '.join
    print("%s decisions recorded: %d" % (label, r['total']))
    print("  plugins: " + join('%s %d' % kv for kv in r['plugins'].most_common()))
    print("  outcomes: "
          + join('%s %d' % kv for kv in r['decisions'].most_common()))
    print("  friction (ask+deny): %d%s" % (
        stops, " -- %.2f%% of the %d calls to %s in the window" % (
            100 * stops / r['calls'], r['calls'], '/'.join(GUARDED_TOOLS))
        if r['calls'] else ''))
    if r['tools']:
        print("  friction by tool: "
              + join('%s %d' % kv for kv in r['tools'].most_common()))
    if r['sessions']:
        print("  sessions stopped at least once: %d of %d"
              % (r['sessions_stopped'], r['sessions']))
    if r['no_prompt_denies']:
        print("  denied because the permission mode cannot prompt: %d"
              % r['no_prompt_denies'])
    if r['overrides']:
        print("  %s prefixes: %d (lifted a verdict %d, refused %d)" % (
            OVERRIDE_VAR, r['overrides']['prefixed'],
            r['overrides']['lifted'], r['overrides']['refused']))
    for line in textwrap.wrap(COVERAGE, 78, initial_indent='  coverage: ',
                              subsequent_indent='    '):
        print(line)
    print()

    # A hook that never ran is a guard that stopped guarding, so it outranks
    # any prompt count.
    if r['errors']:
        print("Hook failures -- the guard never ran, so the call went "
              "unguarded (top %d):" % top)
        for note, n in r['errors'].most_common(top):
            print("  %5d  %s" % (n, note))
        print()
    asks, denies = r['categories']['ask'], r['categories']['deny']
    if stops:
        print("By category, each with one fix:")
        print("    ask  deny  category")
        for cat, _ in (asks + denies).most_common():
            print("  %5d %5d  %-16s  %s" % (
                asks[cat], denies[cat], cat, CATEGORY_HINT.get(cat, '')))
        if 'other' in asks + denies and plugin == 'all':
            print('  ("other" is mostly the guards besides %s)' % THIS_GUARD)
        print()
    if r['commands']:
        print("Top triggering commands (Bash only, top %d):" % top)
        for c, n in r['commands'].most_common(top):
            print("  %5d  %s" % (n, c))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--transcripts',
                    default=os.path.expanduser('~/.claude/projects'),
                    help='transcript root (default: ~/.claude/projects)')
    ap.add_argument('--plugin', default=THIS_GUARD,
                    help="guard to report on, or 'all' (default: %s)" % THIS_GUARD)
    ap.add_argument('--since', default='7d',
                    help="time window: Nd/Nh/Nm or YYYY-MM-DD (default: 7d; "
                         "'all' for no limit)")
    ap.add_argument('--repo', default='',
                    help='only calls whose cwd contains this substring')
    ap.add_argument('--plugins-dir', default=DEFAULT_PLUGINS_DIR,
                    help='Claude Code plugins dir (default: ~/.claude/plugins); '
                         'used to flag a stale installed version')
    ap.add_argument('--top', type=int, default=15, help='rows per ranking')
    ap.add_argument('--json', action='store_true', help='emit JSON')
    args = ap.parse_args()

    try:
        cutoff = parse_since(args.since)
    except ValueError as err:
        sys.exit('--since: %s' % err)
    paths = glob.glob(os.path.join(args.transcripts, '**', '*.jsonl'),
                      recursive=True)
    if not paths:
        sys.exit("No transcripts under %s" % args.transcripts)
    # A command in another script must not fail the report on a Windows console.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')

    decisions, calls, survey = scan(read_events(paths, own_label),
                                    args.plugin, cutoff, args.repo)
    report = build_report(decisions, calls, args.plugin)
    stale = (stale_install(args.plugins_dir, args.plugin)
             if args.plugin != 'all' else None)
    notes = [] if decisions else explain_empty(survey, args.plugin,
                                               args.since, args.repo)
    if args.json:
        print(json.dumps(dict(
            {k: v for k, v in report.items()
             if v is not None and k not in ('commands', 'errors')},
            guards_seen=survey['labels'],
            top_commands=report['commands'].most_common(args.top),
            hook_errors=report['errors'].most_common(args.top),
            coverage=COVERAGE, stale=stale, empty_because=notes or None),
            indent=2))
    else:
        print_text(report, args.top, args.plugin, stale, notes)

    # A --plugin or --repo value nothing can match is a usage error, not a
    # guard with no friction, so it exits non-zero. An empty window is an answer.
    if survey['labels'] and not survey['repo_hits']:
        sys.exit(2)


if __name__ == '__main__':
    main()
