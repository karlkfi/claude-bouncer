"""Tests for the shared session-scratch predicate.

workspace-guard's suite drives it through the outside-workspace check and
branch-guard's through `worktree remove --force`. What is checked here is the
boundary itself, which both guards rely on agreeing.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'lib'))
from bouncer_session import claude_tmp_root, is_session_tmp_path  # noqa: E402

SESS = '11111111-2222-3333-4444-555555555555'


class SessionTmpPathTests(unittest.TestCase):
    def setUp(self):
        self.root = claude_tmp_root()
        self.path = os.path.join(self.root, 'proj', SESS, 'scratchpad', 'pr1')

    def test_root_is_resolved(self):
        self.assertEqual(self.root, os.path.realpath(self.root))

    def test_own_session_path(self):
        self.assertTrue(is_session_tmp_path(self.path, SESS, self.root))

    def test_other_session_path(self):
        other = SESS.replace('1', '9')
        self.assertFalse(is_session_tmp_path(self.path, other, self.root))

    def test_empty_session_id(self):
        self.assertFalse(is_session_tmp_path(self.path, '', self.root))

    def test_session_segment_outside_root(self):
        outside = os.path.join(os.sep + 'var', SESS, 'pr1')
        self.assertFalse(is_session_tmp_path(outside, SESS, self.root))

    def test_sibling_root_prefix(self):
        sibling = self.root + '-evil' + os.sep + SESS
        self.assertFalse(is_session_tmp_path(sibling, SESS, self.root))

    def test_session_id_as_substring_only(self):
        partial = os.path.join(self.root, 'proj', SESS + 'x', 'pr1')
        self.assertFalse(is_session_tmp_path(partial, SESS, self.root))


if __name__ == '__main__':
    unittest.main()
