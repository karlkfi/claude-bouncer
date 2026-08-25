#!/usr/bin/env python3
"""Report where prod-guard friction accumulates, from session transcripts.

Read-only analyzer. The hook itself writes nothing to disk (see PRIVACY.md);
it only emits a decision on stdout. Claude Code records that stdout — plus the
triggering command, cwd, and timestamp — in the session transcripts under
``~/.claude/projects/**/*.jsonl``. This tool re-reads those records and ranks
prod-guard's decisions so you can see, in one command, which prompts dominate
and — most usefully — which *unknown* targets prompt repeatedly, because those
are the pattern gaps a ``.claude/prod-guard.json`` ``nonprod`` entry closes.

It also warns when the installed plugin lags the local marketplace clone, since
third-party marketplaces never refresh on their own and some of the friction
being reported may already be fixed upstream.

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
# The colon is optional because these two reasons gained it only once the deny
# they become under bypassPermissions had to be attributable; the bare form is
# still present in older transcripts and still counts.
OVERRIDE_SIG = re.compile(r'prod-guard:? (?:session )?override acknowledged')

# The hook joins up to three finding reasons with ' | '.
_JOIN = ' | '
# Every reason wraps its target in single quotes; the action leads in backticks.
_QUOTED = re.compile(r"'([^']*)'")
_BACKTICKED = re.compile(r'`([^`]+)`')


# --- Stale-install detection ------------------------------------------------
# Claude Code auto-updates official Anthropic marketplaces only; a third-party
# git marketplace pins its installed version until the user acts, so friction a
# newer release already fixes can linger silently — and for a guard plugin that
# means missing false-negative fixes. Compare the installed version
# (~/.claude/plugins/installed_plugins.json) against the local marketplace
# clone's plugin.json and flag a lag where the user already looks. Every read is
# of state Claude Code already persisted locally — no network, no telemetry —
# and any missing or unparseable file degrades to None so the report itself
# never breaks.
DEFAULT_PLUGINS_DIR = os.path.expanduser('~/.claude/plugins')


def version_tuple(v):
    """Comparable tuple of the leading numeric components of a version string.

    '2.4.0' -> (2, 4, 0); stops at the first non-numeric component so a
    pre-release tag ('2.4.0-rc1' -> (2, 4, 0)) is treated as its base version.
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
    version); falls back to the per-plugin version in the marketplace manifest
    so a multi-plugin marketplace still resolves.
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


def print_staleness(stale):
    if not stale:
        return
    print(f"⚠  {stale['plugin']} {stale['installed']} installed, "
          f"{stale['available']} available in the local marketplace clone.")
    print("   A newer release may already fix some of the friction reported "
          "here. Update with:")
    print(f"     claude plugin marketplace update {stale['marketplace']}")
    print(f"     claude plugin update {stale['plugin']}@{stale['marketplace']}")
    print("   or enable autoUpdate — see \"Keeping it updated\" in the "
          "prod-guard README.\n")


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


def is_pattern_candidate(target):
    """Whether a nonprod pattern could answer this target. A '$'-prefixed one
    is a variable name the hook could not resolve, so a pattern matching it
    would classify every command holding a target in that variable —
    production ones included."""
    return not target.startswith('$')


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
                # 'Top targets' ranks repeated friction, so an unresolved
                # target belongs there; the pattern-gap list is a config edit
                # to act on, and there is no pattern to write for one.
                if cat == 'ask-unknown' and is_pattern_candidate(t):
                    unknown_targets[t] += 1
        if d['command']:
            cmds[' '.join(d['command'].split())[:100]] += 1
    return {
        'total': total, 'decisions': decs, 'plugins': plugins,
        'categories': cats, 'tools': tools, 'overrides': overrides,
        'unknown_targets': unknown_targets, 'targets': targets, 'commands': cmds,
    }


def print_text(r, top, plugin='prod-guard', stale=None):
    total = r['total']
    # --plugin widens the scope past prod-guard, so the header names what was
    # actually counted rather than the guard this script ships with.
    label = 'all-guard' if plugin == 'all' else plugin
    if not total:
        print(f"No {label} decisions found for the given filters.")
        # A stale install is worth saying even with nothing to rank — an old
        # classifier is the one thing a quiet report can still be wrong about.
        if stale:
            print()
            print_staleness(stale)
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

    print_staleness(stale)

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
    ap.add_argument('--plugins-dir', default=DEFAULT_PLUGINS_DIR,
                    help='Claude Code plugins dir (default: ~/.claude/plugins); '
                         'used to flag a stale installed version')
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
    stale = check_staleness(args.plugins_dir, args.plugin)

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
            'stale': stale,
        }, indent=2))
    else:
        print_text(report, args.top, args.plugin, stale)


if __name__ == '__main__':
    main()
