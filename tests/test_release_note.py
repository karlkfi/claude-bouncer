#!/usr/bin/env python3
"""Tests for scripts/release-note.py.

Run with: python3 -m unittest discover tests
     or:  python3 tests/test_release_note.py

Two layers:
  * Unit tests import `extract` and exercise the four outcomes.
  * End-to-end tests run the script as a subprocess, since the CI check and
    the release harvest both consume its exit status, not just its stdout.
"""
import subprocess
import sys
import unittest
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'scripts' / 'release-note.py'
TEMPLATE = REPO / '.github' / 'pull_request_template.md'

_spec = util.spec_from_file_location('release_note', SCRIPT)
release_note = util.module_from_spec(_spec)
_spec.loader.exec_module(release_note)
extract = release_note.extract

# The instruction comment GitHub keeps in the raw body even after an answer.
COMMENT = '<!--\nWrite a note when a decision moves.\n\n  An example note.\n\nAnswer `None` otherwise.\n-->'


def body(section=None, trailing=''):
    """A body whose release-note section holds `section`."""
    out = '## What\n\nprose\n\n## Release note\n\n' + COMMENT + '\n'
    if section:
        out += '\n' + section + '\n'
    return out + trailing


class TestExtract(unittest.TestCase):
    def test_note_survives_the_comment(self):
        self.assertEqual(
            extract(body('Unanchored `pkill` patterns now deny.')),
            'Unanchored `pkill` patterns now deny.')

    def test_none_is_an_answer(self):
        self.assertEqual(extract(body('None')), 'None')

    def test_empty_section_is_unanswered(self):
        self.assertEqual(extract(body()), release_note.UNANSWERED)

    def test_missing_section(self):
        self.assertEqual(extract('## What\n\nprose\n'), release_note.NO_SECTION)

    def test_empty_body(self):
        self.assertEqual(extract(''), release_note.NO_SECTION)
        self.assertEqual(extract(None), release_note.NO_SECTION)

    def test_answer_above_the_comment(self):
        """The template puts the comment first, but an author may not."""
        b = '## Release note\n\nA note.\n\n' + COMMENT + '\n'
        self.assertEqual(extract(b), 'A note.')

    def test_stops_at_the_next_heading(self):
        b = body('None', trailing='\n## Checklist\n\n- [x] tests\n')
        self.assertEqual(extract(b), 'None')

    def test_section_need_not_be_last(self):
        b = '## Release note\n\nA note.\n\n## Docs\n\nREADME.\n'
        self.assertEqual(extract(b), 'A note.')

    def test_multi_line_note_is_joined(self):
        self.assertEqual(extract(body('One line.\nTwo line.')),
                         'One line.\nTwo line.')

    def test_whitespace_only_section_is_unanswered(self):
        self.assertEqual(extract('## Release note\n\n   \n\t\n'),
                         release_note.UNANSWERED)

    def test_heading_must_be_its_own_line(self):
        """A mention in prose is not the section."""
        self.assertEqual(extract('See the ## Release note block.\n'),
                         release_note.NO_SECTION)


class TestShippedTemplate(unittest.TestCase):
    """The template as committed must read as unanswered.

    This is the regression the block was reshaped around: an earlier version
    pre-filled `None`, which made a section nobody read indistinguishable
    from a considered answer.
    """

    def test_template_is_unanswered(self):
        self.assertEqual(extract(TEMPLATE.read_text()), release_note.UNANSWERED)

    def test_template_answered_with_none(self):
        filled = TEMPLATE.read_text() + '\nNone\n'
        self.assertEqual(extract(filled), 'None')


class TestSubprocess(unittest.TestCase):
    def run_script(self, text):
        return subprocess.run([sys.executable, str(SCRIPT)], input=text,
                              capture_output=True, text=True)

    def test_answer_exits_zero(self):
        for section in ('None', 'A real note.'):
            with self.subTest(section=section):
                p = self.run_script(body(section))
                self.assertEqual(p.returncode, 0)
                self.assertEqual(p.stdout.strip(), section)

    def test_unanswered_exits_nonzero(self):
        p = self.run_script(body())
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.strip(), release_note.UNANSWERED)

    def test_missing_section_exits_nonzero(self):
        p = self.run_script('## What\n\nprose\n')
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.strip(), release_note.NO_SECTION)

    def test_shipped_template_exits_nonzero(self):
        p = self.run_script(TEMPLATE.read_text())
        self.assertEqual(p.returncode, 1)


if __name__ == '__main__':
    unittest.main()
