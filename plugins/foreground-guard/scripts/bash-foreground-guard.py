#!/usr/bin/env python3
"""PreToolUse hook: guard the session's main thread from foreground waits.

Two classes of time-wasters, kept distinct in the code:

1. **Class A — foreground poll/watch.** A Bash command that blocks until
   killed or until an external event: watch/follow modes (`gh run watch`,
   `kubectl logs -f`, `tail -f`, `watch ...`), shell loops that poll with
   `sleep`, chained repeat-with-sleep sequences (`cmd; sleep N; cmd`), and
   bare `sleep N` waits at or above a configurable floor. The main thread —
   the one the user is talking to — sits blocked the whole time. The guard
   denies (config may de-escalate to `ask`) and teaches the three fixes: one
   non-blocking snapshot, a Monitor whose script exits when the condition
   flips, or an explicit `timeout N` bound. Backgrounding is not among them
   — a detached poll still blocks (see Exemptions).

2. **Class B — slow command with an inadequate timeout.** A command the repo
   *knows* takes longer than the Bash tool's default 2-minute timeout
   (envtest suites, e2e runs, `-race` builds), about to run in the foreground
   with that default (or any timeout below the registered minimum). The tool
   will kill it before it finishes and the entire run is wasted. The guard
   denies (config may de-escalate to `ask`), naming the exact fix:
   `timeout: <min>` on the Bash call, or `run_in_background: true`. Both are
   parameters of the Bash call, so the agent is the only party that can
   apply either — a human answering a prompt can only let the doomed run
   proceed. The default registry is EMPTY — slow-command knowledge is
   per-repo and lives in `.claude/foreground-guard.json`.

Exemptions: `run_in_background: true` exempts Class B only — it is the fix
that class teaches. It is not a fix for a poll: a detached `gh run watch` or
`sleep`-loop holds a task slot for the whole wait and hands back output the
agent cannot date, so Class A still blocks (with the backgrounded wording).
A trailing `&` detaches the blocking command (per-segment `&` exempts just
that segment); a `timeout N ...` wrap exempts the wrapped segment from
Class A — an explicit bound is exactly the fix the guard teaches, and the
Bash tool's own timeout still backstops it (a silent defer, by design).

Decision semantics: the hook ONLY returns `deny`/`ask` or passes through
silently (defer). It NEVER emits `permissionDecision: "allow"` — an allow
would bypass the user's permission settings and the sibling guards.

Both classes deny by default. The verdict picks who resolves the
uncertainty, and the resource at stake here is the session's own main
thread: there is no blast radius, no credential, and no third party, so a
human at the prompt holds no fact the agent lacks. Every finding ships a
rewrite the agent can apply — a snapshot command, a Monitor, a `timeout`
bound, a `timeout:` on the call — so a deny resolves in the agent's own loop
while an ask spends a context switch to learn nothing. Class B is the sharp
case: its fixes are parameters of the Bash call, so approving the prompt
produces exactly the killed run the class exists to prevent.

`"action": "ask"` on either class de-escalates it back to a prompt. That is
the supervised posture — for someone who wants to watch the guard work —
and it is opt-in, because it is the setting that spends a person's
attention. In an unattended permission mode (`dontAsk` or
`bypassPermissions`) a de-escalated `ask` is emitted as `deny` again: the
reason reaches the agent rather than parking on a prompt nobody answers.

A `FOREGROUND_GUARD_OVERRIDE=<reason>` prefix makes the hook defer silently,
in every permission mode. The agent holds the missing fact — this wait is
genuinely wanted — and states it; a wrong assertion costs waiting and
nothing else, so it is not worth a person's time to confirm. Deferring is
not allowing: normal permissions and the sibling guards still see the call.
The stated reason survives in the transcript on the command string itself,
which is the audit record (and the one `friction-report.py` counts).

Fail modes: fail OPEN on infrastructure errors (unparseable input, bad
config, unexpected exception — the hook stays silent and never breaks the
session). Within Class A, unknown durations lean toward friction (`sleep $N`
blocks: an unresolvable wait is treated as a long one — a false positive
costs the agent one retry, a false negative parks the main thread). An
argument the hook cannot expand is never routed to a human: they see the
same unexpanded `$N` and can resolve it no better, so the reason asks for
the literal instead. This is a productivity guard, not a security boundary:
on *parsing* uncertainty (unbalanced quotes, heredoc oddities) it defers
rather than guessing.

Parsing rules inherited from the sibling guards:
  * Heredoc bodies are stripped textually BEFORE tokenization, so body text
    is never parsed as command segments (the workspace-guard #83 bug class).
  * `bash some-script.sh` stays opaque — no script-file inspection. Only
    quoted `bash -c '...'` / `eval ...` bodies are recursed into (bounded).

Reads the hook JSON on stdin, emits a PreToolUse decision on stdout.
"""
import fnmatch
import json
import os
import re
import shlex
import sys

# The parsing primitives every claude-bouncer guard shares. This guard keeps its
# own tokenizer and segmenter: it needs to know which segment was BACKGROUNDED,
# which the shared operator-preserving lexer does not answer. The copy under
# this plugin's `lib/` is vendored from the root; see scripts/sync-lib.py.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
from bouncer_parse import (                                    # noqa: E402
    ASSIGNMENT_RE, COMMENT_PRECEDERS, _consume_heredoc_body,
    _skip_balanced_parens, strip_heredoc_bodies,
)

DEFAULT_BASH_TIMEOUT_MS = 120000
DEFAULT_SLEEP_FLOOR_SECONDS = 10

# ---------------------------------------------------------------------------
# Configuration: .claude/foreground-guard.json
# ---------------------------------------------------------------------------

def _config_paths():
    """Candidate config files, in read order: user, project, explicit env.
    Scalars are last-present-wins (project overrides user); pattern lists and
    the slow-command registry merge additively across all files."""
    paths = []
    home = os.environ.get('HOME')
    if home:
        paths.append(os.path.join(home, '.claude', 'foreground-guard.json'))
    proj = os.environ.get('CLAUDE_PROJECT_DIR')
    if proj:
        paths.append(os.path.join(proj, '.claude', 'foreground-guard.json'))
    extra = os.environ.get('FOREGROUND_GUARD_CONFIG')
    if extra:
        paths.append(extra)
    return paths


def load_config():
    """Merged config with defaults. Any unreadable/malformed file loses only
    itself (fail OPEN on config infrastructure): the built-in behavior still
    applies, so a typo never silently disables the guard."""
    cfg = {
        'poll_enabled': True,
        'poll_action': 'deny',         # 'deny' | 'ask' (config may supervise)
        'extra_watch_patterns': [],    # additive regexes over a segment string
        'exempt_watch_patterns': [],   # additive allowlist; suppresses matches
        'sleep_floor_seconds': DEFAULT_SLEEP_FLOOR_SECONDS,
        'slow_enabled': True,
        'slow_action': 'deny',         # 'deny' | 'ask' (config may supervise)
        'slow_commands': {},           # {regex: ms} | {cmd: {glob: ms}}, additive
        'hint': '',                    # per-repo watcher-machinery hint
    }
    for path in _config_paths():
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        poll = data.get('poll')
        if isinstance(poll, dict):
            if isinstance(poll.get('enabled'), bool):
                cfg['poll_enabled'] = poll['enabled']
            if poll.get('action') in ('ask', 'deny'):
                cfg['poll_action'] = poll['action']
            pats = poll.get('extra_watch_patterns')
            if isinstance(pats, list):
                cfg['extra_watch_patterns'] += [p for p in pats
                                                if isinstance(p, str)]
            exempt = poll.get('exempt_watch_patterns')
            if isinstance(exempt, list):
                cfg['exempt_watch_patterns'] += [p for p in exempt
                                                 if isinstance(p, str)]
            floor = poll.get('sleep_floor_seconds')
            if isinstance(floor, (int, float)) and not isinstance(floor, bool):
                cfg['sleep_floor_seconds'] = floor
        slow = data.get('slow')
        if isinstance(slow, dict):
            if isinstance(slow.get('enabled'), bool):
                cfg['slow_enabled'] = slow['enabled']
            if slow.get('action') in ('ask', 'deny'):
                cfg['slow_action'] = slow['action']
            cmds = slow.get('commands')
            if isinstance(cmds, dict):
                for pat, ms in cmds.items():
                    if not isinstance(pat, str):
                        continue
                    if isinstance(ms, (int, float)) \
                            and not isinstance(ms, bool):
                        cfg['slow_commands'][pat] = int(ms)
                    elif isinstance(ms, dict):
                        # Target-aware form: {command: {argument glob: ms}}.
                        # Target maps merge additively per command word.
                        targets = cfg['slow_commands'].get(pat)
                        if not isinstance(targets, dict):
                            targets = {}
                        for tgt, v in ms.items():
                            if isinstance(tgt, str) \
                                    and isinstance(v, (int, float)) \
                                    and not isinstance(v, bool):
                                targets[tgt] = int(v)
                        if targets:
                            cfg['slow_commands'][pat] = targets
        if isinstance(data.get('hint'), str):
            cfg['hint'] = data['hint']
    return cfg


# ---------------------------------------------------------------------------
# Watch/follow registry (Class A)
# ---------------------------------------------------------------------------

# (label, regex over the wrapper-stripped segment string, snapshot alternative).
# Regexes are anchored to the tool name so `grep -f patterns.txt` and
# `git log --follow` never match. Extensible via config extra_watch_patterns.
BUILTIN_WATCH = [
    ('gh pr checks --watch',
     r'^gh\s+pr\s+checks\b.*\s--watch\b',
     'run `gh pr checks <pr>` once without `--watch`'),
    ('gh run watch',
     r'^gh\s+run\s+watch\b',
     'check once with `gh run view <run-id>`'),
    ('kubectl logs --follow',
     r'^(?:kubectl|oc)\b.*\blogs\b.*(?:\s-f\b|\s--follow\b)',
     'snapshot with `kubectl logs --tail=100` (no `-f`)'),
    ('kubectl get --watch',
     r'^(?:kubectl|oc)\b.*\bget\b.*(?:\s-w(?:\s|$)|\s--watch(?:-only)?\b)',
     'run `kubectl get` once without `-w`/`--watch`'),
    ('tail --follow',
     r'^tail\b.*(?:\s-[A-Za-z0-9]*[fF][A-Za-z0-9]*(?=\s|$)|\s--follow\b)',
     'read the end once with `tail -n 100`'),
    ('journalctl --follow',
     r'^journalctl\b.*(?:\s-f\b|\s--follow\b)',
     'snapshot with `journalctl -n 100` (no `-f`)'),
    ('docker logs --follow',
     r'^(?:docker|podman|nerdctl)\b.*\blogs\b.*(?:\s-f\b|\s--follow\b)',
     'snapshot with `docker logs --tail 100` (no `-f`)'),
    ('watch',
     r'^watch\s+\S',
     'run the wrapped command once directly (no `watch`)'),
]

def watch_matchers(cfg):
    """Compiled (label, regex, alternative) list: built-ins plus config
    extras. A broken config pattern loses itself, not the list."""
    out = []
    for label, pat, alt in BUILTIN_WATCH:
        out.append((label, re.compile(pat), alt))
    for pat in cfg['extra_watch_patterns']:
        try:
            out.append((pat, re.compile(pat),
                        'take one non-blocking snapshot instead'))
        except re.error:
            continue
    return out


def exempt_matchers(cfg):
    """Compiled allowlist regexes over the wrapper-stripped segment string.
    A segment matching any of these is exempt from Class A watch/follow
    detection — exemptions win over matches, mirroring prod-guard's
    `classify()` suppression precedence, so a repo can quiet a specific
    built-in watch pattern that false-positives without disabling all of
    Class A. A broken pattern loses itself, not the list."""
    out = []
    for pat in cfg['exempt_watch_patterns']:
        try:
            out.append(re.compile(pat))
        except re.error:
            continue
    return out


# ---------------------------------------------------------------------------
# Shell parsing: heredoc stripping, tokenization, segment splitting
# ---------------------------------------------------------------------------

PUNCT_CHARS = frozenset(';()<>|&')


def tokenize(raw):
    """shlex-tokenize with POSIX quoting and punctuation grouping. Backticks
    and newlines are rewritten to `;` first so substitutions and multi-line
    commands split into their own segments. Returns None on unbalanced
    quotes (caller defers: fail-open on parse errors)."""
    raw = raw.replace('`', ';').replace('\n', ';')
    lex = shlex.shlex(raw, posix=True, punctuation_chars=';()<>|&')
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


# Redirect operator tokens: part of the same simple command, not separators.
# Gluing them matters for background detection — `./server > log & sleep 2`
# must keep `./server > log` as one segment so its `&` terminator marks it
# backgrounded (otherwise the `&` lands on the redirect target and the
# startup-grace `sleep` false-positives as a poll).
REDIR = frozenset({'>', '>>', '<', '<<', '<<<', '>&', '<&', '>|', '&>', '&>>'})


def split_segments(tokens):
    """Split a token stream into (argv, terminator) pairs on every separator
    token (`;`, `&`, `&&`, `|`, `||`, parens, and mixed runs). The
    terminator is the token that ended the segment ('' at end of input); a
    terminator of exactly `&` marks the segment as backgrounded. Redirect
    operators do NOT split: the operator and its target word are dropped
    from the segment (the target is never a command). Crude splitting only
    ever creates extra segments to inspect, never hides a watch/sleep
    command behind an operator."""
    segs, cur = [], []
    skip_next_word = False
    for t in tokens:
        if t and all(c in PUNCT_CHARS for c in t):
            if t in REDIR:
                skip_next_word = True
                continue
            if cur:
                segs.append((cur, t))
            cur = []
            skip_next_word = False
        elif skip_next_word:
            skip_next_word = False
        else:
            cur.append(t)
    if cur:
        segs.append((cur, ''))
    return segs


LOOP_KEYWORDS = frozenset({'while', 'until', 'for'})
OTHER_KEYWORDS = frozenset({
    'do', 'done', 'then', 'else', 'elif', 'fi', 'if', 'in', 'case', 'esac',
    'select', '{', '}', '!', '[[', ']]',
})
# Wrappers that prefix another command without changing whether it blocks.
PLAIN_WRAPPERS = frozenset({'command', 'nohup', 'builtin', 'time', 'exec',
                            'stdbuf', 'unbuffer'})
SHELL_NAMES = frozenset({'bash', 'sh', 'zsh', 'dash', 'ksh'})

# sudo flags that take a value (skip flag + value when stripping).
SUDO_VALUE_FLAGS = frozenset({'-u', '--user', '-g', '--group', '-p', '--prompt'})


def strip_head(argv, state):
    """Peel shell keywords, env-var prefixes, and launcher wrappers off a
    segment so the blocking command underneath is classified, not the
    wrapper. Side effects on `state`:
      * 'loop'            — a while/until/for keyword led the segment
      * 'override'        — the reason from a FOREGROUND_GUARD_OVERRIDE=<reason>
                            prefix (a str, possibly empty; None when unseen)
      * 'timeout_wrapped' — (per-segment, reset by caller) the segment is
                            wrapped in `timeout N ...`: an explicit bound,
                            which exempts it from Class A (allow-through)."""
    while argv:
        head = os.path.basename(argv[0])
        if head in LOOP_KEYWORDS:
            state['loop'] = True
            argv = argv[1:]
        elif head in OTHER_KEYWORDS:
            argv = argv[1:]
        elif ASSIGNMENT_RE.match(argv[0]):
            name, _, _val = argv[0].partition('=')
            if name == 'FOREGROUND_GUARD_OVERRIDE':
                state['override'] = _val
            argv = argv[1:]
        elif head == 'sudo':
            argv = argv[1:]
            while argv and argv[0].startswith('-'):
                argv = argv[2:] if argv[0] in SUDO_VALUE_FLAGS else argv[1:]
        elif head == 'env':
            argv = argv[1:]
            while argv:
                if argv[0].startswith('-'):
                    argv = argv[2:] if argv[0] in ('-u', '--unset', '-C',
                                                   '--chdir') else argv[1:]
                elif ASSIGNMENT_RE.match(argv[0]):
                    name, _, _val = argv[0].partition('=')
                    if name == 'FOREGROUND_GUARD_OVERRIDE':
                        state['override'] = _val
                    argv = argv[1:]
                else:
                    break
        elif head == 'timeout':
            argv = argv[1:]
            while argv and argv[0].startswith('-'):
                argv = argv[2:] if argv[0] in ('-k', '--kill-after', '-s',
                                               '--signal') else argv[1:]
            if argv:
                argv = argv[1:]  # the DURATION operand
            state['timeout_wrapped'] = True
        elif head in PLAIN_WRAPPERS:
            argv = argv[1:]
            while argv and argv[0].startswith('-'):
                argv = argv[1:]
        else:
            break
    return argv


# ---------------------------------------------------------------------------
# Sleep duration parsing
# ---------------------------------------------------------------------------

_SLEEP_ARG_RE = re.compile(r'^(\d+(?:\.\d*)?|\.\d+)([smhd]?)$')
_SLEEP_UNIT = {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}


def sleep_seconds(argv):
    """Total seconds a `sleep` argv waits: sums GNU-style duration args
    (`sleep 1m 30`), `inf`/`infinity` -> float('inf'). Returns None when any
    argument is unresolvable (`sleep $N`) — the caller treats unknown as
    long (a false positive costs one prompt; a false negative parks the
    main thread)."""
    total = 0.0
    for a in argv[1:]:
        if a.startswith('-'):
            continue
        if a in ('inf', 'infinity'):
            return float('inf')
        m = _SLEEP_ARG_RE.match(a)
        if not m:
            return None
        total += float(m.group(1)) * _SLEEP_UNIT[m.group(2)]
    return total


# ---------------------------------------------------------------------------
# Findings and messages
# ---------------------------------------------------------------------------

ASK, DENY = 1, 2

CONFIG_HINT = ('Config: .claude/foreground-guard.json '
               '(see the foreground-guard README).')

ISSUES_URL = 'https://github.com/karlkfi/claude-bouncer/issues'

# Permission modes in which nobody is expected to be watching the prompt
# stream, so a config-de-escalated `ask` buys friction and no answer. Only
# reachable via `"action": "ask"` — the defaults deny in every mode.
#
# `auto` is deliberately absent. The name reads as unattended and the mode
# isn't: an `ask` there reaches Claude Code's standard prompt and somebody
# answers it, so converting it removed the human rather than protecting them.
# A repo that set `"action": "ask"` asked to be prompted, and that is the more
# specific signal. branch-guard dropped `auto` from its own set in v1.7.0 for
# the same reason, and workspace-guard treats `bypassPermissions` alone as
# human-free.
UNATTENDED_MODES = frozenset({'dontAsk', 'bypassPermissions'})


def deny_tail():
    """The tail appended once to a deny: how to get through it when the
    foreground wait is genuinely wanted, and where to report a wrong verdict.
    Built at the emit site rather than per finding — up to three findings are
    joined into one reason, and the escape hatch belongs to the decision.

    Ordered after the fixes, never before them: an escape hatch offered
    first is the one the agent reaches for first."""
    return ('If the wait is genuinely required, re-run the same command with '
            'a FOREGROUND_GUARD_OVERRIDE=<reason> prefix and this guard '
            'stands aside. If this verdict looks wrong, say so to the user '
            'rather than working '
            'around it: `/foreground-guard:friction-report` shows how often '
            'the pattern lands, and it can be filed at %s.' % ISSUES_URL)


# cfg['backgrounded'] is runtime state, not config — main() sets it from the
# payload. Class A findings word the cost around it, but not the fixes: a
# backgrounded poll blocks too, so `run_in_background` is never one of them.
def _where(cfg):
    return 'in the background' if cfg.get('backgrounded') else 'in the foreground'


# Monitor is the sanctioned way to wait on a condition that flips elsewhere:
# the event arrives dated and no Bash task slot is held for the wait (#22).
def _fixups(alt):
    return ('Instead: (1) %s; (2) arm a Monitor whose script exits when the '
            'condition flips — it wakes the session with a dated event and '
            'holds no Bash task slot; or (3) bound the wait explicitly with '
            '`timeout <seconds> ...`.' % alt)


def finding_a(cfg, what, alt):
    sev = DENY if cfg['poll_action'] == 'deny' else ASK
    cost = ('holds a task slot for the whole wait and hands back output that '
            'cannot be dated' if cfg.get('backgrounded')
            else 'blocks the session\'s main thread for the whole wait')
    msg = ('foreground-guard: %s — this %s. %s'
           % (what, cost, _fixups(alt)))
    if cfg['hint']:
        msg += ' This repo: %s' % cfg['hint']
    msg += ' ' + CONFIG_HINT
    return (sev, msg)


def finding_watch(cfg, seg_str, label, alt):
    shown = seg_str if len(seg_str) <= 80 else seg_str[:77] + '...'
    return finding_a(cfg, '`%s` runs in watch/follow mode (%s) and blocks '
                          'until killed' % (shown, label), alt)


def finding_loop(cfg):
    return finding_a(
        cfg, 'a `while`/`until`/`for` loop with `sleep` polls %s' % _where(cfg),
        'run the loop body ONCE now, dropping the loop and the `sleep`, and '
        'check again next turn if it is not ready')


def finding_sandwich(cfg):
    return finding_a(
        cfg, 'a repeat-with-sleep chain (`cmd; sleep N; cmd; ...`) polls %s'
             % _where(cfg),
        'run the check ONCE now, dropping the `sleep` and the repeat, and '
        'check again next turn if it is not ready')


def finding_sleep(cfg, secs):
    """The bare-sleep finding. An unresolvable duration gets its own first
    fix: the hook cannot expand `$DELAY` and neither can a reader, so the
    rewrite is to write the literal rather than to route the question at
    anyone."""
    parked = 'a background task' if cfg.get('backgrounded') else 'the main thread'
    if secs is None:
        desc = 'an unresolvable duration (treated as long)'
        alt = ('write the literal duration — the hook cannot expand it, and '
               'anything under the %g s floor never lands here'
               % cfg['sleep_floor_seconds'])
    else:
        desc = '~%g s' % secs
        alt = ('skip the wait — do the follow-up check now, and if the thing '
               'is not ready, come back next turn')
    return finding_a(cfg, '`sleep` parks %s for %s' % (parked, desc), alt)


def finding_slow(cfg, what, min_ms, timeout_ms, timeout_was_set):
    sev = DENY if cfg['slow_action'] == 'deny' else ASK
    cur = ('the %d ms timeout set on this call' % timeout_ms
           if timeout_was_set
           else 'the default %d ms timeout' % timeout_ms)
    msg = ('foreground-guard: this command matches %s, registered as needing '
           'at least %d ms, but it would run in the foreground with %s — the '
           'Bash tool will kill it before it finishes and the whole run is '
           'wasted. Fix: set `timeout: %d` on this Bash call, or run it with '
           'run_in_background: true.'
           % (what, min_ms, cur, min_ms))
    msg += ' ' + CONFIG_HINT
    return (sev, msg)


# ---------------------------------------------------------------------------
# Class A analysis
# ---------------------------------------------------------------------------

def analyze_class_a(raw, cfg, depth=0):
    """Class A findings for a command string. Returns (findings, override).
    Recurses (bounded) into `bash -c '...'` / `eval ...` bodies so a quoted
    poll loop can't ride past the guard; `bash some-script.sh` stays opaque
    (no script-file inspection)."""
    findings = []
    state = {'loop': False, 'override': None}
    if depth > 3:
        return findings, None

    raw = strip_heredoc_bodies(raw)

    # A trailing `&` detaches the whole command (including a backgrounded
    # subshell or loop): nothing here blocks the main thread.
    stripped = raw.rstrip()
    if stripped.endswith('&') and not stripped.endswith('&&'):
        return findings, None

    tokens = tokenize(raw)
    if tokens is None:
        return findings, None  # unparseable: fail-open, defer

    matchers = watch_matchers(cfg)
    exempts = exempt_matchers(cfg)
    floor = cfg['sleep_floor_seconds']

    # Per-segment classification. kind: 'sleep' | 'cmd' | None (empty/opaque).
    seg_kinds = []       # for the sandwich rule, foreground segments only
    loop_sleep = False   # a foreground `sleep` seen anywhere (any duration)
    sleep_findings = []
    for group, term in split_segments(tokens):
        bg = (term == '&')
        state['timeout_wrapped'] = False
        argv = strip_head(list(group), state)
        if not argv:
            continue
        if bg or state['timeout_wrapped']:
            continue  # detached or explicitly bounded: exempt from Class A
        head = os.path.basename(argv[0])
        if head == 'sleep':
            secs = sleep_seconds(argv)
            seg_kinds.append('sleep')
            loop_sleep = True
            if secs is None or secs >= floor:
                sleep_findings.append(finding_sleep(cfg, secs))
            continue
        seg_kinds.append('cmd')
        if head in SHELL_NAMES:
            # bash -c 'while ...; do sleep 5; done': analyze the body.
            body = None
            for i, tok in enumerate(argv[1:-1], start=1):
                if tok == '-c':
                    body = argv[i + 1]
                    break
            if body:
                sub_f, sub_o = analyze_class_a(body, cfg, depth + 1)
                findings += sub_f
                if state['override'] is None:
                    state['override'] = sub_o
            continue
        if head == 'eval':
            sub_f, sub_o = analyze_class_a(' '.join(argv[1:]), cfg, depth + 1)
            findings += sub_f
            if state['override'] is None:
                state['override'] = sub_o
            continue
        # Basename the head so `/usr/bin/tail -f` still matches `^tail`.
        seg_str = ' '.join([head] + argv[1:])
        # Allowlist wins over the watch/follow registry: a segment matching
        # an exempt pattern is quieted (a repo silencing a false-positive
        # built-in without disabling all of Class A).
        if any(rx.search(seg_str) for rx in exempts):
            continue
        for label, rx, alt in matchers:
            if rx.search(seg_str):
                findings.append(finding_watch(cfg, seg_str, label, alt))
                break

    if state['loop'] and loop_sleep:
        # Loop-with-sleep: the canonical foreground poll. Any sleep duration
        # counts — the loop multiplies it. Subsumes the per-sleep findings.
        findings.append(finding_loop(cfg))
    else:
        # Chained repeat-with-sleep: a sleep sandwiched between commands is
        # a poll regardless of duration (`gh pr checks; sleep 5; gh pr
        # checks` waits below the bare-sleep floor but is still a poll).
        sandwich = any(
            k == 'sleep' and 'cmd' in seg_kinds[:i] and 'cmd' in seg_kinds[i + 1:]
            for i, k in enumerate(seg_kinds))
        if sandwich:
            findings.append(finding_sandwich(cfg))
        findings += sleep_findings

    return findings, state['override']


# ---------------------------------------------------------------------------
# Class B analysis
# ---------------------------------------------------------------------------

# Interpreter options that consume the following word, which is what tells an
# option's argument from the script. Measured against GNU bash 5.3.15: the
# argument is a separate token and mandatory, and a letter takes it wherever it
# sits in the cluster, so `bash -ox errexit gate.sh` runs `gate.sh` (Q149).
# There is no `--rcfile=path` form; bash rejects it as an invalid option.
SHELL_OPT_ARG_LETTERS = frozenset('coO')
SHELL_OPT_ARG_WORDS = frozenset({'--rcfile', '--init-file'})


def split_shell_options(argv):
    """Split an interpreter argv into the shell's own options and what follows.

    Returns `(opts, rest)`: `opts` pairs each option word with the separate
    argument it consumes, or None where it consumes none, and `rest` is the
    argv from the script word on. Reading stops at `--`, at `-`, and at the
    first non-option, so a `-n` past the script name belongs to the script.
    `+x` is an option too — bash accepts the `+` forms at invocation."""
    opts = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == '-' or tok == '--':
            i += 1
            break
        if len(tok) < 2 or tok[0] not in '-+':
            break
        if tok.startswith('--'):
            takes_arg = tok in SHELL_OPT_ARG_WORDS
        else:
            takes_arg = bool(SHELL_OPT_ARG_LETTERS & set(tok[1:]))
        if takes_arg and i + 1 < len(argv):
            opts.append((tok, argv[i + 1]))
            i += 2
        else:
            opts.append((tok, None))
            i += 1
    return opts, argv[i:]


def shell_noexec(opts):
    """True when the interpreter's own options carry noexec: the shell parses
    the script and executes nothing, so the segment can never be slow however
    it is registered. `opts` holds only the words ahead of the script or the
    `-c` body, so `bash gate.sh -n` — where `-n` belongs to the script — is
    still a real run. `-o noexec` counts (Q138); `+o noexec` and `+n` turn
    noexec back off, so those are real runs."""
    for opt, arg in opts:
        if opt[0] != '-' or opt.startswith('--'):
            continue
        if 'n' in opt[1:] or ('o' in opt[1:] and arg == 'noexec'):
            return True
    return False


def simple_commands(raw, depth=0):
    """Every simple command in `raw` as an argv, starting at its command word:
    heredoc bodies stripped, env prefixes and launcher wrappers peeled, `bash
    -c '...'` / `eval ...` bodies recursed into (bounded), a plain `bash
    script.sh` reduced to the script, and a parse-only `bash -n ...` dropped.
    Returns None when the string does not tokenize — the caller defers rather
    than guess."""
    if depth > 3:
        return []
    tokens = tokenize(strip_heredoc_bodies(raw))
    if tokens is None:
        return None
    out = []
    state = {'loop': False, 'override': None}
    for group, _term in split_segments(tokens):
        state['timeout_wrapped'] = False
        argv = strip_head(list(group), state)
        if not argv:
            continue
        if os.path.basename(argv[0]) in SHELL_NAMES:
            opts, rest = split_shell_options(argv)
            if shell_noexec(opts):
                continue  # parse-only: nothing under it runs
            body = None
            for opt, arg in opts:
                if arg is not None and opt.startswith('-') \
                        and not opt.startswith('--') and 'c' in opt[1:]:
                    body = arg
                    break
            if body is not None:
                sub = simple_commands(body, depth + 1)
                if sub is None:
                    return None
                out += sub
                continue
            argv = rest  # `bash scripts/gate.sh`: the script is the command
            if not argv:
                continue
        elif os.path.basename(argv[0]) == 'eval':
            sub = simple_commands(' '.join(argv[1:]), depth + 1)
            if sub is None:
                return None
            out += sub
            continue
        out.append(argv)
    return out


def runs_command(rx, argv):
    """True when `rx` matches at the command position of `argv` — the leftmost
    match has to begin inside the command word. That is what separates running
    a registered script from naming it: `scripts/gate.sh --all` matches,
    `grep -n foo scripts/gate.sh` does not."""
    m = rx.search(' '.join(argv))
    return m is not None and m.start() < len(argv[0])


def runs_target(name, glob_pat, argv):
    """True when `argv` runs command `name` with an argument word matching
    `glob_pat`. The command word is compared by basename and each argument is
    glob-matched as a WHOLE token, so a quoted argument that merely mentions
    the target (`make -n help NOTE="... e2e ..."`) can never fire (#21)."""
    return os.path.basename(argv[0]) == name and \
        any(fnmatch.fnmatchcase(a, glob_pat) for a in argv[1:])


def analyze_class_b(raw, cfg, timeout_ms, timeout_was_set):
    """Class B findings: registered slow command about to run with an
    inadequate foreground timeout. Two registration forms:

      * regex -> ms: matched at the command position of a simple command, so
        a pattern naming a script fires on the invocation and not on a
        `grep`/`wc`/commit message that merely mentions the path (#16);
        prefix a pattern with `.*` to match an argument instead — which puts
        the whole segment, quoted arguments included, in scope.
      * command -> {argument glob -> ms}: the hook does the anchoring itself —
        exact command word, whole-token glob per argument — so the common
        `make <target>` case needs no regex and cannot leak into a quoted
        argument (#21).

    The registry ships empty and is populated per-repo."""
    findings = []
    commands = simple_commands(raw)
    if commands is None:
        return findings  # unparseable: fail-open, defer
    for pat, spec in cfg['slow_commands'].items():
        if isinstance(spec, dict):
            for glob_pat, min_ms in spec.items():
                if timeout_ms >= min_ms:
                    continue
                if any(runs_target(pat, glob_pat, argv) for argv in commands):
                    findings.append(finding_slow(
                        cfg, 'the slow-command target `%s %s`'
                        % (pat, glob_pat), min_ms, timeout_ms,
                        timeout_was_set))
            continue
        min_ms = spec
        if timeout_ms >= min_ms:
            continue
        try:
            rx = re.compile(pat)
        except re.error:
            continue  # a broken pattern loses itself
        if any(runs_command(rx, argv) for argv in commands):
            findings.append(finding_slow(
                cfg, 'the slow-command pattern `%s`' % pat, min_ms,
                timeout_ms, timeout_was_set))
    return findings


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return  # unparseable input: fail-open
    if data.get('tool_name') != 'Bash':
        return
    tool_input = data.get('tool_input') or {}
    command = tool_input.get('command') or ''
    if not command.strip():
        return
    if os.environ.get('FOREGROUND_GUARD_DISABLE') == '1':
        return

    cfg = load_config()
    # Backgrounding answers Class B and only Class B (see below); Class A
    # findings read it to word themselves for a detached wait.
    cfg['backgrounded'] = tool_input.get('run_in_background') is True
    # Class A analysis always runs: it is also the only place the
    # FOREGROUND_GUARD_OVERRIDE=<reason> prefix is parsed, and the override
    # must still clear a Class B block when poll is disabled.
    findings, override = analyze_class_a(command, cfg)
    # The agent has asserted the wait is wanted, and a wrong assertion costs
    # waiting and nothing else. Defer rather than route it at a human: an
    # approved prompt would add a context switch and no information. Not an
    # allow — normal permissions and the sibling guards still see the call —
    # and the stated reason is on the command string, which the transcript
    # keeps whether or not this hook says anything.
    if override is not None:
        return
    if not cfg['poll_enabled']:
        findings = []

    # `run_in_background: true` is the fix Class B teaches, so a backgrounded
    # slow command passes untouched. A backgrounded poll does NOT pass: the
    # wait moves off the main thread but still holds a task slot, and the
    # agent still returns to a read whose freshness it cannot judge.
    if cfg['slow_enabled'] and cfg['slow_commands'] and not cfg['backgrounded']:
        timeout_ms = tool_input.get('timeout')
        timeout_was_set = isinstance(timeout_ms, (int, float)) \
            and not isinstance(timeout_ms, bool)
        if not timeout_was_set:
            try:
                timeout_ms = int(os.environ.get('BASH_DEFAULT_TIMEOUT_MS', ''))
            except ValueError:
                timeout_ms = DEFAULT_BASH_TIMEOUT_MS
        findings += analyze_class_b(command, cfg, int(timeout_ms),
                                    timeout_was_set)

    if not findings:
        return  # nothing to object to: defer to normal permissions

    severity = max(sev for sev, _ in findings)
    # De-duplicate reasons while keeping order; cap so the prompt stays
    # readable.
    reasons, seen = [], set()
    for sev, reason in sorted(findings, key=lambda f: -f[0]):
        if reason not in seen:
            seen.add(reason)
            reasons.append(reason)
    reason = ' | '.join(reasons[:3])

    mode = data.get('permission_mode') or ''
    decision = 'deny' if severity == DENY else 'ask'
    # Only a config-de-escalated `ask` reaches here, and an unattended mode is
    # where it cannot be answered: in `dontAsk` Claude Code turns it into its
    # own generic deny and the reason is lost, and in `bypassPermissions` it
    # stalls on a prompt with no one there. Deny instead: equally blocking, and
    # the reason is fed back so the agent self-corrects.
    if decision == 'ask' and mode in UNATTENDED_MODES:
        decision = 'deny'
    if decision == 'deny':
        reason += ' ' + deny_tail()
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': decision,
        'permissionDecisionReason': reason}}))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001 — fail-open on any infrastructure error
        if os.environ.get('FOREGROUND_GUARD_DEBUG') == '1':
            raise
        sys.exit(0)
