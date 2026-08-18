"""Tests for the optional repo-state checks: push overlap and PR overlap.

Both directions again, and the negative direction carries more weight here than
in the status rules. These checks run against a live repo and a live `gh`, so a
rule that fires too readily denies a push that is fine -- and the session that
copies the suggested rebase publishes a release branch's whole backlog. The
cases below pin the narrowing that costs the most to get wrong: line ranges
rather than paths, a widened range rather than an exact one, and a release
branch left alone.

Git fixtures are real repositories built in a temp directory. `origin/main` is
written straight into `refs/remotes/` -- the check only ever reads it, so a
remote-tracking ref with no remote behind it is the whole of what it needs, and
nothing here touches the network. The `gh` half has no local equivalent, so
`open_prs` and `pr_ranges` are replaced for those cases.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, 'scripts', 'bash-pipe-guard.py')


def load_module():
    spec = importlib.util.spec_from_file_location('bash_pipe_guard', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pg = load_module()

# A file long enough that two edits can be far apart in it. Path intersection
# alone cannot tell those two edits from a collision, which is the whole reason
# these checks read ranges.
NUMBERED = ['line %02d' % i for i in range(1, 41)]


def run_git(root, *args):
    subprocess.run(('git', '-C', root) + args, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write(root, path, lines):
    with open(os.path.join(root, path), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


def edit(root, path, changes):
    """Rewrite ``path`` with ``{line number: replacement}`` applied."""
    lines = list(NUMBERED)
    for number, text in changes.items():
        lines[number - 1] = text
    write(root, path, lines)


def commit(root, message):
    run_git(root, 'add', '-A')
    run_git(root, '-c', 'user.email=t@example.com', '-c', 'user.name=T',
            'commit', '-q', '-m', message)


def build(root, mine, theirs, branch='topic', paths=('f.txt', 'g.txt'),
          attributes=''):
    """A repo where ``branch`` and `origin/main` both moved off a shared base.

    Each side's edits are ``{path: {line number: replacement}}``.
    """
    run_git(root, 'init', '-q', '-b', 'main')
    for path in paths:
        write(root, path, NUMBERED)
    if attributes:
        write(root, '.gitattributes', [attributes])
    commit(root, 'base')

    run_git(root, 'checkout', '-q', '-b', branch)
    for path, changes in mine.items():
        edit(root, path, changes)
    if mine:
        commit(root, 'branch work')

    run_git(root, 'checkout', '-q', 'main')
    for path, changes in theirs.items():
        edit(root, path, changes)
    if theirs:
        commit(root, 'the base moves')
    run_git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    run_git(root, 'checkout', '-q', branch)


def remove_tree(path):
    """rmtree that survives git's read-only object files.

    git writes loose objects mode 444. POSIX unlinks them anyway because the
    parent directory is writable; Windows refuses, so `TemporaryDirectory`
    raises during cleanup and every fixture in this file fails there.
    """
    def force(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    handler = {'onexc': force} if sys.version_info >= (3, 12) else {'onerror': force}
    shutil.rmtree(path, **handler)


class GitFixture(unittest.TestCase):
    """A temp directory per test, cleaned up whatever the test did."""

    def repo(self, *args, **kwargs):
        root = tempfile.mkdtemp()
        self.addCleanup(remove_tree, root)
        build(root, *args, **kwargs)
        return root


class TestRanges(unittest.TestCase):
    """The arithmetic under both checks, exercised without a repo."""

    def test_a_hunk_carries_its_context(self):
        self.assertEqual((7, 13), pg.hunk_range(10, 1))

    def test_a_range_never_starts_before_line_one(self):
        self.assertEqual((1, 5), pg.hunk_range(1, 2))

    def test_an_insertion_spans_the_line_it_follows(self):
        """`-a,0` covers no pre-image line, and still collides beside it."""
        self.assertEqual((7, 13), pg.hunk_range(10, 0))

    def test_ranges_meet_and_miss(self):
        self.assertTrue(pg.ranges_meet([(1, 5)], [(5, 9)]))
        self.assertTrue(pg.ranges_meet([(1, 20)], [(5, 9)]))
        self.assertFalse(pg.ranges_meet([(1, 5)], [(6, 9)]))
        self.assertFalse(pg.ranges_meet([], [(1, 9)]))

    def test_hunks_are_read_from_the_pre_image_side(self):
        """Post-image numbers are each side's own and mean nothing to the
        other; only the shared ancestor's numbering compares."""
        diff = ('diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n'
                '@@ -10,2 +40,3 @@\n'
                'diff --git a/g.txt b/g.txt\n--- a/g.txt\n+++ b/g.txt\n'
                '@@ -1 +1 @@\n')
        self.assertEqual({'f.txt': [(7, 14)], 'g.txt': [(1, 4)]},
                         pg.parse_hunks(diff))

    def test_a_new_file_has_no_pre_image_to_collide_with(self):
        self.assertEqual({}, pg.parse_hunks(
            'diff --git a/new.txt b/new.txt\n--- /dev/null\n+++ b/new.txt\n'
            '@@ -0,0 +1,3 @@\n'))

    def test_a_removed_comment_line_is_not_a_file_header(self):
        """An SQL comment `-- DROP` leaves the diff as `--- DROP`, which reads
        exactly like the header naming a file called `DROP`."""
        diff = ('diff --git a/schema.sql b/schema.sql\n'
                '--- a/schema.sql\n+++ b/schema.sql\n'
                '@@ -10,2 +10,0 @@\n--- DROP TABLE users;\n--- keep this\n'
                '@@ -40 +38 @@\n-old\n+new\n')
        self.assertEqual({'schema.sql': [(7, 14), (37, 43)]},
                         pg.parse_hunks(diff))


class TestPushOverlap(GitFixture):

    CFG = {'base_ref': 'origin/main'}

    def test_the_same_lines_collide(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {10: 'theirs'}})
        self.assertEqual(['f.txt'], pg.push_overlap(root, self.CFG))

    def test_opposite_ends_of_one_file_do_not(self):
        """Path intersection reads this as a collision. It is not one, and
        reading it as one is what makes the check unusable."""
        root = self.repo({'f.txt': {2: 'mine'}}, {'f.txt': {38: 'theirs'}})
        self.assertEqual([], pg.push_overlap(root, self.CFG))

    def test_edits_within_six_lines_collide(self):
        """Each range spans the three lines of context the diff carries either
        side, so two edits four apart still share one."""
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {14: 'theirs'}})
        self.assertEqual(['f.txt'], pg.push_overlap(root, self.CFG))

    def test_edits_seven_lines_apart_do_not(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {17: 'theirs'}})
        self.assertEqual([], pg.push_overlap(root, self.CFG))

    def test_different_files_do_not(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {'g.txt': {10: 'theirs'}})
        self.assertEqual([], pg.push_overlap(root, self.CFG))

    def test_a_base_that_has_not_moved_is_silent(self):
        """A branch behind nothing has nothing to rebase onto."""
        root = self.repo({'f.txt': {10: 'mine'}}, {})
        self.assertEqual([], pg.push_overlap(root, self.CFG))

    def test_a_release_branch_is_never_told_to_rebase(self):
        """Rebasing `release-1.5` onto `main` publishes everything merged since
        the tag. Wrong answer, not a noisy one."""
        for branch in ('release-1.5', 'release/1.5', 'hotfix/urgent', 'v2.4'):
            with self.subTest(branch):
                root = self.repo({'f.txt': {10: 'mine'}},
                                 {'f.txt': {10: 'theirs'}}, branch=branch)
                self.assertEqual([], pg.push_overlap(root, self.CFG))

    def test_a_topic_branch_named_like_neither_still_fires(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {10: 'theirs'}},
                         branch='claude/fix-the-thing')
        self.assertEqual(['f.txt'], pg.push_overlap(root, self.CFG))

    def test_release_patterns_are_configurable(self):
        cfg = dict(self.CFG, release_patterns=['^stabilization-'])
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {10: 'theirs'}},
                         branch='stabilization-7')
        self.assertEqual([], pg.push_overlap(root, cfg))
        self.assertEqual(['f.txt'], pg.push_overlap(root, self.CFG),
                         'the default patterns should not know this name')

    def test_a_detached_head_is_silent(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {10: 'theirs'}})
        run_git(root, 'checkout', '-q', '--detach')
        self.assertEqual([], pg.push_overlap(root, self.CFG))

    def test_a_base_ref_that_is_not_there_is_silent(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {10: 'theirs'}})
        self.assertEqual([], pg.push_overlap(root, {'base_ref': 'origin/nope'}))

    def test_a_directory_that_is_not_a_repo_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], pg.push_overlap(tmp, self.CFG))


class TestOverlapIgnore(GitFixture):
    """Paths contended by construction, and the case where discounting them is
    still wrong."""

    CFG = {'base_ref': 'origin/main', 'overlap_ignore': ['f.txt']}

    def setUp(self):
        probe = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {20: 'theirs'}})
        if pg.merge_conflicts(probe, 'origin/main') is None:
            self.skipTest('git here has no `merge-tree --write-tree`')

    def test_an_ignored_path_is_discounted_when_the_merge_is_clean(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {14: 'theirs'}})
        self.assertEqual([], pg.push_overlap(root, self.CFG))
        self.assertEqual(['f.txt'], pg.push_overlap(root, {}),
                         'without the ignore it is an overlap')

    def test_an_ignored_path_still_counts_when_the_merge_fails(self):
        """A merge driver refuses some of these -- a row deleted on one side
        and edited on the other -- so the discount asks git first."""
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {10: 'theirs'}})
        self.assertEqual(['f.txt'], pg.push_overlap(root, self.CFG))

    def test_a_glob_covers_a_directory(self):
        cfg = {'base_ref': 'origin/main', 'overlap_ignore': ['*.txt']}
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {14: 'theirs'}})
        self.assertEqual([], pg.push_overlap(root, cfg))

    def test_an_unignored_path_is_unaffected(self):
        root = self.repo({'g.txt': {10: 'mine'}}, {'g.txt': {10: 'theirs'}})
        self.assertEqual(['g.txt'], pg.push_overlap(root, self.CFG))


class TestPrOverlap(GitFixture):
    """The `gh` half. `open_prs` and `pr_ranges` stand in for the two calls
    that would otherwise need a network and a token."""

    CFG = {'base_ref': 'origin/main'}

    def stub(self, prs, ranges):
        """Replace the two `gh` probes for the duration of one test."""
        real_prs, real_ranges = pg.open_prs, pg.pr_ranges
        pg.open_prs = lambda root: prs
        pg.pr_ranges = lambda root, number: ranges.get(number)
        self.addCleanup(setattr, pg, 'open_prs', real_prs)
        self.addCleanup(setattr, pg, 'pr_ranges', real_ranges)

    @staticmethod
    def pr(number, branch, paths, title='some work'):
        return {'number': number, 'title': title, 'headRefName': branch,
                'files': [{'path': p} for p in paths]}

    def test_an_open_pr_on_the_same_lines(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {})
        self.stub([self.pr(7, 'other', ['f.txt'])], {7: {'f.txt': [(8, 14)]}})
        self.assertEqual([(7, 'some work', ['f.txt'], True)],
                         pg.pr_overlap(root, self.CFG))

    def test_an_open_pr_elsewhere_in_the_same_file(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {})
        self.stub([self.pr(7, 'other', ['f.txt'])], {7: {'f.txt': [(30, 34)]}})
        self.assertEqual([], pg.pr_overlap(root, self.CFG))

    def test_a_pr_on_another_file(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {})
        self.stub([self.pr(7, 'other', ['g.txt'])], {7: {'g.txt': [(8, 14)]}})
        self.assertEqual([], pg.pr_overlap(root, self.CFG))

    def test_this_branch_own_pr_is_not_an_overlap(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {})
        self.stub([self.pr(7, 'topic', ['f.txt'])], {7: {'f.txt': [(8, 14)]}})
        self.assertEqual([], pg.pr_overlap(root, self.CFG))

    def test_a_failed_diff_fetch_falls_back_to_the_path(self):
        """Marked as such, so the reason does not claim a precision it does
        not have."""
        root = self.repo({'f.txt': {10: 'mine'}}, {})
        self.stub([self.pr(7, 'other', ['f.txt'])], {})
        self.assertEqual([(7, 'some work', ['f.txt'], False)],
                         pg.pr_overlap(root, self.CFG))

    def test_the_diff_fetches_are_capped(self):
        """Past the cap an entry rests on the shared path alone, and says so."""
        root = self.repo({'f.txt': {10: 'mine'}}, {})
        prs = [self.pr(n, 'other-%d' % n, ['f.txt'])
               for n in range(1, pg.MAX_PR_DIFFS + 3)]
        self.stub(prs, {n: {'f.txt': [(8, 14)]} for n, _ in enumerate(prs, 1)})
        got = pg.pr_overlap(root, self.CFG)
        self.assertEqual(pg.MAX_PR_DIFFS, sum(1 for h in got if h[3]))
        self.assertEqual(len(prs), len(got))

    def test_an_ignored_path_is_discounted(self):
        root = self.repo({'f.txt': {10: 'mine'}}, {})
        self.stub([self.pr(7, 'other', ['f.txt'])], {7: {'f.txt': [(8, 14)]}})
        self.assertEqual([], pg.pr_overlap(
            root, dict(self.CFG, overlap_ignore=['f.txt'])))

    def test_gh_saying_nothing_is_silence(self):
        """Offline, or a rate-limited token: a missed catch, never a deny."""
        root = self.repo({'f.txt': {10: 'mine'}}, {})
        self.stub(None, {})
        self.assertEqual([], pg.pr_overlap(root, self.CFG))

    def test_a_branch_with_no_changes_asks_nobody(self):
        root = self.repo({}, {})
        self.stub([self.pr(7, 'other', ['f.txt'])], {7: {'f.txt': [(8, 14)]}})
        self.assertEqual([], pg.pr_overlap(root, self.CFG))


class TestDispatch(GitFixture):
    """Which commands reach the checks at all."""

    def setUp(self):
        self.root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {10: 'theirs'}})
        with open(os.path.join(REPO, 'pipe-guard.json'), encoding='utf-8') as fh:
            shipped = json.load(fh)
        shipped['repo_state'] = {'base_ref': 'origin/main'}
        self.reg = pg.Registry(shipped)
        self.repo_arg = pg.Repo(self.root, self.reg.repo_state)

    def decide(self, cmd, background=False):
        return pg.decide(cmd, background, self.reg, repo=self.repo_arg)

    def test_a_push_is_denied(self):
        reason = self.decide('git push -u origin HEAD')
        self.assertIn('has moved since this branch left it', reason)
        self.assertIn('f.txt', reason)
        self.assertIn('PIPE_GUARD_OVERRIDE=<reason>', reason)

    def test_the_reason_offers_a_way_out_that_fits_the_rule(self):
        """No exit status is involved here, so the status wording would be an
        answer to a question nobody asked."""
        reason = self.decide('git push -u origin HEAD')
        self.assertIn('overlap is known and deliberate', reason)
        self.assertNotIn('status genuinely does not matter', reason)

    def test_the_override_prefix_still_works(self):
        self.assertFalse(self.decide(
            'PIPE_GUARD_OVERRIDE=known conflict git push'))

    def test_a_command_that_merely_names_a_push_is_not_one(self):
        for cmd in ('git commit -m "git push landed the fix"',
                    'grep -rn "git push" docs/',
                    'echo git push',
                    "cat <<'EOF'\ngit push\nEOF"):
            with self.subTest(cmd):
                self.assertFalse(self.decide(cmd))

    def test_a_dry_run_or_a_delete_changes_no_base(self):
        for cmd in ('git push --dry-run', 'git push -n origin HEAD',
                    'git push --delete origin stale', 'git push --help'):
            with self.subTest(cmd):
                self.assertFalse(self.decide(cmd))

    def test_a_wrapped_push_is_still_a_push(self):
        self.assertTrue(self.decide('time git push origin HEAD'))

    def test_the_status_rules_answer_first(self):
        """A piped push loses its status, which is the nearer cause."""
        self.assertIn("exit status is the filter's",
                      self.decide('git push 2>&1 | tail -3'))

    def test_off_unless_the_project_asked(self):
        """The negative control for the whole feature: the same repo, the same
        push, and no `repo_state` key."""
        reg = pg.Registry({})
        self.assertFalse(pg.decide('git push -u origin HEAD', False, reg))
        self.assertFalse(pg.decide('git push -u origin HEAD', False, reg,
                                   repo=None))


class TestRegistryWiring(unittest.TestCase):

    def test_absent_means_off(self):
        self.assertIsNone(pg.Registry({}).repo_state)

    def test_an_empty_object_is_still_presence(self):
        self.assertEqual({}, pg.Registry({'repo_state': {}}).repo_state)

    def test_the_shipped_registry_leaves_it_off(self):
        """Off by default: these checks shell out, and a repo that never asked
        for them should never pay for them."""
        with open(os.path.join(REPO, 'pipe-guard.json'), encoding='utf-8') as fh:
            self.assertIsNone(pg.Registry(json.load(fh)).repo_state)

    def test_a_project_file_turns_it_on(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, '.claude'))
            path = os.path.join(root, '.claude', 'pipe-guard.json')
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump({'repo_state': {'base_ref': 'origin/trunk'}}, fh)
            reg = pg.load_registry(root)
            self.assertEqual({'base_ref': 'origin/trunk'}, reg.repo_state)
            self.assertTrue(reg.gates, 'the defaults must survive it')


class TestFailSilent(unittest.TestCase):
    """Every probe here shells out, and the hook runs on every Bash call."""

    def test_a_missing_binary_is_not_a_crash(self):
        status, out = pg.capture(('definitely-not-a-real-binary',), REPO)
        self.assertIsNone(status)
        self.assertEqual('', out)

    def test_a_failing_git_returns_nothing(self):
        self.assertIsNone(pg.git(REPO, 'rev-parse', 'refs/heads/no-such-branch'))

    def test_repo_state_off_needs_no_repo(self):
        self.assertIsNone(pg.repo_state('/no/such/dir', pg.Registry({})))

    def test_a_cwd_outside_a_repo_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = pg.Registry({'repo_state': {}})
            self.assertIsNone(pg.repo_state(tmp, reg))


class TestHookEndToEnd(GitFixture):
    """Through the process, with the project registry Claude Code would read."""

    def run_hook(self, cwd, command):
        payload = {'tool_name': 'Bash', 'cwd': cwd,
                   'tool_input': {'command': command}}
        proc = subprocess.run([sys.executable, SCRIPT], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, CLAUDE_PROJECT_DIR=cwd))
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc.stdout.strip()

    def project(self, config):
        root = self.repo({'f.txt': {10: 'mine'}}, {'f.txt': {10: 'theirs'}})
        os.makedirs(os.path.join(root, '.claude'))
        with open(os.path.join(root, '.claude', 'pipe-guard.json'), 'w',
                  encoding='utf-8') as fh:
            json.dump(config, fh)
        return root

    def test_a_configured_repo_denies_the_push(self):
        root = self.project({'repo_state': {'base_ref': 'origin/main'}})
        out = self.run_hook(root, 'git push -u origin HEAD')
        payload = json.loads(out)['hookSpecificOutput']
        self.assertEqual('deny', payload['permissionDecision'])
        self.assertIn('f.txt', payload['permissionDecisionReason'])

    def test_the_same_repo_without_the_key_says_nothing(self):
        root = self.project({'gates': [r'^bazelisk(\s|$)']})
        self.assertEqual('', self.run_hook(root, 'git push -u origin HEAD'))


if __name__ == '__main__':
    unittest.main()
