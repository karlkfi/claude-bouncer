#!/usr/bin/env python3
"""Report where foreground-guard friction accumulates, from session transcripts.

Read-only analyzer. The hook itself writes nothing to disk (see PRIVACY.md); it
only emits a decision on stdout. Claude Code records that stdout — plus the
triggering command, cwd, and timestamp — in the session transcripts under
``~/.claude/projects/**/*.jsonl``. This tool re-reads those records and ranks
foreground-guard's decisions so you can see, in one command, which prompts
dominate and — most usefully — which *category* of main-thread waste keeps
prompting, because each category maps to one concrete fix (background the wait,
take a snapshot, set an adequate timeout). The category taxonomy is stable and
documented below so the ``reduce-foreground-guard-prompts`` skill can consume it.

Nothing here changes the hook or adds telemetry: it parses data Claude Code
already persisted locally.

Usage:
    python3 scripts/friction-report.py                 # last 7 days
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

# foreground-guard builds every prompt reason from one of five finding helpers
# in bash-foreground-guard.py, each carrying a stable signature substring. The
# taxonomy — four Class A categories plus one Class B — is the report's public
# contract (the reduce-foreground-guard-prompts skill keys off these names):
#
#   watch         Class A watch/follow mode (gh run watch, tail -f, watch ...)
#   loop-sleep    Class A while/until/for loop that polls with sleep
#   sandwich      Class A chained repeat-with-sleep (cmd; sleep N; cmd)
#   bare-sleep    Class A long bare `sleep N` at/above the floor
#   slow-timeout  Class B slow command about to run with an inadequate timeout
#
# The signatures are mutually exclusive; a reason segment matches exactly one.
CATEGORY_PATTERNS = {
    'watch':        re.compile(r'runs in watch/follow mode'),
    'loop-sleep':   re.compile(r'loop with `sleep` polls'),
    'sandwich':     re.compile(r'repeat-with-sleep chain'),
    'bare-sleep':   re.compile(r'parks the main thread for'),
    'slow-timeout': re.compile(r'matches the slow-command pattern'),
}

# One-line fix per category: what stops the prompt. Class A fixes are agent
# behavior (background / snapshot); slow-timeout is a per-call timeout.
CATEGORY_HINT = {
    'watch':        'take one non-blocking snapshot, or re-run with run_in_background: true',
    'loop-sleep':   'take one status check now; background the wait or check again next turn',
    'sandwich':     'take one status check now; background the wait or check again next turn',
    'bare-sleep':   'skip or background the wait; do the follow-up check now',
    'slow-timeout': 'set an adequate `timeout:` on the Bash call, or run it in the background',
}

# Categories whose reason leads with the offending command/pattern in backticks:
# watch names the blocking command, slow-timeout names the registered pattern.
# The loop/sandwich/bare-sleep reasons lead with a generic template (`sleep`,
# `while`/…), so their backtick is not a real target — use the joined command.
NAMED_TARGET_CATS = frozenset({'watch', 'slow-timeout'})

# A deny downgraded by FOREGROUND_GUARD_OVERRIDE keeps its underlying category
# but is emitted as `ask` prefixed with this signature. Counted separately so an
# over-used override is visible. The guard name is load-bearing: the sibling
# guards phrase their own override prefix identically apart from it, so an
# unanchored pattern reports their overrides as this guard's under --plugin all.
OVERRIDE_SIG = re.compile(r'foreground-guard override acknowledged')

# The hook joins up to three finding reasons with ' | '.
_JOIN = ' | '
# foreground-guard wraps the offending command/pattern (and its fixes) in
# backticks; the FIRST backtick span in a segment is the target.
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
    """Plugin label from a hook command, e.g. '.../bash-foreground-guard.py'
    -> 'foreground-guard'. Returns None if the command names no *.py guard."""
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


def named_target(segment):
    """The command/pattern the guard names in backticks — meaningful only for
    watch (the blocking command) and slow-timeout (the registered pattern). The
    other categories lead with a generic template, so return None."""
    if category_of(segment) not in NAMED_TARGET_CATS:
        return None
    m = _BACKTICKED.search(segment)
    return m.group(1) if m else None


def tool_of(segment):
    """First word of the named target, e.g. `gh run watch 456` -> 'gh',
    `make test-race\\b` -> 'make'. None when the segment names no target."""
    tgt = named_target(segment)
    if not tgt:
        return None
    words = tgt.split()
    return words[0] if words else None


def build_report(decisions):
    decs = collections.Counter()
    plugins = collections.Counter()
    cats = collections.Counter()
    tools = collections.Counter()
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
        for seg in split_reasons(reason):
            cat = category_of(seg)
            cats[cat] += 1
            tgt = named_target(seg)
            if tgt:
                targets[tgt] += 1
            tool = tool_of(seg)
            if tool:
                tools[tool] += 1
        if d['command']:
            cmds[' '.join(d['command'].split())[:100]] += 1
    return {
        'total': total, 'decisions': decs, 'plugins': plugins,
        'categories': cats, 'tools': tools, 'overrides': overrides,
        'targets': targets, 'commands': cmds,
    }


def print_text(r, top, plugin='foreground-guard'):
    total = r['total']
    label = 'guard' if plugin == 'all' else plugin
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
    # Only foreground-guard's own overrides are counted, so the line has no
    # place under a header covering every guard — omit it rather than show one
    # guard's statistic as if it summarized the set.
    if r['overrides'] and plugin != 'all':
        print(f"  FOREGROUND_GUARD_OVERRIDE downgrades: {r['overrides']}")
    print()

    if r['categories']:
        print("By category (prompts) — each maps to one fix:")
        for cat, n in r['categories'].most_common():
            hint = CATEGORY_HINT.get(cat, '')
            print(f"  {n:5}  {cat:13}  {hint}")
        print()
    if r['tools']:
        print(f"By flagged tool (watch/slow, top {top}):")
        for tool, n in r['tools'].most_common(top):
            print(f"  {n:5}  {tool}")
        print()
    if r['targets']:
        print(f"Top flagged targets — watch commands & slow patterns (top {top}):")
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
    ap.add_argument('--plugin', default='foreground-guard',
                    help="guard to report on, or 'all' (default: foreground-guard)")
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
        out = {
            'total': report['total'],
            'decisions': dict(report['decisions']),
            'plugins': dict(report['plugins']),
            'categories': dict(report['categories']),
            'tools': dict(report['tools']),
            'top_targets': report['targets'].most_common(args.top),
            'top_commands': report['commands'].most_common(args.top),
        }
        # Same reasoning as the text report: a foreground-guard-only count is
        # absent, not zero, in an all-guards document.
        if args.plugin != 'all':
            out['overrides'] = report['overrides']
        print(json.dumps(out, indent=2))
    else:
        print_text(report, args.top, args.plugin)


if __name__ == '__main__':
    main()
