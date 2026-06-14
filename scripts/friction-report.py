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


def iter_decisions(paths, plugin, cutoff, repo):
    """Yield decision dicts from the given transcript files.

    Builds a per-file toolUseID -> Bash command map (ids are session-scoped)
    so each decision can name the command that triggered it.
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
            if plugin != 'all' and name != plugin:
                continue
            cwd = rec.get('cwd') or ''
            if repo and repo not in cwd:
                continue
            ts = parse_ts(rec)
            if cutoff and ts and ts < cutoff:
                continue

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


def categorize(reason):
    """Return {category: [tokens]} for the buckets present in a reason string."""
    out = {}
    for cat, pat in REASON_PATTERNS.items():
        m = pat.search(reason)
        if m:
            out[cat] = [t.strip() for t in m.group(1).split(',') if t.strip()]
    return out


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


def print_text(r, top):
    total = r['total']
    if not total:
        print("No guard decisions found for the given filters.")
        return
    asks = r['decisions'].get('ask', 0) + r['decisions'].get('deny', 0)
    print(f"Guard decisions analyzed: {total}")
    by_plugin = ", ".join(f"{k} {v}" for k, v in r['plugins'].most_common())
    print(f"  plugins: {by_plugin}")
    parts = [f"{k} {v}" for k, v in r['decisions'].most_common()]
    print(f"  outcomes: {', '.join(parts)}")
    pct = (100 * asks / total) if total else 0
    print(f"  friction (ask+deny): {asks} ({pct:.0f}% of decisions)\n")

    if r['categories']:
        print("By category (prompts):")
        for cat, n in r['categories'].most_common():
            print(f"  {n:5}  {cat}")
        print()
    if r['paths']:
        print(f"Top offending paths (top {top}):")
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
    ap.add_argument('--plugin', default='workspace-guard',
                    help="guard to report on, or 'all' (default: workspace-guard)")
    ap.add_argument('--since', default='7d',
                    help="time window: Nd/Nh/Nm or YYYY-MM-DD (default: 7d; "
                         "use 'all' for no limit)")
    ap.add_argument('--repo', default='',
                    help='only decisions whose cwd contains this substring')
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

    decisions = list(iter_decisions(paths, args.plugin, cutoff, args.repo))
    report = build_report(decisions, args.raw)

    if args.json:
        print(json.dumps({
            'total': report['total'],
            'decisions': dict(report['decisions']),
            'plugins': dict(report['plugins']),
            'categories': dict(report['categories']),
            'top_paths': report['paths'].most_common(args.top),
            'top_commands': report['commands'].most_common(args.top),
        }, indent=2))
    else:
        print_text(report, args.top)


if __name__ == '__main__':
    main()
