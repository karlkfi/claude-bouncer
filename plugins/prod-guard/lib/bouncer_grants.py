# ---------------------------------------------------------------------
# VENDORED COPY -- do not edit. Source: lib/bouncer_grants.py
# Regenerate with: python3 scripts/sync-lib.py
# ---------------------------------------------------------------------
"""Session-scoped grant store shared by the guards.

A ``PreToolUse`` hook returns ``ask`` and is never told the answer, so it
cannot remember an approval it never saw. ``PostToolUse`` closes the loop: it
fires only when the tool actually ran, which for a downgraded finding means
the human approved the prompt. A guard therefore records nothing at ask time
and everything at post time, and the failure direction falls the right way --
forgetting costs one more prompt, while remembering takes positive evidence
that the tool ran.

The store is deliberately dumb: a per-session JSON file of opaque grant
strings. What a grant *means* is the calling guard's business -- prod-guard
grants exact target names, workspace-guard grants decision shapes, and the
worktree grant names a path prefix -- and those semantics must not migrate in
here, because the three do not agree and an abstraction over them would be
wrong for all of them. What the guards do agree on is the mechanics below:
session-keyed file, first-grant timestamp, atomic replace, opportunistic
sweep, and every error failing toward more prompts.

``namespace`` is the directory segment under ``~/.claude``. A guard that owns
its grants alone passes its own name; grants two guards must both see pass a
shared one, which is the only reason this is a parameter rather than derived.
"""
import json
import os
import re
import time

DEFAULT_TTL = 8 * 3600        # a grant outlives a workday session, not a resumed one
FILE_MAX_AGE = 7 * 86400      # stale session files swept opportunistically


def grants_path(namespace, session_id):
    """State file for one session's grants under ``namespace``, or None when
    it cannot exist (no HOME, no usable session id)."""
    home = os.environ.get('HOME')
    if not home or not isinstance(session_id, str) or not session_id.strip():
        return None
    # A single directory segment. The character class alone is not enough:
    # '.' and '..' both satisfy it, and '..' climbs out of ~/.claude entirely.
    if not namespace or namespace in ('.', '..') \
            or not re.match(r'^[A-Za-z0-9._-]+$', namespace):
        return None
    slug = re.sub(r'[^A-Za-z0-9._-]', '_', session_id.strip())[:80]
    return os.path.join(home, '.claude', namespace, 'session-grants',
                        slug + '.json')


def load_grants(namespace, session_id, now=None, ttl=DEFAULT_TTL):
    """The session's unexpired grants as a set of strings. Any error -- missing
    file, bad JSON, wrong shape -- returns the empty set: a broken store means
    more prompts, never fewer."""
    path = grants_path(namespace, session_id)
    if path is None:
        return set()
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        grants = data.get('grants', [])
    except (OSError, ValueError, AttributeError):
        return set()
    now = time.time() if now is None else now
    out = set()
    for g in grants:
        if not isinstance(g, dict):
            continue
        target, ts = g.get('target'), g.get('ts')
        if isinstance(target, str) and isinstance(ts, (int, float)) \
                and 0 <= now - ts <= ttl:
            out.add(target)
    return out


def record_grants(namespace, session_id, targets, reason, now=None):
    """Append grants for `targets` not already on record. The first-grant
    timestamp is kept, so the TTL never slides. Atomic replace so a torn write
    cannot corrupt the store; any failure is silent and loses only the
    recording, which costs one more prompt."""
    path = grants_path(namespace, session_id)
    if path is None or not targets:
        return
    now = time.time() if now is None else now
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            grants = [g for g in data.get('grants', []) if isinstance(g, dict)]
        except (OSError, ValueError, AttributeError):
            grants = []
        have = {g.get('target') for g in grants}
        for t in sorted(targets):
            if t not in have:
                grants.append({'target': t, 'reason': reason, 'ts': now})
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'grants': grants}, f)
        os.replace(tmp, path)
        sweep_stale(os.path.dirname(path), now)
    except OSError:
        return


def sweep_stale(dirpath, now):
    """Best-effort hygiene: drop session files old enough that their grants all
    expired long ago."""
    try:
        for name in os.listdir(dirpath):
            p = os.path.join(dirpath, name)
            try:
                if now - os.path.getmtime(p) > FILE_MAX_AGE:
                    os.unlink(p)
            except OSError:
                continue
    except OSError:
        return
