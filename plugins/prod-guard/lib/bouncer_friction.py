# ---------------------------------------------------------------------
# VENDORED COPY -- do not edit. Source: lib/bouncer_friction.py
# Regenerate with: python3 scripts/sync-lib.py
# ---------------------------------------------------------------------
"""Reading hook decisions back out of Claude Code's session transcripts.

The half of a friction report every guard must agree on: how Claude Code
records a tool call, a hook's verdict and a denial, and which plugin a record
belongs to. What a reason means stays in each guard's own report.

No hook imports this. It is read-only over ``~/.claude/projects/**/*.jsonl``,
which Claude Code writes whether or not a report is ever run.
"""
import collections
import datetime as dt
import json
import os
import re

# Plugins whose name does not end in `-guard`. A new guard needs no entry; a
# name outside that shape does, or its denies stay invisible. A bare `\w+: `
# opener would read `error: ` and `Traceback: ` as denies.
NON_GUARD_PLUGINS = ('pr-sentinel',)

# Claude Code keeps a hook's stdout only for a call it goes on to run, so a deny
# leaves no attachment. Its trace is the error the blocked call handed back,
# which carries the hook's reason verbatim: bare through Claude Code 2.1.275,
# and behind `PreToolUse:<tool> hook error: ` from 2.1.281.
DENY_TEXT = re.compile(
    r'^(?:Error:\s*)?(?:PreToolUse:\S+ hook error:\s*)?('
    + '|'.join([r'[a-z0-9-]+-guard']
               + [re.escape(p) for p in NON_GUARD_PLUGINS])
    + r'):\s')

# The one attachment type that carries a verdict. A failed or cancelled hook
# has empty stdout too, and reading that as a defer claims a guard looked and
# let the call through when it never ran.
HOOK_OK = 'hook_success'

# A line holding none of these is no event, and most of a transcript is such
# lines, so the check spares parsing them.
_MARKERS = ('tool_use', 'tool_result', 'PreToolUse:')

# `session` is the transcript's own id, shared with its subagents' files.
Call = collections.namedtuple(
    'Call', 'call_id tool command cwd ts session')
# `decision` is allow, ask, deny, defer, or `error` for a hook that never
# reached a verdict. `tool` and `command` come from the call it judged.
Decision = collections.namedtuple(
    'Decision', 'call_id plugin decision reason tool command cwd ts session')


def parse_since(spec):
    """A tz-aware UTC cutoff, or None for no limit. Accepts Nd/Nh/Nm or
    YYYY-MM-DD, and raises ValueError on anything else."""
    if not spec or spec == 'all':
        return None
    m = re.fullmatch(r'(\d+)([dhm])', spec)
    if m:
        unit = {'d': 'days', 'h': 'hours', 'm': 'minutes'}[m.group(2)]
        return (dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(**{unit: int(m.group(1))}))
    try:
        day = dt.datetime.strptime(spec, '%Y-%m-%d')
    except ValueError:
        raise ValueError('expected Nd/Nh/Nm or YYYY-MM-DD, got %r' % spec)
    return day.replace(tzinfo=dt.timezone.utc)


def parse_ts(rec):
    ts = rec.get('timestamp')
    if not isinstance(ts, str):
        return None
    try:
        when = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def in_window(event, cutoff, repo):
    """Whether an event meets a report's `--since` and `--repo` filters."""
    return (not cutoff or event.ts >= cutoff) and (not repo or repo in event.cwd)


def guard_name(command):
    """Plugin label from a hook command, e.g. '.../bash-prod-guard.py' ->
    'prod-guard', or None when it names no guard.

    The label has to be the word DENY_TEXT reads off a deny, because the two
    are counted together, so `pr-sentinel-guard.py` folds onto `pr-sentinel`. A
    command naming no *.py falls back to its launcher's basename, kept only in
    the shape DENY_TEXT reads, which leaves a project's own hook scripts and
    the shared `run-python-hook.cmd` unlabelled."""
    m = re.search(r'([A-Za-z0-9_-]+)\.py', command or '')
    if m:
        name = re.sub(r'^bash-', '', m.group(1))
    else:
        m = re.match(r'\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', command or '')
        word = next((g for g in m.groups() if g), '') if m else ''
        stem = re.sub(r'\.[A-Za-z0-9]+$', '', re.split(r'[/\\]', word)[-1])
        name = re.sub(r'^run-', '', stem)
        if not name.endswith('-guard') and name not in NON_GUARD_PLUGINS:
            return None
    trimmed = re.sub(r'-guard$', '', name)
    return trimmed if trimmed in NON_GUARD_PLUGINS else name


def named_guard(reason):
    """The guard whose opener a reason carries, or None."""
    m = DENY_TEXT.match(reason)
    return m.group(1) if m else None


def deny_from_result(block):
    """(guard, reason) if a tool_result block is a hook deny, else None. The
    reason comes back opening with the guard's name in both recorded forms."""
    if not block.get('is_error'):
        return None
    text = block.get('content')
    if isinstance(text, list):
        text = ''.join(p.get('text') or '' for p in text if isinstance(p, dict))
    if not isinstance(text, str):
        return None
    text = text.strip()
    m = DENY_TEXT.match(text)
    return (m.group(1), text[m.start(1):]) if m else None


def _clip(line, cap=120):
    """One line, clipped from the middle: a hook failure opens with a long
    plugin path and ends with what went wrong."""
    line = ' '.join(line.split())
    if len(line) <= cap:
        return line
    head = (cap - 1) * 2 // 3
    return line[:head] + '...' + line[head + 3 - cap:]


def error_note(att):
    """One line naming what went wrong with a hook that reached no verdict. A
    crash carries `exitCode` and `stderr`, a cancellation neither."""
    parts = [att.get('type') or 'unknown']
    if att.get('exitCode') is not None:
        parts.append('exit %s' % att['exitCode'])
    if att.get('timedOut'):
        ms = att.get('timeoutMs')
        parts.append('timed out after %sms' % ms if ms else 'timed out')
    lines = (att.get('stderr') or '').strip().splitlines()
    if lines:
        parts.append(_clip(lines[0]))
    return ': '.join(parts)


def verdict(att):
    """(decision, reason) an attachment records. Empty stdout is a defer."""
    if att.get('type') != HOOK_OK:
        return 'error', error_note(att)
    try:
        out = json.loads(att.get('stdout') or '{}')['hookSpecificOutput']
        decision = out.get('permissionDecision') or 'defer'
        reason = out.get('permissionDecisionReason') or ''
    except (ValueError, KeyError, TypeError, AttributeError):
        return 'defer', ''
    return decision, reason if isinstance(reason, str) else ''


def _records(path):
    """The timestamped records of one transcript that may hold an event.

    A record with no timestamp is dropped, so `--since` can place whatever is
    left. Those are `api-request-blob` records, which replay the conversation
    sent to the API: each tool call and deny in one is also in a timestamped
    record of the same file."""
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if not any(m in line for m in _MARKERS):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and parse_ts(rec):
                    yield rec
    except OSError:
        return


def _blocks(rec, kind):
    msg = rec.get('message')
    content = msg.get('content') if isinstance(msg, dict) else None
    for block in content if isinstance(content, list) else ():
        if isinstance(block, dict) and block.get('type') == kind:
            yield block


def read_events(paths, own_label=None):
    """Yield a Call for every tool call in the transcripts and a Decision for
    every guard verdict on one, each once, whatever the filters.

    Everything is keyed on the tool-use id. A resumed or forked session copies
    earlier records into a new file, so a repeat can be in another transcript
    as well as in the same one, and the id is the part a copy never changes.

    Some attachments record `command hook 7` for the hook command, and the
    number identifies no guard. Those are attributed by their reason: by its
    opener, else by `own_label(decision, reason)`, which a report passes to
    recognize the verdicts its guard leaves unprefixed.

    Two sources per file: the attachment stream, then the error tool results
    that hold the denies (see DENY_TEXT). An `ask` the operator declines hands
    its reason back as an error too, and the attachment it left is what keeps
    it out of the second."""
    seen_calls, decided = set(), set()
    for path in sorted(paths):
        calls, attachments, denies = {}, [], []
        for rec in _records(path):
            att = rec.get('attachment')
            if isinstance(att, dict):
                if str(att.get('hookName')).startswith('PreToolUse:'):
                    attachments.append((rec, att))
                continue
            for block in _blocks(rec, 'tool_use'):
                cid = block.get('id')
                if not cid or cid in calls:
                    continue
                inp = block.get('input')
                command = inp.get('command') if isinstance(inp, dict) else ''
                calls[cid] = Call(cid, block.get('name') or '',
                                  command if isinstance(command, str) else '',
                                  rec.get('cwd') or '', parse_ts(rec),
                                  rec.get('sessionId') or path)
            for block in _blocks(rec, 'tool_result'):
                found = deny_from_result(block)
                if found:
                    denies.append((rec, block.get('tool_use_id'), found))

        for cid, call in calls.items():
            if cid not in seen_calls:
                seen_calls.add(cid)
                yield call

        def decision(rec, cid, plugin, outcome, reason, tool):
            call = calls.get(cid)
            return Decision(cid, plugin, outcome, reason,
                            call.tool if call else tool,
                            call.command if call else '',
                            rec.get('cwd') or '', parse_ts(rec),
                            rec.get('sessionId') or path)

        for rec, att in attachments:
            outcome, reason = verdict(att)
            plugin = guard_name(att.get('command')) or named_guard(reason)
            if not plugin and own_label and outcome != 'error':
                plugin = own_label(outcome, reason)
            if not plugin:
                continue
            cid = att.get('toolUseID')
            key = (plugin, cid or rec.get('uuid'))
            if key in decided:
                continue
            decided.add(key)
            yield decision(rec, cid, plugin, outcome, reason,
                           att['hookName'].split(':', 1)[1])

        for rec, cid, (plugin, reason) in denies:
            # A result no call joins to is text about a denial, not one.
            if cid not in calls or (plugin, cid) in decided:
                continue
            decided.add((plugin, cid))
            yield decision(rec, cid, plugin, 'deny', reason, '')


# --- Stale-install detection ------------------------------------------------
# Claude Code refreshes official marketplaces only, so friction a newer release
# already fixes can linger under a pinned install. Every read is of local state
# Claude Code keeps, and any missing or unparseable file means no opinion.

def _version_tuple(v):
    """'1.5.0' -> (1, 5, 0), stopping at the first non-numeric component, or
    None when nothing numeric leads."""
    out = []
    for part in re.split(r'[.\-+]', str(v or '').strip()):
        if not part.isdigit():
            break
        out.append(int(part))
    return tuple(out) or None


def _read_json(*parts):
    try:
        with open(os.path.join(*parts), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _installed(plugins_dir, plugin):
    """(version, marketplace) of the newest install record, or (None, None)."""
    plugins = _read_json(plugins_dir, 'installed_plugins.json').get('plugins')
    for key, records in (plugins if isinstance(plugins, dict) else {}).items():
        name, _, marketplace = key.partition('@')
        if name == plugin:
            versions = [r.get('version') for r in records or ()
                        if isinstance(r, dict) and _version_tuple(r.get('version'))]
            return (max(versions, key=_version_tuple, default=None),
                    marketplace or None)
    return None, None


def _available(plugins_dir, plugin, marketplace):
    """The version the marketplace's local clone advertises, or None."""
    known = _read_json(plugins_dir, 'known_marketplaces.json').get(marketplace)
    clone = (known.get('installLocation') if isinstance(known, dict) else None
             ) or os.path.join(plugins_dir, 'marketplaces', marketplace)
    entries = _read_json(clone, '.claude-plugin', 'marketplace.json').get('plugins')
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict) and entry.get('name') == plugin:
            return entry.get('version')
    return None


def stale_install(plugins_dir, plugin):
    """What to say when the installed plugin lags the local marketplace clone,
    else None."""
    installed, marketplace = _installed(plugins_dir, plugin)
    if not installed or not marketplace:
        return None
    available = _available(plugins_dir, plugin, marketplace)
    newer = _version_tuple(available)
    if not newer or newer <= _version_tuple(installed):
        return None
    return {'plugin': plugin, 'installed': installed,
            'available': available, 'marketplace': marketplace}
