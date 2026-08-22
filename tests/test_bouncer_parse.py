r"""Tests for the parser shared by every claude-bouncer guard.

Two of these lock in behaviour that only exists because the copies were merged.
Before the merge each guard had a version of `strip_heredoc_bodies` and
`strip_comments`, and each version was missing something the other had:
workspace-guard could not fold a `\`-newline continuation, and
exit-status-guard could not see a heredoc whose body held an unbalanced quote.
Neither gap was visible from inside the repo that had it -- both suites were
green. They are asserted here so a future edit cannot quietly reintroduce
either one.
"""
import os
import sys
import unittest
from importlib import util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, 'lib', 'bouncer_parse.py')


def load():
    spec = util.spec_from_file_location('bouncer_parse', LIB)
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bp = load()


class StripCommentsTests(unittest.TestCase):
    def test_unquoted_comment_goes_and_its_newline_stays(self):
        # The newline has to survive: shlex's own comment handling eats it, and
        # the next line's tokens then merge into the commented command.
        self.assertEqual('echo hi \ncat x', bp.strip_comments('echo hi # note\ncat x'))

    def test_quoted_hash_is_text(self):
        self.assertEqual("echo 'a # b'", bp.strip_comments("echo 'a # b'"))
        self.assertEqual('echo "a # b"', bp.strip_comments('echo "a # b"'))

    def test_mid_word_hash_is_not_a_comment(self):
        # bash starts a comment only at the start of a word; shlex does not.
        self.assertEqual('echo file#1', bp.strip_comments('echo file#1'))

    def test_continuation_is_folded(self):
        # Came from exit-status-guard. Left in place, `tokenize` makes the
        # newline a command boundary and an `&&` chain written across two lines
        # reads as a `;` sequence -- a different rule, and the wrong verdict.
        self.assertEqual('make check  && echo ok',
                         bp.strip_comments('make check \\\n && echo ok'))


class StripHeredocBodiesTests(unittest.TestCase):
    def test_body_and_terminator_go(self):
        self.assertEqual('cat <<EOF\n', bp.strip_heredoc_bodies('cat <<EOF\nbody\nEOF\n'))

    def test_tab_stripping_form(self):
        self.assertEqual('cat <<-EOF\n', bp.strip_heredoc_bodies('cat <<-EOF\n\tb\n\tEOF\n'))

    def test_unquoted_delimiter_body_comes_back_expanded(self):
        expanded = []
        bp.strip_heredoc_bodies('cat <<EOF\nbody\nEOF\n', expanded)
        self.assertEqual(['body\nEOF\n'], expanded)

    def test_quoted_delimiter_body_does_not(self):
        expanded = []
        bp.strip_heredoc_bodies("cat <<'EOF'\nbody\nEOF\n", expanded)
        self.assertEqual([], expanded)

    def test_heredoc_inside_a_quoted_substitution_with_a_stray_quote(self):
        # Came from workspace-guard. The other copy pre-scanned for the closing
        # `)` while tracking quotes, so the unbalanced `"` in the body read as
        # an unterminated substitution and the whole body survived -- and a
        # commit message containing a quote is the shape that produces it.
        cmd = 'git commit -F "$(cat <<\'MSG\'\nhello "world\nMSG\n)"'
        self.assertEqual('git commit -F "$(cat <<\'MSG\'\n)"',
                         bp.strip_heredoc_bodies(cmd))

    def test_arithmetic_shift_is_not_a_heredoc(self):
        self.assertEqual('echo $((1<<3))', bp.strip_heredoc_bodies('echo $((1<<3))'))
        self.assertEqual('((a<<b)); echo hi', bp.strip_heredoc_bodies('((a<<b)); echo hi'))

    def test_here_string_is_a_different_operator(self):
        self.assertEqual('grep x <<< "$v"', bp.strip_heredoc_bodies('grep x <<< "$v"'))

    def test_quoted_operator_is_text(self):
        self.assertEqual("echo '<<EOF' ; echo real",
                         bp.strip_heredoc_bodies("echo '<<EOF' ; echo real"))

    def test_unterminated_body_is_swallowed_and_reportable(self):
        # Bash hands an unterminated body over as data, so it is stripped. A
        # guard that would rather keep judging the text reads it back out.
        cmd = "cat <<'EOF'\nkubectl delete ns payments"
        seen = []
        self.assertEqual("cat <<'EOF'\n", bp.strip_heredoc_bodies(cmd, None, seen))
        self.assertEqual(['kubectl delete ns payments'], seen)

    def test_terminated_body_is_not_reported_as_unterminated(self):
        seen = []
        bp.strip_heredoc_bodies('cat <<EOF\nbody\nEOF\n', None, seen)
        self.assertEqual([], seen)


class LexTests(unittest.TestCase):
    def test_operators_become_their_own_tokens(self):
        self.assertEqual(['a', '&&', 'b', '|', 'c'], self.lex('a && b | c'))

    def test_pipe_both_streams_is_one_operator(self):
        # `|&` split into `|` and `&` reads as a backgrounded command that never
        # ran. Only exit-status-guard's copy had it in SEPARATORS.
        self.assertEqual(['a', '|&', 'b'], self.lex('a |& b'))

    def test_newline_is_a_boundary_not_whitespace(self):
        self.assertEqual(['a', '\n', 'b'], self.lex('a\nb'))

    def test_unbalanced_quote_raises_so_callers_defer(self):
        with self.assertRaises(ValueError):
            bp.lex('echo "unclosed')

    def lex(self, cmd):
        return bp.glue_dollar_paren(bp.split_operator_runs(bp.lex(cmd)))


class CommandSubstitutionTests(unittest.TestCase):
    def test_single_quoted_substitution_is_literal(self):
        self.assertEqual([], bp.command_substitutions("echo '$(rm -rf /)'"))

    def test_double_quoted_substitution_is_live(self):
        self.assertEqual(['id'], bp.command_substitutions('echo "$(id)"'))

    def test_quotes_off_makes_every_substitution_live(self):
        # How bash reads an unquoted heredoc body: the apostrophe in a `don't`
        # must not switch the scanner off for the rest of the body.
        self.assertEqual(['id'], bp.command_substitutions("don't $(id)", quotes=False))

    def test_arithmetic_holds_no_command(self):
        self.assertEqual([], bp.command_substitutions('echo $((1+2))'))


class CommandHeadTests(unittest.TestCase):
    def test_env_prefix_is_peeled(self):
        self.assertEqual(['cmd', 'arg'], bp.strip_env_prefix(['A=1', 'B=2', 'cmd', 'arg']))

    def test_a_bare_assignment_is_not_a_command(self):
        self.assertEqual([], bp.strip_env_prefix(['A=1']))

    def test_shell_keywords_are_peeled(self):
        self.assertEqual(['cmd'], bp.strip_sh_keywords(['if', 'cmd']))


class VendoringTests(unittest.TestCase):
    """The copies under each plugin are what actually ship."""

    def test_every_plugin_carries_an_identical_copy(self):
        with open(LIB) as f:
            canonical = f.read()
        plugins = os.path.join(ROOT, 'plugins')
        names = sorted(d for d in os.listdir(plugins)
                       if os.path.isdir(os.path.join(plugins, d)))
        self.assertTrue(names, 'no plugins found')
        for name in names:
            copy = os.path.join(plugins, name, 'lib', 'bouncer_parse.py')
            self.assertTrue(os.path.isfile(copy), 'missing vendored copy: %s' % name)
            with open(copy) as f:
                body = f.read()
            self.assertIn(canonical, body,
                          '%s has a stale copy; run scripts/sync-lib.py' % name)



class ReadmeTests(unittest.TestCase):
    """The root README's table names a version per plugin, which goes stale
    silently: nothing installs from it, so nobody finds out by being wrong."""

    def test_version_table_matches_the_manifest(self):
        import json
        import re
        with open(os.path.join(ROOT, '.claude-plugin', 'marketplace.json')) as f:
            entries = {p['name']: p['version'] for p in json.load(f)['plugins']}
        with open(os.path.join(ROOT, 'README.md')) as f:
            readme = f.read()
        # The leading cell is the plugin's icon, so the name cell is optionally
        # preceded by one. assertEqual below is what keeps this honest: a regex
        # that stops matching finds fewer than five rows and fails.
        rows = dict(re.findall(
            r'^\|(?:[^|]*\|)? \[([a-z-]+)\]\(plugins/[a-z-]+\) \| ([0-9][^ |]*) \|',
            readme, re.M))
        self.assertEqual(entries, rows,
                         'README version table and marketplace.json disagree')

if __name__ == '__main__':
    unittest.main()
