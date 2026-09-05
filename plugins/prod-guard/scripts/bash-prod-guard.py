#!/usr/bin/env python3
"""PreToolUse hook: block mutating commands aimed at production targets and
block mutating commands that rely on shared ambient context instead of
pinning their target.

Two distinct threat models, kept distinct in the code:

1. **Prod blast-radius** — a mutating verb (`kubectl delete`, `terraform
   destroy`, `gcloud ... update`) whose *resolved* target is a production
   environment. Explicit targeting does not prevent this — `kubectl --context
   prod delete ns x` is explicit AND catastrophic — so the guard classifies
   the resolved target against a configurable denylist and hard-blocks
   (`deny`) mutations that land on prod.

2. **Ambient-context clobbering** — a mutating command that relies on shared
   mutable state (the kubeconfig current-context, the active gcloud config,
   the docker context, the default aws profile) instead of pinning its
   target. A parallel session can repoint that state between the moment the
   command is written and the moment it runs, silently redirecting the
   mutation. The guard denies (`deny`) with a fix-it that tells the caller to
   pin the target with the tool's flag and retry.

Decision semantics (per simple command; worst verdict wins across a chain):

  * read-only verb, uncovered tool, or non-prod explicit target -> **defer**
    (no output; normal permission settings apply — this guard never emits
    `allow`, so it composes with other guards instead of overriding them).
  * mutating verb + target classified PROD                      -> **deny**,
    downgradable to `ask` with a `PROD_GUARD_OVERRIDE=<reason>` prefix — or
    with `PROD_GUARD_SESSION_OVERRIDE=<reason>`, which additionally records a
    session-scoped grant for the explicit prod target(s) once the human
    approves (detected by the PostToolUse hook: it only fires if the command
    ran). Later prefixed commands against granted targets in the same session
    defer silently; unprefixed commands, other targets, other sessions, and
    expired grants (8 h TTL) still deny/ask as usual.
  * mutating verb + explicit target that matches no pattern     -> **ask**
    (fail CLOSED on the security decision: unknown is never silently allowed).
  * mutating verb + no explicit target (ambient context)        -> **deny**
    with a self-healing fix-it (add the pin flag and retry): the ambient
    target is clobber-prone shared state, so the command cannot run until the
    target is explicit. Downgradable to `ask` with `PROD_GUARD_OVERRIDE`.
  * target-repointing switch commands (`kubectl config use-context`,
    `kubectx`, `gcloud config set`, `az account set`, `docker context use`)
    -> **deny** regardless of the new target's classification: they repoint
    the very shared state threat model 2 is about for *every* parallel
    session, and each has a per-command pin flag that makes the switch
    unnecessary. The fix-it names that flag; `PROD_GUARD_OVERRIDE` downgrades
    to ask when a switch is genuinely intended.
  * other shared-state writers -> **ask** (deny when a named target
    classifies PROD): credential logins and config edits with no per-command
    pin alternative (`gcloud auth`, `oc login`, `aws configure`, kubeconfig
    edits), and the namespace switches (`kubens`, `oc project`) whose clobber
    blast radius stays inside an already-guarded cluster.

Fail modes: fail OPEN on infrastructure errors (unparseable input, missing
config, unexpected exceptions — the hook stays silent and never breaks the
session), fail CLOSED on the security decision (unknown target + mutating
verb prompts). When unsure whether a verb mutates, it is treated as mutating:
false negatives (an unrecognized destructive form) are the failure mode to
fear; a false positive costs one keystroke.

Target classification is pure local string work: the guard parses the flag or
reads the tool's local config file (kubeconfig current-context, gcloud active
config, docker config.json, azure profile). It never makes a network or
cluster call.

Reads the hook JSON on stdin, emits a PreToolUse decision on stdout.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

# The parsing primitives every claude-bouncer guard shares. The copy under this
# plugin's `lib/` is vendored from the repository root; see scripts/sync-lib.py.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
from bouncer_parse import ASSIGNMENT_RE, PUNCT_CHARS, strip_heredoc_bodies  # noqa: E402
from bouncer_grants import grants_path, load_grants, record_grants  # noqa: E402
import time

# ---------------------------------------------------------------------------
# Target classification: PROD / NONPROD / UNKNOWN
# ---------------------------------------------------------------------------

# Built-in production patterns. A target matching any of these is PROD.
# Word-bounded so `prod` matches `gke_acme_prod-us` and `my-prod-cluster`
# but not `product-demo`... it DOES match `product` ("prod" + "uct" is
# blocked by the boundary? no — see the optional `(uction)?` group); the
# bias is deliberate: over-matching prod costs a prompt, under-matching
# costs an outage.
BUILTIN_PROD = [
    r'(^|[^a-z0-9])prod(uction)?([^a-z0-9]|$)',
    r'(^|[^a-z0-9])prd([^a-z0-9]|$)',
    r'(^|[^a-z0-9])live([^a-z0-9]|$)',
]

# Built-in non-production patterns: local cluster providers, loopback
# registries, and the common environment names. Checked after the built-in
# prod list, so a name matching both built-in lists (e.g. `prod-staging-mirror`)
# classifies PROD — but see classify(): a *config* nonprod entry outranks these
# built-in prod patterns, which is the escape hatch for heuristic false
# positives.
BUILTIN_NONPROD = [
    r'^kind-', r'^k3d-', r'minikube', r'docker-desktop', r'desktop-linux',
    r'colima', r'rancher-desktop', r'orbstack',
    r'localhost', r'127\.0\.0\.1', r'\[::1\]',
    r'(^|[^a-z0-9])(dev|develop|development|test|testing|qa|e2e|ci|sandbox|sbx'
    r'|stage|staging|stg|local|demo|lab|preview|scratch)([^a-z0-9]|$)',
]

_PATTERNS = None  # (cfg_prod, cfg_nonprod, builtin_prod, builtin_nonprod), lazy


def _config_paths():
    """Candidate config files, later entries win nothing — all are additive."""
    paths = []
    home = os.environ.get('HOME')
    if home:
        paths.append(os.path.join(home, '.claude', 'prod-guard.json'))
    proj = os.environ.get('CLAUDE_PROJECT_DIR')
    if proj:
        paths.append(os.path.join(proj, '.claude', 'prod-guard.json'))
    extra = os.environ.get('PROD_GUARD_CONFIG')
    if extra:
        paths.append(extra)
    return paths


def _load_config_file(path):
    """Read {"prod": [...], "nonprod": [...]} from a JSON config file.

    Any error (missing file, bad JSON, wrong shape) loses only that source:
    fail-open on config infrastructure, but the built-in patterns still apply
    so the boundary never disappears because of a typo.
    """
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        prod = [p for p in data.get('prod', []) if isinstance(p, str)]
        nonprod = [p for p in data.get('nonprod', []) if isinstance(p, str)]
        return prod, nonprod
    except (OSError, ValueError, AttributeError):
        return [], []


def _split_pattern_list(raw):
    """Split an env-var pattern list on `;` or newlines (regexes may contain
    `,` and `:`, the separators sibling guards use for path lists)."""
    return [p.strip() for p in re.split(r'[;\n]', raw or '') if p.strip()]


def _compile(patterns):
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error:
            continue  # a broken pattern loses itself, not the whole list
    return out


def load_patterns():
    """Compile built-ins and config (user file + project file +
    PROD_GUARD_CONFIG file + PROD_GUARD_{PROD,NONPROD}_PATTERNS env vars) into
    four provenance-tagged lists: (cfg_prod, cfg_nonprod, builtin_prod,
    builtin_nonprod).

    Config is additive to the *set* of patterns — it can only add regexes,
    never delete a built-in — but it carries higher precedence than the
    built-ins in classify(): a human-vetted config `nonprod` entry can clear a
    built-in prod heuristic (see classify()). Narrowing the boundary still
    requires an explicit, reviewable config line — never a silent code
    default."""
    global _PATTERNS
    if _PATTERNS is not None:
        return _PATTERNS
    cfg_prod, cfg_nonprod = [], []
    for path in _config_paths():
        p, n = _load_config_file(path)
        cfg_prod += p
        cfg_nonprod += n
    cfg_prod += _split_pattern_list(os.environ.get('PROD_GUARD_PROD_PATTERNS'))
    cfg_nonprod += _split_pattern_list(os.environ.get('PROD_GUARD_NONPROD_PATTERNS'))
    _PATTERNS = (
        _compile(cfg_prod), _compile(cfg_nonprod),
        _compile(BUILTIN_PROD), _compile(BUILTIN_NONPROD),
    )
    return _PATTERNS


_TRUTHY = frozenset({'1', 'true', 'yes', 'on'})


def gh_strict_enabled(seg_env):
    """True when gh's collaboration-metadata tier is opted out — every mutating
    gh verb is prod-classified (the pre-#18 behavior), for issue-ops shops that
    wire issue/PR comments to deployments. Turned on by a truthy
    PROD_GUARD_GH_STRICT (segment env then process env) or `"gh_strict": true`
    in any prod-guard.json config file. Strengthening-only: any source can turn
    it on and none can turn it off, matching the additive config philosophy
    (loosening the boundary must be a code change, not config drift)."""
    val = seg_env.get('PROD_GUARD_GH_STRICT') or os.environ.get('PROD_GUARD_GH_STRICT')
    if val and val.strip().lower() in _TRUTHY:
        return True
    for path in _config_paths():
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue  # fail OPEN on a bad/missing config; the tier stays on
        if isinstance(data, dict) and data.get('gh_strict') is True:
            return True
    return False


def classify(value):
    """'prod' | 'nonprod' | 'unknown' for a target string (context name,
    project id, profile, subscription, registry ref, repo slug...).

    Precedence, strictest-authority first — config beats built-ins, and within
    each provenance tier prod beats nonprod:

      1. config  prod    -> prod   (an explicit denylist entry wins outright)
      2. config  nonprod -> nonprod (a human-vetted allowlist entry can clear a
                                     built-in prod heuristic — the config
                                     escape hatch for false positives like a
                                     repo slug that merely contains 'prod')
      3. builtin prod    -> prod   (word-bounded prod/prd/live heuristic)
      4. builtin nonprod -> nonprod

    Fail-closed ordering is preserved *within* each tier (prod checked before
    nonprod), so an ambiguous name still fails toward the stricter class among
    equally-authoritative patterns. No match at all is 'unknown' — which the
    policy treats as ask, never allow (deny-on-unknown / fail-closed)."""
    if not value:
        return 'unknown'
    cfg_prod, cfg_nonprod, builtin_prod, builtin_nonprod = load_patterns()
    for rx in cfg_prod:
        if rx.search(value):
            return 'prod'
    for rx in cfg_nonprod:
        if rx.search(value):
            return 'nonprod'
    for rx in builtin_prod:
        if rx.search(value):
            return 'prod'
    for rx in builtin_nonprod:
        if rx.search(value):
            return 'nonprod'
    return 'unknown'


# ---------------------------------------------------------------------------
# Shell parsing: raw command string -> simple commands
# ---------------------------------------------------------------------------

# Shells whose `-c <string>` argument is itself a command to evaluate.
SHELL_NAMES = frozenset({'bash', 'sh', 'zsh', 'dash', 'ksh'})

# Wrappers that prefix another command without changing its meaning for our
# purposes. `xargs` is handled separately (it takes its own flags).
PLAIN_WRAPPERS = frozenset({'command', 'nohup', 'time', 'builtin', 'exec'})

# Wrapper flags that take a SEPARATE value. Dropped one token at a time, the
# value is left behind and reads as the tool, so the segment defers (Q152).
WRAPPER_VALUE_FLAGS = {
    # sudo's `-h` and `-U` are held out: Q158 and Q157.
    'sudo': frozenset({'-C', '--close-from', '-D', '--chdir', '-g', '--group',
                       '-p', '--prompt', '-R', '--chroot',
                       '-T', '--command-timeout', '-u', '--user'}),
    'env': frozenset({'-u', '--unset', '-C', '--chdir', '-P'}),  # -P is BSD's
    'timeout': frozenset({'-k', '--kill-after', '-s', '--signal'}),
    'exec': frozenset({'-a'}),
    'time': frozenset({'-o', '--output', '-f', '--format'}),  # -o both, rest GNU
}


def heredoc_openers(line):
    """Heredoc delimiters opened on one line, in the order the bodies follow:
    a list of (delimiter, quoted, strip_tabs). Only `<<` outside quotes counts
    — inside quotes it is ordinary text — and `<<<` is a here-string, not a
    heredoc. `quoted` is true when the delimiter word carried any quoting
    (`<<'EOF'`, `<<"EOF"`, `<<\\EOF`), which is what makes the body literal."""
    out = []
    i, n = 0, len(line)
    quote = None
    while i < n:
        c = line[i]
        if quote is not None:
            if c == '\\' and quote == '"' and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c == '\\':
            i += 2
            continue
        if c in '\'"':
            quote = c
            i += 1
            continue
        if not line.startswith('<<', i) or line.startswith('<<<', i):
            i += 1
            continue
        i += 2
        strip_tabs = i < n and line[i] == '-'
        if strip_tabs:
            i += 1
        while i < n and line[i] in ' \t':
            i += 1
        word, quoted = [], False
        while i < n:
            c = line[i]
            if c == '\\' and i + 1 < n:
                quoted = True
                word.append(line[i + 1])
                i += 2
            elif c in '\'"':
                quoted = True
                q, i = c, i + 1
                while i < n and line[i] != q:
                    word.append(line[i])
                    i += 1
                i += 1
            elif c in ' \t<>|&;()':
                break
            else:
                word.append(c)
                i += 1
        if word:
            out.append((''.join(word), quoted, strip_tabs))
    return out


def strip_quoted_heredocs(raw):
    """Drop the bodies of heredocs whose delimiter is quoted (`<<'EOF'`).

    The shell performs no expansion and runs nothing from such a body — it is
    literal text handed to the command's stdin — so scanning it for commands
    only ever produces false positives. Prose that quotes a command (a commit
    message, a doc edit, a `python3 - <<'PY'` script) is the common case.

    This is the one place that removes segments from inspection, so it stays
    tight: only a quoted delimiter strips anything, the closing delimiter must
    actually appear (an unterminated heredoc leaves the text in place), and a
    body is never scanned for further heredoc openers. Unquoted `<<EOF` does
    expand and keeps its current treatment."""
    if '<<' not in raw:
        return raw
    lines = raw.split('\n')
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for delim, quoted, strip_tabs in heredoc_openers(line):
            j = i
            while j < len(lines):
                probe = lines[j].lstrip('\t') if strip_tabs else lines[j]
                if probe == delim:
                    break
                j += 1
            if j == len(lines):
                break  # no terminator: not a heredoc we understand, keep it all
            if not quoted:
                out.extend(lines[i:j])
            out.append(lines[j])
            i = j + 1
    return '\n'.join(out)


def tokenize(raw):
    """shlex-tokenize with POSIX quoting and punctuation grouping. Backticks
    and newlines are rewritten to `;` first: a backtick-substituted command
    must split into its own simple command instead of hiding inside a word,
    and shlex treats a newline as plain whitespace, which would merge
    `true\ncmd` into one segment (`$(...)` needs no rewrite — `(` / `)` are
    punctuation chars and act as separators). Rewriting inside quotes only
    ever creates extra segments to inspect, never hides one. Returns None on
    unbalanced quotes (caller defers: fail-open on parse errors)."""
    expanded = []
    raw = strip_heredoc_bodies(raw, expanded, expanded)
    raw = raw.replace('`', ';').replace('\n', ';')
    try:
        tokens = _lex_semicolons(raw)
        # Every body is stripped above, so the ones this guard still has to see
        # come back through the out-lists and are appended as their own
        # segments: the ones bash would EXPAND, and the ones whose terminator
        # never appeared. Dropping either would hide a `kubectl delete` written
        # inside a heredoc -- a false negative, the one direction this guard
        # must not move in, and an unterminated body is malformed input, where
        # judging the text beats trusting the friendlier reading. They are
        # appended rather than left inline because a body is data: an
        # apostrophe in one would otherwise open a quote for the rest of the
        # command.
        for body in expanded:
            tokens.append(';')
            tokens.extend(_lex_semicolons(body.replace('`', ';').replace('\n', ';')))
        return tokens
    except ValueError:
        return None


class QuotedStr(str):
    """A token, carrying the quote characters it was built from.

    posix-mode shlex strips quotes, so `'kubectl --context $C'` and
    `"kubectl --context $C"` come back as the same string -- and for a shell's
    `-c` body those two mean opposite things about who expands `$C`. Subclassing
    `str` keeps every existing token consumer working unchanged; only the caller
    that needs the quoting reads `.quotes`."""
    quotes = frozenset()


class _QuoteTrackingLexer(shlex.shlex):
    """shlex that records which quote characters each token was built from.

    `read_token` assigns the quote character to `self.state` on entering a
    quoted run and leaves it on exit, so recording every assignment across one
    call names that token's quoting. A token built from no quoted run at all
    gets the empty set."""

    def __init__(self, *args, **kwargs):
        self._seen_quotes = set()
        super().__init__(*args, **kwargs)

    def _get_state(self):
        return self._state

    def _set_state(self, value):
        # `state` also takes 'a', 'c', ' ' and None (end of file); only a quote
        # character says anything about how the token was written.
        if value and value in getattr(self, 'quotes', ''):
            self._seen_quotes.add(value)
        self._state = value

    state = property(_get_state, _set_state)

    def read_token(self):
        self._seen_quotes = set()
        token = super().read_token()
        if token is None or token is self.eof:
            return token
        out = QuotedStr(token)
        out.quotes = frozenset(self._seen_quotes)
        return out


def _lex_semicolons(raw):
    lex = _QuoteTrackingLexer(raw, posix=True, punctuation_chars=';()<>|&\n')
    lex.whitespace_split = True
    return list(lex)


def split_simple_commands(tokens):
    """Split a token stream into simple commands on every operator token.

    Any all-punctuation token — `&&`, `|`, `;`, `(`, `)`, and also redirects
    like `>` — ends the current simple command. Treating a redirect as a hard
    break is deliberately crude: the redirect target becomes a one-token
    "command" that matches no covered tool and is ignored, while a covered
    tool AFTER any operator still gets its own segment. Crude splitting only
    ever creates extra segments to inspect (false-positive direction), never
    hides one (false-negative direction)."""
    groups, cur = [], []
    for t in tokens:
        if t and all(c in PUNCT_CHARS for c in t):
            if cur:
                groups.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        groups.append(cur)
    return groups


def extract_env_prefix(argv):
    """Peel leading NAME=VALUE assignments off a simple command. Returns
    (env_dict, remaining_argv). The assignments matter twice over: they can
    pin a target (AWS_PROFILE=dev, TF_WORKSPACE=prod) and they can carry the
    PROD_GUARD_OVERRIDE escape hatch."""
    env = {}
    i = 0
    while i < len(argv) and ASSIGNMENT_RE.match(argv[i]):
        name, _, value = argv[i].partition('=')
        env[name] = value
        i += 1
    return env, argv[i:]


# Only a bare `$NAME` or `${NAME}` reference is expanded. Every other `$` form
# is deliberately NOT matched, so it is left literal and the target stays
# UNKNOWN (→ prompt) rather than being silently mis-expanded: command
# substitution `$(...)`, arithmetic `$((...))`, all `${...}` operator forms
# (`${V:-x}`, `${#V}`, `${!V}`, …) because the `{NAME}` branch requires the `}`
# immediately after the name, and positional/special params (`$1`, `$@`, `$$`,
# `$?`) because the bare branch requires a letter/underscore right after `$`.
VAR_REF_RE = re.compile(r'\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))')


def expand_vars(value, env):
    """Expand simple `$NAME` / `${NAME}` references in `value` against `env`.

    Conservative by construction: an undefined name is left LITERAL (`$CTX`
    stays `$CTX`) rather than blanked, so an unresolvable target stays UNKNOWN
    and still prompts — expansion only ever makes classification *more*
    specific, never silently allows. `env` values are assumed already resolved
    (the caller folds them left-to-right), so one pass suffices."""
    def repl(m):
        name = m.group(1) or m.group(2)
        return env[name] if name in env else m.group(0)
    return VAR_REF_RE.sub(repl, value)


def resolve_assignments(pairs, base_env):
    """Fold an ordered iterable of (name, raw_value) onto a copy of `base_env`,
    expanding each value against the env accumulated so far — left-to-right, as
    the shell does, so `CTX=gke_${P}_${Z}` resolves against earlier
    assignments. Returns the new env dict."""
    env = dict(base_env)
    for name, value in pairs:
        env[name] = expand_vars(value, env)
    return env


# Builtins that carry persisting `NAME=val` operands (`export FOO=bar`).
ASSIGN_BUILTINS = frozenset({'export', 'declare', 'typeset', 'readonly', 'local'})


def expand_argv(argv, env):
    """Expand simple variable references in every token of `argv`."""
    return [expand_vars(tok, env) for tok in argv]


def is_command_lookup(operands):
    """Whether `command`'s flags make this a lookup rather than an invocation.

    `-v` and `-V` both report where a name resolves and execute nothing. Flags
    may be bundled (`-pv`), and `--` ends them, after which a `-v` is an operand
    of whatever runs."""
    for tok in operands:
        if not tok.startswith('-') or tok in ('-', '--'):
            return False
        if 'v' in tok[1:] or 'V' in tok[1:]:
            return True
    return False


def strip_wrappers(argv, env):
    """Remove leading launcher commands (sudo, env, timeout, xargs, ...) so
    the covered tool underneath is classified, not the wrapper. `env`
    assignments found behind `env`/`sudo` merge into the segment env."""
    while argv:
        head = os.path.basename(argv[0])
        value_flags = WRAPPER_VALUE_FLAGS.get(head, frozenset())
        if head == 'sudo':
            argv = argv[1:]
            while argv and argv[0].startswith('-'):
                argv = argv[2:] if argv[0] in value_flags else argv[1:]
        elif head == 'env':
            argv = argv[1:]
            while argv:
                if argv[0].startswith('-'):
                    argv = argv[2:] if argv[0] in value_flags else argv[1:]
                elif ASSIGNMENT_RE.match(argv[0]):
                    name, _, value = argv[0].partition('=')
                    env[name] = value
                    argv = argv[1:]
                else:
                    break
        elif head == 'timeout':
            argv = argv[1:]
            while argv and argv[0].startswith('-'):
                argv = argv[2:] if argv[0] in value_flags else argv[1:]
            if argv:
                argv = argv[1:]  # the DURATION operand
        elif head == 'xargs':
            # Skip xargs and everything up to the first covered tool; if none
            # appears the segment holds nothing we guard.
            rest = argv[1:]
            for i, t in enumerate(rest):
                if os.path.basename(t) in COVERED_TOOLS or os.path.basename(t) in SHELL_NAMES:
                    return rest[i:]
            return []
        elif head in PLAIN_WRAPPERS:
            # `command -v`/`-V` reports where a name resolves and runs
            # nothing, so its operands are names rather than a command to
            # guard. Stripped like a wrapper, the first operand becomes the
            # tool and the rest its verb: `command -v kubectl kubeconform`
            # reads as `kubectl kubeconform`, a verb in no read-only table and
            # so mutating (Q142). Leaving `command` in place defers instead.
            # `command` is the only one of these wrappers with a flag that
            # means "do not run"; a `-v` elsewhere still runs the command.
            if head == 'command' and is_command_lookup(argv[1:]):
                break
            argv = argv[1:]
            while argv and argv[0].startswith('-'):
                argv = argv[2:] if argv[0] in value_flags else argv[1:]
        else:
            break
    return argv


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def flag_values(argv, names):
    """Collect values for the given long/short flags, handling both
    `--flag value` and `--flag=value`. Returns {flag_name: value}."""
    out = {}
    nameset = set(names)
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in nameset and i + 1 < len(argv):
            out[tok] = argv[i + 1]
            i += 2
            continue
        if '=' in tok:
            head, _, val = tok.partition('=')
            if head in nameset:
                out[head] = val
        i += 1
    return out


def first_flag_value(argv, names):
    vals = flag_values(argv, names)
    for n in names:
        if n in vals:
            return vals[n]
    return None


def all_flag_values(argv, names):
    """Every value of the given flags, in order — for repeatable flags like
    docker's `-t` (flag_values keeps only the last occurrence per flag)."""
    out = []
    nameset = set(names)
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in nameset and i + 1 < len(argv):
            out.append(argv[i + 1])
            i += 2
            continue
        if '=' in tok:
            head, _, val = tok.partition('=')
            if head in nameset:
                out.append(val)
        i += 1
    return out


def words_of(argv, value_flags=()):
    """Non-flag tokens of a simple command, with the values of known
    value-taking flags removed so `-n foo` doesn't surface `foo` as a verb.
    Unknown value-taking flags leak their value into the word list; that only
    ever errs toward 'mutating' (the conservative direction)."""
    vf = set(value_flags)
    out = []
    i = 1  # skip argv[0], the tool itself
    while i < len(argv):
        tok = argv[i]
        if tok in vf:
            i += 2
            continue
        if tok.startswith('-'):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


# ---------------------------------------------------------------------------
# Ambient context readers — pure local file reads, no subprocesses except a
# single `git config` lookup for gh (local, no network), no cluster calls.
# ---------------------------------------------------------------------------

def _kubeconfig_paths(seg_env):
    """Every kubeconfig file to consult: all `:`-separated $KUBECONFIG paths
    (kubectl merges them), else ~/.kube/config. Empty if $HOME is unset and no
    $KUBECONFIG is given."""
    kc = seg_env.get('KUBECONFIG') or os.environ.get('KUBECONFIG')
    if kc:
        return [p for p in kc.split(':') if p]
    home = os.environ.get('HOME')
    if home:
        return [os.path.join(home, '.kube', 'config')]
    return []


def kube_current_context(seg_env):
    """current-context from $KUBECONFIG (first path) or ~/.kube/config."""
    paths = _kubeconfig_paths(seg_env)
    if not paths:
        return None
    try:
        with open(paths[0], encoding='utf-8') as f:
            for line in f:
                m = re.match(r'^current-context:\s*["\']?([^"\'\n#]+)', line)
                if m:
                    return m.group(1).strip() or None
    except OSError:
        return None
    return None


def _kube_yaml_field(item_lines, key):
    """First inline `key: value` in a kubeconfig list item, quotes stripped.
    Block keys (`cluster:` with no inline value, i.e. a nested map) don't
    match, so the same key name resolves to the scalar field, never the map."""
    rx = re.compile(r'^\s*(?:-\s+)?' + re.escape(key) + r':\s*(.+?)\s*$')
    for line in item_lines:
        m = rx.match(line)
        if m:
            return m.group(1).strip('"\'') or None
    return None


def _parse_kubeconfig(path):
    """(ctx_to_cluster, cluster_to_server) from one kubeconfig file.

    Line-oriented parse of the block-style subset `kubectl config` writes:
    track the top-level `clusters:` / `contexts:` section, split each into list
    items on `- ` lines, and read the scalar fields from each item. Anything it
    can't parse (flow style, unreadable file) yields empty maps — the caller
    falls back to name-only classification, so this only ever *adds* a chance
    to classify, never removes one."""
    ctx_to_cluster, cluster_to_server = {}, {}
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return ctx_to_cluster, cluster_to_server

    section = None
    item = []

    def flush():
        if not item:
            return
        name = _kube_yaml_field(item, 'name')
        if not name:
            return
        if section == 'clusters':
            server = _kube_yaml_field(item, 'server')
            if server and name not in cluster_to_server:  # first-wins merge
                cluster_to_server[name] = server
        elif section == 'contexts':
            cluster = _kube_yaml_field(item, 'cluster')
            if cluster and name not in ctx_to_cluster:
                ctx_to_cluster[name] = cluster

    for line in lines:
        top = re.match(r'^([A-Za-z0-9_-]+):', line)
        if top:  # a new top-level key ends the current section/item
            flush()
            item = []
            section = top.group(1) if top.group(1) in ('clusters', 'contexts') else None
            continue
        if section is None:
            continue
        if re.match(r'^\s*-\s', line) or re.match(r'^\s*-\s*$', line):
            flush()
            item = [line]
        elif item:
            item.append(line)
    flush()
    return ctx_to_cluster, cluster_to_server


def kube_context_server(name, seg_env):
    """Resolve a context name to its cluster's server URL, merging every
    kubeconfig file. None if the context, its cluster, or the server can't be
    resolved (parse failure, split-across-unread-files, etc.)."""
    if not name:
        return None
    ctx_to_cluster, cluster_to_server = {}, {}
    for path in _kubeconfig_paths(seg_env):
        c2c, c2s = _parse_kubeconfig(path)
        for k, v in c2c.items():
            ctx_to_cluster.setdefault(k, v)
        for k, v in c2s.items():
            cluster_to_server.setdefault(k, v)
    cluster = ctx_to_cluster.get(name)
    if not cluster:
        return None
    return cluster_to_server.get(cluster)


def classify_kube(name, seg_env):
    """Classify a kube-context by the worst (most prod-leaning) of its name
    and its cluster's server URL, on the prod > nonprod > unknown lattice.
    Additive to name-only classify(): a prod server upgrades an innocuously
    named context to prod; an unresolvable server leaves the name verdict
    untouched."""
    name_cls = classify(name)
    if name_cls == 'prod':
        return 'prod'  # prod wins; no need to resolve the server
    server_cls = classify(kube_context_server(name, seg_env))
    if server_cls == 'prod':
        return 'prod'
    if name_cls == 'nonprod' or server_cls == 'nonprod':
        return 'nonprod'
    return 'unknown'


def gcloud_active_project():
    """Project of the active gcloud configuration, read from the local config
    files (never `gcloud config list`, which is slow and can hit the net)."""
    cfg_dir = os.environ.get('CLOUDSDK_CONFIG')
    if not cfg_dir:
        home = os.environ.get('HOME')
        if not home:
            return None
        cfg_dir = os.path.join(home, '.config', 'gcloud')
    name = os.environ.get('CLOUDSDK_ACTIVE_CONFIG_NAME')
    if not name:
        try:
            with open(os.path.join(cfg_dir, 'active_config'), encoding='utf-8') as f:
                name = f.read().strip()
        except OSError:
            name = 'default'
    try:
        with open(os.path.join(cfg_dir, 'configurations', 'config_' + name),
                  encoding='utf-8') as f:
            for line in f:
                m = re.match(r'^\s*project\s*=\s*(\S+)', line)
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def docker_current_context():
    """currentContext from ~/.docker/config.json (or $DOCKER_CONFIG)."""
    cfg_dir = os.environ.get('DOCKER_CONFIG')
    if not cfg_dir:
        home = os.environ.get('HOME')
        if not home:
            return None
        cfg_dir = os.path.join(home, '.docker')
    try:
        with open(os.path.join(cfg_dir, 'config.json'), encoding='utf-8') as f:
            return json.load(f).get('currentContext')
    except (OSError, ValueError):
        return None


def argocd_current_context():
    """current-context from the argocd CLI config."""
    home = os.environ.get('HOME')
    if not home:
        return None
    path = os.path.join(home, '.config', 'argocd', 'config')
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                m = re.match(r'^current-context:\s*["\']?([^"\'\n#]+)', line)
                if m:
                    return m.group(1).strip() or None
    except OSError:
        return None
    return None


def az_default_subscription():
    """Name of the default subscription in ~/.azure/azureProfile.json.
    Azure CLI writes the file with a UTF-8 BOM, hence utf-8-sig."""
    home = os.environ.get('HOME')
    if not home:
        return None
    path = os.path.join(home, '.azure', 'azureProfile.json')
    try:
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
        for sub in data.get('subscriptions', []):
            if sub.get('isDefault'):
                return sub.get('name') or sub.get('id')
    except (OSError, ValueError, AttributeError):
        return None
    return None


# Fields of an aws profile / sso-session that name *where* it reaches and are
# safe to echo in a decision message — never a credential. Ordered by display
# preference so the first present one is the most identifying.
_AWS_ECHOABLE_KEYS = (
    'sso_start_url', 'role_arn', 'sso_session', 'sso_account_id',
    'sso_role_name', 'source_profile', 'sso_region', 'region',
)


def _parse_ini_sections(path):
    """`{section_name: {lower_key: value}}` from an INI file (the AWS config
    shape). Full-line `#`/`;` comments skipped; inline comments are left in the
    value (AWS does not strip them). Unreadable/missing -> {} (fail OPEN)."""
    sections = {}
    try:
        with open(path, encoding='utf-8') as f:
            cur = None
            for line in f:
                s = line.strip()
                if not s or s.startswith('#') or s.startswith(';'):
                    continue
                if s.startswith('[') and s.endswith(']'):
                    cur = s[1:-1].strip()
                    sections.setdefault(cur, {})
                    continue
                if cur is not None and '=' in line:
                    key, _, val = line.partition('=')
                    sections[cur][key.strip().lower()] = val.strip()
    except OSError:
        return {}
    return sections


def aws_profile_fields(name, seg_env):
    """(named, extra) classifiable strings from a profile section in
    ~/.aws/config (or $AWS_CONFIG_FILE), following an `sso_session` reference
    into its `[sso-session NAME]` block.

    The section is `[default]` when name == 'default', else `[profile NAME]`
    (the AWS config convention: only the default profile is bare).

    - named = echoable identifiers (sso_start_url/role_arn/...), in display
      preference order so named[0] is the most identifying present field.
    - extra = every other key's value (classify-only, never echoed — a
      credential_process command may live here).

    None when the file or the profile's section is missing/unreadable, i.e.
    there is no profile to resolve (fail OPEN -> caller's generic ask)."""
    path = seg_env.get('AWS_CONFIG_FILE') or os.environ.get('AWS_CONFIG_FILE')
    if not path:
        home = os.environ.get('HOME')
        if not home:
            return None
        path = os.path.join(home, '.aws', 'config')
    sections = _parse_ini_sections(path)
    section = 'default' if name == 'default' else 'profile ' + name
    profile = sections.get(section)
    if profile is None:
        return None
    merged = dict(profile)
    sso = profile.get('sso_session')
    if sso:
        merged.update(sections.get('sso-session ' + sso, {}))
    named = [merged[k] for k in _AWS_ECHOABLE_KEYS if merged.get(k)]
    extra = [v for k, v in merged.items()
             if k not in _AWS_ECHOABLE_KEYS and v]
    return named, extra


def aws_default_profile(seg_env):
    """(named, extra) for the ambient `[default]` profile — see
    aws_profile_fields. None when there is no default profile to resolve."""
    return aws_profile_fields('default', seg_env)


def aws_named_profile_prod(name, seg_env):
    """Deny description if an explicit `--profile NAME`'s `[profile NAME]`
    section resolves a prod account (a prod sso_start_url/role_arn/... echoes
    that field; a prod value in any other key denies without echo), else None.

    Additive over name-only classification: only a prod signal turns the
    explicit-profile ask into a deny; a nonprod/unknown/unresolvable profile
    resolves nothing here and the caller keeps its by-name verdict."""
    resolved = aws_profile_fields(name, seg_env)
    if resolved is None:
        return None
    named, extra = resolved
    for v in named:
        if classify(v) == 'prod':
            return "aws profile '%s' (%s)" % (name, v)
    for v in extra:
        if classify(v) == 'prod':
            return "aws profile '%s'" % name
    return None


def git_remote_url(cwd):
    """origin URL of the repo at cwd — gh's implied target. A local `git
    config` read (worktree-aware), never a network call."""
    try:
        r = subprocess.run(
            ['git', '-C', cwd or '.', 'config', '--get', 'remote.origin.url'],
            capture_output=True, text=True, timeout=3, check=False)
        url = r.stdout.strip()
        return url or None
    except (OSError, subprocess.SubprocessError):
        return None


def terraform_workspace(cwd):
    """Workspace pinned by the working directory's .terraform/environment
    file (what `terraform workspace select` writes). Absent -> None."""
    try:
        with open(os.path.join(cwd or '.', '.terraform', 'environment'),
                  encoding='utf-8') as f:
            ws = f.read().strip()
            return ws or None
    except OSError:
        return None


# Well-known "where the state lives" fields per backend type. These name the
# remote state location (a bucket/key/org/workspace) and are safe to echo in a
# decision message — never a credential.
_TF_BACKEND_FIELDS = {
    's3': ('bucket', 'key'),
    'gcs': ('bucket', 'prefix'),
    'azurerm': ('storage_account_name', 'container_name', 'key'),
    'oss': ('bucket', 'prefix'),   # alibaba
    'cos': ('bucket', 'prefix'),   # tencent
}


def terraform_backend(cwd):
    """The remote state location `terraform init` recorded in
    .terraform/terraform.tfstate, as (type, named, extra):

    - named  = echoable state-location strings (bucket/key/org/workspace) for
      the known backend types.
    - extra  = every string leaf of an unknown/future backend's config
      (classify-only; never echoed, since it may include a credential).

    None when there is no initialized remote backend: missing/unreadable file,
    malformed JSON, no `backend` object, or a `local` backend. Fail OPEN."""
    path = os.path.join(cwd or '.', '.terraform', 'terraform.tfstate')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    be = data.get('backend')
    if not isinstance(be, dict):
        return None
    btype = be.get('type')
    cfg = be.get('config')
    if not isinstance(btype, str) or not isinstance(cfg, dict):
        return None
    if btype == 'local':
        return None
    if btype in ('remote', 'cloud'):
        named = []
        org = cfg.get('organization')
        if isinstance(org, str):
            named.append(org)
        ws = cfg.get('workspaces')
        if isinstance(ws, dict):
            for key in ('name', 'prefix'):
                val = ws.get(key)
                if isinstance(val, str):
                    named.append(val)
            tags = ws.get('tags')
            if isinstance(tags, list):
                named += [t for t in tags if isinstance(t, str)]
        return (btype, named, [])
    fields = _TF_BACKEND_FIELDS.get(btype)
    if fields is not None:
        named = [cfg[k] for k in fields if isinstance(cfg.get(k), str)]
        return (btype, named, [])
    # Unknown/future backend: classify every string leaf as a secure catch-all,
    # but report only the type — an incidental credential must never be echoed.
    extra = [v for v in cfg.values() if isinstance(v, str)]
    return (btype, [], extra)


def terraform_backend_prod(cwd):
    """Deny description if the initialized backend's state location matches a
    prod pattern, else None. Known types echo the matched location; the
    unknown-type catch-all reports only the backend type. Purely additive:
    only the PROD verdict affects policy (see the Q3 plan doc)."""
    be = terraform_backend(cwd)
    if be is None:
        return None
    btype, named, extra = be
    for val in named:
        if classify(val) == 'prod':
            return "the terraform %s backend (%s)" % (btype, val)
    for val in extra:
        if classify(val) == 'prod':
            return "the terraform %s backend" % btype
    return None


def _pulumi_home_dir(seg_env):
    """`$PULUMI_HOME` (segment env then process env), else `~/.pulumi`. None
    when neither is resolvable (no $HOME)."""
    home = seg_env.get('PULUMI_HOME') or os.environ.get('PULUMI_HOME')
    if home:
        return home
    h = os.environ.get('HOME')
    if h:
        return os.path.join(h, '.pulumi')
    return None


# Project-file extensions in pulumi's `encoding.Exts` order — the first one that
# exists in a directory is THE project file, and its absolute path is the string
# pulumi sha1-hashes to key the workspace file. Order matters: `.json` wins.
_PULUMI_PROJECT_EXTS = ('.json', '.yaml', '.yml')


def _pulumi_project_name(path, ext):
    """The `name` field of a pulumi project file. JSON: the top-level `name`
    key. YAML: the first top-level `name:` scalar (quotes/inline-comment
    stripped, block maps skipped). None if unreadable or unnamed — fail OPEN."""
    try:
        if ext == '.json':
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                name = data.get('name')
                return name if isinstance(name, str) and name else None
            return None
        with open(path, encoding='utf-8') as f:
            for line in f:
                m = re.match(r'^name:\s*["\']?([^"\'\n#]+)', line)
                if m:
                    return m.group(1).strip() or None
    except (OSError, ValueError):
        return None
    return None


def _pulumi_find_project(cwd):
    """(projectpath, name) for the nearest `Pulumi.{json,yaml,yml}` at or above
    cwd — pulumi's `DetectProjectPathFrom`, walking up and stopping at the first
    directory that holds a project file (extensions tried in `encoding.Exts`
    order). `projectpath` is the absolute, cleaned path pulumi hashes. None when
    no project file is found or its name can't be read (fail OPEN)."""
    d = os.path.abspath(cwd or '.')
    while True:
        for ext in _PULUMI_PROJECT_EXTS:
            p = os.path.join(d, 'Pulumi' + ext)
            if os.path.isfile(p):
                name = _pulumi_project_name(p, ext)
                return (p, name) if name else None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def pulumi_selected_stack(seg_env, cwd):
    """The currently-selected stack for the project at cwd, read from pulumi's
    per-project workspace settings file
    `<home>/workspaces/<name>-<sha1hex(projectpath)>-workspace.json` (`stack`
    field). None when there is no project, no workspace file, or no selection
    recorded. Pure local read; fail OPEN on any error."""
    phome = _pulumi_home_dir(seg_env)
    if not phome:
        return None
    found = _pulumi_find_project(cwd)
    if not found:
        return None
    path, name = found
    digest = hashlib.sha1(path.encode('utf-8')).hexdigest()  # noqa: S324
    wfile = os.path.join(phome, 'workspaces',
                         '%s-%s-workspace.json' % (name, digest))
    try:
        with open(wfile, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    stack = data.get('stack')
    return stack if isinstance(stack, str) and stack else None


# ---------------------------------------------------------------------------
# Session-scoped override grants
# ---------------------------------------------------------------------------
# A PROD_GUARD_SESSION_OVERRIDE=<reason> prefix asks like PROD_GUARD_OVERRIDE
# does, but once the human approves and the command actually runs (detected by
# the PostToolUse hook — it never fires for a denied ask), the explicit prod
# targets it hit are granted for the rest of the session. Later prefixed
# commands whose deny findings are fully covered by grants defer silently.
# Grants are exact-string, per-session, and bounded by a TTL; only explicit
# targets are ever grantable (ambient state can be repointed mid-session, and
# shared-state switches clobber every parallel session).

SESSION_GRANT_TTL = 8 * 3600      # a grant outlives a workday session, not a resumed one

# The store itself is lib/bouncer_grants.py, shared with the other guards; what
# a grant *means* stays here, because the guards do not agree on that. Under
# this namespace a grant is an exact prod target string.
GRANT_NAMESPACE = 'prod-guard'


def _session_grants_path(session_id):
    """This guard's grant file for one session. A thin alias over the shared
    store, kept because the store's own tests address it by this name."""
    return grants_path(GRANT_NAMESPACE, session_id)


def load_session_grants(session_id, now=None):
    """The session's unexpired granted targets as a set."""
    return load_grants(GRANT_NAMESPACE, session_id, now=now,
                       ttl=SESSION_GRANT_TTL)


def record_session_grants(session_id, targets, reason, now=None):
    """Record grants for the explicit prod targets a command actually hit."""
    record_grants(GRANT_NAMESPACE, session_id, targets, reason, now=now)


# ---------------------------------------------------------------------------
# Findings and policy
# ---------------------------------------------------------------------------

ASK, DENY = 1, 2

# A tail fragment of the reasons below, never emitted alone, so its 'prod-guard'
# always sits behind an opener. check-hook-attribution.py counts literals rather
# than emit paths and flags it as naming the plugin off position 0; expected.
CONFIG_HINT = ('Patterns: built-ins plus .claude/prod-guard.json '
               '(see the prod-guard README).')

# Every reason below opens 'prod-guard: ', and that opener is a cross-guard
# contract rather than decoration: Claude Code names the plugin in neither the
# permission prompt a human answers nor the error text handed back to the agent,
# so the reason's own wording is the only in-band trace of which guard decided —
# on an ask as much as on a deny. Sibling guards read it as
# `^(?:Error:\s*)?([a-z0-9-]+-guard):\s` (foreground-guard 0.5.1
# scripts/friction-report.py), and a reason that misses it is uncountable under
# --plugin all. Reword a reason freely; keep the opener, colon included.
# ReasonAttributionTests in tests/test_prod_guard.py holds this over every path
# that can emit a decision, on both verdicts.


def deny_prod(action, target_desc, grant_targets=None):
    """`grant_targets` (tuple of target strings) marks the finding as
    session-grantable: it is passed only when the prod classification came
    from an explicit pin, never for ambient resolutions or shared-state
    switches — those must re-prompt every time. It also gates the session
    clause below: the reason offers the batch form only where a grant could
    actually be recorded, so the other deny builders — which pass none — go
    on advertising the per-command form alone."""
    session_hint = ''
    if grant_targets:
        session_hint = (
            ' For a batch against this target, PROD_GUARD_SESSION_OVERRIDE=<reason> '
            'prompts once and then admits later commands carrying that same '
            'prefix for %d h.' % (SESSION_GRANT_TTL // 3600))
    return (DENY,
            'prod-guard: `%s` targets %s, which matches a production pattern. '
            'Mutating commands against production targets are blocked. If this '
            'is intentional, prefix the command with PROD_GUARD_OVERRIDE=<reason> '
            'to downgrade the block to a confirmation prompt.%s %s'
            % (action, target_desc, session_hint, CONFIG_HINT),
            tuple(grant_targets) if grant_targets else None)


def ask_unknown(action, target_desc):
    return (ASK,
            'prod-guard: `%s` targets %s, which matches neither a production '
            'nor a non-production pattern — unknown targets are never silently '
            'allowed. Confirm it is safe, or add a nonprod pattern to '
            '.claude/prod-guard.json to classify it. %s'
            % (action, target_desc, CONFIG_HINT),
            None)


def deny_ambient(action, ambient_desc, pin_hint):
    return (DENY,
            'prod-guard: `%s` relies on %s — shared mutable state that a '
            'parallel session can repoint before this command runs, so the '
            'true target is ambiguous at run time. Mutating commands must pin '
            'their target explicitly: add %s and retry. To run against the '
            'ambient target anyway, prefix the command with '
            'PROD_GUARD_OVERRIDE=<reason> for a confirmation prompt.'
            % (action, ambient_desc, pin_hint),
            None)


def deny_switch(action, what, pin_hint):
    """A command whose whole purpose is to repoint the shared active target.
    Denied regardless of what the new target classifies as: the repoint
    clobbers every parallel session, and a per-command pin flag makes it
    unnecessary."""
    return (DENY,
            'prod-guard: `%s` repoints %s, which is shared by every session '
            'on this machine — parallel sessions relying on ambient context '
            'will silently follow it. Switching shared state is blocked: pin '
            'the target per command with %s instead. If the switch is '
            'genuinely intended, prefix the command with '
            'PROD_GUARD_OVERRIDE=<reason> for a confirmation prompt.'
            % (action, what, pin_hint),
            None)


def ask_switch(action, what):
    """Shared-state writers with no per-command pin alternative (credential
    logins, kubeconfig writers, default-namespace switches): confirm rather
    than block."""
    return (ASK,
            'prod-guard: `%s` writes %s, which is shared by every session '
            'on this machine — parallel sessions relying on ambient context '
            'will silently follow it. Prefer per-command pinning '
            '(--context/--project/--profile) over switching shared state.'
            % (action, what),
            None)


def policy(action, explicit_value, explicit_desc,
           ambient_value, ambient_desc, pin_hint, classify_fn=classify):
    """The shared decision matrix for a mutating verb, given how the target
    resolved. Returns a finding tuple or None (defer). `classify_fn` lets a
    tool refine classification (e.g. kube resolves the context's server URL);
    it defaults to the plain name-based classify()."""
    if explicit_value is not None:
        cls = classify_fn(explicit_value)
        if cls == 'prod':
            return deny_prod(action, explicit_desc, (explicit_value,))
        if cls == 'nonprod':
            return None
        return ask_unknown(action, explicit_desc)
    # No explicit pin: the target is whatever the shared ambient state says.
    if ambient_value is not None:
        if classify_fn(ambient_value) == 'prod':
            return deny_prod(action, ambient_desc + ' (ambient)')
        return deny_ambient(action, ambient_desc, pin_hint)
    return deny_ambient(action, ambient_desc, pin_hint)


# ---------------------------------------------------------------------------
# Per-tool verb tables and evaluators
# ---------------------------------------------------------------------------

def action_of(argv, words):
    """Human-readable label for a finding: the tool plus its first verbs,
    with flags elided so `kubectl --context prod-x delete ns` reads as
    `kubectl delete ns`."""
    return ' '.join([os.path.basename(argv[0])] + list(words[:3]))


# kubectl verbs that only read. Everything else — including verbs this table
# has never heard of — is treated as mutating (unsure => mutating).
KUBECTL_READONLY = frozenset({
    'get', 'describe', 'logs', 'top', 'explain', 'api-resources',
    'api-versions', 'version', 'cluster-info', 'diff', 'events', 'wait',
    'auth', 'completion', 'options', 'kustomize',
    # oc (OpenShift) shares this evaluator; its read-only extras:
    'status', 'whoami', 'projects', 'process',
})
KUBECTL_CONFIG_READONLY = frozenset({
    'view', 'get-contexts', 'get-clusters', 'get-users', 'current-context',
})
# kube flags whose value must not be mistaken for a verb.
KUBE_VALUE_FLAGS = (
    '--context', '--kube-context', '--kubeconfig', '-n', '--namespace',
    '-o', '--output', '-f', '--filename', '-l', '--selector', '--cluster',
    '--user', '--server',
)

HELM_LOCAL_OR_READONLY = frozenset({
    'list', 'ls', 'status', 'get', 'history', 'show', 'inspect', 'search',
    'template', 'lint', 'version', 'env', 'verify', 'pull', 'fetch',
    'dependency', 'repo', 'registry', 'plugin', 'create', 'package',
    'completion', 'docs', 'help',
})

FLUX_READONLY = frozenset({
    'get', 'logs', 'version', 'check', 'tree', 'events', 'stats', 'trace',
    'export', 'diff', 'build', 'completion',
})

TF_READONLY = frozenset({
    'plan', 'validate', 'fmt', 'show', 'output', 'graph', 'version', 'init',
    'get', 'console', 'providers', 'metadata',
})

DOCKER_READONLY = frozenset({
    'ps', 'images', 'inspect', 'logs', 'version', 'info', 'history', 'top',
    'stats', 'diff', 'events', 'search', 'port', 'save', 'wait',
})

GH_READONLY = frozenset({
    'list', 'view', 'status', 'checks', 'diff', 'download', 'search',
    'browse', 'version', 'auth', 'help', 'completion', 'clone', 'checkout',
})

# Every gh verb that mutates. Detection only — treated as mutating unless the
# (resource, verb) pair lands in the collaboration tier below. Anything not
# listed defers (unsure => not mutating is acceptable here: gh is denylist-only,
# so an unrecognized verb was never going to deny anyway).
GH_MUTATING_VERBS = frozenset({
    'delete', 'merge', 'close', 'edit', 'create', 'comment', 'review',
    'ready', 'lock', 'unlock', 'transfer', 'archive', 'unarchive', 'rename',
    'set', 'run', 'rerun', 'cancel', 'enable', 'disable', 'sync', 'fork',
    'reopen', 'upload', 'develop',
})

# Collaboration-metadata tier: mutating gh verbs with no path to an outage —
# they never touch code, CI, releases, or infrastructure and are reversible
# (close/edit/reopen). Keyed by the gh command (resource, `words[0]`); the value
# is the set of subcommands that are collaboration-safe. A mutating gh command
# in this tier DEFERS even against a prod-matching repo, because under-matching
# a `gh issue create` costs nothing so the deny buys no safety (issue #18).
# This is an allowlist, mirroring the read-only verb tables: any mutating
# (resource, verb) NOT listed here stays prod-classified (deny on prod). The
# opt-in `gh_strict` knob disables the whole tier for issue-ops shops that wire
# comments to deploys. Verbs deliberately NOT here stay strict: `pr merge`,
# `issue delete`/`transfer`/`lock`, `repo edit`, and everything under `release`,
# `secret`, `variable`, `workflow`, `run`, `ruleset`, plus `lock`/`fork`/`sync`.
GH_COLLAB_VERBS = {
    'issue': frozenset({'create', 'edit', 'comment', 'close', 'reopen'}),
    'pr': frozenset({'create', 'edit', 'comment', 'review', 'ready',
                     'close', 'reopen'}),
    'gist': frozenset({'create'}),
    'label': frozenset({'create', 'edit', 'delete', 'clone'}),
}

ARGOCD_MUTATING = frozenset({
    'sync', 'create', 'delete', 'set', 'unset', 'rollback', 'patch', 'edit',
    'terminate-op', 'update', 'add', 'rm', 'remove', 'run',
})
ARGOCD_READONLY = frozenset({
    'list', 'get', 'version', 'logs', 'history', 'resources', 'manifests',
    'diff', 'wait',
})

EKSCTL_READONLY = frozenset({'get', 'version', 'info', 'completion', 'help'})

DOCTL_MUTATING = frozenset({
    'create', 'delete', 'rm', 'update', 'resize', 'reboot', 'power-on',
    'power-off', 'power-cycle', 'shutdown', 'rebuild', 'restore', 'migrate',
    'upgrade', 'save', 'set', 'remove', 'add', 'attach', 'detach',
})
DOCTL_READONLY = frozenset({'list', 'ls', 'get', 'version', 'help'})

# gcloud/aws/az verbs are matched by token scan: a mutating match anywhere in
# the command wins over a read-only match (conservative on mixed signals).
GCLOUD_MUTATING_PREFIXES = (
    'create', 'delete', 'update', 'deploy', 'set-', 'add-', 'remove-',
    'import', 'restart', 'resize', 'promote', 'failover', 'rotate', 'reset',
    'cancel', 'submit', 'patch', 'enable', 'disable', 'attach', 'detach',
    'migrate', 'ssh', 'scp', 'apply', 'start', 'stop', 'restore', 'purge',
    'drain', 'suspend', 'resume', 'upgrade', 'rollback', 'abandon', 'copy-',
    'run-', 'execute', 'invoke', 'trigger',
)
# `read`/`tail` are the verbs of `gcloud logging read`/`gcloud logging tail`,
# both read-only log queries. No gcloud surface uses them as mutating verbs,
# and a mutating token anywhere else in the command still wins the scan, so
# listing them here can't downgrade a genuinely mutating command (issue #16).
GCLOUD_READONLY = frozenset({
    'list', 'describe', 'help', 'info', 'version', 'topic', 'export',
    'config-helper', 'read', 'tail',
})
GCLOUD_READONLY_PREFIXES = ('print-', 'get-')

AWS_MUTATING_PREFIXES = (
    'create-', 'delete-', 'update-', 'put-', 'terminate-', 'stop-', 'start-',
    'reboot-', 'modify-', 'attach-', 'detach-', 'associate-', 'disassociate-',
    'run-', 'invoke', 'publish', 'purge-', 'set-', 'tag-', 'untag-',
    'register-', 'deregister-', 'enable-', 'disable-', 'restore-', 'reset-',
    'rotate-', 'cancel-', 'revoke-', 'authorize-', 'import-', 'copy-',
    'promote-', 'replace-', 'reencrypt', 'batch-write', 'batch-delete',
    'send-', 'execute-', 'admin-',
)
AWS_MUTATING = frozenset({'cp', 'mv', 'sync', 'rm', 'mb', 'rb'})
AWS_READONLY_PREFIXES = ('describe-', 'list-', 'get-', 'batch-get-', 'head-')
AWS_READONLY = frozenset({'ls', 'help', 'wait', 'presign'})

AZ_MUTATING = frozenset({
    'create', 'delete', 'update', 'set', 'add', 'remove', 'start', 'stop',
    'restart', 'deallocate', 'purge', 'import', 'deploy', 'assign', 'revoke',
    'invoke', 'exec', 'scale', 'upgrade', 'rotate', 'regenerate', 'reset',
    'cancel', 'approve', 'reject', 'enable', 'disable', 'attach', 'detach',
    'move', 'recover', 'restore', 'failover', 'run-command', 'repair',
})
AZ_READONLY = frozenset({
    'list', 'show', 'version', 'find', 'help', 'browse', 'wait', 'export',
})
AZ_READONLY_PREFIXES = ('get-', 'check-', 'list-')


def _scan_verb(tokens, mutating_exact=frozenset(), mutating_prefixes=(),
               readonly_exact=frozenset(), readonly_prefixes=()):
    """Token-scan verb classifier for multi-level CLIs (gcloud/aws/az/doctl).
    Returns ('mutating', token) / ('readonly', token) / ('unknown', None).
    Mutating matches win: on mixed signals the command is treated as
    mutating. Unknown is the caller's problem — and callers treat it as
    mutating too."""
    ro_hit = None
    mut_prefixes = tuple(mutating_prefixes)
    ro_prefixes = tuple(readonly_prefixes)
    for t in tokens:
        tl = t.lower()
        # str.startswith(()) is False, so empty prefix tuples are inert.
        if tl in mutating_exact or (mut_prefixes and tl.startswith(mut_prefixes)):
            return 'mutating', t
        if ro_hit is None and (
                tl in readonly_exact
                or (ro_prefixes and tl.startswith(ro_prefixes))):
            ro_hit = t
    if ro_hit is not None:
        return 'readonly', ro_hit
    return 'unknown', None


def _kube_target(argv, seg_env, context_flags):
    """(explicit_value, explicit_desc, ambient_value, ambient_desc) for a
    kubeconfig-based tool. The desc names the resolved context and, when an
    explicit `-n`/`--namespace` is present, the namespace the mutation lands in
    — so the deny/ask prompt always displays where it goes (issue #10)."""
    ns = first_flag_value(argv, ('-n', '--namespace'))
    ns_suffix = ("; namespace '%s'" % ns) if ns else ''
    ctx = first_flag_value(argv, context_flags)
    if ctx is not None:
        return (ctx, "kube-context '%s' (from %s%s)"
                % (ctx, context_flags[0], ns_suffix), None, '')
    ambient = kube_current_context(seg_env)
    if ambient is not None:
        desc = "the ambient kube-context (currently '%s'%s)" % (ambient, ns_suffix)
    else:
        desc = ('the ambient kube-context (unresolvable at hook time%s)'
                % ns_suffix)
    return None, '', ambient, desc


def eval_kubectl(argv, seg_env, ctx):
    words = words_of(argv, KUBE_VALUE_FLAGS)
    action = action_of(argv, words)
    verb = words[0] if words else None
    if verb is None:
        return []
    if verb == 'config':
        sub = words[1] if len(words) > 1 else None
        if sub in KUBECTL_CONFIG_READONLY:
            return []
        # `config set current-context <ctx>` is `use-context` by another name.
        is_set_current = (sub == 'set' and len(words) > 2
                          and words[2] == 'current-context')
        if sub == 'use-context' or is_set_current:
            name_idx = 3 if is_set_current else 2
            name = words[name_idx] if len(words) > name_idx else None
            if name and classify_kube(name, seg_env) == 'prod':
                return [deny_prod(action, "kube-context '%s'" % name)]
            return [deny_switch(action, 'the shared kubeconfig current-context',
                                'kubectl --context <ctx>')]
        # set/set-context/delete-context/rename-context/unset/...: mutations
        # of the shared kubeconfig itself (but not of which context is
        # current), with no per-command pin alternative.
        return [ask_switch(action, 'the shared kubeconfig')]
    if verb in KUBECTL_READONLY:
        return []
    if verb == 'rollout' and len(words) > 1 and words[1] in ('status', 'history'):
        return []
    # oc (OpenShift) shares this evaluator; its extra shared-state verbs:
    if verb == 'login':
        # `oc login <server>` writes credentials and a context into the
        # shared kubeconfig (kubectl has no such verb, so no collision).
        return [ask_switch(action, 'the shared kubeconfig (login writes a new context)')]
    if verb == 'project':
        name = words[1] if len(words) > 1 else None
        if name is None:
            return []  # bare `oc project` only prints the current project
        return [ask_switch(action, 'the shared kubeconfig (current project of the context)')]
    explicit, edesc, ambient, adesc = _kube_target(argv, seg_env, ('--context',))
    f = policy(action, explicit, edesc, ambient, adesc, 'kubectl --context <ctx>',
               classify_fn=lambda v: classify_kube(v, seg_env))
    return [f] if f else []


def eval_helm(argv, seg_env, ctx):
    words = words_of(argv, KUBE_VALUE_FLAGS)
    action = action_of(argv, words)
    verb = words[0] if words else None
    if verb is None:
        return []
    if verb == 'push':
        # helm push CHART REMOTE — the remote registry ref is the target.
        remote = words[-1] if len(words) >= 3 else None
        cls = classify(remote)
        if cls == 'prod':
            return [deny_prod(action, "registry '%s'" % remote, (remote,))]
        if cls == 'nonprod':
            return []
        return [ask_unknown(action, "registry '%s'" % (remote or '<unresolved>'))]
    if verb in HELM_LOCAL_OR_READONLY:
        return []
    explicit, edesc, ambient, adesc = _kube_target(argv, seg_env, ('--kube-context',))
    f = policy(action, explicit, edesc, ambient, adesc, 'helm --kube-context <ctx>',
               classify_fn=lambda v: classify_kube(v, seg_env))
    return [f] if f else []


def eval_flux(argv, seg_env, ctx):
    words = words_of(argv, KUBE_VALUE_FLAGS)
    action = action_of(argv, words)
    verb = words[0] if words else None
    if verb is None or verb in FLUX_READONLY:
        return []
    explicit, edesc, ambient, adesc = _kube_target(argv, seg_env, ('--context',))
    f = policy(action, explicit, edesc, ambient, adesc, 'flux --context <ctx>',
               classify_fn=lambda v: classify_kube(v, seg_env))
    return [f] if f else []


def eval_kubectx(argv, seg_env, ctx):
    # A lone `-` is the "previous context" operand, not a flag.
    args = [a for a in argv[1:] if a == '-' or not a.startswith('-')]
    flags = [a for a in argv[1:] if a != '-' and a.startswith('-')]
    if '-d' in flags:
        return [ask_switch('kubectx -d', 'the shared kubeconfig')]
    if not args or '-c' in flags or '--current' in flags:
        return []
    name = args[0]
    if name == '-':
        return [deny_switch('kubectx -', 'the shared kubeconfig current-context',
                            'kubectl --context <ctx>')]
    if classify_kube(name, seg_env) == 'prod':
        return [deny_prod('kubectx %s' % name, "kube-context '%s'" % name)]
    return [deny_switch('kubectx %s' % name, 'the shared kubeconfig current-context',
                        'kubectl --context <ctx>')]


def eval_kubens(argv, seg_env, ctx):
    args = [a for a in argv[1:] if not a.startswith('-')]
    flags = [a for a in argv[1:] if a.startswith('-')]
    if not args or '-c' in flags or '--current' in flags:
        return []
    return [ask_switch('kubens %s' % args[0],
                       "the shared kubeconfig (default namespace of the current context)")]


def eval_gcloud(argv, seg_env, ctx):
    words = words_of(argv, ('--project', '--zone', '--region', '--account',
                            '--configuration', '--impersonate-service-account'))
    action = action_of(argv, words)
    lw = [w.lower() for w in words]
    # Special cases first: commands that mutate shared local state.
    if 'get-credentials' in lw:
        return [ask_switch(action, 'the shared kubeconfig (writes a new context)')]
    if words[:2] == ['config', 'set'] or words[:2] == ['config', 'unset']:
        value = words[3] if len(words) > 3 else None
        if value and classify(value) == 'prod':
            return [deny_prod(action, "gcloud config value '%s'" % value)]
        return [deny_switch(action, 'the shared active gcloud configuration',
                            'the matching per-command flag (gcloud --project '
                            '<id> / --zone / --region / --account)')]
    if words[:2] == ['config', 'configurations']:
        if len(words) > 2 and words[2] in ('list', 'describe'):
            return []
        # activate repoints the active configuration; create activates the
        # new one by default; delete/rename mutate which configs exist.
        return [deny_switch(action, 'the shared active gcloud configuration',
                            'the matching per-command flag (gcloud --project '
                            '<id> / --zone / --region / --account)')]
    if words[:1] == ['auth'] and len(words) > 1 and words[1] not in ('list', 'print-access-token', 'print-identity-token'):
        return [ask_switch(action, 'the shared gcloud credentials')]
    kind, _ = _scan_verb(lw, mutating_prefixes=GCLOUD_MUTATING_PREFIXES,
                         readonly_exact=GCLOUD_READONLY,
                         readonly_prefixes=GCLOUD_READONLY_PREFIXES)
    if kind == 'readonly':
        return []
    # Mutating or unknown (unknown => mutating). Resolve the project.
    flags = flag_values(argv, ('--project', '--zone', '--region', '--account'))
    env_proj = seg_env.get('CLOUDSDK_CORE_PROJECT') or os.environ.get('CLOUDSDK_CORE_PROJECT')
    if flags or env_proj:
        # Explicit pin: classify every provided locator; prod anywhere wins.
        values = list(flags.values()) + ([env_proj] if env_proj else [])
        descs = ', '.join("'%s'" % v for v in values)
        classes = [classify(v) for v in values]
        if 'prod' in classes:
            return [deny_prod(action, 'the gcloud project/locator %s' % descs,
                              tuple(values))]
        if 'nonprod' in classes:
            return []
        return [ask_unknown(action, 'the gcloud project/locator %s' % descs)]
    ambient = gcloud_active_project()
    if ambient is not None and classify(ambient) == 'prod':
        return [deny_prod(action, "the active gcloud config project '%s' (ambient)" % ambient)]
    desc = ("the ambient gcloud active configuration (project '%s')" % ambient
            if ambient else 'the ambient gcloud active configuration (unresolvable at hook time)')
    return [deny_ambient(action, desc, 'gcloud --project <id>')]


def ambient_aws_default_profile_decision(action, seg_env, pin,
                                         generic_desc, named_desc):
    """Escalate the ambient ~/.aws/config [default] profile to a decision: a
    prod signal in any resolved field denies; otherwise a naming (or generic)
    ambient ask. Shared by eval_aws and eval_eksctl, which both reach AWS
    through the same default-profile chain. Additive-only: content moves the
    call unknown -> deny, never unknown -> defer.

    named_desc is a %-format template taking the most-identifying resolved
    field; generic_desc is used when nothing echoable resolved."""
    resolved = aws_default_profile(seg_env)
    if resolved is not None:
        named, extra = resolved
        for v in named:
            if classify(v) == 'prod':
                return deny_prod(action,
                                 "the default aws profile (%s) (ambient)" % v)
        for v in extra:
            if classify(v) == 'prod':
                return deny_prod(action, 'the default aws profile (ambient)')
        if named:
            return deny_ambient(action, named_desc % named[0], pin)
    return deny_ambient(action, generic_desc, pin)


def eval_aws(argv, seg_env, ctx):
    words = words_of(argv, ('--profile', '--region', '--output', '--query'))
    action = action_of(argv, words)
    lw = [w.lower() for w in words]
    if lw[:1] == ['configure']:
        return [ask_switch(action, 'the shared aws config/credentials files')]
    if 'update-kubeconfig' in lw:
        return [ask_switch(action, 'the shared kubeconfig (writes a new context)')]
    kind, _ = _scan_verb(lw, mutating_exact=AWS_MUTATING,
                         mutating_prefixes=AWS_MUTATING_PREFIXES,
                         readonly_exact=AWS_READONLY,
                         readonly_prefixes=AWS_READONLY_PREFIXES)
    if kind == 'readonly':
        return []
    profile = (first_flag_value(argv, ('--profile',))
               or seg_env.get('AWS_PROFILE') or os.environ.get('AWS_PROFILE')
               or seg_env.get('AWS_DEFAULT_PROFILE') or os.environ.get('AWS_DEFAULT_PROFILE'))
    region = first_flag_value(argv, ('--region',))
    if profile:
        desc = "aws profile '%s'" % profile
        targets = (profile,)
        cls = classify(profile)
        if cls == 'unknown' and region:
            cls = classify(region)
            desc = "aws profile '%s' / region '%s'" % (profile, region)
            targets = (profile, region)
        if cls == 'prod':
            return [deny_prod(action, desc, targets)]
        if cls == 'nonprod':
            return []
        # Name (and region) told us nothing. Resolve where `[profile NAME]`
        # in ~/.aws/config actually reaches — a prod sso_start_url/role_arn/...
        # denies. Additive: this only escalates unknown -> deny; a
        # nonprod/unknown/unresolvable profile keeps the by-name ask below.
        named_prod = aws_named_profile_prod(profile, seg_env)
        if named_prod:
            return [deny_prod(action, named_prod, (profile,))]
        return [ask_unknown(action, desc)]
    # No explicit profile pinned: the target is the `[default]` profile, whose
    # sso_start_url / role_arn / sso_session in ~/.aws/config name the account
    # it reaches. A prod default profile denies; anything else still asks
    # (ambient), now naming what resolved. Purely additive over the prior
    # always-ask — a missing/unreadable config resolves nothing.
    pin = 'aws --profile <name> (or AWS_PROFILE=<name>)'
    return [ambient_aws_default_profile_decision(
        action, seg_env, pin,
        'the ambient default aws profile',
        "the ambient default aws profile (%s)")]


def eval_az(argv, seg_env, ctx):
    words = words_of(argv, ('--subscription', '--resource-group', '-g',
                            '--name', '-n', '--output', '-o', '--query'))
    action = action_of(argv, words)
    lw = [w.lower() for w in words]
    if lw[:2] == ['account', 'set']:
        sub = first_flag_value(argv, ('--subscription', '-s'))
        if sub and classify(sub) == 'prod':
            return [deny_prod(action, "azure subscription '%s'" % sub)]
        return [deny_switch(action, 'the shared active azure subscription',
                            'az --subscription <id>')]
    if lw[:1] == ['login'] or lw[:1] == ['logout'] or lw[:2] == ['account', 'clear']:
        return [ask_switch(action, 'the shared azure credentials')]
    kind, _ = _scan_verb(lw, mutating_exact=AZ_MUTATING,
                         readonly_exact=AZ_READONLY,
                         readonly_prefixes=AZ_READONLY_PREFIXES)
    if kind == 'readonly':
        return []
    sub = first_flag_value(argv, ('--subscription', '-s'))
    if sub:
        cls = classify(sub)
        if cls == 'prod':
            return [deny_prod(action, "azure subscription '%s'" % sub, (sub,))]
        if cls == 'nonprod':
            return []
        return [ask_unknown(action, "azure subscription '%s'" % sub)]
    ambient = az_default_subscription()
    if ambient is not None and classify(ambient) == 'prod':
        return [deny_prod(action, "the active azure subscription '%s' (ambient)" % ambient)]
    desc = ("the ambient azure subscription (currently '%s')" % ambient
            if ambient else 'the ambient azure subscription (unresolvable at hook time)')
    return [deny_ambient(action, desc, 'az --subscription <id>')]


def eval_terraform(argv, seg_env, ctx):
    words = words_of(argv, ('-chdir',))
    action = action_of(argv, words)
    verb = words[0].lower() if words else None
    if verb is None:
        return []
    if verb == 'workspace':
        sub = words[1] if len(words) > 1 else None
        if sub in ('list', 'show'):
            return []
        name = words[2] if len(words) > 2 else None
        if name and classify(name) == 'prod':
            return [deny_prod(action, "terraform workspace '%s'" % name)]
        return [ask_switch(action, "the directory's selected terraform workspace")]
    if verb == 'state':
        sub = words[1] if len(words) > 1 else None
        if sub in ('list', 'show', 'pull'):
            return []
        # state rm/mv/push/replace-provider rewrite the backend state.
    elif verb == 'login':
        return [ask_switch(action, 'the shared terraform credentials')]
    elif verb in TF_READONLY:
        return []
    # apply/destroy/import/taint/refresh/state-rewrite/unknown: mutating.
    # The backend state location is the real target — a prod bucket/TFC
    # workspace denies regardless of the (weak-proxy) workspace name. Purely
    # additive: it only escalates to deny, never silences an ask.
    cwd = ctx.get('cwd')
    ws = seg_env.get('TF_WORKSPACE') or os.environ.get('TF_WORKSPACE')
    if ws:
        cls = classify(ws)
        if cls == 'prod':
            return [deny_prod(action, "terraform workspace '%s' (TF_WORKSPACE)" % ws,
                              (ws,))]
        backend = terraform_backend_prod(cwd)
        if backend:
            return [deny_prod(action, backend)]
        if cls == 'nonprod':
            return []
        return [ask_unknown(action, "terraform workspace '%s' (TF_WORKSPACE)" % ws)]
    ambient = terraform_workspace(cwd)
    if ambient is not None and classify(ambient) == 'prod':
        return [deny_prod(action, "the selected terraform workspace '%s'" % ambient)]
    backend = terraform_backend_prod(cwd)
    if backend:
        return [deny_prod(action, backend)]
    desc = ("the selected terraform workspace ('%s') and its backend" % ambient
            if ambient else 'an unresolved terraform workspace/backend')
    return [deny_ambient(action, desc, 'TF_WORKSPACE=<workspace> terraform ...')]


# Docker context names that mean "the local daemon" — mutating the local
# daemon is a workstation concern, not a prod concern, so they defer.
DOCKER_LOCAL_CONTEXTS = frozenset({
    'default', 'desktop-linux', 'docker-desktop', 'colima', 'orbstack',
    'rancher-desktop',
})


def eval_docker(argv, seg_env, ctx):
    words = words_of(argv, ('--context', '-H', '--host', '--connection'))
    # The standalone docker-compose binary is `docker compose` without the
    # `docker` prefix; normalize so both spell the same word sequence.
    if os.path.basename(argv[0]) == 'docker-compose':
        words = ['compose'] + words
    action = action_of(argv, words)
    verb = words[0].lower() if words else None
    if verb is None:
        return []
    if verb == 'context':
        sub = words[1] if len(words) > 1 else None
        if sub == 'use':
            name = words[2] if len(words) > 2 else None
            if name and classify(name) == 'prod':
                return [deny_prod(action, "docker context '%s'" % name)]
            return [deny_switch(action, 'the shared docker context',
                                'docker --context <ctx>')]
        return []  # ls/show/inspect/create/rm: no daemon mutation
    if verb == 'push':
        ref = words[1] if len(words) > 1 else None
        cls = classify(ref)
        if cls == 'prod':
            return [deny_prod(action, "image ref '%s'" % ref, (ref,))]
        if cls == 'nonprod':
            return []
        return [ask_unknown(action, "image ref '%s'" % (ref or '<unresolved>'))]
    if verb == 'compose':
        sub = words[1] if len(words) > 1 else None
        if sub in ('config', 'ps', 'ls', 'logs', 'images', 'version',
                   'events', 'top', 'port'):
            return []
        if sub == 'push' or (sub == 'build' and '--push' in argv):
            # The registry lives in the compose file's image: keys, which the
            # hook does not parse — unresolvable target, so fail closed.
            return [ask_unknown(action,
                                'images defined in the compose file '
                                '(registry unresolvable at hook time)')]
        if sub == 'build':
            return []  # local build
        # up/down/rm/stop/kill/exec/run/...: daemon mutations — fall through
        # to the context resolution below.
    elif verb in ('build', 'buildx', 'bake'):
        if '--push' in argv:
            # Classify EVERY -t/--tag: one prod tag among several must deny
            # even when the first tag is a dev registry.
            tags = all_flag_values(argv, ('-t', '--tag'))
            classes = [classify(t) for t in tags]
            if 'prod' in classes:
                bad = tags[classes.index('prod')]
                return [deny_prod(action, "image tag '%s'" % bad, (bad,))]
            if tags and all(c == 'nonprod' for c in classes):
                return []
            unknowns = [t for t, c in zip(tags, classes) if c == 'unknown']
            return [ask_unknown(action, "pushed image tag(s) %s" %
                                (', '.join("'%s'" % t for t in unknowns)
                                 or '<unresolved>'))]
        return []  # local build
    if verb in DOCKER_READONLY:
        return []
    if verb in ('image', 'volume', 'network', 'system', 'container') and \
            len(words) > 1 and words[1] in ('ls', 'list', 'inspect', 'df', 'events', 'logs'):
        return []
    # Mutating daemon verb (rm/rmi/prune/run/exec/stop/kill/...): where?
    # `--connection` is podman's remote-target flag (shares this evaluator);
    # docker has no such flag, so including it is inert there.
    explicit = first_flag_value(argv, ('--context', '--connection')) \
        or seg_env.get('DOCKER_HOST') or os.environ.get('DOCKER_HOST') \
        or seg_env.get('CONTAINER_HOST') or os.environ.get('CONTAINER_HOST')
    if explicit:
        cls = classify(explicit)
        if cls == 'prod':
            return [deny_prod(action, "docker context/host '%s'" % explicit,
                              (explicit,))]
        if cls == 'nonprod':
            return []
        return [ask_unknown(action, "docker context/host '%s'" % explicit)]
    ambient = docker_current_context()
    if ambient is None or ambient in DOCKER_LOCAL_CONTEXTS:
        return []  # local daemon: a workstation concern, not a prod one
    cls = classify(ambient)
    if cls == 'prod':
        return [deny_prod(action, "the ambient docker context '%s'" % ambient)]
    if cls == 'nonprod':
        return []
    return [deny_ambient(action, "the ambient docker context ('%s')" % ambient,
                        'docker --context <ctx>')]


def eval_gh(argv, seg_env, ctx):
    """gh deviates from the ambient rule deliberately: its implied target —
    the cwd repo's origin remote — is pinned by the worktree, not shared
    clobberable state, and prompting on every `gh pr create` would be pure
    noise. So gh gets denylist-only treatment: a mutating gh command whose
    resolved repo matches a prod pattern is denied; everything else defers.

    Mutating verbs are tiered (issue #18): collaboration metadata (`gh issue
    create`, `gh pr comment`, label ops) has no path to an outage — reversible,
    never touches code/CI/releases/infra — so it defers even against a prod
    repo, buying no safety from a deny. Only the strict tier (`pr merge`, `repo
    delete`, releases, secrets, workflows, `api` writes) prod-classifies. The
    opt-in `gh_strict` knob folds the collaboration tier back into strict for
    issue-ops shops that wire comments to deploys."""
    words = words_of(argv, ('-R', '--repo', '-t', '--title', '-b', '--body',
                            '-F', '--body-file', '-m', '--method', '-f', '-H'))
    action = action_of(argv, words)
    lw = [w.lower() for w in words]
    resource = lw[0] if lw else None
    if resource is None or resource in GH_READONLY:
        return []
    if resource == 'api':
        method = (first_flag_value(argv, ('-X', '--method')) or 'GET').upper()
        if method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return []
        # `api` can hit any endpoint, so a write is always prod-classified.
    else:
        if not any(w in GH_MUTATING_VERBS for w in lw):
            return []
        # Tier by the (resource, verb) pair — gh's grammar puts the verb at
        # words[1] deterministically, so an unknown pair falls through to the
        # strict tier (the secure default).
        verb = lw[1] if len(lw) > 1 else None
        collab = verb in GH_COLLAB_VERBS.get(resource, frozenset())
        if collab and not gh_strict_enabled(seg_env):
            return []
    candidates = [first_flag_value(argv, ('-R', '--repo'))
                  or seg_env.get('GH_REPO') or os.environ.get('GH_REPO')
                  or git_remote_url(ctx.get('cwd'))]
    if resource == 'repo' and len(words) > 2:
        candidates.append(words[2])  # `gh repo delete OWNER/REPO` is positional
    for repo in candidates:
        if repo and classify(repo) == 'prod':
            return [deny_prod(action, "repository '%s'" % repo, (repo,))]
    return []


# ssh flags that consume the following token as their value, so it is never
# mistaken for the destination host. Boolean flags (-v, -N, -t, -4, ...) take
# no value and are simply skipped. All are single-dash (ssh has no --long form).
SSH_VALUE_FLAGS = frozenset({
    '-b', '-c', '-D', '-E', '-e', '-F', '-I', '-i', '-J', '-L', '-l', '-m',
    '-O', '-o', '-p', '-Q', '-R', '-S', '-W', '-w',
})


def _ssh_host(dest):
    """Bare hostname from an ssh destination: strip a `ssh://` scheme and any
    path, a `user@` prefix, and a trailing `:port`. Bracketed IPv6 literals
    (`[::1]:22`) keep their brackets so they still match the loopback pattern;
    an unbracketed IPv6 literal is left whole (it has several colons, so the
    port strip below leaves it untouched)."""
    if not dest:
        return None
    d = dest
    if d.startswith('ssh://'):
        d = d[len('ssh://'):].split('/', 1)[0]
    if '@' in d:
        d = d.rsplit('@', 1)[1]
    if d.startswith('['):
        end = d.find(']')
        if end != -1:
            d = d[:end + 1]  # [::1]:22 -> [::1]
    elif d.count(':') == 1:
        d = d.split(':', 1)[0]  # host:port -> host
    return d or None


def eval_ssh(argv, seg_env, ctx):
    """ssh gets denylist-only treatment like gh: the destination host is
    pinned on the command line, not clobber-prone ambient state, so only the
    prod-blast-radius model applies. Any ssh into a prod host is treated as
    mutating — an interactive prod shell is the blast radius, and a read-only
    remote command can't be told from a destructive one — so a prod
    destination (or `-J` jump host) is denied and everything else defers."""
    hosts = []
    dest = None
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in SSH_VALUE_FLAGS:
            if tok == '-J' and i + 1 < len(argv):
                # `-J [user@]host[:port][,...]` — each hop is a host we transit.
                hosts += [_ssh_host(p) for p in argv[i + 1].split(',')]
            i += 2
            continue
        if tok.startswith('-'):
            i += 1
            continue
        dest = tok  # first bare operand is the destination; the rest is the
        break       # remote command, whose flags must not be reparsed as ssh's
    if dest is not None:
        hosts.append(_ssh_host(dest))
    action = 'ssh %s' % dest if dest else 'ssh'
    for h in hosts:
        if h and classify(h) == 'prod':
            return [deny_prod(action, "host '%s'" % h, (h,))]
    return []


def eval_argocd(argv, seg_env, ctx):
    words = words_of(argv, ('--server', '--grpc-web-root-path', '--project'))
    action = action_of(argv, words)
    lw = [w.lower() for w in words]
    if lw[:1] == ['login']:
        return [ask_switch(action, 'the shared argocd CLI context')]
    if lw[:1] == ['context']:
        name = words[1] if len(words) > 1 else None
        if not name:
            return []
        if classify(name) == 'prod':
            return [deny_prod(action, "argocd context '%s'" % name)]
        return [ask_switch(action, 'the shared argocd CLI context')]
    kind, _ = _scan_verb(lw, mutating_exact=ARGOCD_MUTATING,
                         readonly_exact=ARGOCD_READONLY)
    if kind == 'readonly':
        return []
    server = first_flag_value(argv, ('--server',))
    if server:
        cls = classify(server)
        if cls == 'prod':
            return [deny_prod(action, "argocd server '%s'" % server, (server,))]
        if cls == 'nonprod':
            return []
        return [ask_unknown(action, "argocd server '%s'" % server)]
    ambient = argocd_current_context()
    if ambient is not None and classify(ambient) == 'prod':
        return [deny_prod(action, "the ambient argocd context '%s'" % ambient)]
    desc = ("the ambient argocd context ('%s')" % ambient
            if ambient else 'the ambient argocd context (unresolvable at hook time)')
    return [deny_ambient(action, desc, 'argocd --server <host>')]


def eval_eksctl(argv, seg_env, ctx):
    words = words_of(argv, ('--cluster', '--name', '--region', '--profile'))
    action = action_of(argv, words)
    verb = words[0].lower() if words else None
    if verb is None or verb in EKSCTL_READONLY:
        return []
    if verb == 'utils' and len(words) > 1 and words[1] == 'write-kubeconfig':
        return [ask_switch(action, 'the shared kubeconfig (writes a new context)')]
    flags = flag_values(argv, ('--cluster', '--name', '--region', '--profile'))
    profile = (seg_env.get('AWS_PROFILE') or os.environ.get('AWS_PROFILE')
               or seg_env.get('AWS_DEFAULT_PROFILE')
               or os.environ.get('AWS_DEFAULT_PROFILE'))
    values = list(flags.values()) + ([profile] if profile else [])
    if values:
        descs = ', '.join("'%s'" % v for v in values)
        classes = [classify(v) for v in values]
        if 'prod' in classes:
            return [deny_prod(action, 'the eks cluster/profile %s' % descs,
                              tuple(values))]
        if 'nonprod' in classes:
            return []
        return [ask_unknown(action, 'the eks cluster/profile %s' % descs)]
    # Nothing pinned (no flags, no AWS_PROFILE/AWS_DEFAULT_PROFILE): eksctl
    # reaches AWS through the ~/.aws/config [default] profile, so resolve it
    # like eval_aws does -> a prod default profile denies, else ambient ask.
    return [ambient_aws_default_profile_decision(
        action, seg_env, 'eksctl --cluster <name> --profile <name>',
        'the ambient aws profile/region',
        'the ambient aws profile/region (default profile %s)')]


def eval_doctl(argv, seg_env, ctx):
    words = words_of(argv, ('--context', '--region', '--output'))
    action = action_of(argv, words)
    lw = [w.lower() for w in words]
    if 'kubeconfig' in lw and 'save' in lw:
        return [ask_switch(action, 'the shared kubeconfig (writes a new context)')]
    kind, _ = _scan_verb(lw, mutating_exact=DOCTL_MUTATING,
                         readonly_exact=DOCTL_READONLY)
    if kind == 'readonly':
        return []
    ctx_name = first_flag_value(argv, ('--context',))
    if ctx_name:
        cls = classify(ctx_name)
        if cls == 'prod':
            return [deny_prod(action, "doctl context '%s'" % ctx_name, (ctx_name,))]
        if cls == 'nonprod':
            return []
        return [ask_unknown(action, "doctl context '%s'" % ctx_name)]
    return [deny_ambient(action, 'the ambient doctl auth context',
                        'doctl --context <name>')]


# pulumi verbs that only read or act locally — everything else (up/update,
# destroy, refresh, import, cancel, watch, state ..., unknown) is mutating.
PULUMI_READONLY = frozenset({
    'preview', 'about', 'version', 'whoami', 'logs', 'console', 'plugin',
    'convert', 'install', 'schema', 'help', 'completion',
})
# pulumi `stack` subcommands that only read.
PULUMI_STACK_READONLY = frozenset({'ls', 'output', 'export', 'history', 'graph'})
# Value-taking flags, so a value placed before the verb isn't read as one.
PULUMI_VALUE_FLAGS = (
    '--stack', '-s', '--cwd', '-C', '--color', '--tracing', '--profiling',
    '--config-file', '--policy-pack', '--secrets-provider', '-m', '--message',
)


def _pulumi_decide(action, explicit, seg_env, ctx):
    """Classify an explicit pulumi stack (flag or positional); no stack pinned
    -> resolve the per-project selected stack from disk. A prod selection denies
    (ambient); nonprod/unknown/unresolved keep the ambient ask — the selection
    is clobber-prone shared state, so only prod escalates (additive; see the
    pulumi-ambient-stack plan doc)."""
    if explicit is not None:
        cls = classify(explicit)
        if cls == 'prod':
            return [deny_prod(action, "pulumi stack '%s'" % explicit, (explicit,))]
        if cls == 'nonprod':
            return []
        return [ask_unknown(action, "pulumi stack '%s'" % explicit)]
    ambient = pulumi_selected_stack(seg_env, ctx.get('cwd'))
    if ambient is not None and classify(ambient) == 'prod':
        return [deny_prod(action, "the selected pulumi stack '%s' (ambient)" % ambient)]
    desc = ("the ambient selected pulumi stack (currently '%s')" % ambient
            if ambient else 'the ambient selected pulumi stack (unresolved at hook time)')
    return [deny_ambient(action, desc, 'pulumi --stack <name>')]


def eval_pulumi(argv, seg_env, ctx):
    words = words_of(argv, PULUMI_VALUE_FLAGS)
    action = action_of(argv, words)
    verb = words[0].lower() if words else None
    if verb is None or verb in PULUMI_READONLY:
        return []
    if verb in ('login', 'logout'):
        return [ask_switch(action, 'the shared pulumi credentials/backend')]
    if verb == 'stack':
        sub = words[1].lower() if len(words) > 1 else None
        if sub is None or sub in PULUMI_STACK_READONLY:
            return []
        name = words[2] if len(words) > 2 else first_flag_value(argv, ('--stack', '-s'))
        if sub in ('select', 'unselect'):
            if name and classify(name) == 'prod':
                return [deny_prod(action, "pulumi stack '%s'" % name)]
            return [ask_switch(action, 'the selected pulumi stack')]
        # init/rm/rename/tag/import/change-secrets-provider: mutate the stack.
        return _pulumi_decide(action, name, seg_env, ctx)
    if verb == 'config':
        sub = words[1].lower() if len(words) > 1 else None
        if sub is None or sub == 'get':
            return []
        # set/rm/set-all/rm-all/cp/refresh: mutate the selected stack's config.
    return _pulumi_decide(action, first_flag_value(argv, ('--stack', '-s')),
                          seg_env, ctx)


# ansible ad-hoc modules that only read remote state (no change).
ANSIBLE_READONLY_MODULES = frozenset({'ping', 'setup', 'debug', 'gather_facts'})
# Inspection flags that make even ansible-playbook read-only (never connect).
ANSIBLE_READONLY_FLAGS = ('--syntax-check', '--list-hosts', '--list-tasks',
                          '--list-tags')
# Value-taking flags, so their values aren't mistaken for the host pattern.
ANSIBLE_VALUE_FLAGS = frozenset({
    '-i', '--inventory', '--inventory-file', '-l', '--limit',
    '-m', '--module-name', '-a', '--args', '-e', '--extra-vars',
    '-u', '--user', '--become-user', '--become-method',
    '-c', '--connection', '-T', '--timeout', '-f', '--forks',
    '-M', '--module-path', '-t', '--tree', '--start-at-task',
    '--tags', '--skip-tags', '--vault-id', '--vault-password-file',
    '--private-key', '--key-file', '--ssh-common-args', '--ssh-extra-args',
    '--scp-extra-args', '--sftp-extra-args', '-P', '--poll', '-B', '--background',
})


def _ansible_pattern(argv):
    """First positional token of an ad-hoc `ansible` command — the host
    pattern — skipping flags and their values."""
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in ANSIBLE_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith('-'):
            i += 1
            continue
        return tok
    return None


def _ini_inventory(path):
    """`inventory = <value>` under `[defaults]` in an ansible.cfg (INI). None
    if absent/unreadable — fail OPEN."""
    try:
        with open(path, encoding='utf-8') as f:
            section = None
            for line in f:
                s = line.strip()
                if s.startswith('[') and s.endswith(']'):
                    section = s[1:-1].strip().lower()
                    continue
                if section == 'defaults':
                    m = re.match(r'inventory\s*=\s*(.+?)\s*(?:[;#].*)?$', s)
                    if m:
                        return m.group(1).strip() or None
    except OSError:
        return None
    return None


def ansible_ambient_inventory(seg_env, cwd):
    """The inventory ansible would use with no `-i`: ANSIBLE_INVENTORY, then
    the `[defaults] inventory` of ANSIBLE_CONFIG / ./ansible.cfg. None if
    unresolved (the default /etc/ansible/hosts is unnamed — nothing to
    classify)."""
    inv = seg_env.get('ANSIBLE_INVENTORY') or os.environ.get('ANSIBLE_INVENTORY')
    if inv:
        return inv
    cfg = seg_env.get('ANSIBLE_CONFIG') or os.environ.get('ANSIBLE_CONFIG')
    paths = ([cfg] if cfg else []) + [os.path.join(cwd or '.', 'ansible.cfg')]
    for p in paths:
        val = _ini_inventory(p)
        if val:
            return val
    return None


def eval_ansible(argv, seg_env, ctx):
    """ansible / ansible-playbook target the *inventory* (which hosts a play
    runs against). The inventory is authoritative for classification; the host
    pattern and `--limit` can only escalate a decision to deny, never force an
    ask when the inventory itself resolves nonprod."""
    adhoc = os.path.basename(argv[0]) == 'ansible'
    if any(f in argv for f in ANSIBLE_READONLY_FLAGS):
        return []  # --syntax-check/--list-* never connect or change anything
    if adhoc:
        module = first_flag_value(argv, ('-m', '--module-name'))
        if module and module.split('.')[-1] in ANSIBLE_READONLY_MODULES:
            return []  # ping/setup/debug: read-only even against prod
    invs = all_flag_values(argv, ('-i', '--inventory', '--inventory-file'))
    limits = all_flag_values(argv, ('-l', '--limit'))
    pattern = _ansible_pattern(argv) if adhoc else None
    escalators = list(limits) + ([pattern] if pattern else [])
    action = ('ansible %s' % pattern if adhoc and pattern
              else os.path.basename(argv[0]))
    for t in invs + escalators:
        if classify(t) == 'prod':
            return [deny_prod(action, "the ansible target '%s'" % t, (t,))]
    if invs:
        for v in invs:
            if classify(v) == 'unknown':
                return [ask_unknown(action, "the ansible inventory '%s'" % v)]
        return []  # every -i inventory classifies nonprod
    ambient = ansible_ambient_inventory(seg_env, ctx.get('cwd'))
    if ambient is not None:
        cls = classify(ambient)
        if cls == 'prod':
            return [deny_prod(action, "the ambient ansible inventory '%s'" % ambient)]
        if cls == 'nonprod':
            return []
    return [deny_ambient(action, 'the ambient ansible inventory '
                        '(ANSIBLE_INVENTORY / ansible.cfg / /etc/ansible/hosts)',
                        '-i <inventory>')]


def eval_kustomize(argv, seg_env, ctx):
    # kustomize only renders/edits local files; the cluster mutation happens
    # in whatever kubectl/helm it pipes into, which gets its own segment.
    return []


EVALUATORS = {
    'kubectl': eval_kubectl,
    'oc': eval_kubectl,            # OpenShift CLI: kubectl's verb/flag model
    'helm': eval_helm,
    'flux': eval_flux,
    'kubectx': eval_kubectx,
    'kubens': eval_kubens,
    'gcloud': eval_gcloud,
    'aws': eval_aws,
    'az': eval_az,
    'terraform': eval_terraform,
    'tofu': eval_terraform,
    'docker': eval_docker,
    'podman': eval_docker,         # daemonless but shares docker's CLI shape
    'nerdctl': eval_docker,
    'docker-compose': eval_docker,  # standalone compose v1 binary
    'gh': eval_gh,
    'ssh': eval_ssh,
    'argocd': eval_argocd,
    'eksctl': eval_eksctl,
    'doctl': eval_doctl,
    'pulumi': eval_pulumi,
    'ansible': eval_ansible,
    'ansible-playbook': eval_ansible,
    'kustomize': eval_kustomize,
}
COVERED_TOOLS = frozenset(EVALUATORS)


# ---------------------------------------------------------------------------
# Segment evaluation and main
# ---------------------------------------------------------------------------

# A help flag makes the CLI print usage and exit — no config is read, no target
# is contacted, nothing mutates — so a mutating verb's help page is not a
# mutating command. Terraform spells its long form `-help`.
HELP_LONG_FLAGS = frozenset({'--help', '-help'})
# Tools where `--help` is NOT a help flag. OpenSSH has no long options: `ssh
# host --help` runs `--help` on the remote, so the connection to the target
# still happens and must still be judged.
NO_HELP_FLAG = frozenset({'ssh'})
# Tools where `-h` is not help: on docker/podman/nerdctl it is `--hostname`,
# so `docker run -h box image` is an ordinary mutating run.
NO_SHORT_HELP = frozenset({'docker', 'podman', 'nerdctl', 'docker-compose'})
# A version flag prints the CLI's own version and exits. Unlike a help flag it
# only counts as the *sole* argument: elsewhere `--version` takes a value on a
# mutating subcommand (`helm install … --version 2.0.0`, `eksctl create cluster
# --version 1.29`, `flux install --version=v2.0.0`). With nothing else in the
# command there is no target token at all, so nothing is left to classify —
# which is why this needs no per-tool exclusion, not even ssh.
VERSION_FLAGS = frozenset({'--version', '-version', '-V', '-v'})


def is_help_invocation(tool, argv):
    """True when this segment only asks the CLI for its usage text."""
    if tool in NO_HELP_FLAG:
        return False
    args = argv[1:]
    if HELP_LONG_FLAGS.intersection(args):
        return True
    if tool not in NO_SHORT_HELP and '-h' in args:
        return True
    return args[0] == 'help'


def evaluate_command_string(raw, ctx, depth=0, exported=None, shell=None):
    """Findings for a full command string: tokenize, split into simple
    commands, evaluate each. Recurses (bounded) into `sh -c '...'` and
    `eval ...` bodies so a quoted nested command can't ride past the guard.
    `sh -c` starts a child, so its body is evaluated against the invoking
    segment's exported env; `eval` starts none, so its body gets the same
    shell's vars, exported or not (Q151). Returns (findings, override_seen,
    session_reason) — session_reason is the value of the first inline
    PROD_GUARD_SESSION_OVERRIDE assignment, or None when the prefix is
    absent."""
    findings = []
    override = False
    session_reason = None
    if depth > 3:
        return findings, override, session_reason
    tokens = tokenize(raw)
    if tokens is None:
        return findings, override, session_reason  # unparseable: fail-open
    # Two scopes, both seeded from what this shell inherited (os.environ at the
    # top level, the invoking segment's exported env for a nested `sh -c` body
    # and its same-shell env for an `eval` one):
    #   shell_env  — variables visible to same-shell `$VAR` expansion. A bare
    #     `P=x` segment (a shell var, not exported) accumulates here so a later
    #     segment on the same line resolves it.
    #   exported   — what a CHILD process inherits. Bare assignments do NOT go
    #     here; only `export NAME=val` does. A SINGLE-QUOTED nested `sh -c`
    #     body is expanded against this, never against bare shell vars —
    #     under-expanding a nested body is safe (unresolved target →
    #     prompt/deny); over-expanding it could turn a genuinely-unpinned
    #     command into a false defer. A body written in double quotes is a
    #     different case: the parent expands it against `shell_env` while
    #     building the argument, so the resolved value reaches the tool whether
    #     or not the name was exported (Q131). The `-c` branch below reads the
    #     quoting off the token to tell the two apart.
    if exported is None:
        exported = dict(os.environ)
    shell_env = dict(exported if shell is None else shell)
    exported = dict(exported)
    for group in split_simple_commands(tokens):
        seg_env, argv_raw = extract_env_prefix(group)
        # This segment's inline `A=x cmd` assignments, resolved left-to-right
        # against the current shell env. They export to this command's own
        # children but do not persist to the chain.
        inline = resolve_assignments(seg_env.items(), shell_env)
        seg_inline = {k: inline[k] for k in seg_env}
        same_shell = {**shell_env, **seg_inline}   # for this command's args
        child_env = {**exported, **seg_inline}     # for its `sh -c` body
        if 'PROD_GUARD_OVERRIDE' in seg_inline:
            override = True
        if 'PROD_GUARD_SESSION_OVERRIDE' in seg_inline and session_reason is None:
            session_reason = seg_inline['PROD_GUARD_SESSION_OVERRIDE']
        # Expanded view for classification; raw view (nested-body extraction)
        # is left unexpanded so the child re-expands it against child_env.
        argv = strip_wrappers(expand_argv(argv_raw, same_shell), seg_inline)
        argv_raw = strip_wrappers(list(argv_raw), {})
        if not argv:
            # Assignment-only segment (`P=x`): a shell var, not exported.
            shell_env.update(seg_inline)
            continue
        tool = os.path.basename(argv[0])
        if tool in ASSIGN_BUILTINS:
            # `export NAME=val …`: operands persist and are exported. argv is
            # already expanded, so the values are resolved.
            for tok in argv[1:]:
                if not tok.startswith('-') and ASSIGNMENT_RE.match(tok):
                    name, _, value = tok.partition('=')
                    shell_env[name] = value
                    exported[name] = value
            continue
        if tool in SHELL_NAMES:
            # bash -c 'kubectl ...': evaluate the -c body as its own command,
            # inheriting only exported vars. That holds for a single-quoted
            # body, which reaches the child verbatim. A body with no
            # single-quoted run was expanded by the PARENT while building the
            # argument, so its `$C` resolves against this shell's vars whether
            # or not the name was exported -- do that here and let the child
            # re-expand what is left. Mixed quoting keeps the child's env:
            # only some runs were parent-expanded, and under-expanding is the
            # safe direction (unresolved target -> prompt).
            body = first_flag_value(argv_raw, ('-c',))
            if body:
                # The default stands in for a body that reached here as a
                # plain str rather than a token (`-c=…`, which flag_values
                # splits): unknown quoting reads as single, so it expands in
                # the child and nothing new resolves.
                if "'" not in getattr(body, 'quotes', "'"):
                    body = expand_vars(body, same_shell)
                sub_f, sub_o, sub_s = evaluate_command_string(body, ctx, depth + 1, child_env)
                findings += sub_f
                override = override or sub_o
                session_reason = session_reason if session_reason is not None else sub_s
            continue
        if tool == 'eval':
            # No child, so the body expands in this shell under either quoting:
            # single quotes stop only the PARENT, and `eval` expands the same
            # text itself. `child_env` rides on for a real child inside (Q151).
            sub_f, sub_o, sub_s = evaluate_command_string(
                ' '.join(argv_raw[1:]), ctx, depth + 1, child_env, same_shell)
            findings += sub_f
            override = override or sub_o
            session_reason = session_reason if session_reason is not None else sub_s
            continue
        evaluator = EVALUATORS.get(tool)
        if evaluator is None:
            continue  # uncovered tool: defer
        if len(argv) == 1:
            continue  # bare tool name prints help; nothing to guard
        if is_help_invocation(tool, argv) or (
                len(argv) == 2 and argv[1] in VERSION_FLAGS):
            continue
        findings += evaluator(argv, seg_inline, ctx)
    return findings, override, session_reason


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return  # unparseable input: fail-open
    if data.get('tool_name') != 'Bash':
        return
    command = (data.get('tool_input') or {}).get('command') or ''
    if not command.strip():
        return
    if os.environ.get('PROD_GUARD_DISABLE') == '1':
        return

    ctx = {'cwd': data.get('cwd') or os.getcwd()}

    if data.get('hook_event_name') == 'PostToolUse':
        # The command RAN — so if PreToolUse downgraded a deny to an ask, the
        # human approved it. Record session grants for the explicit prod
        # targets it hit; nothing is ever emitted from this branch.
        if 'PROD_GUARD_SESSION_OVERRIDE' not in command:
            return
        findings, _, session_reason = evaluate_command_string(command, ctx)
        if session_reason is None:
            return
        targets = {t for sev, _r, gts in findings if sev == DENY and gts
                   for t in gts}
        record_session_grants(data.get('session_id'), targets, session_reason)
        return

    findings, override, session_reason = evaluate_command_string(command, ctx)
    if not findings:
        return  # nothing to object to: defer to normal permissions

    grantable = []
    if session_reason is not None:
        granted = load_session_grants(data.get('session_id'))
        # Drop deny findings whose explicit prod targets were ALL granted
        # earlier this session; what survives decides as usual.
        findings = [f for f in findings
                    if not (f[0] == DENY and f[2]
                            and all(t in granted for t in f[2]))]
        if not findings:
            return  # every finding covered by a session grant: silent defer
        grantable = sorted({t for sev, _r, gts in findings
                            if sev == DENY and gts for t in gts})

    severity = max(sev for sev, _r, _t in findings)
    # De-duplicate reasons while keeping order; cap so the prompt stays readable.
    reasons, seen = [], set()
    for sev, reason, _t in sorted(findings, key=lambda f: -f[0]):
        if reason not in seen:
            seen.add(reason)
            reasons.append(reason)
    reason = ' | '.join(reasons[:3])

    if severity == DENY and session_reason is not None:
        severity = ASK
        if grantable:
            grant_note = (
                'Approving records a session grant for target(s) %s: further '
                'PROD_GUARD_SESSION_OVERRIDE-prefixed commands against them '
                'in this session will not re-prompt (expires after %d h). '
                % (', '.join("'%s'" % t for t in grantable),
                   SESSION_GRANT_TTL // 3600))
        else:
            grant_note = (
                'No session grant applies here — only explicit production '
                'targets are session-grantable; ambient-context and '
                'shared-state-switch blocks confirm every time. ')
        reason = ('prod-guard: session override acknowledged '
                  '(PROD_GUARD_SESSION_OVERRIDE is set) — downgraded from '
                  'deny to a confirmation prompt. ' + grant_note + reason)
    elif severity == DENY and override:
        severity = ASK
        reason = ('prod-guard: override acknowledged (PROD_GUARD_OVERRIDE is '
                  'set) — downgraded from deny to a confirmation prompt. '
                  + reason)

    decision = 'deny' if severity == DENY else 'ask'
    # In bypassPermissions / full-auto mode there is no one to answer an ask;
    # deny blocks identically but feeds the reason back so the agent can
    # route around the target instead of stalling on an unanswerable prompt.
    if decision == 'ask' and data.get('permission_mode') == 'bypassPermissions':
        decision = 'deny'
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': decision,
        'permissionDecisionReason': reason}}))


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001 — fail-open on any infrastructure error
        if os.environ.get('PROD_GUARD_DEBUG') == '1':
            raise
        sys.exit(0)
