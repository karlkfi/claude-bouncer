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

    def test_an_assignment_subscript_holds_no_comment(self):
        # bash 5.3.15 reads `FOO[a #b]=x cat f` as a prefix and prints f (Q217).
        for cmd in ('FOO[a #b]=x cat f', 'X=1 FOO[a #b]+=x cat f'):
            with self.subTest(cmd=cmd):
                self.assertEqual(cmd, bp.strip_comments(cmd))
        # Outside an assignment, bash starts the comment there.
        self.assertEqual('echo a[x ', bp.strip_comments('echo a[x #y]'))

    def test_a_substitution_close_holds_no_comment(self):
        # bash 5.3.15 prints each `#b` as part of its word and runs `id`: the
        # close of a substitution does not end the word (Q264). Read as a
        # comment, the `#b` took the `; id` with it.
        for cmd in ('echo $(true)#b; id', 'cat <(true)#b; id', 'echo >(true)#b; id',
                    'echo $((1))#b; id', 'x=$(cat <(echo a)#b); id',
                    'echo $(case x in a) echo;; esac)#b; id',
                    'echo $(echo ")")#b; id'):
            with self.subTest(cmd=cmd):
                self.assertEqual(cmd, bp.strip_comments(cmd))
        # A continuation joins `$(true)` and `#b` into the same word.
        self.assertEqual('echo $(true)#b; id', bp.strip_comments('echo $(true)\\\n#b; id'))
        # A subshell's close does end the word, and so does a space.
        self.assertEqual('(true)', bp.strip_comments('(true)#b; id'))
        self.assertEqual('echo $(true) ', bp.strip_comments('echo $(true) #b; id'))

    def test_a_backtick_or_brace_expansion_holds_no_comment(self):
        # bash 5.3.15 runs `id` after each: a comment in backticks ends at the
        # closing backtick, and a `#` in `${...}` is text (Q254).
        for cmd in ('echo `true # c` ; id', 'echo `true # c`#x ; id',
                    'echo "`echo "a # c"`" ; id', 'echo ${x:- #} ; id',
                    'echo ${x:-${y:- #}} ; id', 'echo ${x:-$(echo }) #} ; id',
                    'echo ${x:-`echo } #`} ; id', 'echo "${x:-" #"}" ; id',
                    "echo ${x:-$'\\'} #'} ; id",
                    # A `"` in a `$(...)` in double quotes ends nothing (Q257).
                    'echo "$(echo "a # c")" ; id'):
            with self.subTest(cmd=cmd):
                self.assertEqual(cmd, bp.strip_comments(cmd))
        self.assertEqual('echo ${x:-a #} ; id',
                         bp.strip_comments('echo ${x:-a\\\n #} ; id'))
        # A quoted `}` does not close one, and a bare `{` does not nest, so the
        # comment after the real close still goes.
        for cmd in ('echo ${x:-"}"} #c', "echo ${x:-'}'} #c", 'echo ${x:-\\}} #c',
                    'echo ${x:-{a} #c}', 'echo ${#x} #c', 'echo $# #c',
                    'echo `echo a`#x #c'):
            with self.subTest(cmd=cmd):
                self.assertEqual(cmd[:cmd.rindex(' #c') + 1], bp.strip_comments(cmd))
        # Unterminated, each is read as before.
        self.assertEqual('echo ${x:- ', bp.strip_comments('echo ${x:- # c'))
        self.assertEqual('echo `true ', bp.strip_comments('echo `true # c'))


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

    def test_a_word_knows_whether_an_operator_followed_it_directly(self):
        self.assertTrue(bp.lex('echo 2>f')[1].glued)
        self.assertFalse(bp.lex('echo 2 > f')[1].glued)
        self.assertFalse(bp.lex('echo 2 >f')[1].glued)

    def test_a_backtick_substitution_is_one_word(self):
        # bash parses the body on its own, so neither its spaces nor its `;`
        # split the word it sits in (Q274). Split, each fragment was judged as
        # a path or a command of its own.
        for cmd, words in (('cat `cat list.txt`', ['cat', '`cat list.txt`']),
                           ('echo `a; b` c', ['echo', '`a; b`', 'c']),
                           ('echo a`b c`d e', ['echo', 'a`b c`d', 'e']),
                           ('echo `echo "a b"` c', ['echo', '`echo "a b"`', 'c']),
                           ('echo `echo \\`x\\`` c', ['echo', '`echo \\`x\\``', 'c']),
                           ('x=`date` cmd', ['x=`date`', 'cmd'])):
            with self.subTest(cmd=cmd):
                self.assertEqual(words, self.lex(cmd))
        self.assertTrue(bp.is_assignment(bp.lex('x=`date` cmd')[0]))
        # Quoted or escaped, a backtick opens nothing, and one that never
        # closes is left as it was.
        self.assertEqual(['cat', 'a`b', 'c'], self.lex("cat 'a`b' c"))
        self.assertEqual(['echo', '`', 'a'], self.lex('echo \\` a'))
        self.assertEqual(['echo', '`a', 'b'], self.lex('echo `a b'))

    def test_a_quote_in_a_substitution_in_double_quotes_closes_nothing(self):
        # bash 5.3.15 parses each body on its own and runs `id` after it, so
        # the inner `"` does not close the outer string (Q257). Read as
        # closing it, the real closing `"` was unbalanced and lex raised.
        for cmd, word in (('echo "$(echo \'"\')" ; id', '$(echo \'"\')'),
                          ('echo "$(echo $\'"\')" ; id', '$(echo $\'"\')'),
                          ('echo "`echo \'"\'`" ; id', '`echo \'"\'`'),
                          ('echo "$(printf \'%s"\' x)" ; id', '$(printf \'%s"\' x)'),
                          ('echo "$(case x in x) echo \'"\';; esac)" ; id',
                           '$(case x in x) echo \'"\';; esac)')):
            with self.subTest(cmd=cmd):
                self.assertEqual(['echo', word, ';', 'id'], self.lex(cmd))
        # Where POSIX quoting reads the line anyway, it is read as before. A
        # nested body's command then spills into the outer words, which is
        # what judges one nested past MAX_SUBST_DEPTH.
        self.assertEqual(['echo', '$(echo ")', ';', 'id'],
                         self.lex('echo "$(echo \\")" ; id'))
        self.assertIn('cat', self.lex('echo "$(echo "$(cat f)")"'))
        self.assertEqual(['echo', 'a$x', 'b'], self.lex('echo "a$x" b'))
        with self.assertRaises(ValueError):
            bp.lex('echo "$(echo a) b')

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


class ProcessSubstitutionTests(unittest.TestCase):
    """`<(…)` and `>(…)` bodies, which command_substitutions never returns (Q277).

    Each "runs" case created a file under `env -i /opt/homebrew/bin/bash
    --norc --noprofile -c` (5.3.15) with `touch` as the body, and each "runs
    nothing" case did not.
    """
    def test_bash_runs_these(self):
        for cmd, body in (('tail <(make check)', 'make check'),
                          ('true >(rm x)', 'rm x'),
                          ('echo x 2>(touch two)', 'touch two'),
                          ('echo a=<(touch eq)', 'touch eq'),
                          ('make check > >(tee log)', 'tee log'),
                          ('diff <(a | b) <(c)', 'a | b')):
            with self.subTest(cmd=cmd):
                self.assertIn(body, bp.process_substitutions(cmd))

    def test_bash_runs_none_of_these(self):
        for cmd in ('echo "<(id)"', "echo '<(id)'", 'echo \\<(id)',
                    'echo >>(id)', 'echo &>(id)', 'cat <<(id)'):
            with self.subTest(cmd=cmd):
                self.assertEqual([], bp.process_substitutions(cmd))

    def test_command_substitutions_still_returns_none(self):
        # The new function is additive: no other guard's view of `<(…)` moves.
        self.assertEqual([], bp.command_substitutions('tail <(make check)'))

    def test_one_inside_a_command_substitution_is_left_for_the_rescan(self):
        cmd = 'x=$(cat <(make check))'
        self.assertEqual([], bp.process_substitutions(cmd))
        self.assertEqual(['make check'],
                         bp.process_substitutions(bp.command_substitutions(cmd)[0]))

    def test_a_span_covers_the_whole_substitution(self):
        text = 'diff <(a) x'
        spans = []
        bp.process_substitutions(text, spans=spans)
        start, end = spans[0]
        self.assertEqual('<(a)', text[start:end])


class BacktickBodyTests(unittest.TestCase):
    """A backtick body is returned as the command bash runs (Q226).

    bash drops the backslash before a backtick, `$` or backslash inside one,
    and before `"` too when the substitution sits in double quotes. Returned
    raw, ``echo `echo \\`id\\``` came back as ``echo \\`id\\``` and re-scanning
    found nothing. Driven under `env -i /opt/homebrew/bin/bash --norc
    --noprofile -c` (5.3.15).
    """
    def walk(self, cmd):
        bodies = bp.command_substitutions(cmd)
        return bodies + [b for body in bodies for b in self.walk(body)]

    def test_a_nested_substitution_is_found_on_the_rescan(self):
        # bash runs the inner command in each of these.
        for cmd in ('echo `echo \\`id\\``', 'echo "`echo \\`id\\``"',
                    'x=`echo \\`id\\``', 'echo `echo $(echo \\`id\\`)`'):
            with self.subTest(cmd=cmd):
                self.assertIn('id', self.walk(cmd))

    def test_what_bash_leaves_escaped_stays_escaped(self):
        # bash runs no inner command for these.
        for cmd in ('echo `echo \\\\\\`id\\\\\\``', "echo `echo '\\`id\\`'`",
                    'echo "\\`id\\`"'):
            with self.subTest(cmd=cmd):
                self.assertNotIn('id', self.walk(cmd))

    def test_a_double_quote_unescapes_only_inside_double_quotes(self):
        # printf prints `<a b>` for the first and `<"a><b">` for the second.
        self.assertEqual(['printf x "a b"'],
                         bp.command_substitutions('echo "`printf x \\"a b\\"`"'))
        self.assertEqual(['printf x \\"a b\\"'],
                         bp.command_substitutions('echo `printf x \\"a b\\"`'))


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


class CommentInSubstitutionTests(unittest.TestCase):
    """A comment inside a `$(…)` is text to its newline (Q230).

    Read as syntax, a `)` in the comment closed the substitution there and an
    apostrophe opened a quoted run that never closed, so the command on the
    next line was either outside the body or no body came back at all. Driven
    under `env -i /opt/homebrew/bin/bash --norc --noprofile -c` (5.3.15), each
    body below runs its second line.
    """
    def test_a_paren_or_apostrophe_in_a_comment_is_text(self):
        for body in ('# )\ncat /etc/passwd', 'echo a # )\ncat /etc/passwd',
                     "# don't\ncat /etc/passwd", '# <<EOF\ncat /etc/passwd',
                     ' (echo a)# )\ncat /etc/passwd', 'echo a;# )\ncat /etc/passwd',
                     "echo $'a' #b)\ncat /etc/passwd", 'cat <(echo a) #b)\ncat /etc/passwd',
                     'echo a \\\n#b )\ncat /etc/passwd'):
            with self.subTest(body=body):
                self.assertEqual([body], bp.command_substitutions(
                    'echo "$(%s)" T' % body))
                self.assertEqual([body], bp.command_substitutions(
                    'x=$(%s) T' % body))

    def test_a_hash_inside_a_word_starts_no_comment(self):
        # None of these `#` starts a comment in bash (`$(echo a)#b` prints
        # `a#b`, `<(true)#b` prints `/dev/fd/63#b`), so the `)` after each
        # still closes the substitution.
        for body in ('echo a#', 'echo $#', 'echo ${#x}', 'echo $(echo a)#b',
                     'echo $((1))#b', 'echo \\ #', 'echo "a"#b', "echo $'a'#b",
                     'cat <(echo a)#b', 'tee >(cat)#b', 'echo a<(true)#b',
                     'echo a\\\n#b'):
            with self.subTest(body=body):
                self.assertEqual([body], bp.command_substitutions(
                    'echo "$(%s)" T' % body))


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
    """The `+` and a subscript sit on the name's side of the `=`, so a name
    recovered from the token has them removed and the form says which was
    there -- except after `env`, which is not the shell (Q174, Q214)."""

    def test_a_plain_assignment(self):
        self.assertEqual(('A', bp.ASSIGN_PLAIN, '1'), bp.split_assignment('A=1'))

    def test_an_append_is_reported_with_the_bare_name(self):
        self.assertEqual(('A', bp.ASSIGN_APPEND, '1'),
                         bp.split_assignment('A+=1'))

    def test_a_subscript_is_reported_with_the_bare_name(self):
        for word in ('A[0]=1', 'A[0]+=1', 'A[a[0]]=1', 'A[]=1'):
            with self.subTest(word=word):
                self.assertEqual(('A', bp.ASSIGN_SUBSCRIPT, '1'),
                                 bp.split_assignment(word))

    def test_an_equals_inside_the_subscript_is_not_the_operator(self):
        # `FOO[a=b]=x cat f` reads f on bash 5.3.15, so the value is `x`.
        self.assertEqual(('A', bp.ASSIGN_SUBSCRIPT, 'x'),
                         bp.split_assignment('A[a=b]=x'))

    def test_env_takes_the_plus_as_part_of_the_name(self):
        # `env 'A+=1' cmd` exports `A+` and leaves `A` alone. Measured:
        # `env 'SP+=/x' printenv 'SP+'` prints `/x`.
        self.assertEqual(('A+', bp.ASSIGN_PLAIN, '1'),
                         bp.split_assignment('A+=1', append_is_operator=False))

    def test_env_takes_the_subscript_as_part_of_the_name(self):
        self.assertEqual(('A[0]', bp.ASSIGN_PLAIN, '1'),
                         bp.split_assignment('A[0]=1', append_is_operator=False))

    def test_env_and_shell_agree_on_a_plain_assignment(self):
        self.assertEqual(bp.split_assignment('A=1'),
                         bp.split_assignment('A=1', append_is_operator=False))

    def test_an_equals_in_the_value_is_kept(self):
        self.assertEqual(('A', bp.ASSIGN_APPEND, 'b=c'),
                         bp.split_assignment('A+=b=c'))

    def test_an_empty_append_value(self):
        self.assertEqual(('A', bp.ASSIGN_APPEND, ''), bp.split_assignment('A+='))


class SubscriptAssignmentTests(unittest.TestCase):
    """`FOO[0]=x cat f` reads f: bash peels a subscripted prefix whether or not
    FOO is an array, and runs the command behind it (Q214).

    Both tables are bash's own answer, taken on 5.3.15 as
    ``env -i /opt/homebrew/bin/bash --norc --noprofile -c '<word> cat f'`` and
    read for whether f's contents printed; 3.2.57 agrees on every row. The
    brackets match by depth, so `FOO[a[0]]=x` is one subscript and the first
    `]` at depth 0 closes: `FOO[a]b]=x` and `FOO[]]=x` are command names.
    An unclosed `[` runs nothing at all: both bashes reject the whole string
    (`unexpected EOF while looking for matching ']'`, rc 2).
    """

    PEELS = ('FOO[0]=x', 'FOO[0]+=x', 'FOO[]=x', 'FOO[a]=x', 'FOO[a[0]]=x',
             'FOO[0]="a b"', 'FOO[1]=x', 'FOO[-1]=x', 'FOO[@]=x', 'FOO[*]=x',
             'FOO[i+1]=x', 'FOO[0]=', 'FOO[[0]]=x', 'FOO[a=b]=x', '_[0]=x',
             'F1[0]=x', 'FOO[0]==', 'FOO[$i]=x', 'FOO[${i}]=x',
             'FOO["0"]=x', "FOO['a']=x")
    RUNS_A_COMMAND = ('FOO[a]b]=x', 'FOO[]]=x',
                      '0FOO[0]=x', "'FOO[0]=x'", '"FOO[0]=x"', r'FOO\[0]=x',
                      "F'O'O[0]=x", 'FOO[0]"=x"', r'FOO[0]\=x', "'FOO'[0]=x",
                      'FOO"[0]"=x',
                      'FOO[0]x=y', 'FOO[0]+x=y', 'FOO[0]', '[0]=x',
                      'FOO[0][1]=x')
    SYNTAX_ERRORS = ('FOO[0=x', 'FOO[a[b]=x')

    def test_a_subscripted_prefix_is_peeled(self):
        for word in self.PEELS:
            with self.subTest(word=word):
                self.assertEqual(['cat', 'f'],
                                 bp.strip_env_prefix(bp.lex(word + ' cat f')))

    def test_a_word_bash_runs_as_a_command_is_not_peeled(self):
        for word in self.RUNS_A_COMMAND:
            with self.subTest(word=word):
                toks = bp.lex(word + ' cat f')
                self.assertEqual(toks, bp.strip_env_prefix(toks))

    def test_an_unclosed_subscript_is_not_peeled(self):
        # bash runs nothing here; reading the word as a command name keeps
        # every later word in view, which errs toward more checking.
        for word in self.SYNTAX_ERRORS:
            with self.subTest(word=word):
                toks = bp.lex(word + ' cat f')
                self.assertEqual(toks, bp.strip_env_prefix(toks))

    def test_a_subscript_is_one_word_whatever_it_holds(self):
        """Each of these peels in bash 5.3.15 (Q217). shlex splits on a space
        or operator first and strips quotes, so the lexer reads the subscript
        from the raw text, closing at the `]` bash closes at: not a quoted or
        escaped one, nor one inside an expansion."""
        for word in ('FOO[a b]=x', 'FOO[a;b]=x', 'FOO[a|b]=x', 'FOO[a<b]=x',
                     'FOO[$(echo 0)]=x', 'FOO[$((1+1))]=x',
                     'FOO[`echo 0`]=x', 'FOO[""]=x', 'FOO["a]b"]=x',
                     r'FOO[a\]b]=x', r'FOO[a\[b]=x', 'FOO[${x:-]}]=x',
                     'FOO[$(echo ])]=x', 'FOO[a\nb]=x', 'FOO[a #b]=x',
                     'FOO[a b]+=x', 'FOO[a b]="c d"'):
            with self.subTest(word=word):
                self.assertEqual(['cat', 'f'],
                                 bp.strip_env_prefix(bp.lex(word + ' cat f')))

    def test_a_subscript_is_read_wherever_an_assignment_can_stand(self):
        # bash 5.3.15 peels the subscript in each of these and runs `cat f`.
        for cmd in ('X=1 FOO[a;b]=x cat f', 'true; FOO[a;b]=x cat f',
                    'true && FOO[a;b]=x cat f', 'if FOO[a;b]=x cat f',
                    '( FOO[a;b]=x cat f', '2>/dev/null FOO[a;b]=x cat f',
                    '>/dev/null FOO[a;b]=x cat f', 'time FOO[a;b]=x cat f',
                    'time -p FOO[a;b]=x cat f', 'time -- FOO[a;b]=x cat f',
                    'x=$(true) FOO[a;b]=x cat f', '(true) && FOO[a;b]=x cat f',
                    'case x in x) FOO[a;b]=x cat f',
                    'case x in (x) FOO[a;b]=x cat f',
                    'case x in y) :;; x) FOO[a;b]=x cat f'):
            with self.subTest(cmd=cmd):
                toks = bp.lex(cmd)
                self.assertIn('FOO[a;b]=x', toks)
                self.assertEqual(['cat', 'f'], toks[-2:])

    def test_an_operand_subscript_still_splits(self):
        # Only an assignment keeps its subscript whole: `echo FOO[a;b]=x` runs
        # `b]=x` as a second command, and `FOO[a b] cat f` runs `FOO[a b]`.
        self.assertEqual(['echo', 'FOO[a', ';', 'b]=x'],
                         bp.lex('echo FOO[a;b]=x'))
        self.assertEqual(['cat', 'f', '>', 'x', 'FOO[a', ';', 'b]=x'],
                         bp.lex('cat f >x FOO[a;b]=x'))
        self.assertEqual(['FOO[a b]', 'cat', 'f'], bp.lex('FOO[a b] cat f'))
        self.assertEqual(['FOO[a b]', ';', 'cat', 'f'], bp.lex('FOO[a b];cat f'))
        # A substitution's `)` puts back the position before its `(`: bash
        # 5.3.15 runs `cat f` in each of these.
        for cmd in ('diff <(true) FOO[a;cat f;]', 'echo $((1)) FOO[a;cat f;]',
                    'echo $(true) FOO[a;cat f;]', 'cat < <(true) FOO[a;cat f;]',
                    'echo $(echo $(true)) FOO[a;cat f;]',
                    'time echo -p FOO[a;cat f;]',
                    'echo $(case x in x) true;; esac) FOO[a;cat f;]',
                    'echo $(case x in x) (true);; esac) FOO[a;cat f;]',
                    'echo $(case x in x|y) :;; z) :;; esac) FOO[a;cat f;]'):
            with self.subTest(cmd=cmd):
                self.assertIn('FOO[a', bp.lex(cmd))

    def test_an_unclosed_subscript_falls_back_to_the_split(self):
        # bash reports a syntax error and runs nothing; splitting reads more.
        self.assertEqual(['FOO[a', 'b'], bp.lex('FOO[a b'))


class AssignmentTests(unittest.TestCase):
    """Bash decides what a word IS before it removes the quotes (Q170, Q139).

    The table is bash's own answer, taken on 5.3.15 with
    ``bash -c "<word>; printf '[%s]' \"$SP\""``: a set variable prints its
    value, an unset one prints ``[]`` and ``SP=/x: No such file or directory``
    -- the word holds a `/`, so bash runs it as a path. Quoting
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
    every quoted spelling leaves the shell in ``/tmp``, because the ``cd`` is an
    argument to a program: ``<word>: command not found`` for all but ``'time'``,
    which finds macOS's ``/usr/bin/time`` and runs ``cd`` in a child (3.2.57
    agrees on every row). ``\\if`` is the case a ``.quotes`` check would miss:
    it carries no quote character and is still not the keyword.
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


class DiscardedWritesTests(unittest.TestCase):
    """Q204. Narrow on purpose: every miss leaves a reason as it was, and every
    phantom write is a false sentence in it."""

    def test_the_writes_it_names(self):
        for cmd, want in (
                ('echo hi > out.txt', ['`> out.txt`']),
                ('echo hi >> out.txt', ['`>> out.txt`']),
                ('echo hi &> out.txt', ['`&> out.txt`']),
                ('echo hi >& out.txt', ['`>& out.txt`']),
                ('echo hi 2>err.log', ['`2> err.log`']),
                ("cat > t.py <<'EOF'\nx\nEOF\nmake check | tail",
                 ['`> t.py`']),
                ("python3 <<'EOF'\nopen('f', 'w')\nEOF\n",
                 ['the heredoc script fed to `python3`']),
                ('make check | tee log.txt', ['`tee log.txt`']),
                ("sed -i '' s/a/b/ f.go", ['`sed -i`']),
                ('sed --in-place s/a/b/ f.go', ['`sed -i`']),
                ('cp a b && make', ['`cp` to `b`']),
                ('mv -t dir a b', ['`mv`']),
                ('FOO=1 tee x < in', ['`tee x`']),
                # A spaced digit is an argument, so stdout is what is redirected.
                ('echo 2 > f', ['`> f`']),
                ('head -n 5 >out', ['`> out`']),
                ("echo '2'>f", ['`> f`']),
                ('cp a b 3 > log', ['`> log`', '`cp` to `3`']),
                ('tee x 2>/dev/null', ['`tee x`']),
                ('cp a b 2&>log', ['`&> log`', '`cp` to `2`']),
                ('cp a b 2&>>log', ['`&>> log`', '`cp` to `2`']),
                ('echo $(( 1 > 0 )) > r.txt', ['`> r.txt`'])):
            with self.subTest(cmd=cmd):
                self.assertEqual(want, bp.discarded_writes(cmd))

    def test_what_writes_nothing_is_not_named(self):
        for cmd in ('make check | tail',
                    'make check 2>&1 | tail',
                    'make check >/dev/null 2>&1',
                    'echo hi >&2',
                    'make | tee -a /dev/stderr',
                    'cat <<EOF | python3\nx\nEOF\n',  # the heredoc feeds cat
                    "sed -n 's/a/b/p' f.go",
                    'cp -r src',
                    '[[ a > b ]] && make',
                    '(( x > 3 )) && echo y',
                    'diff <(sort a) <(sort b)',
                    "echo 'a > b'",
                    'git status',
                    "echo 'unbalanced"):
            with self.subTest(cmd=cmd):
                self.assertEqual([], bp.discarded_writes(cmd))

    def test_a_digit_the_lexer_could_not_place_names_nothing(self):
        # A shlex without the lookahead `glued` reads leaves it None, and
        # `2>/dev/null` would otherwise make `2` cp's destination.
        tokens = bp.lex('cp a b 2>/dev/null')
        tokens[3].glued = None
        self.assertEqual([], bp._segment_writes(tokens))

    def test_the_note_leaves_a_reason_with_no_write_alone(self):
        self.assertEqual('R.', bp.note_discarded_writes('R.', 'make | tail'))

    def test_the_note_is_appended_after_the_reason(self):
        self.assertEqual(
            'R. Nothing in this call ran, so these did not happen either: '
            '`> a`. Any file they would have written is unchanged.',
            bp.note_discarded_writes('R.', 'echo x > a; make | tail'))

    def test_the_note_caps_its_list(self):
        cmd = '; '.join('echo x > f%d' % k for k in range(7))
        self.assertIn('`> f4`, and 2 more.',
                      bp.note_discarded_writes('R.', cmd))

    def test_a_failure_costs_the_note_and_never_the_reason(self):
        self.assertEqual('R.', bp.note_discarded_writes('R.', None))


class AnsiCQuoteTests(unittest.TestCase):
    """`$'\\''` is one apostrophe to bash (Q225).

    Read as a closed single quote followed by an opening one, it inverted the
    quote state for everything after it. Driven under
    `env -i /opt/homebrew/bin/bash --norc --noprofile -c` (5.3.15): each
    command below runs the one written after the `$'...'`.
    """
    def test_substitutions_after_one_are_found(self):
        for cmd, body in (("echo $'\\'' \"$(id)\"", 'id'),
                          ("echo $'\\'' $(id)", 'id'),
                          ("echo $'it\\'s' `id`", 'id'),
                          ("echo \"$(echo $'\\'' ; id)\"", "echo $'\\'' ; id")):
            with self.subTest(cmd=cmd):
                self.assertEqual([body], bp.command_substitutions(cmd))

    def test_its_inside_is_literal(self):
        # Nothing substitutes inside `$'...'`, and inside double quotes `$'`
        # is two characters of text.
        self.assertEqual([], bp.command_substitutions("echo $'$(id)'"))
        self.assertEqual([], bp.command_substitutions("echo $'\\'' '$(id)'"))
        self.assertEqual(['id'], bp.command_substitutions("echo \"it's $'\" $(id)"))

    def test_a_hash_inside_one_is_text(self):
        cmd = "echo $'\\' #' ; id"
        self.assertEqual(cmd, bp.strip_comments(cmd))

    def test_one_in_a_subscript_closes_where_bash_closes_it(self):
        # bash runs `id` after `FOO[$'\']=x #'];id`: the subscript holds
        # `']=x #`. Closed at the escaped quote, the `]=` after it read as an
        # assignment and the `#` as a comment hiding `id`. Inside double
        # quotes `$'` is text, so `FOO["$'"]` closes at its first `]`.
        cmd = "FOO[$'\\']=x #'];id"
        self.assertEqual(cmd, bp.strip_comments(cmd))
        for word, end in (("FOO[$'a\\'];b']=x", 14), ("FOO[$'\\']=x #']", 15),
                          ("FOO[\"$'\"]=x", 9)):
            with self.subTest(word=word):
                self.assertEqual(end, bp._subscript_end(word, 3))

    def test_a_heredoc_after_one_is_found(self):
        self.assertEqual("cat <<EOF ; echo $'\\''\nid",
                         bp.strip_heredoc_bodies("cat <<EOF ; echo $'\\''\nx\nEOF\nid"))

    def test_one_as_a_heredoc_delimiter_ends_where_bash_ends_it(self):
        # bash ends `<<$'E\x4fF'` at an `EOF` line, prints `$(echo SUB)`
        # unexpanded, and runs the line after. Read as `$` and a quoted word,
        # the body ran to the end of the input and swallowed that line.
        for word in ("$'EOF'", "$'E\\x4fF'"):
            with self.subTest(word=word):
                exp = []
                self.assertEqual(
                    'cat <<%s\nid' % word,
                    bp.strip_heredoc_bodies(
                        'cat <<%s\n$(echo SUB)\nEOF\nid' % word, exp))
                self.assertEqual([], exp)

    def test_the_lexer_reads_the_word_bash_does(self):
        self.assertEqual(['echo', "'", ';', 'id'], bp.lex("echo $'\\'' ; id"))
        self.assertEqual(["x=a'b", 'id'], bp.lex("x=$'a\\'b' id"))
        self.assertEqual(['kubectl', 'get'], bp.lex("$'\\x6bubectl' get"))
        self.assertEqual(['echo', "$'", ';', 'id'], bp.lex('echo "$\'" ; id'))
        # Quoting, so not an assignment: bash looks for a program `SP=x`.
        self.assertFalse(bp.is_assignment(bp.lex("$'SP=x'")[0]))

    def test_escapes_decode_as_bash_decodes_them(self):
        # printf '<%s>' on each of these under bash 5.3.15, read through od -c.
        for body, word in ((r"a\x41\101\u00e9\q", 'aAA\u00e9\\q'),
                           (r'\x', '\\x'), (r"\'", "'"), (r'x\0yz', 'x'),
                           (r'\cA', '\x01'), (r'\e', '\x1b'), (r'\"\?', '"?')):
            with self.subTest(body=body):
                self.assertEqual(word, bp._ansi_c_decode(body))


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
