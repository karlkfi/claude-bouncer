#!/usr/bin/env python3
"""Report where exit-status-guard friction accumulates, from session transcripts.

Read-only analyzer. The hook itself writes nothing to disk (see PRIVACY.md); it
only emits a decision on stdout. Claude Code records that stdout -- plus the
triggering command, cwd, and timestamp -- in the session transcripts under
``~/.claude/projects/**/*.jsonl``. This tool re-reads those records and ranks
the guard's denials so you can see, in one command, which rule fires most and
what Claude was doing when it tripped.

Nothing here changes the hook or adds telemetry: it parses data Claude Code
already persisted locally.

Every exit-status-guard verdict is a `deny`, so a high count is not by itself bad --
each one is a false green the model was about to report as a pass. What matters
is the shape: a rule that fires on the same command over and over is either a
habit worth fixing upstream, or a defect in the registry.

Usage:
    python3 scripts/friction-report.py                 # last 7 days, this guard
    python3 scripts/friction-report.py --since 24h
    python3 scripts/friction-report.py --since 2026-06-01 --repo gateway
    python3 scripts/friction-report.py --plugin all --top 20
    python3 scripts/friction-report.py --json           # machine-readable
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import sys
import textwrap

# The guard this script's REASON_PATTERNS describe. Other guards' decisions are
# counted (--plugin all) but their reasons carry no tokens we can categorize.
THIS_GUARD = 'exit-status-guard'

# A label is the hook script's filename minus `bash-`, so transcripts written
# before the 2.0.0 rename carry the 1.x one. The default filter accepts both:
# dropping them would report friction that has not gone anywhere as absent.
GUARD_ALIASES = {THIS_GUARD: (THIS_GUARD, 'pipe-guard')}

# Each rule's reason carries a phrase no other rule uses. Matching on those
# rather than on a prefix keeps the categories stable when the advice text is
# reworded, which it is more often than the diagnosis.
REASON_PATTERNS = {
    'piped':      re.compile(r"exit status is the filter's"),
    'pipestatus': re.compile(r"does not exist in zsh"),
    'background': re.compile(r"runs in the background, but this call's exit"),
    'sequenced':  re.compile(r"runs the second whatever the first returned"),
}

# The gate each denial names, in backticks after the guard's own name. The
# prefix is optional because it is not in denials recorded before it shipped,
# and dropping those would report gates that were hit as never hit.
GATE_RE = re.compile(r'^(?:[a-z0-9-]+-guard:\s+)?`([^`]+)`')

# Volatile command fragments to collapse so near-identical commands group
# together. --raw disables this.
NORMALIZERS = [
    (re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
                r'[0-9a-f]{4}-[0-9a-f]{12}\b'), '<uuid>'),
    (re.compile(r'\b[0-9a-f]{40}\b'), '<sha>'),
    (re.compile(r'\b\d{4,}\b'), '<n>'),
]

DEFAULT_PLUGINS_DIR = os.path.expanduser('~/.claude/plugins')


def normalize(tok):
    for pat, repl in NORMALIZERS:
        tok = pat.sub(repl, tok)
    return tok


def version_tuple(v):
    """Comparable tuple of the leading numeric components of a version string.

    '1.5.0' -> (1, 5, 0); stops at the first non-numeric component, so a
    pre-release tag is treated as its base version. None when nothing numeric.
    """
    if not v:
        return None
    out = []
    for part in re.split(r'[.\-+]', str(v).strip()):
        if not part.isdigit():
            break
        out.append(int(part))
    return tuple(out) or None


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def installed_plugin_info(plugins_dir, plugin):
    """(version, marketplace) for the installed `plugin`, or (None, None)."""
    data = _read_json(os.path.join(plugins_dir, 'installed_plugins.json'))
    if not isinstance(data, dict):
        return None, None
    for key, records in (data.get('plugins') or {}).items():
        name, _, marketplace = key.partition('@')
        if name != plugin:
            continue
        best, best_t = None, None
        for rec in records or []:
            v = rec.get('version') if isinstance(rec, dict) else None
            t = version_tuple(v)
            if t is not None and (best_t is None or t > best_t):
                best, best_t = v, t
        return best, (marketplace or None)
    return None, None


def marketplace_location(plugins_dir, marketplace):
    known = _read_json(os.path.join(plugins_dir, 'known_marketplaces.json'))
    if isinstance(known, dict):
        entry = known.get(marketplace)
        if isinstance(entry, dict) and entry.get('installLocation'):
            return entry['installLocation']
    return os.path.join(plugins_dir, 'marketplaces', marketplace)


def available_plugin_version(plugins_dir, plugin, marketplace):
    """Version the marketplace clone advertises for `plugin`, or None."""
    if not marketplace:
        return None
    loc = marketplace_location(plugins_dir, marketplace)
    manifest = _read_json(os.path.join(loc, '.claude-plugin', 'plugin.json'))
    if isinstance(manifest, dict) and manifest.get('name') == plugin:
        if manifest.get('version'):
            return manifest['version']
    mkt = _read_json(os.path.join(loc, '.claude-plugin', 'marketplace.json'))
    if isinstance(mkt, dict):
        for p in (mkt.get('plugins') or []):
            if isinstance(p, dict) and p.get('name') == plugin:
                return p.get('version')
    return None


def check_staleness(plugins_dir, plugin):
    """Staleness info when the installed plugin lags the marketplace clone.

    A third-party git marketplace pins its installed version until the user
    acts, so friction a newer release already fixes can linger silently. Any
    missing or unparseable file degrades to None -- the report never breaks.
    """
    if plugin == 'all':
        return None
    installed, marketplace = installed_plugin_info(plugins_dir, plugin)
    if not installed:
        return None
    available = available_plugin_version(plugins_dir, plugin, marketplace)
    if not available:
        return None
    it, at = version_tuple(installed), version_tuple(available)
    if it is None or at is None or not it < at:
        return None
    return {'plugin': plugin, 'installed': installed,
            'available': available, 'marketplace': marketplace}


def parse_since(spec):
    """A tz-aware UTC cutoff, or None. Accepts Nd/Nh/Nm or YYYY-MM-DD."""
    if not spec:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    m = re.fullmatch(r'(\d+)([dhm])', spec)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - {'d': dt.timedelta(days=n),
                      'h': dt.timedelta(hours=n),
                      'm': dt.timedelta(minutes=n)}[unit]
    try:
        return dt.datetime.strptime(spec, '%Y-%m-%d').replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        sys.exit("--since: expected Nd/Nh/Nm or YYYY-MM-DD, got %r" % spec)


def parse_ts(rec):
    ts = rec.get('timestamp')
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None


def guard_name(command):
    """Plugin label from a hook command, e.g. '.../bash-exit-status-guard.py'
    -> 'exit-status-guard'. None if the command names no *.py guard."""
    m = re.search(r'([A-Za-z0-9_-]+)\.py', command or '')
    if not m:
        return None
    return re.sub(r'^bash-', '', m.group(1))


def iter_decisions(paths):
    """Yield every guard decision found in the given transcript files.

    Builds a per-file toolUseID -> Bash command map (ids are session-scoped) so
    each decision can name the command that triggered it.
    """
    for path in paths:
        cmd_by_id = {}
        records = []
        try:
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    msg = rec.get('message') or {}
                    for b in (msg.get('content') or []):
                        if (isinstance(b, dict) and b.get('type') == 'tool_use'
                                and b.get('name') == 'Bash' and b.get('id')):
                            cmd_by_id[b['id']] = (
                                b.get('input') or {}).get('command', '')
                    records.append(rec)
        except OSError:
            continue

        for rec in records:
            att = rec.get('attachment')
            if not isinstance(att, dict) or att.get('hookName') != 'PreToolUse:Bash':
                continue
            name = guard_name(att.get('command'))
            if name is None:
                continue
            stdout = att.get('stdout') or ''
            decision, reason = 'defer', ''   # empty stdout => hook stayed silent
            if stdout.strip():
                try:
                    hso = json.loads(stdout).get('hookSpecificOutput') or {}
                    decision = hso.get('permissionDecision', 'defer')
                    reason = hso.get('permissionDecisionReason', '')
                except ValueError:
                    pass
            yield {'plugin': name, 'decision': decision, 'reason': reason,
                   'cwd': rec.get('cwd') or '', 'ts': parse_ts(rec),
                   'command': cmd_by_id.get(att.get('toolUseID'), '')}


def scan(paths, plugin, cutoff, repo):
    """(matched decisions, survey) for the given filters.

    The survey records how far each filter got, so an empty result can name the
    filter that emptied it instead of reading like a guard with zero friction.
    """
    matched = []
    wanted = GUARD_ALIASES.get(plugin, (plugin,))
    survey = {'labels': collections.Counter(), 'plugin_hits': 0,
              'repo_hits': 0, 'latest': None}
    for d in iter_decisions(paths):
        survey['labels'][d['plugin']] += 1
        if plugin != 'all' and d['plugin'] not in wanted:
            continue
        survey['plugin_hits'] += 1
        if repo and repo not in d['cwd']:
            continue
        survey['repo_hits'] += 1
        ts = d['ts']
        if ts and (survey['latest'] is None or ts > survey['latest']):
            survey['latest'] = ts
        if cutoff and ts and ts < cutoff:
            continue
        matched.append(d)
    return matched, survey


def explain_empty(survey, plugin, since, repo):
    """Lines naming which filter emptied the result. Call only when it is."""
    if not survey['labels']:
        return ["No guard decisions in the scanned transcripts at all "
                "(no PreToolUse:Bash hook has run, or the transcript root is "
                "wrong)."]
    if not survey['plugin_hits']:
        found = ", ".join("%s (%d)" % (k, v)
                          for k, v in survey['labels'].most_common())
        return ["--plugin %r matched no guard in the scanned transcripts." % plugin,
                "  Guards found: " + found,
                "  A label comes from the hook script's filename, so it can "
                "differ from the plugin name.",
                "  An installed guard is also absent here if it emitted "
                "nothing: a hook run that",
                "  produces no stdout leaves no transcript record to read."]
    if not survey['repo_hits']:
        scope = "all guards'" if plugin == 'all' else "%s's" % plugin
        return ["--repo %r matched no cwd among %s %d decisions."
                % (repo, scope, survey['plugin_hits']),
                "  It is a plain substring match on the recorded cwd."]
    return ["--since %s excluded all %d matching decisions."
            % (since, survey['repo_hits']),
            "  The most recent is %s; use --since all for no limit."
            % survey['latest'].strftime('%Y-%m-%d')]


def coverage_note(plugin):
    """What the scan structurally cannot see.

    Claude Code records a hook attachment only when the hook writes to stdout,
    and this guard writes only when it denies. Every silent run -- the
    overwhelming majority -- leaves nothing to count, so these totals are the
    denials, not the traffic.
    """
    note = ["Denials only. This guard emits nothing when it has no objection, "
            "and a hook run with no stdout leaves no transcript record, so "
            "there is no denominator here -- these counts are not a rate."]
    if plugin == 'all':
        note.append("Guards emit on different terms, so the plugins: counts "
                    "are not a like-for-like ranking.")
    return note


def categorize(reason):
    """The rule that produced a reason, as a one-element dict.

    A reason matching no pattern -- another guard's under --plugin all, or one
    of ours we don't recognize -- buckets as 'other', so the category table sums
    to the denial count instead of silently dropping the remainder.
    """
    for cat, pat in REASON_PATTERNS.items():
        if pat.search(reason):
            return cat
    return 'other'


def build_report(decisions, raw):
    r = {'total': 0, 'decisions': collections.Counter(),
         'categories': collections.Counter(), 'gates': collections.Counter(),
         'commands': collections.Counter(), 'plugins': collections.Counter()}
    for d in decisions:
        r['total'] += 1
        r['decisions'][d['decision']] += 1
        r['plugins'][d['plugin']] += 1
        if d['decision'] not in ('ask', 'deny'):
            continue
        r['categories'][categorize(d['reason'])] += 1
        m = GATE_RE.match(d['reason'])
        if m:
            r['gates'][m.group(1) if raw else normalize(m.group(1))] += 1
        if d['command']:
            cmd = ' '.join(d['command'].split())[:100]
            r['commands'][cmd if raw else normalize(cmd)] += 1
    return r


def print_text(r, top, stale=None, plugin=THIS_GUARD, notes=()):
    if not r['total']:
        print("No guard decisions found for the given filters.")
        for line in notes:
            print(line)
        return
    blocked = r['decisions'].get('deny', 0) + r['decisions'].get('ask', 0)
    print("Guard decisions analyzed: %d" % r['total'])
    print("  plugins: " + ", ".join("%s %d" % (k, v)
                                    for k, v in r['plugins'].most_common()))
    print("  outcomes: " + ", ".join("%s %d" % (k, v)
                                     for k, v in r['decisions'].most_common()))
    print("  blocked: %d" % blocked)
    for line in textwrap.wrap(' '.join(coverage_note(plugin)), 78,
                              initial_indent='  coverage: ',
                              subsequent_indent='    '):
        print(line)
    print()

    if stale:
        print("!  %s %s installed, %s available in the marketplace clone."
              % (stale['plugin'], stale['installed'], stale['available']))
        print("   A newer release may already fix the rule below. Update with:")
        print("     /plugin marketplace update %s && /reload-plugins\n"
              % stale['marketplace'])

    if r['categories']:
        print("By rule:")
        for cat, n in r['categories'].most_common():
            print("  %5d  %s" % (n, cat))
        if plugin == 'all' and 'other' in r['categories']:
            print('  ("other" = denials from guards besides %s)' % THIS_GUARD)
        print()
    if r['gates']:
        print("Top gates whose status was being lost (top %d):" % top)
        for g, n in r['gates'].most_common(top):
            print("  %5d  %s" % (n, g))
        print()
    if r['commands']:
        print("Top triggering commands (top %d):" % top)
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
                    help="guard to report on, or 'all' (default: %s); the "
                         "label is the hook script's filename minus a 'bash-' "
                         "prefix" % THIS_GUARD)
    ap.add_argument('--since', default='7d',
                    help="time window: Nd/Nh/Nm or YYYY-MM-DD (default: 7d; "
                         "'all' for no limit)")
    ap.add_argument('--repo', default='',
                    help='only decisions whose cwd contains this substring')
    ap.add_argument('--plugins-dir', default=DEFAULT_PLUGINS_DIR,
                    help='Claude Code plugins dir (default: ~/.claude/plugins)')
    ap.add_argument('--top', type=int, default=15, help='rows per ranking')
    ap.add_argument('--raw', action='store_true',
                    help='do not collapse volatile command fragments')
    ap.add_argument('--json', action='store_true', help='emit JSON')
    args = ap.parse_args()

    cutoff = None if args.since == 'all' else parse_since(args.since)
    paths = glob.glob(os.path.join(args.transcripts, '**', '*.jsonl'),
                      recursive=True)
    if not paths:
        sys.exit("No transcripts under %s" % args.transcripts)

    decisions, survey = scan(paths, args.plugin, cutoff, args.repo)
    report = build_report(decisions, args.raw)
    stale = check_staleness(args.plugins_dir, args.plugin)
    notes = [] if decisions else explain_empty(survey, args.plugin,
                                               args.since, args.repo)

    if args.json:
        print(json.dumps({
            'total': report['total'],
            'decisions': dict(report['decisions']),
            'plugins': dict(report['plugins']),
            'guards_seen': dict(survey['labels']),
            'categories': dict(report['categories']),
            'top_gates': report['gates'].most_common(args.top),
            'top_commands': report['commands'].most_common(args.top),
            'stale': stale,
            'coverage': coverage_note(args.plugin),
            'empty_because': notes or None,
        }, indent=2))
    else:
        print_text(report, args.top, stale, args.plugin, notes)

    # A --plugin or --repo value nothing in the transcripts can match is a usage
    # error, not a guard with zero friction; exit non-zero so it cannot be
    # mistaken for an answer. A satisfiable filter over an empty window is a
    # real answer, as is a setup with no recorded decisions yet.
    if survey['labels'] and not survey['repo_hits']:
        sys.exit(2)


if __name__ == '__main__':
    main()
