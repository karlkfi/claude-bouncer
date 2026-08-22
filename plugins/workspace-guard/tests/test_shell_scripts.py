"""Tests for the shell helpers in ``scripts/``.

The Python scripts each have a suite; the shell ones had none, though
``lint-backlog.sh`` is wired into ``.githooks/pre-commit`` and so gates every
backlog commit. A bug there is silent in exactly the way a linter's bugs are:
it passes a file it should reject.

Three of the four (``lint-backlog.sh``, ``next-task.sh``, ``backlog-metrics.sh``)
are vendored from the ``backlog`` skill — a fix belongs upstream in
``karlkfi/claude-skills`` as well as here, or the next vendor drop reverts it.

Every test shells out to ``bash``. That is Git Bash on Windows, where these
scripts are ordinary GNU-tool scripts and run the same; the skip guard exists
for a host with no bash at all, not as a platform exemption.
"""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
BASH = shutil.which("bash")

SHELL_SCRIPTS = [
    "backlog-metrics.sh",
    "capture-prompt-screenshot.sh",
    "lint-backlog.sh",
    "next-task.sh",
]

# A minimal backlog that satisfies every rule. Tests mutate one thing at a time
# so a failure names the rule that broke rather than "the fixture is invalid".
VALID = """\
# Project Status

**Status:** \U0001f532 ready · \U0001f6ab blocked
**Size:** S = one session/PR
**Labels:** `security` `tests`
**Next ID:** Q5

## Queue

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q1"></a>Q1 | Do the first thing | `tests` | \U0001f532 | S | A short note. |
| <a id="Q2"></a>Q2 | Do the second thing | `security` | \U0001f6ab | S | Blocked by [Q1](#Q1). Needs it first. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q3"></a>Q3 | Someday thing | `tests` | M | **Demand:** someone asks for it. |
"""


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def run(script, *args, cwd=None, env=None):
    """Run a scripts/ helper, returning the CompletedProcess."""
    e = os.environ.copy()
    e.update(env or {})
    return subprocess.run(
        [BASH, os.path.join(SCRIPTS, script), *args],
        capture_output=True, text=True, cwd=cwd, env=e, timeout=60,
    )


@unittest.skipUnless(BASH, "no bash on PATH")
class ShellSyntaxTests(unittest.TestCase):
    """`bash -n` every shell script.

    The cheapest possible gate, and the only one that covers
    `capture-prompt-screenshot.sh` — it drives a live Claude session and a
    window manager, so there is nothing to assert about its behavior here.
    """

    def test_every_shell_script_parses(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                p = subprocess.run([BASH, "-n", os.path.join(SCRIPTS, name)],
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(p.returncode, 0, p.stderr)

    def test_every_shell_script_is_executable(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                self.assertTrue(os.access(os.path.join(SCRIPTS, name), os.X_OK),
                                f"{name} is not executable")


@unittest.skipUnless(BASH, "no bash on PATH")
class LintBacklogTests(unittest.TestCase):
    """`lint-backlog.sh` content rules. It gates every backlog commit."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "STATUS.md")

    def tearDown(self):
        self._tmp.cleanup()

    def lint(self, text, env=None):
        write(self.path, text)
        return run("lint-backlog.sh", self.path, env=env)

    def assertClean(self, text, env=None):
        p = self.lint(text, env)
        self.assertEqual(p.returncode, 0, f"want clean, got:\n{p.stdout}{p.stderr}")

    def assertFlags(self, text, needle, env=None):
        p = self.lint(text, env)
        self.assertNotEqual(p.returncode, 0, "want a lint failure, got exit 0")
        self.assertIn(needle, (p.stdout + p.stderr).lower())

    def test_a_valid_file_passes(self):
        self.assertClean(VALID)

    def test_the_repos_own_backlog_passes(self):
        # Regression guard: the shipped file must satisfy the shipped linter.
        p = run("lint-backlog.sh", os.path.join(REPO, "docs", "STATUS.md"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_counter_must_exceed_every_used_id(self):
        self.assertFlags(VALID.replace("**Next ID:** Q5", "**Next ID:** Q2"),
                         "next id")

    def test_a_malformed_counter_is_flagged(self):
        self.assertFlags(VALID.replace("**Next ID:** Q5", "**Next ID:** 5"),
                         "next id")

    def test_duplicate_ids_are_flagged(self):
        self.assertFlags(
            VALID.replace('<a id="Q2"></a>Q2', '<a id="Q1"></a>Q1'), "duplicate")

    def test_an_anchor_must_match_its_visible_id(self):
        self.assertFlags(
            VALID.replace('<a id="Q1"></a>Q1 |', '<a id="Q9"></a>Q1 |'),
            "does not match")

    def test_a_row_without_an_anchor_is_flagged(self):
        self.assertFlags(VALID.replace('<a id="Q1"></a>Q1 |', 'Q1 |'), "anchor")

    def test_old_format_state_markers_are_flagged(self):
        for marker in ("✅", "▶", "\U0001f4a4"):
            with self.subTest(marker=marker):
                self.assertFlags(
                    VALID.replace("| \U0001f532 | S | A short note.",
                                  f"| {marker} | S | A short note."),
                    "old format")

    def test_an_unknown_state_marker_is_flagged(self):
        self.assertFlags(
            VALID.replace("| \U0001f532 | S | A short note.",
                          "| x | S | A short note."), "st must be")

    def test_notes_over_the_hard_cap_are_flagged(self):
        self.assertFlags(VALID.replace("A short note.", "n" * 260), "max")

    def test_long_notes_without_a_link_are_flagged(self):
        # Over the link threshold (200) but under the hard cap (250).
        self.assertFlags(VALID.replace("A short note.", "n" * 220), "links no document")

    def test_long_notes_with_a_link_pass(self):
        self.assertClean(
            VALID.replace("A short note.", "n" * 200 + " [plan](plan/x.md)"))

    def test_the_notes_caps_are_configurable(self):
        text = VALID.replace("A short note.", "n" * 120)
        self.assertClean(text)
        self.assertFlags(text, "max", env={"NOTES_MAX_CHARS": "50"})

    def test_blocked_by_requires_the_blocked_state(self):
        self.assertFlags(
            VALID.replace("| \U0001f6ab | S | Blocked by [Q1](#Q1).",
                          "| \U0001f532 | S | Blocked by [Q1](#Q1)."),
            "blocked by")

    def test_an_unresolvable_id_link_is_flagged(self):
        self.assertFlags(VALID.replace("[Q1](#Q1)", "[Q77](#Q77)"), "q77")

    def test_a_deferred_trigger_needs_a_concrete_verb(self):
        self.assertFlags(
            VALID.replace("**Demand:** someone asks for it.",
                          "we might want this one day."), "demand")

    def test_a_last_touched_line_is_flagged(self):
        # The bare form the old format actually used (verified against this
        # repo's own pre-migration history), which is what the rule anchors on.
        self.assertFlags(
            VALID.replace("**Next ID:** Q5",
                          "Last touched: 2026-01-01\n**Next ID:** Q5"),
            "last touched")

    def test_a_file_without_a_queue_section_is_flagged(self):
        self.assertFlags(VALID.replace("## Queue", "## Backlog"), "queue")

    def test_a_missing_file_is_an_error_not_a_pass(self):
        p = run("lint-backlog.sh", os.path.join(self.dir, "nope.md"))
        self.assertNotEqual(p.returncode, 0)


@unittest.skipUnless(BASH, "no bash on PATH")
class NextTaskTests(unittest.TestCase):
    """`next-task.sh` picks the top ready row and formats a kickoff prompt."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = write(os.path.join(self.dir, "STATUS.md"), VALID)

    def tearDown(self):
        self._tmp.cleanup()

    def test_it_prints_a_prompt_for_the_top_ready_row(self):
        p = run("next-task.sh", self.path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Q1: Do the first thing", p.stdout)
        self.assertIn("docs/STATUS.md", p.stdout)
        self.assertIn("A short note.", p.stdout)

    def test_title_mode_is_just_the_id_and_item(self):
        p = run("next-task.sh", "--title", self.path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "Q1: Do the first thing")

    def test_it_skips_blocked_rows(self):
        # Q1 blocked -> Q2 is still blocked -> nothing ready.
        write(self.path, VALID.replace(
            "| \U0001f532 | S | A short note.", "| \U0001f6ab | S | A short note."))
        p = run("next-task.sh", self.path)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no ready", p.stderr)

    def test_it_strips_link_markup_from_the_title(self):
        write(self.path, VALID.replace(
            "Do the first thing", "[Do the first thing](plan/x.md)"))
        p = run("next-task.sh", "--title", self.path)
        self.assertEqual(p.stdout.strip(), "Q1: Do the first thing")

    def test_a_deferred_row_is_never_picked(self):
        # Q3 lives in the Deferred table; with no ready Queue row, nothing is
        # returned rather than the deferred one.
        text = VALID.replace("| \U0001f532 | S | A short note.",
                             "| \U0001f6ab | S | A short note.")
        write(self.path, text)
        p = run("next-task.sh", "--title", self.path)
        self.assertNotIn("Q3", p.stdout)

    def test_a_missing_file_exits_two(self):
        p = run("next-task.sh", os.path.join(self.dir, "nope.md"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("not found", p.stderr)


@unittest.skipUnless(BASH and shutil.which("git"), "needs bash and git")
class BacklogMetricsTests(unittest.TestCase):
    """`backlog-metrics.sh` replays the backlog file's own git history."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "STATUS.md")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        write(self.path, VALID)
        self.git("add", "STATUS.md")
        self.git("commit", "-q", "-m", "docs(status): file Q1 and Q2")
        # Complete Q1 -- the removal verb is what makes throughput honest.
        write(self.path, VALID.replace(
            '| <a id="Q1"></a>Q1 | Do the first thing | `tests` '
            '| \U0001f532 | S | A short note. |\n', ""))
        self.git("commit", "-qam", "docs(status): complete Q1")

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.dir, check=True,
                       capture_output=True, text=True, timeout=60)

    def test_summary_mode_runs_over_real_history(self):
        p = run("backlog-metrics.sh", self.path, cwd=self.dir)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.strip(), "summary produced no output")

    def test_events_mode_emits_a_row_for_a_completed_item(self):
        p = run("backlog-metrics.sh", "--events", self.path, cwd=self.dir)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Q1", p.stdout)
        rows = [ln for ln in p.stdout.splitlines() if ln.startswith("Q1")]
        self.assertTrue(rows, f"no Q1 event row in:\n{p.stdout}")
        self.assertIn("complete", rows[0],
                      "the docs(status) verb should be recorded as the reason")

    def test_a_missing_file_exits_two(self):
        p = run("backlog-metrics.sh", os.path.join(self.dir, "nope.md"),
                cwd=self.dir)
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
