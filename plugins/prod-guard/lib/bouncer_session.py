# ---------------------------------------------------------------------
# VENDORED COPY -- do not edit. Source: lib/bouncer_session.py
# Regenerate with: python3 scripts/sync-lib.py
# ---------------------------------------------------------------------
"""Which paths belong to the running session, shared by the guards.

Claude Code gives each session a scratch tree under a per-user temp root, and
two guards must agree on where it is: workspace-guard exempts it from the
outside-workspace check, and branch-guard lets a worktree inside it be removed
with ``--force``. Both answers rest on the same two facts, so they live here.
"""
import os
import tempfile


def claude_tmp_root():
    """Realpath of Claude Code's per-user temp root.

    Claude Code stores each session's background-task output under
    ``<root>/<encoded-project>/<session-uuid>/tasks/<id>.output``. This layout
    is an undocumented internal convention — there is no hook field that names
    it — so we infer the root. If Claude Code ever relocates the dir, paths
    simply stop matching the allow below and revert to ``ask`` (fail-safe), so
    inferring it never weakens the boundary.

    On POSIX the root is ``/tmp/claude-<uid>`` (mode 0700, per-UID). Windows
    has no ``os.getuid()`` and no per-UID suffix: the root is ``claude`` inside
    the per-user temp dir (``%LOCALAPPDATA%\\Temp\\claude``, verified against a
    live install). ``hasattr`` is the discriminator rather than ``os.name``
    because the missing call is the actual condition. The Windows root is
    partly environment-derived (``tempfile.gettempdir()`` honours ``TMP`` /
    ``TEMP``), which does not widen the boundary: a tampered value makes paths
    fail to match and fall back to ``ask``, and the allow below additionally
    requires the *running* session's own uuid as a path segment.
    """
    if hasattr(os, 'getuid'):
        return os.path.realpath('/tmp/claude-%d' % os.getuid())
    return os.path.realpath(os.path.join(tempfile.gettempdir(), 'claude'))


def is_session_tmp_path(rp, session_id, tmp_root):
    """True when resolved path ``rp`` is THIS session's own Claude-managed
    scratch — i.e. inside ``tmp_root`` AND carrying ``session_id`` as a path
    segment (the ``<session-uuid>`` directory).

    Reading back the agent's own background-task output is not the boundary
    workspace-guard guards, so such paths are allowed silently. The scope is
    deliberately per-session, NOT the whole ``/tmp/claude-<uid>`` root: another
    session's or project's task output can contain secrets, and allowing a
    different session to read it would be a cross-context leak. Matched against
    the resolved realpath (not the raw token), so a symlink planted in the temp
    dir that escapes the root resolves outside ``tmp_root`` and is still
    flagged. An empty ``session_id`` (hook field absent) disables the allow. (Q21)
    """
    if not session_id:
        return False
    if rp != tmp_root and not rp.startswith(tmp_root + os.sep):
        return False
    return session_id in rp.split(os.sep)
