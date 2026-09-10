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


class SubstitutionSpanTests(unittest.TestCase):
    """`spans` says where each body's substitution sat, which its text cannot.

    A caller resolving a body's relative paths needs the directory in force at
    that point in the string, and two identical bodies written on either side of
    a `cd` are indistinguishable by text alone (Q169).
    """
    def test_a_span_covers_the_whole_substitution(self):
        text = 'cd sub && echo $(cat ../x)'
        spans = []
        self.assertEqual(['cat ../x'],
                         bp.command_substitutions(text, spans=spans))
        start, end = spans[0]
        self.assertEqual('$(cat ../x)', text[start:end])

    def test_a_backtick_span_covers_its_own_delimiters(self):
        text = 'echo `cat x`'
        spans = []
        bp.command_substitutions(text, spans=spans)
        start, end = spans[0]
        self.assertEqual('`cat x`', text[start:end])

    def test_two_identical_bodies_get_distinct_spans(self):
        # The whole reason a position beats keying on body text.
        text = 'cat $(id) && cd sub && cat $(id)'
        spans = []
        self.assertEqual(['id', 'id'],
                         bp.command_substitutions(text, spans=spans))
        self.assertEqual(2, len(set(spans)))
        for start, end in spans:
            self.assertEqual('$(id)', text[start:end])

    def test_a_skipped_substitution_contributes_no_span(self):
        # Single-quoted and arithmetic are not substitutions, so the spans stay
        # aligned with the bodies rather than counting what was passed over.
        text = "echo '$(a)' $((1+2)) $(b)"
        spans = []
        self.assertEqual(['b'], bp.command_substitutions(text, spans=spans))
        start, end = spans[0]
        self.assertEqual('$(b)', text[start:end])


class HeredocBodySequenceTests(unittest.TestCase):
    """`bodies` reports every heredoc; `expanded` reports a subset (Q169).

    A caller pairing a body with the `<<WORD` the tokenizer still sees has to
    count them the way the tokenizer does, and one quoted delimiter earlier in
    the string shifts every later body by one.
    """
    CMD = "cat <<'A' && cat <<B\nliteral\nA\nlive\nB\n"

    def test_every_body_is_reported_with_its_quoting(self):
        bodies = []
        bp.strip_heredoc_bodies(self.CMD, bodies=bodies)
        self.assertEqual([("literal\nA\n", True), ("live\nB\n", False)], bodies)

    def test_expanded_alone_would_shift_the_index(self):
        bodies, expanded = [], []
        bp.strip_heredoc_bodies(self.CMD, bodies=bodies, expanded=expanded)
        self.assertEqual(["live\nB\n"], expanded)
        self.assertEqual(1, [b for b, _ in bodies].index(expanded[0]))

    def test_own_level_only_reports_only_the_top_levels(self):
        # A substitution's own heredoc stays inside the body the recursion gets,
        # so it must not be counted against a `<<` the outer stream never shows.
        cmd = 'cat <<A && echo "$(cat <<X\nb\nX\n)"\ntop\nA\n'
        bodies = []
        bp.strip_heredoc_bodies(cmd, own_level_only=True, bodies=bodies)
        self.assertEqual([("top\nA\n", False)], bodies)


class CasePatternScanTests(unittest.TestCase):
    """A `case` pattern's `)` needs no opener, so it must not end a `$(…)` (Q81).

    Every command here was run under bash 5.3 while these were written: each
    prints its clause's output followed by the `T` after the substitution,
    which is what says bash read the whole clause as inside it. Only bash 3.2
    agrees with the pre-fix reading, where the body came back as `case $x in a`
    and the clause -- heredocs included -- was never scanned.
    """
    def test_bare_pattern_does_not_close_the_substitution(self):
        self.assertEqual(['case $x in a) cat /etc/passwd;; esac'],
                         bp.command_substitutions(
                             'echo "$(case $x in a) cat /etc/passwd;; esac)" T'))

    def test_parenthesised_pattern_still_works(self):
        self.assertEqual(['case $x in (a) cat /etc/passwd;; esac'],
                         bp.command_substitutions(
                             'echo "$(case $x in (a) cat /etc/passwd;; esac)" T'))

    def test_every_clause_terminator_returns_to_a_pattern(self):
        for term in (';;', ';&', ';;&'):
            body = 'case $x in a) echo P%s b) cat /etc/passwd;; esac' % term
            self.assertEqual([body],
                             bp.command_substitutions('echo "$(%s)" T' % body),
                             term)

    def test_a_nested_case_closes_only_its_own_clause(self):
        body = 'case $x in a) case $y in b) cat /etc/passwd;; esac;; esac'
        self.assertEqual([body], bp.command_substitutions('echo "$(%s)" T' % body))

    def test_esac_may_stand_where_a_pattern_would(self):
        # `case $x in esac` is a clause with no patterns, so the next `)` is
        # the substitution's own.
        self.assertEqual(['case $x in esac'],
                         bp.command_substitutions('echo "$(case $x in esac)" T'))

    def test_a_quoted_pattern_keeps_its_paren_literal(self):
        body = 'case $x in "a)b") cat /etc/passwd;; esac'
        self.assertEqual([body], bp.command_substitutions('echo "$(%s)" T' % body))

    def test_case_is_a_keyword_only_in_command_position(self):
        # `echo case` passes an operand. Reading it as the keyword would swallow
        # the real close and drop a substitution that reads fine today.
        for body in ('echo case', 'grep -c case /dev/null', 'echo esac in case'):
            self.assertEqual([body],
                             bp.command_substitutions('echo "$(%s)" T' % body), body)

    def test_a_keyword_reopens_command_position(self):
        body = 'if true; then case $x in a) cat /etc/passwd;; esac; fi'
        self.assertEqual([body], bp.command_substitutions('echo "$(%s)" T' % body))

    def test_a_heredoc_in_a_clause_is_reached(self):
        # The gap this closes: the clause body was never scanned, so a heredoc
        # written there went with it. An odd quote in that body was a second
        # mechanism, closed since by HeredocInSubstitutionTests below.
        body = "case $x in a) cat <<EOF\n$(cat /etc/passwd)\nEOF\n;; esac"
        self.assertEqual([body], bp.command_substitutions('echo "$(%s)" T' % body))


class HeredocInSubstitutionTests(unittest.TestCase):
    """A heredoc body inside a `$(…)` is data, so the scan steps over it (Q109).

    Read as shell syntax, an apostrophe in the body opened a single-quoted run
    that never closed: the scan ran to end-of-input, returned no terminator,
    and `command_substitutions` yielded nothing -- the whole substitution went
    unexamined. `strip_heredoc_bodies` does not pre-empt this in a `case`
    clause, where its own context tracking ends the substitution at the
    pattern's `)` before the `<<` is reached.
    """
    def test_an_apostrophe_in_a_body_does_not_swallow_the_substitution(self):
        body = "case $x in a) cat <<EOF\nit's fine\nEOF\ncat /etc/passwd;; esac"
        self.assertEqual([body], bp.command_substitutions('echo "$(%s)" T' % body))

    def test_a_tab_stripped_delimiter_is_recognised(self):
        body = "case $x in a) cat <<-EOF\n\tit's fine\n\tEOF\ncat /etc/passwd;; esac"
        self.assertEqual([body], bp.command_substitutions('echo "$(%s)" T' % body))

    def test_a_quoted_delimiter_still_ends_its_body(self):
        body = "case $x in a) cat <<'EOF'\nit's fine\nEOF\ncat /etc/passwd;; esac"
        self.assertEqual([body], bp.command_substitutions('echo "$(%s)" T' % body))

    def test_an_unterminated_body_runs_to_the_end(self):
        # bash swallows a body with no terminator to end-of-input, so no `)`
        # after it can close the substitution -- returning nothing is correct.
        self.assertEqual([], bp.command_substitutions(
            'echo "$(cat <<EOF\nit\'s fine\n)" T'))

    def test_a_herestring_arms_nothing(self):
        body = 'grep pat <<< "it\'s fine"'
        self.assertEqual([body], bp.command_substitutions('echo "$(%s)" T' % body))

    def test_an_arithmetic_shift_arms_nothing(self):
        # `1<<4` is a shift. Armed as a delimiter it would swallow `4));…` as
        # body text and the substitution would never close.
        body = 'n=$((1<<4)); cat /etc/passwd'
        self.assertEqual([body], bp.command_substitutions('echo "$(%s)" T' % body))


class OwnLevelHeredocStripTests(unittest.TestCase):
    """`own_level_only` drops the top level's bodies and copies the rest (Q119).

    A caller re-scanning the raw string for substitution bodies needs the top
    level's heredoc data gone -- an apostrophe in one opens a quoted run that
    swallows the scan -- and needs each substitution's own heredocs left whole,
    terminators included, because that is the text the recursion strips next.
    Stripping every level gives it a body whose `<<WORD` has lost its
    terminator, which is the Q113 trap the recovery exists to avoid.
    """
    def test_a_top_level_body_is_dropped(self):
        self.assertEqual("cat <<EOF\necho after",
                         bp.strip_heredoc_bodies("cat <<EOF\nbody\nEOF\necho after",
                                                 own_level_only=True))

    def test_a_substitutions_own_body_is_copied_through(self):
        cmd = 'echo "$(cat <<X\nb\nX\ncat /outside)"'
        self.assertEqual(cmd, bp.strip_heredoc_bodies(cmd, own_level_only=True))

    def test_the_default_still_strips_every_level(self):
        self.assertEqual('echo "$(cat <<X\ncat /outside)"',
                         bp.strip_heredoc_bodies(
                             'echo "$(cat <<X\nb\nX\ncat /outside)"'))

    def test_an_apostrophe_above_no_longer_hides_the_substitution(self):
        # The row's mechanism at parser level: flat, the `'` in the first body
        # opens a run that swallows the `$(…)` and the scan returns nothing.
        cmd = "cat <<EOF\ndon't\nEOF\necho \"$(cat <<X\nb\nX\ncat /outside)\""
        self.assertEqual(["cat <<X\nb\nX\ncat /outside"],
                         bp.command_substitutions(
                             bp.strip_heredoc_bodies(cmd, own_level_only=True)))
        self.assertEqual([], bp.command_substitutions(cmd))

    def test_a_yielded_body_survives_the_full_strip_the_recursion_runs(self):
        # The property the recovery rests on, and it is about the BODIES rather
        # than the returned string: each still carries its terminator, so the
        # full strip that follows drops the data and leaves the read after it.
        # The returned string itself is not re-strippable -- the top level's own
        # `<<EOF` is disarmed there exactly as the default strip disarms it, and
        # a second pass would swallow the rest (the Q113 trap, one level up).
        cmd = "cat <<EOF\ndon't\nEOF\necho \"$(cat <<X\nb\nX\ncat /outside)\""
        body, = bp.command_substitutions(
            bp.strip_heredoc_bodies(cmd, own_level_only=True))
        self.assertEqual("cat <<X\ncat /outside", bp.strip_heredoc_bodies(body))

    def test_a_backtick_substitution_keeps_its_body_too(self):
        cmd = "echo \"`cat <<X\nb\nX\ncat /outside`\""
        self.assertEqual(cmd, bp.strip_heredoc_bodies(cmd, own_level_only=True))


class CommandHeadTests(unittest.TestCase):
    def test_env_prefix_is_peeled(self):
        self.assertEqual(['cmd', 'arg'], bp.strip_env_prefix(['A=1', 'B=2', 'cmd', 'arg']))

    def test_a_bare_assignment_is_not_a_command(self):
        self.assertEqual([], bp.strip_env_prefix(['A=1']))

    def test_shell_keywords_are_peeled(self):
        self.assertEqual(['cmd'], bp.strip_sh_keywords(['if', 'cmd']))

    def test_an_append_prefix_is_peeled(self):
        # `NAME+=v` assigns in command position exactly as `NAME=v` does; bash
        # 5.3.15 runs `cmd` for both (Q174).
        self.assertEqual(['cmd'], bp.strip_env_prefix(['A+=1', 'cmd']))
        self.assertEqual(['cmd'], bp.strip_env_prefix(['A=1', 'B+=2', 'cmd']))

    def test_a_plus_inside_the_name_is_not_an_assignment(self):
        # `S+P=/x` is `command not found` -- the `+` is only an operator
        # directly before the `=`.
        self.assertEqual(['S+P=/x', 'cmd'],
                         bp.strip_env_prefix(['S+P=/x', 'cmd']))

    def test_a_quoted_keyword_is_not_peeled(self):
        self.assertEqual(['if', 'cmd'], bp.strip_sh_keywords(bp.lex("'if' cmd")))

    def test_a_plain_keyword_is_still_peeled(self):
        self.assertEqual(['cmd'], bp.strip_sh_keywords(bp.lex('if cmd')))


class SplitAssignmentTests(unittest.TestCase):
    """The `+` belongs to the operator, so a name recovered from the token has
    to have it removed -- except after `env`, which is not the shell (Q174)."""

    def test_a_plain_assignment_does_not_append(self):
        self.assertEqual(('A', False, '1'), bp.split_assignment('A=1'))

    def test_an_append_is_reported_with_the_bare_name(self):
        self.assertEqual(('A', True, '1'), bp.split_assignment('A+=1'))

    def test_env_takes_the_plus_as_part_of_the_name(self):
        # `env 'A+=1' cmd` exports `A+` and leaves `A` alone. Measured:
        # `env 'SP+=/x' printenv 'SP+'` prints `/x`.
        self.assertEqual(('A+', False, '1'),
                         bp.split_assignment('A+=1', append_is_operator=False))

    def test_env_and_shell_agree_on_a_plain_assignment(self):
        self.assertEqual(bp.split_assignment('A=1'),
                         bp.split_assignment('A=1', append_is_operator=False))

    def test_an_equals_in_the_value_is_kept(self):
        self.assertEqual(('A', True, 'b=c'), bp.split_assignment('A+=b=c'))

    def test_an_empty_append_value(self):
        self.assertEqual(('A', True, ''), bp.split_assignment('A+='))


class AssignmentTests(unittest.TestCase):
    """Bash decides what a word IS before it removes the quotes (Q170, Q139).

    The table is bash's own answer, taken on 5.3.15 with
    ``bash -c "<word>; printf '[%s]' \"$SP\""``: a set variable prints its
    value, an unset one prints ``[]`` after a ``command not found``. Quoting
    anywhere up to and including the ``=`` disarms the assignment; quoting
    after it is ordinary, which is how a break-glass reason with a space in it
    is written.

    `RUNS_A_COMMAND` is Q139's nine spellings, grouped by the mechanism that
    disarms each -- because that row measured the class and warned it is a
    floor, not a census. A fix written as *quoting the name* passes the first
    six and still arms on ``SP\\=/x``, where the escape falls on the ``=``
    itself and so belongs to neither name nor value. The empty pairs are here
    for the same reason: they put no characters in the token, so only an offset
    can see them.
    """

    ASSIGNS = ('SP=/x', 'SP="/x"', "SP='/x'", 'SP=/x"y"', 'SP=$(echo /x)',
               'SP=one"two"')
    RUNS_A_COMMAND = ("'SP=/x'", '"SP=/x"',            # the whole word quoted
                      "S'P'=/x", 'S"P"=/x',            # part of the name
                      "S''P=/x", 'SP""=/x',            # an empty pair
                      'SP"="/x',                       # the `=` itself
                      r'\SP=/x', r'S\P=/x', r'SP\=/x')  # escaped, not quoted

    def test_a_plain_prefix_assigns(self):
        for word in self.ASSIGNS:
            with self.subTest(word=word):
                self.assertTrue(bp.is_assignment(bp.lex(word)[0]))

    def test_a_quoted_name_or_equals_runs_a_command(self):
        for word in self.RUNS_A_COMMAND:
            with self.subTest(word=word):
                self.assertFalse(bp.is_assignment(bp.lex(word)[0]))

    def test_a_word_that_is_no_assignment_at_all_is_never_one(self):
        for word in ('cat', "'cat'", '1A=x', '-A=x'):
            with self.subTest(word=word):
                self.assertFalse(bp.is_assignment(bp.lex(word)[0]))

    def test_a_plain_str_reads_as_written_plain(self):
        """A hand-built token carries no record, so it keeps the old reading."""
        self.assertTrue(bp.is_assignment('SP=/x'))

    def test_a_quoted_prefix_is_not_peeled_as_env(self):
        toks = bp.lex("'LC_ALL=C' cat /x")
        self.assertEqual(['LC_ALL=C', 'cat', '/x'], bp.strip_env_prefix(toks))

    def test_a_plain_prefix_is_still_peeled_as_env(self):
        toks = bp.lex('LC_ALL=C cat /x')
        self.assertEqual(['cat', '/x'], bp.strip_env_prefix(toks))

    def test_the_offset_is_into_the_stripped_token(self):
        self.assertEqual(2, bp.lex('SP"="/x')[0].quoted_from)
        self.assertIsNone(bp.lex('SP=/x')[0].quoted_from)

    def test_the_quote_characters_are_still_recorded(self):
        """prod-guard reads `.quotes` to tell a `-c` body's expander apart."""
        self.assertEqual(frozenset("'"), bp.lex("'a b'")[0].quotes)
        self.assertEqual(frozenset('"'), bp.lex('"a b"')[0].quotes)
        self.assertEqual(frozenset(), bp.lex('ab')[0].quotes)


class ReservedWordTests(unittest.TestCase):
    """The keyword half of Q170: quoting decides this too, and more bluntly.

    An assignment has an ``=`` for the quoting to sit after, so `is_assignment`
    compares offsets. A reserved word has no such split -- quoting ANY part of
    it makes bash look for a program of that name -- so the test is simply
    whether the word carries a quote at all.

    Measured on bash 5.3.15 with ``cd /tmp; <word> cd /etc; pwd``. The plain
    keyword changes directory (``time``, ``!``) or opens a compound command;
    every quoted spelling prints ``<word>: command not found`` and leaves the
    shell in ``/tmp``, because the ``cd`` is an argument to a program that does
    not exist. ``\\if`` is the case a ``.quotes`` check would miss: it carries
    no quote character and is still not the keyword.
    """

    RUNS_A_COMMAND = ("'if'", '"if"', 'i"f"', r'\if',   # if
                      "'then'", "'do'", "'time'",       # more of SH_KEYWORDS
                      "'!'", "'{'")                     # the punctuation ones

    def test_a_plain_keyword_is_reserved(self):
        for word in ('if', 'then', 'do', 'time', '!', '{', '[['):
            with self.subTest(word=word):
                self.assertTrue(bp.is_reserved_word(bp.lex(word)[0]))

    def test_a_quoted_keyword_runs_a_command(self):
        for word in self.RUNS_A_COMMAND:
            with self.subTest(word=word):
                self.assertFalse(bp.is_reserved_word(bp.lex(word)[0]))

    def test_a_word_that_is_no_keyword_at_all_is_never_one(self):
        for word in ('cat', 'iff', 'IF'):
            with self.subTest(word=word):
                self.assertFalse(bp.is_reserved_word(bp.lex(word)[0]))

    def test_a_plain_str_reads_as_written_plain(self):
        """A hand-built token carries no record, so it keeps the old reading."""
        self.assertTrue(bp.is_reserved_word('if'))

    def test_quoting_after_the_first_character_still_disarms(self):
        """Where the assignment rule and this one part company.

        `SP="/x"` assigns because the quote falls past the `=`; there is no
        `=` here, so the same shape (`i"f"`) is just a command name.
        """
        self.assertTrue(bp.is_assignment(bp.lex('SP="/x"')[0]))
        self.assertFalse(bp.is_reserved_word(bp.lex('i"f"')[0]))


class OperatorTests(unittest.TestCase):
    """The operator half of the same quoting rule.

    A word made entirely of punctuation chars has the TEXT of an operator and
    is not one: `cat ';' f` passes `;` to `cat`. Reading it as a separator
    cuts the command there, so `cat` keeps no operands and `f` is read as a
    command name -- for workspace-guard a positive ALLOW rather than a missed
    prompt, since no guarded reader is left holding a path.

    Measured on bash 5.3.15 in a directory holding only `ok.txt`:
    ``cat ';' ok.txt`` prints ``cat: ;: No such file or directory`` then
    ``hello``, rc=1. So no file named `;` has to exist -- `cat` reports the
    missing operand and reads the rest.
    """

    def test_a_plain_operator_is_one(self):
        for op in (';', '|', '&&', '\n', '(', ')'):
            with self.subTest(op=op):
                self.assertTrue(bp.is_operator(bp.lex('a %s b' % op)[1],
                                               bp.SEPARATORS))

    def test_a_quoted_operator_is_a_word(self):
        for word in ("';'", '";"', r'\;', "'&&'", "'|'", "')'"):
            with self.subTest(word=word):
                self.assertFalse(bp.is_operator(bp.lex('cat %s f' % word)[1],
                                                bp.SEPARATORS))

    def test_a_quoted_redirect_is_a_word(self):
        self.assertFalse(bp.is_operator(bp.lex("cat '>' f")[1], bp.REDIR))
        self.assertTrue(bp.is_operator(bp.lex('cat > f')[1], bp.REDIR))

    def test_a_word_outside_the_vocab_is_never_an_operator(self):
        self.assertFalse(bp.is_operator(bp.lex('cat f')[1], bp.SEPARATORS))

    def test_a_plain_str_reads_as_written_plain(self):
        """A hand-built token carries no record, so it keeps the old reading."""
        self.assertTrue(bp.is_operator(';', bp.SEPARATORS))

    def test_a_quoted_operator_run_is_not_decomposed(self):
        """`split_operator_runs` asks the same question before splitting."""
        self.assertEqual([';', ';'], bp.split_operator_runs(bp.lex('a ;; b')[1:2]))
        self.assertEqual([';;'],
                         [str(t) for t in
                          bp.split_operator_runs(bp.lex("cat ';;' f")[1:2])])


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
