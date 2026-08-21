#!/usr/bin/env python3
"""Report where foreground-guard friction accumulates, from session transcripts.

Read-only analyzer. The hook itself writes nothing to disk (see PRIVACY.md); it
only emits a decision on stdout. Claude Code records that stdout — plus the
triggering command, cwd, and timestamp — in the session transcripts under
``~/.claude/projects/**/*.jsonl``. This tool re-reads those records and ranks
foreground-guard's decisions so you can see, in one command, which prompts
dominate and — most usefully — which *category* of main-thread waste keeps
prompting, because each category maps to one concrete fix (take a snapshot,
defer the recheck, set an adequate timeout). The category taxonomy is stable and
documented below so the ``reduce-foreground-guard-prompts`` skill can consume it.

Nothing here changes the hook or adds telemetry: it parses data Claude Code
already persisted locally.

Usage:
    python3 scripts/friction-report.py                 # last 7 days
    python3 scripts/friction-report.py --since 24h
    python3 scripts/friction-report.py --since 2026-06-01 --repo gateway
    python3 scripts/friction-report.py --plugin all --top 20
    python3 scripts/friction-report.py --json           # machine-readable

Each hook decision *that lets the call through* is recorded as an ``attachment``
line of type ``hook_success`` carrying ``hookName`` (``PreToolUse:Bash``), the
hook ``command`` (which names the guard script), and ``stdout`` (the decision
JSON). The triggering Bash command is joined back via ``toolUseID``.

An attachment of any other ``type`` holds no verdict — the hook crashed or was
cancelled, so the call ran unguarded. Those are counted as ``error``, not folded
into ``defer``; see ``HOOK_OK``.

A ``deny`` is recorded nowhere in that stream — see ``DENY_TEXT`` below — so it
is recovered from the error tool result the blocked call handed back instead.

The friction *rate* divides by Bash calls, not by decision records — see
``SCOPE_KEYS`` for why the decision stream cannot be a denominator.
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
    # "parks a background task" is the backgrounded wording of the same finding.
    'bare-sleep':   re.compile(r'`sleep` parks (?:the main thread|a background '
                               r'task) for'),
    'slow-timeout': re.compile(r'matches the slow-command (?:pattern|target)'),
}

# One-line fix per category: what stops the prompt. Class A fixes are agent
# behavior (snapshot / defer the recheck — backgrounding a poll does not quiet
# it); slow-timeout is a per-call timeout.
CATEGORY_HINT = {
    'watch':        'take one non-blocking snapshot instead of streaming',
    'loop-sleep':   'take one status check now; check again next turn',
    'sandwich':     'take one status check now; check again next turn',
    'bare-sleep':   'skip the wait; do the follow-up check now',
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

# A denying hook leaves no attachment of its own. Claude Code persists hook
# stdout only for a call it goes on to run: measured over 601 local transcripts,
# 48k allow/ask attachments and not one deny, while the denials sat in plain
# sight as tool results (issue #25). Counting the attachment stream alone
# therefore reports zero friction for a guard running in a mode where asks
# become denies — the mode where the friction is worst.
#
# The trace a deny does leave is the error handed back to the agent, whose text
# is verbatim the reason the hook printed, joined to the command by
# `tool_use_id` like any other tool result. Every reason this guard denies with
# opens `foreground-guard: `, so that opener is the key. A guard wording its
# reason differently is not recoverable this way and still under-counts its
# denies under `--plugin all`; the report says so rather than showing a zero.
# The convention that makes the opener a usable key across repos is written up
# in docs/development/cross-guard-deny-convention.md.
#
# Siblings whose plugin name does not end in `-guard`. The `-guard` suffix is
# shape enough on its own — it cannot open ordinary error prose — so a new guard
# needs no edit here, but a name outside that shape has to be listed or its
# denies stay invisible. Widening to a bare `\w+: ` opener instead is what this
# tuple exists to avoid: `error: `, `fatal: `, `warning: ` and `Traceback: ` all
# open ordinary tool failures, and reading those as denies would credit each to
# a plugin that does not exist.
NON_GUARD_PLUGINS = ('pr-sentinel',)

DENY_TEXT = re.compile(
    r'^(?:Error:\s*)?('
    + '|'.join([r'[a-z0-9-]+-guard']
               + [re.escape(p) for p in NON_GUARD_PLUGINS])
    + r'):\s')

# An attachment records what became of the hook, not only what it said, and only
# this type carries a verdict. The others mean the hook never spoke, so reading
# their empty stdout as a silent defer claims the guard looked and let the call
# through when it was in fact never consulted (issue #29) — and the cross-guard
# view is where you would go to notice a sibling has stopped running at all.
#
# Measured over 782 local transcripts, 66,223 PreToolUse:Bash attachments:
# 60,748 `hook_success` with stdout, 5,472 `hook_non_blocking_error` (all exit
# 126, a sibling guard's hook script left non-executable for a day), 2
# `hook_cancelled` (timed out; these carry no exitCode at all), and one
# `hook_success` with empty stdout — the only genuinely silent record in the
# tree. Every attachment carries `type`, so the check never has to guess.
HOOK_OK = 'hook_success'

# The denominator, tallied alongside the decisions (issue #27).
#
# Decisions cannot be one. A silent defer leaves no record — Claude Code writes
# an attachment only for a hook that produced stdout, and this guard prints
# nothing on a defer and never prints `allow` by design — so its decision stream
# holds exactly its own prompts and a friction share of it reads 100% however the
# config is tuned. The siblings that do emit `allow` land their defers in the
# stream, so the same word, "decisions", counts two different populations.
#
# Bash calls are recorded whatever a hook decided, so they mean the same thing
# for every guard. Scope is every in-window call, not only those in sessions this
# guard spoke in: restricting to prompting sessions drops the quiet sessions a
# working config produces, so the rate climbs as friction falls. Measured over
# 776 local transcripts, that scoping read 1.6% over all time and 3.1% over the
# last 7 days — a week holding 3 prompts against the 257 behind the all-time
# figure.
#
# The wider scope assumes what the transcripts cannot show: that the guard ran
# for the whole window. Calls predating its install sit in the denominator, so a
# fresh install reads low until the window catches up. The report says so.
SCOPE_KEYS = ('bash_calls', 'sessions', 'sessions_prompted')

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
    -> 'foreground-guard'. Returns None if the command names no *.py guard.

    The label has to be the same word DENY_TEXT reads off a deny, because the
    two streams are counted together. A plugin whose name does not end in
    `-guard` may still call its hook script `<name>-guard.py` — pr-sentinel's is
    `pr-sentinel-guard.py` — and left alone that splits one plugin across two
    labels: its asks under `<name>-guard` and its denies under `<name>`, so
    `--plugin all` lists it twice and neither `--plugin <name>` value gets both
    halves. Fold the script name back onto the plugin's own."""
    m = re.search(r'([A-Za-z0-9_-]+)\.py', command or '')
    if not m:
        return None
    name = re.sub(r'^bash-', '', m.group(1))
    trimmed = re.sub(r'-guard$', '', name)
    return trimmed if trimmed in NON_GUARD_PLUGINS else name


def deny_from_result(block):
    """(guard, reason) if this tool_result block is a hook deny, else None.

    A blocked call comes back as an error whose text is the guard's reason;
    anything else (a failing command, a rejected prompt, another tool's error)
    does not open with a `<name>-guard: ` reason and is left alone."""
    if not block.get('is_error'):
        return None
    text = block.get('content')
    if isinstance(text, list):
        text = ''.join(p.get('text', '') for p in text if isinstance(p, dict))
    if not isinstance(text, str):
        return None
    text = text.strip()
    m = DENY_TEXT.match(text)
    return (m.group(1), text) if m else None


def clip(line, cap=120):
    """One whitespace-collapsed line, clipped from the middle. A hook failure
    puts a long plugin path in the middle and the actual failure at the end
    ('... Permission denied'), which end-clipping is exactly wrong for."""
    line = ' '.join(line.split())
    if len(line) <= cap:
        return line
    head = (cap - 1) * 2 // 3
    return line[:head] + '…' + line[head - cap + 1:]


def error_note(att):
    """One line naming what went wrong with a hook that produced no verdict.

    Built from the record's own fields, which differ by failure: a crash carries
    `exitCode` and `stderr`, a cancellation carries `timedOut`/`timeoutMs` and
    neither of those, so nothing is assumed present."""
    parts = [att.get('type') or 'unknown']
    code = att.get('exitCode')
    if code is not None:
        parts.append(f'exit {code}')
    if att.get('timedOut'):
        ms = att.get('timeoutMs')
        parts.append(f'timed out after {ms}ms' if ms else 'timed out')
    lines = (att.get('stderr') or '').strip().splitlines()
    if lines:
        parts.append(clip(lines[0]))
    return ': '.join(parts)


def scope(rec, cutoff, repo):
    """(cwd, ts) for a record, or None if it falls outside the filters."""
    cwd = rec.get('cwd') or ''
    if repo and repo not in cwd:
        return None
    ts = parse_ts(rec)
    if cutoff and ts and ts < cutoff:
        return None
    return cwd, ts


def new_scope():
    """A zeroed denominator tally (see SCOPE_KEYS)."""
    return dict.fromkeys(SCOPE_KEYS, 0)


def iter_decisions(paths, plugin, cutoff, repo, tally=None):
    """Yield decision dicts from the given transcript files.

    Builds a per-file toolUseID -> Bash command map (ids are session-scoped)
    so each decision can name the command that triggered it. Reads two sources:
    the hook attachment stream (allow/ask/defer) and the error tool results that
    carry the denies the attachment stream omits.

    A `tally` dict from new_scope() collects the denominator as it goes — the
    in-scope Bash calls and the sessions holding them. It is complete only once
    the generator is exhausted.
    """
    for path in paths:
        cmd_by_id = {}
        records = []
        calls = 0
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
                    # Index Bash tool_use commands for the join. Indexing spans
                    # the whole file (a decision may sit outside the window
                    # while its command record does not); the tally does not.
                    msg = rec.get('message') or {}
                    n_bash = 0
                    for b in (msg.get('content') or []):
                        if (isinstance(b, dict) and b.get('type') == 'tool_use'
                                and b.get('name') == 'Bash' and b.get('id')):
                            cmd_by_id[b['id']] = (b.get('input') or {}).get('command', '')
                            n_bash += 1
                    if n_bash and tally is not None and scope(rec, cutoff, repo):
                        calls += n_bash
                    records.append(rec)
        except OSError:
            continue

        prompted = False  # this session saw at least one ask/deny in scope
        decided = set()   # (guard, toolUseID) seen in the attachment stream
        for rec in records:
            att = rec.get('attachment')
            if not isinstance(att, dict) or att.get('hookName') != 'PreToolUse:Bash':
                continue
            name = guard_name(att.get('command'))
            if name is None:
                continue
            decided.add((name, att.get('toolUseID')))
            if plugin != 'all' and name != plugin:
                continue
            window = scope(rec, cutoff, repo)
            if window is None:
                continue
            cwd, ts = window

            if att.get('type') != HOOK_OK:
                # No verdict to read: the hook never ran to completion, so the
                # call went through unguarded rather than deferred (see HOOK_OK).
                decision, reason = 'error', error_note(att)
            else:
                stdout = att.get('stdout') or ''
                decision, reason = 'defer', ''  # empty stdout => hook was silent
                if stdout.strip():
                    try:
                        out = json.loads(stdout)
                        hso = out.get('hookSpecificOutput') or {}
                        decision = hso.get('permissionDecision', 'defer')
                        reason = hso.get('permissionDecisionReason', '')
                    except ValueError:
                        pass
            prompted = prompted or decision in ('ask', 'deny')
            yield {
                'plugin': name, 'decision': decision, 'reason': reason,
                'cwd': cwd, 'ts': ts,
                'command': cmd_by_id.get(att.get('toolUseID'), ''),
            }

        for rec in records:
            for block in ((rec.get('message') or {}).get('content') or []):
                if not isinstance(block, dict) or block.get('type') != 'tool_result':
                    continue
                found = deny_from_result(block)
                if found is None:
                    continue
                name, reason = found
                if plugin != 'all' and name != plugin:
                    continue
                tuid = block.get('tool_use_id')
                # No Bash tool_use behind it means a sibling guard blocked some
                # other tool (Edit, Write) — out of scope for a Bash report.
                if tuid not in cmd_by_id:
                    continue
                if (name, tuid) in decided:
                    continue
                window = scope(rec, cutoff, repo)
                if window is None:
                    continue
                cwd, ts = window
                prompted = True
                yield {
                    'plugin': name, 'decision': 'deny', 'reason': reason,
                    'cwd': cwd, 'ts': ts, 'command': cmd_by_id[tuid],
                }

        if tally is not None and (calls or prompted):
            tally['bash_calls'] += calls
            tally['sessions'] += 1
            tally['sessions_prompted'] += 1 if prompted else 0


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


def build_report(decisions, tally=None):
    decs = collections.Counter()
    plugins = collections.Counter()
    cats = collections.Counter()
    tools = collections.Counter()
    targets = collections.Counter()
    cmds = collections.Counter()
    errors = collections.Counter()
    overrides = 0
    total = 0
    for d in decisions:
        total += 1
        decs[d['decision']] += 1
        plugins[d['plugin']] += 1
        if d['decision'] == 'error':
            errors[f"{d['plugin']}: {d['reason']}"] += 1
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
        'targets': targets, 'commands': cmds, 'errors': errors,
        'scope': tally,
    }


def print_text(r, top, plugin='foreground-guard'):
    total = r['total']
    label = 'guard' if plugin == 'all' else plugin
    if not total:
        print(f"No {label} decisions found for the given filters.")
        return
    asks = r['decisions'].get('ask', 0) + r['decisions'].get('deny', 0)
    print(f"{label} decisions recorded: {total}")
    by_plugin = ", ".join(f"{k} {v}" for k, v in r['plugins'].most_common())
    print(f"  plugins: {by_plugin}")
    parts = [f"{k} {v}" for k, v in r['decisions'].most_common()]
    print(f"  outcomes: {', '.join(parts)}")
    # The rate divides by Bash calls, never by the decisions above — see
    # SCOPE_KEYS. With no tally the count prints alone rather than acquiring a
    # denominator it does not have.
    sc = r.get('scope') or {}
    calls = sc.get('bash_calls', 0)
    if not calls:
        print(f"  friction (ask+deny): {asks}")
    else:
        prompted, sessions = sc['sessions_prompted'], sc['sessions']
        print(f"  friction (ask+deny): {asks} — {100 * asks / calls:.1f}% of "
              f"the {calls} Bash call{'' if calls == 1 else 's'} in the window")
        print(f"  sessions with a prompt: {prompted} of {sessions} "
              f"({100 * prompted / sessions:.0f}%)")
        print("  note: the denominator is every Bash call in the window, so a "
              "guard installed")
        print("        part-way through it reads low until the window catches "
              "up")
    # Under one guard the deny recovery is complete, so the count stands on its
    # own. Across guards it is not, and an unqualified zero would read as "this
    # one never blocks" — the exact misreading issue #25 was about.
    if plugin == 'all':
        print("  note: denies are read from tool-result text, the only place "
              "Claude Code records")
        print("        them — a guard whose reason does not open with its own "
              "plugin name under-reports")
    # Only foreground-guard's own overrides are counted, so the line has no
    # place under a header covering every guard — omit it rather than show one
    # guard's statistic as if it summarized the set.
    if r['overrides'] and plugin != 'all':
        print(f"  FOREGROUND_GUARD_OVERRIDE downgrades: {r['overrides']}")
    print()

    # Ahead of the prompt rankings: a hook that never ran is not friction, it is
    # a guard that stopped guarding, and it is the one thing here worth acting
    # on before any prompt count.
    if r.get('errors'):
        print("Hook failures — the guard never ran, so the call went "
              f"unguarded (top {top}):")
        for note, n in r['errors'].most_common(top):
            print(f"  {n:5}  {note}")
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

    tally = new_scope()
    decisions = list(iter_decisions(paths, args.plugin, cutoff, args.repo,
                                    tally))
    report = build_report(decisions, tally)

    if args.json:
        out = {
            # `total` still counts decision records; the rate's denominator is
            # `bash_calls` (see SCOPE_KEYS), so consumers keep their key.
            'total': report['total'],
            'bash_calls': tally['bash_calls'],
            'sessions': tally['sessions'],
            'sessions_prompted': tally['sessions_prompted'],
            'decisions': dict(report['decisions']),
            'plugins': dict(report['plugins']),
            'categories': dict(report['categories']),
            'tools': dict(report['tools']),
            'top_targets': report['targets'].most_common(args.top),
            'top_commands': report['commands'].most_common(args.top),
            'hook_errors': report['errors'].most_common(args.top),
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
