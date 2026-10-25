"""Tests for the shared session-grant store.

prod-guard's suite already covers the mechanics through its own wrappers (TTL,
corruption, the non-sliding first-grant timestamp), and those keep passing
unchanged, which is what shows the migration preserved behaviour. What is new
here is the `namespace` parameter -- three consumers now share one store and
they must not see each other's grants, nor be able to steer the path.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'lib'))
import bouncer_grants as grants  # noqa: E402


class GrantStoreTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self._home = os.environ.get('HOME')
        os.environ['HOME'] = self.home
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self._home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self._home

    def test_a_grant_round_trips(self):
        grants.record_grants('alpha', 's1', {'x'}, 'why')
        self.assertEqual({'x'}, grants.load_grants('alpha', 's1'))

    def test_namespaces_do_not_see_each_other(self):
        """The isolation the three consumers depend on: prod-guard's target
        strings must never satisfy workspace-guard's shape lookup."""
        grants.record_grants('alpha', 's1', {'x'}, 'why')
        self.assertEqual(set(), grants.load_grants('beta', 's1'))

    def test_a_shared_namespace_is_how_two_guards_agree(self):
        """The converse, and the contract behind the worktree grant: one guard
        writes and another reads, by using the same namespace."""
        grants.record_grants('bouncer', 's1', {'/tmp/wt'}, 'why')
        self.assertEqual({'/tmp/wt'}, grants.load_grants('bouncer', 's1'))

    def test_sessions_do_not_see_each_other(self):
        grants.record_grants('alpha', 's1', {'x'}, 'why')
        self.assertEqual(set(), grants.load_grants('alpha', 's2'))

    def test_a_traversing_namespace_yields_no_path(self):
        """The namespace is a directory segment, so it must not be able to
        steer the write out of ~/.claude."""
        for bad in ('../evil', 'a/b', '', 'x\x00y', '.'):
            self.assertIsNone(grants.grants_path(bad, 's1'), bad)

    def test_an_unusable_session_yields_no_path(self):
        for bad in (None, '', '   ', 123):
            self.assertIsNone(grants.grants_path('alpha', bad), repr(bad))

    def test_a_session_id_is_sanitised_into_one_filename(self):
        path = grants.grants_path('alpha', '../../x y/z')
        self.assertEqual(os.path.join(self.home, '.claude', 'alpha',
                                      'session-grants'),
                         os.path.dirname(path))
        self.assertNotIn('/', os.path.basename(path)[:-5])

    def test_an_expired_grant_is_not_returned(self):
        grants.record_grants('alpha', 's1', {'x'}, 'why',
                             now=time.time() - grants.DEFAULT_TTL - 1)
        self.assertEqual(set(), grants.load_grants('alpha', 's1'))

    def test_a_caller_may_set_its_own_ttl(self):
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=time.time() - 100)
        self.assertEqual(set(), grants.load_grants('alpha', 's1', ttl=50))
        self.assertEqual({'x'}, grants.load_grants('alpha', 's1', ttl=500))

    def test_a_corrupt_store_reads_empty_rather_than_raising(self):
        """Fail toward more prompts: an unreadable store grants nothing."""
        path = grants.grants_path('alpha', 's1')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f:
            f.write('{not json')
        self.assertEqual(set(), grants.load_grants('alpha', 's1'))

    def test_recording_nothing_creates_no_file(self):
        grants.record_grants('alpha', 's1', set(), 'why')
        self.assertFalse(os.path.exists(grants.grants_path('alpha', 's1')))

    def test_the_first_timestamp_survives_a_second_record(self):
        """The TTL must not slide, or a busy session never re-prompts."""
        early = time.time() - 100
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=early)
        grants.record_grants('alpha', 's1', {'x', 'y'}, 'why', now=time.time())
        with open(grants.grants_path('alpha', 's1')) as f:
            by_target = {g['target']: g['ts'] for g in json.load(f)['grants']}
        self.assertEqual(early, by_target['x'])
        self.assertNotEqual(early, by_target['y'])

    def _entries(self):
        with open(grants.grants_path('alpha', 's1')) as f:
            return json.load(f)['grants']

    def test_an_approval_after_expiry_renews_the_grant(self):
        """Q307: an expired entry used to block the re-record, so every later
        command in the session asked."""
        t0 = 1_000_000.0
        grants.record_grants('alpha', 's1', {'x'}, 'first', now=t0)
        later = t0 + 9 * 3600
        self.assertEqual(set(), grants.load_grants('alpha', 's1', now=later))
        grants.record_grants('alpha', 's1', {'x'}, 'second', now=later)
        self.assertEqual({'x'}, grants.load_grants('alpha', 's1', now=later))
        self.assertEqual([{'target': 'x', 'reason': 'second', 'ts': later}],
                         self._entries())

    def test_a_grant_live_at_exactly_the_ttl_is_not_extended(self):
        t0 = 1_000_000.0
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=t0)
        at = t0 + grants.DEFAULT_TTL
        self.assertEqual({'x'}, grants.load_grants('alpha', 's1', now=at))
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=at)
        self.assertEqual([t0], [g['ts'] for g in self._entries()])

    def test_a_grant_one_second_past_the_ttl_is_replaced(self):
        t0 = 1_000_000.0
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=t0)
        past = t0 + grants.DEFAULT_TTL + 1
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=past)
        self.assertEqual([past], [g['ts'] for g in self._entries()])

    def test_expiry_on_record_follows_the_callers_ttl(self):
        """A caller loading with a short TTL must record with the same one, or
        a grant load calls expired is one record still calls live."""
        t0 = 1_000_000.0
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=t0, ttl=50)
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=t0 + 100, ttl=50)
        self.assertEqual({'x'}, grants.load_grants('alpha', 's1',
                                                   now=t0 + 100, ttl=50))

    def test_a_malformed_entry_does_not_block_recording(self):
        path = grants.grants_path('alpha', 's1')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f:
            json.dump({'grants': [{'target': ['x'], 'ts': 0}]}, f)
        grants.record_grants('alpha', 's1', {'x'}, 'why')
        self.assertEqual({'x'}, grants.load_grants('alpha', 's1'))

    def test_a_future_timestamp_is_replaced_rather_than_kept(self):
        """load_grants reads a timestamp ahead of the clock as expired, so
        record must too, or a skewed entry blocks renewal for good."""
        t0 = 1_000_000.0
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=t0 + 100)
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=t0)
        self.assertEqual({'x'}, grants.load_grants('alpha', 's1', now=t0))
        self.assertEqual([t0], [g['ts'] for g in self._entries()])

    def test_a_live_duplicate_keeps_the_target_without_a_new_entry(self):
        path = grants.grants_path('alpha', 's1')
        os.makedirs(os.path.dirname(path))
        t0 = 1_000_000.0
        with open(path, 'w') as f:
            json.dump({'grants': [{'target': 'x', 'ts': t0 - 9 * 3600},
                                  {'target': 'x', 'ts': t0 - 60}]}, f)
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=t0)
        self.assertEqual([t0 - 9 * 3600, t0 - 60],
                         [g['ts'] for g in self._entries()])

    def test_renewal_leaves_other_targets_alone(self):
        t0 = 1_000_000.0
        grants.record_grants('alpha', 's1', {'x', 'y'}, 'why', now=t0)
        later = t0 + 9 * 3600
        grants.record_grants('alpha', 's1', {'x'}, 'why', now=later)
        self.assertEqual({'x': later, 'y': t0},
                         {g['target']: g['ts'] for g in self._entries()})


if __name__ == '__main__':
    unittest.main()
