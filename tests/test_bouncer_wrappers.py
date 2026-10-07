"""Tests for the shared wrapper option record and its arity walk.

Each guard's own suite drives the walk through its peel loop. What is checked
here is the record itself: that the value-only view agrees with the closed
grammar it is derived from, and that each row's value letters are the ones the
real tool asks an argument for.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'lib'))
import bouncer_wrappers as wrappers  # noqa: E402


class GrammarTests(unittest.TestCase):
    def test_sudo_value_options_match_its_getopt(self):
        # sudo 1.9.17p2 answers `option requires an argument` for exactly these
        # short letters, plus `-h`: getopt takes it as `h::`, and sudo's own
        # parse then takes a plain next word as the host (Q158).
        self.assertEqual(
            {o for o in wrappers.WRAPPER_VALUE_OPTS['sudo'] if len(o) == 2},
            {'-' + c for c in 'acCDghprRtTuU'})
        self.assertNotIn('h', wrappers.WRAPPER_GRAMMAR['sudo'].short_none)

    def test_env_carries_both_bsd_and_gnu_value_options(self):
        # `-P` is BSD's and `-a` GNU's (Q245, Q250).
        for opt in ('-u', '-C', '-S', '-P', '-a', '--argv0', '--unset'):
            with self.subTest(opt=opt):
                self.assertIn(opt, wrappers.WRAPPER_VALUE_OPTS['env'])

    def test_value_view_is_derived_from_the_closed_grammar(self):
        for name, w in wrappers.WRAPPER_GRAMMAR.items():
            with self.subTest(name=name):
                self.assertEqual(wrappers.WRAPPER_VALUE_OPTS[name],
                                 wrappers.value_options(w))
                # A letter is in at most one of a row's short lists.
                letters = w.short_none + w.short_val + w.short_opt
                self.assertEqual(len(letters), len(set(letters)))

    def test_optional_values_are_not_value_options(self):
        # `--preserve-env=list` takes its value attached or not at all.
        self.assertNotIn('--preserve-env', wrappers.WRAPPER_VALUE_OPTS['sudo'])
        self.assertNotIn('-e', wrappers.WRAPPER_VALUE_OPTS['xargs'])


class WrapperOptionTests(unittest.TestCase):
    SUDO = wrappers.WRAPPER_VALUE_OPTS['sudo']

    def walk(self, *argv):
        return wrappers.wrapper_option(list(argv), self.SUDO)

    def test_short_shapes(self):
        self.assertEqual(self.walk('-u', 'root', 'cmd'), (2, '-u', 'root'))
        self.assertEqual(self.walk('-uroot', 'cmd'), (1, '-u', 'root'))
        self.assertEqual(self.walk('-nu', 'root'), (2, '-u', 'root'))
        self.assertEqual(self.walk('-n', 'cmd'), (1, None, None))
        # The walk stops at the first value letter: `-uKarl` names a user.
        self.assertEqual(self.walk('-uKarl'), (1, '-u', 'Karl'))

    def test_long_shapes(self):
        self.assertEqual(self.walk('--user', 'root'), (2, '--user', 'root'))
        self.assertEqual(self.walk('--user=root'), (1, '--user', 'root'))
        self.assertEqual(self.walk('--us', 'root'), (2, '--user', 'root'))
        self.assertEqual(self.walk('--ro', 'r'), (2, '--role', 'r'))
        self.assertEqual(self.walk('--non-interactive', 'cmd'), (1, None, None))

    def test_ambiguous_prefix_and_end_of_options_take_one_word(self):
        # `--c` prefixes four value options, which sudo rejects.
        self.assertEqual(self.walk('--c', 'x'), (1, None, None))
        self.assertEqual(self.walk('--', 'x'), (1, None, None))

    def test_value_missing_at_the_end(self):
        self.assertEqual(self.walk('-u'), (2, '-u', None))


if __name__ == '__main__':
    unittest.main()
