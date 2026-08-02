#!/usr/bin/env python3
"""Report where workspace-guard friction accumulates, from session transcripts.

Read-only analyzer. The hook itself writes nothing to disk (see PRIVACY.md);
it only emits a decision on stdout. Claude Code records that stdout — plus the
triggering command, cwd, branch, and timestamp — in the session transcripts
under ``~/.claude/projects/**/*.jsonl``. This tool re-reads those records and
ranks the guard's decisions so you can see, in one command, which prompts
dominate and what Claude was doing when it got prompted.

Nothing here changes the hook or adds telemetry: it parses data Claude Code
already persisted locally.

Usage:
    python3 scripts/friction-report.py                 # last 7 days, this guard
    python3 scripts/friction-report.py --since 24h
    python3 scripts/friction-report.py --since 2026-06-01 --repo gateway
    python3 scripts/friction-report.py --plugin all --raw --top 20
    python3 scripts/friction-report.py --json           # machine-readable

Each hook decision is recorded as an ``attachment`` line of type
``hook_success`` carrying ``hookName`` (``PreToolUse:Bash``), the hook
``command`` (which names the guard script), and ``stdout`` (the decision JSON).
The triggering Bash command is joined back via ``toolUseID``.
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
THIS_GUARD = 'workspace-guard'

# The reason strings emitted by build_reason() in bash-workspace-guard.py.
# Each category prefixes a comma-joined token list and ends before ". Fix:".
REASON_PATTERNS = {
    'outside':   re.compile(r"Outside-workspace path\(s\): (.*?)\. Fix:"),
    'expand':    re.compile(r"Runtime-expanded arg\(s\)[^:]*: (.*?)\. Fix:"),
    'untracked': re.compile(r"Relative path\(s\) after an untracked cd: (.*?)\. Fix:"),
}

# Volatile path segments to collapse so near-identical paths group together in
# the "top paths" ranking (e.g. every per-session /tmp/claude-NNN/... folds to
# one row). --raw disables this.
NORMALIZERS = [
    (re.compile(r'\btoolu_[A-Za-z0-9]+'), '<tooluse>'),
    (re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
                r'[0-9a-f]{4}-[0-9a-f]{12}\b'), '<uuid>'),
    (re.compile(r'(claude-)\d+'), r'\1<uid>'),
    (re.compile(r'-Users-[^/ ,]+'), '<encoded-project>'),
    (re.compile(r'\b\d{4,}\b'), '<n>'),
]


def normalize_path(tok):
    for pat, repl in NORMALIZERS:
        tok = pat.sub(repl, tok)
    return tok


# --- Stale-install detection (Q30) ------------------------------------------
# Claude Code auto-updates official marketplaces only; a third-party git
# marketplace pins its installed version until the user acts, so friction a
# newer release already fixes can linger silently. We compare the installed
# version (~/.claude/plugins/installed_plugins.json) against the local
# marketplace clone's plugin.json and flag a lag. All reads are of state Claude
# Code already persisted locally — no network, no telemetry; any missing or
# unparseable file degrades silently (return None) so the report never breaks.
DEFAULT_PLUGINS_DIR = os.path.expanduser('~/.claude/plugins')


def version_tuple(v):
    """Comparable tuple of the leading numeric components of a version string.

    '1.5.0' -> (1, 5, 0); stops at the first non-numeric component so a
    pre-release tag ('1.5.0-rc1' -> (1, 5, 0)) is treated as its base version.
    Returns None when nothing numeric is present.
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
    """(version, marketplace) for the installed `plugin`, or (None, None).

    installed_plugins.json keys plugins as '<name>@<marketplace>' and maps each
    to a list of install records (one per scope); we take the highest version.
    """
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
    """Filesystem path of the cloned `marketplace`, from known_marketplaces.json
    when present, else the conventional plugins/marketplaces/<name> path."""
    known = _read_json(os.path.join(plugins_dir, 'known_marketplaces.json'))
    if isinstance(known, dict):
        entry = known.get(marketplace)
        if isinstance(entry, dict) and entry.get('installLocation'):
            return entry['installLocation']
    return os.path.join(plugins_dir, 'marketplaces', marketplace)


def available_plugin_version(plugins_dir, plugin, marketplace):
    """Version the marketplace clone advertises for `plugin`, or None.

    Prefers the clone's `.claude-plugin/plugin.json` (the plugin's self-declared
    version, per issue #71); falls back to the per-plugin version in the
    marketplace manifest so a multi-plugin marketplace still resolves.
    """
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
    """Staleness info when the installed `plugin` lags the marketplace clone,
    else None. Skipped for `plugin == 'all'` (no single plugin to check)."""
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
    """Return a tz-aware UTC cutoff datetime, or None. Accepts Nd/Nh/Nm or a
    YYYY-MM-DD date."""
    if not spec:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    m = re.fullmatch(r'(\d+)([dhm])', spec)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {'d': dt.timedelta(days=n),
                 'h': dt.timedelta(hours=n),
                 'm': dt.timedelta(minutes=n)}[unit]
        return now - delta
    try:
        d = dt.datetime.strptime(spec, '%Y-%m-%d')
        return d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        sys.exit(f"--since: expected Nd/Nh/Nm or YYYY-MM-DD, got {spec!r}")


def parse_ts(rec):
    ts = rec.get('timestamp')
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None


def guard_name(command):
    """Plugin label from a hook command, e.g. '.../bash-workspace-guard.py'
    -> 'workspace-guard'. Returns None if the command names no *.py guard."""
    m = re.search(r'([A-Za-z0-9_-]+)\.py', command or '')
    if not m:
        return None
    base = m.group(1)
    base = re.sub(r'^bash-', '', base)
    return base


def iter_decisions(paths):
    """Yield every guard decision found in the given transcript files.

    Builds a per-file toolUseID -> Bash command map (ids are session-scoped)
    so each decision can name the command that triggered it. Filtering is the
    caller's job (see scan), which keeps the labels this pass saw available
    for diagnosing an empty result.
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
                    # Index Bash tool_use commands for the join.
                    msg = rec.get('message') or {}
                    for b in (msg.get('content') or []):
                        if (isinstance(b, dict) and b.get('type') == 'tool_use'
                                and b.get('name') == 'Bash' and b.get('id')):
                            cmd_by_id[b['id']] = (b.get('input') or {}).get('command', '')
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
            cwd = rec.get('cwd') or ''
            ts = parse_ts(rec)

            stdout = att.get('stdout') or ''
            decision, reason = 'defer', ''   # empty stdout => hook stayed silent
            if stdout.strip():
                try:
                    out = json.loads(stdout)
                    hso = out.get('hookSpecificOutput') or {}
                    decision = hso.get('permissionDecision', 'defer')
                    reason = hso.get('permissionDecisionReason', '')
                except ValueError:
                    pass
            yield {
                'plugin': name, 'decision': decision, 'reason': reason,
                'cwd': cwd, 'ts': ts,
                'command': cmd_by_id.get(att.get('toolUseID'), ''),
            }


def scan(paths, plugin, cutoff, repo):
    """Return (matched decisions, survey) for the given filters.

    The survey records how far each filter got, so an empty result can name the
    filter that emptied it instead of reading like a guard with zero friction
    (issue 97). Filters are applied in order — plugin, then repo, then window —
    and the first that drops everything is the one worth reporting.
    """
    matched = []
    survey = {'labels': collections.Counter(), 'plugin_hits': 0,
              'repo_hits': 0, 'latest': None}
    for d in iter_decisions(paths):
        survey['labels'][d['plugin']] += 1
        if plugin != 'all' and d['plugin'] != plugin:
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
    """Lines explaining an empty result: which filter emptied it, and what the
    transcripts do contain. Call only when the result is in fact empty."""
    if not survey['labels']:
        return ["No guard decisions in the scanned transcripts at all "
                "(no PreToolUse:Bash hook has run, or the transcript root is "
                "wrong)."]
    if not survey['plugin_hits']:
        found = ", ".join(f"{k} ({v})" for k, v in survey['labels'].most_common())
        return [f"--plugin {plugin!r} matched no guard in the scanned transcripts.",
                f"  Guards found: {found}",
                "  A label comes from the hook script's filename, so it can "
                "differ from the plugin name",
                "  (pr-sentinel's hook is pr-sentinel-guard.py, so its label is "
                "pr-sentinel-guard).",
                "  An installed guard is also absent here if it emitted nothing: "
                "a hook run that",
                "  produces no stdout leaves no transcript record to read."]
    if not survey['repo_hits']:
        scope = "all guards'" if plugin == 'all' else f"{plugin}'s"
        return [f"--repo {repo!r} matched no cwd among {scope} "
                f"{survey['plugin_hits']} decisions.",
                "  It is a plain substring match on the recorded cwd."]
    return [f"--since {since} excluded all {survey['repo_hits']} matching "
            f"decisions.",
            f"  The most recent is {survey['latest']:%Y-%m-%d}; use "
            "--since all for no limit."]


def coverage_note(plugin):
    """Sentences naming what the scan structurally cannot see.

    Claude Code records a hook attachment only when the hook writes to stdout,
    so a silent run — a defer, or an early return on a payload the guard skips
    before it analyzes anything — leaves nothing to count. Every total is
    therefore a floor, and a guard whose unreached path hides traffic reads the
    same as a guard that saw that traffic and stayed quiet (issue 96).
    """
    note = ["Emitted decisions only — a silent hook run (a defer, or an early "
            "return on a payload the guard skips before analyzing it) leaves "
            "no transcript record, so these totals are floors."]
    if plugin == 'all':
        note.append("Guards emit on different terms, so the plugins: counts "
                    "are not a like-for-like ranking.")
    return note


def categorize(reason):
    """Return {category: [tokens]} for the buckets present in a reason string.

    A reason matching none of the patterns — another guard's prompt under
    --plugin all, or a workspace-guard reason we don't recognize — buckets as
    'other' with no tokens, so the category table sums to the friction count
    instead of silently dropping the remainder.
    """
    out = {}
    for cat, pat in REASON_PATTERNS.items():
        m = pat.search(reason)
        if m:
            out[cat] = [t.strip() for t in m.group(1).split(',') if t.strip()]
    return out or {'other': []}


def build_report(decisions, raw):
    decs = collections.Counter()
    cats = collections.Counter()
    paths = collections.Counter()
    cmds = collections.Counter()
    plugins = collections.Counter()
    total = 0
    for d in decisions:
        total += 1
        decs[d['decision']] += 1
        plugins[d['plugin']] += 1
        if d['decision'] in ('ask', 'deny'):
            for cat, toks in categorize(d['reason']).items():
                cats[cat] += 1
                for t in toks:
                    paths[t if raw else normalize_path(t)] += 1
            if d['command']:
                cmds[' '.join(d['command'].split())[:100]] += 1
    return {
        'total': total, 'decisions': decs, 'categories': cats,
        'paths': paths, 'commands': cmds, 'plugins': plugins,
    }


def print_text(r, top, stale=None, plugin=THIS_GUARD, notes=()):
    total = r['total']
    if not total:
        print("No guard decisions found for the given filters.")
        for line in notes:
            print(line)
        return
    asks = r['decisions'].get('ask', 0) + r['decisions'].get('deny', 0)
    print(f"Guard decisions analyzed: {total}")
    by_plugin = ", ".join(f"{k} {v}" for k, v in r['plugins'].most_common())
    print(f"  plugins: {by_plugin}")
    parts = [f"{k} {v}" for k, v in r['decisions'].most_common()]
    print(f"  outcomes: {', '.join(parts)}")
    pct = (100 * asks / total) if total else 0
    print(f"  friction (ask+deny): {asks} ({pct:.0f}% of decisions)")
    for line in textwrap.wrap(' '.join(coverage_note(plugin)), 78,
                              initial_indent='  coverage: ',
                              subsequent_indent='    '):
        print(line)
    print()

    if stale:
        print(f"⚠  {stale['plugin']} {stale['installed']} installed, "
              f"{stale['available']} available in the marketplace clone.")
        print("   A newer release may already reduce the friction below. "
              "Update with:")
        print(f"     /plugin marketplace update {stale['marketplace']} "
              "&& /reload-plugins")
        print("   or enable autoUpdate (see README \"Updating\").\n")

    if r['categories']:
        print("By category (prompts):")
        for cat, n in r['categories'].most_common():
            print(f"  {n:5}  {cat}")
        if plugin == 'all' and 'other' in r['categories']:
            print(f'  ("other" = prompts from guards besides {THIS_GUARD}, plus '
                  f"any {THIS_GUARD} reason this report doesn't recognize)")
        print()
    if r['paths']:
        scope = f'{THIS_GUARD} only, ' if plugin == 'all' else ''
        print(f"Top offending paths ({scope}top {top}):")
        for p, n in r['paths'].most_common(top):
            print(f"  {n:5}  {p}")
        print()
    if r['commands']:
        print(f"Top triggering commands (top {top}):")
        for c, n in r['commands'].most_common(top):
            print(f"  {n:5}  {c}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--transcripts',
                    default=os.path.expanduser('~/.claude/projects'),
                    help='transcript root (default: ~/.claude/projects)')
    ap.add_argument('--plugin', default=THIS_GUARD,
                    help=f"guard to report on, or 'all' (default: {THIS_GUARD}); "
                         "the label is the hook script's filename minus a "
                         "'bash-' prefix, which can differ from the plugin name")
    ap.add_argument('--since', default='7d',
                    help="time window: Nd/Nh/Nm or YYYY-MM-DD (default: 7d; "
                         "use 'all' for no limit)")
    ap.add_argument('--repo', default='',
                    help='only decisions whose cwd contains this substring')
    ap.add_argument('--plugins-dir', default=DEFAULT_PLUGINS_DIR,
                    help='Claude Code plugins dir (default: ~/.claude/plugins); '
                         'used to flag a stale installed version')
    ap.add_argument('--top', type=int, default=15, help='rows per ranking')
    ap.add_argument('--raw', action='store_true',
                    help='do not collapse volatile path segments')
    ap.add_argument('--json', action='store_true', help='emit JSON')
    args = ap.parse_args()

    cutoff = None if args.since == 'all' else parse_since(args.since)
    paths = glob.glob(os.path.join(args.transcripts, '**', '*.jsonl'),
                      recursive=True)
    if not paths:
        sys.exit(f"No transcripts under {args.transcripts}")

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
            'top_paths': report['paths'].most_common(args.top),
            'paths_scope': THIS_GUARD,
            'top_commands': report['commands'].most_common(args.top),
            'stale': stale,
            'coverage': coverage_note(args.plugin),
            'empty_because': notes or None,
        }, indent=2))
    else:
        print_text(report, args.top, stale, args.plugin, notes)

    # A --plugin or --repo value nothing in the transcripts can match is a usage
    # error, not a guard with zero friction; exit non-zero so it can't be
    # mistaken for an answer. A satisfiable filter over an empty window is a
    # real answer, as is a setup with no recorded decisions yet — both exit 0.
    if survey['labels'] and not survey['repo_hits']:
        sys.exit(2)


if __name__ == '__main__':
    main()
