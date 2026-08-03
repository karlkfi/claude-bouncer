#!/usr/bin/env python3
"""Report where prod-guard friction accumulates, from session transcripts.

Read-only analyzer. The hook itself writes nothing to disk (see PRIVACY.md);
it only emits a decision on stdout. Claude Code records that stdout — plus the
triggering command, cwd, and timestamp — in the session transcripts under
``~/.claude/projects/**/*.jsonl``. This tool re-reads those records and ranks
prod-guard's decisions so you can see, in one command, which prompts dominate
and — most usefully — which *unknown* targets prompt repeatedly, because those
are the pattern gaps a ``.claude/prod-guard.json`` ``nonprod`` entry closes.

Nothing here changes the hook or adds telemetry: it parses data Claude Code
already persisted locally.

Usage:
    python3 scripts/friction-report.py                 # last 7 days, prod-guard
    python3 scripts/friction-report.py --since 24h
    python3 scripts/friction-report.py --since 2026-06-01 --repo gateway
    python3 scripts/friction-report.py --plugin all --top 20
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

# prod-guard builds every decision reason from one of five helpers in
# bash-prod-guard.py, each carrying a stable signature substring. Three deny
# (deny-prod, deny-ambient, deny-switch) and two ask (ask-unknown, ask-switch).
# Matching is first-match in dict order: deny-switch must precede ask-switch,
# whose broader signature also appears in deny-switch reasons (and in the
# pre-2.3 ask-form switch decisions still present in old transcripts).
CATEGORY_PATTERNS = {
    'deny-prod':    re.compile(r'matches a production pattern'),
    'ask-unknown':  re.compile(r'matches neither a production'),
    'deny-ambient': re.compile(r'shared mutable state that a parallel session'),
    'deny-switch':  re.compile(r'Switching shared state is blocked'),
    'ask-switch':   re.compile(r'is shared by every session'),
}

# One-line hint per category: what the user does to stop the prompt.
CATEGORY_HINT = {
    'deny-prod':    'intended block (prefix PROD_GUARD_OVERRIDE=<reason> only if truly intentional)',
    'ask-unknown':  'add a vetted nonprod pattern for the target to .claude/prod-guard.json',
    'deny-ambient': 'pin the target explicitly (--context/--project/--profile/…) — the deny self-heals once pinned',
    'deny-switch':  'pin per-command instead of switching shared state — the deny names the flag',
    'ask-switch':   'pin per-command instead of switching shared state',
}

# A deny downgraded by PROD_GUARD_OVERRIDE or PROD_GUARD_SESSION_OVERRIDE keeps
# its deny-prod signature but is emitted as `ask` with this prefix. Counted
# separately so an over-used override is visible. The 'prod-guard' prefix is
# load-bearing: sibling guards emit 'foreground-guard override acknowledged' and
# the like, which under --plugin all would otherwise land in this counter.
OVERRIDE_SIG = re.compile(r'prod-guard (?:session )?override acknowledged')

# The hook joins up to three finding reasons with ' | '.
_JOIN = ' | '
# Every reason wraps its target in single quotes; the action leads in backticks.
_QUOTED = re.compile(r"'([^']*)'")
_BACKTICKED = re.compile(r'`([^`]+)`')


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
    """Plugin label from a hook command, e.g. '.../bash-prod-guard.py'
    -> 'prod-guard'. Returns None if the command names no *.py guard."""
    m = re.search(r'([A-Za-z0-9_-]+)\.py', command or '')
    if not m:
        return None
    return re.sub(r'^bash-', '', m.group(1))


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


def split_reasons(reason):
    """The '|'-joined reason split into per-finding segments."""
    return [p.strip() for p in reason.split(_JOIN) if p.strip()]


def category_of(segment):
    """The friction category of one reason segment, or 'other'."""
    for cat, rx in CATEGORY_PATTERNS.items():
        if rx.search(segment):
            return cat
    return 'other'


def targets_of(segment):
    """Single-quoted targets in a segment, minus '<…>' placeholders (an
    unresolved ambient value has nothing to rank)."""
    return [t for t in _QUOTED.findall(segment) if t and not t.startswith('<')]


def tool_of(reason):
    """First word of the leading backtick action, e.g. `kubectl delete ns`
    -> 'kubectl'. None if the reason has no action."""
    m = _BACKTICKED.search(reason)
    if not m:
        return None
    words = m.group(1).split()
    return words[0] if words else None


def build_report(decisions):
    decs = collections.Counter()
    plugins = collections.Counter()
    cats = collections.Counter()
    tools = collections.Counter()
    unknown_targets = collections.Counter()
    targets = collections.Counter()
    cmds = collections.Counter()
    overrides = 0
    total = 0
    for d in decisions:
        total += 1
        decs[d['decision']] += 1
        plugins[d['plugin']] += 1
        if d['decision'] not in ('ask', 'deny'):
            continue
        reason = d['reason']
        if OVERRIDE_SIG.search(reason):
            overrides += 1
        tool = tool_of(reason)
        if tool:
            tools[tool] += 1
        for seg in split_reasons(reason):
            cat = category_of(seg)
            cats[cat] += 1
            for t in targets_of(seg):
                targets[t] += 1
                if cat == 'ask-unknown':
                    unknown_targets[t] += 1
        if d['command']:
            cmds[' '.join(d['command'].split())[:100]] += 1
    return {
        'total': total, 'decisions': decs, 'plugins': plugins,
        'categories': cats, 'tools': tools, 'overrides': overrides,
        'unknown_targets': unknown_targets, 'targets': targets, 'commands': cmds,
    }


def print_text(r, top, plugin='prod-guard'):
    total = r['total']
    # --plugin widens the scope past prod-guard, so the header names what was
    # actually counted rather than the guard this script ships with.
    label = 'all-guard' if plugin == 'all' else plugin
    if not total:
        print(f"No {label} decisions found for the given filters.")
        return
    asks = r['decisions'].get('ask', 0) + r['decisions'].get('deny', 0)
    print(f"{label} decisions analyzed: {total}")
    by_plugin = ", ".join(f"{k} {v}" for k, v in r['plugins'].most_common())
    print(f"  plugins: {by_plugin}")
    parts = [f"{k} {v}" for k, v in r['decisions'].most_common()]
    print(f"  outcomes: {', '.join(parts)}")
    pct = (100 * asks / total) if total else 0
    print(f"  friction (ask+deny): {asks} ({pct:.0f}% of decisions)")
    # Only prod-guard's own downgrades are counted, so the line has no place in
    # an all-guards header — --plugin prod-guard (the default) reports it.
    if r['overrides'] and plugin != 'all':
        print(f"  PROD_GUARD_OVERRIDE downgrades: {r['overrides']}")
    print()

    if r['categories']:
        print("By category (prompts):")
        for cat, n in r['categories'].most_common():
            hint = CATEGORY_HINT.get(cat, '')
            print(f"  {n:5}  {cat:12}  {hint}")
        print()
    if r['tools']:
        print("By tool (prompts):")
        for tool, n in r['tools'].most_common(top):
            print(f"  {n:5}  {tool}")
        print()
    if r['unknown_targets']:
        print(f"Unclassified targets — pattern-gap candidates (top {top}):")
        print("  Vet each; if non-prod, add a nonprod pattern to "
              ".claude/prod-guard.json to stop the prompt.")
        for t, n in r['unknown_targets'].most_common(top):
            print(f"  {n:5}  {t}")
        print()
    if r['targets']:
        print(f"Top targets, all prompts (top {top}):")
        for t, n in r['targets'].most_common(top):
            print(f"  {n:5}  {t}")
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
    ap.add_argument('--plugin', default='prod-guard',
                    help="guard to report on, or 'all' (default: prod-guard)")
    ap.add_argument('--since', default='7d',
                    help="time window: Nd/Nh/Nm or YYYY-MM-DD (default: 7d; "
                         "use 'all' for no limit)")
    ap.add_argument('--repo', default='',
                    help='only decisions whose cwd contains this substring')
    ap.add_argument('--top', type=int, default=15, help='rows per ranking')
    ap.add_argument('--json', action='store_true', help='emit JSON')
    args = ap.parse_args()

    cutoff = None if args.since == 'all' else parse_since(args.since)
    paths = glob.glob(os.path.join(args.transcripts, '**', '*.jsonl'),
                      recursive=True)
    if not paths:
        sys.exit(f"No transcripts under {args.transcripts}")

    decisions = list(iter_decisions(paths, args.plugin, cutoff, args.repo))
    report = build_report(decisions)

    if args.json:
        print(json.dumps({
            'total': report['total'],
            'decisions': dict(report['decisions']),
            'plugins': dict(report['plugins']),
            'categories': dict(report['categories']),
            'tools': dict(report['tools']),
            'overrides': report['overrides'],
            'top_unknown_targets': report['unknown_targets'].most_common(args.top),
            'top_targets': report['targets'].most_common(args.top),
            'top_commands': report['commands'].most_common(args.top),
        }, indent=2))
    else:
        print_text(report, args.top, args.plugin)


if __name__ == '__main__':
    main()
