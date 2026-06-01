#!/usr/bin/env python3
"""Tests for scripts/bash-workspace-guard.py.

Run with: python3 -m unittest discover tests
     or:  python3 tests/test_workspace_guard.py

Two layers:
  * Unit tests import `files_in_command` and exercise per-command parsing.
  * End-to-end tests invoke the script as a subprocess and inspect the
    PreToolUse decision JSON it emits.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "bash-workspace-guard.py"

# Filename has a dash, so import by path.
_spec = util.spec_from_file_location("workspace_guard", SCRIPT)
guard = util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class SpecShapeTests(unittest.TestCase):
    """Guard against silent removal of guarded commands."""

    def test_spec_covers_documented_commands(self):
        self.assertEqual(
            set(guard.SPEC.keys()),
            {"grep", "rg", "sed", "awk", "jq", "cat", "head", "tail"},
        )

    def test_documented_aliases_present(self):
        self.assertEqual(
            guard.ALIASES,
            {"egrep": "grep", "fgrep": "grep",
             "gawk": "awk", "mawk": "awk"},
        )


class FilesInCommandTests(unittest.TestCase):
    """Per-SPEC-row file extraction."""

    # --- cat / head / tail ---------------------------------------------------

    def test_cat_positional_file(self):
        self.assertEqual(guard.files_in_command(["cat", "foo.txt"]), ["foo.txt"])

    def test_cat_multiple_positionals(self):
        self.assertEqual(
            guard.files_in_command(["cat", "a.txt", "b.txt"]),
            ["a.txt", "b.txt"],
        )

    def test_cat_dash_kept_as_positional(self):
        # main() filters '-' before the workspace check; files_in_command
        # itself returns it as a positional.
        self.assertEqual(guard.files_in_command(["cat", "-"]), ["-"])

    def test_head_consume_short_flag(self):
        self.assertEqual(
            guard.files_in_command(["head", "-n", "20", "foo.txt"]),
            ["foo.txt"],
        )

    def test_head_inline_eq_flag(self):
        self.assertEqual(
            guard.files_in_command(["head", "--lines=20", "foo.txt"]),
            ["foo.txt"],
        )

    def test_tail_unknown_flag_assumed_zero_arg(self):
        # `tail -f foo.txt` -> -f isn't in `consume`, so file is foo.txt.
        self.assertEqual(
            guard.files_in_command(["tail", "-f", "foo.txt"]),
            ["foo.txt"],
        )

    # --- grep ----------------------------------------------------------------

    def test_grep_pattern_positional(self):
        self.assertEqual(
            guard.files_in_command(["grep", "PAT", "foo.txt"]),
            ["foo.txt"],
        )

    def test_grep_prog_suppressed_by_dash_e(self):
        # -e PAT means the first positional is a file, not a pattern.
        self.assertEqual(
            guard.files_in_command(["grep", "-e", "PAT", "foo.txt"]),
            ["foo.txt"],
        )

    def test_grep_prog_suppressed_by_long_regexp(self):
        self.assertEqual(
            guard.files_in_command(["grep", "--regexp", "PAT", "foo.txt"]),
            ["foo.txt"],
        )

    def test_grep_file_flag_short(self):
        self.assertEqual(
            guard.files_in_command(["grep", "-f", "patterns.txt", "foo.txt"]),
            ["patterns.txt", "foo.txt"],
        )

    def test_grep_file_flag_long_inline(self):
        self.assertEqual(
            guard.files_in_command(
                ["grep", "--file=patterns.txt", "foo.txt"]
            ),
            ["patterns.txt", "foo.txt"],
        )

    def test_grep_consume_two_value_flag_chain(self):
        # -A 3 consumes the 3, then PAT is prog, foo.txt is the file.
        self.assertEqual(
            guard.files_in_command(["grep", "-A", "3", "PAT", "foo.txt"]),
            ["foo.txt"],
        )

    # --- sed -----------------------------------------------------------------

    def test_sed_default_program_positional(self):
        self.assertEqual(
            guard.files_in_command(["sed", "s/a/b/", "foo.txt"]),
            ["foo.txt"],
        )

    def test_sed_prog_suppressed_by_dash_e(self):
        self.assertEqual(
            guard.files_in_command(["sed", "-e", "s/a/b/", "foo.txt"]),
            ["foo.txt"],
        )

    def test_sed_file_flag(self):
        self.assertEqual(
            guard.files_in_command(["sed", "-f", "script.sed", "foo.txt"]),
            ["script.sed", "foo.txt"],
        )

    # --- awk -----------------------------------------------------------------

    def test_awk_program_positional(self):
        self.assertEqual(
            guard.files_in_command(["awk", "{print}", "foo.txt"]),
            ["foo.txt"],
        )

    def test_awk_skip_assignment_operands(self):
        # `FS=,` is a var assignment, not a file.
        self.assertEqual(
            guard.files_in_command(["awk", "{print}", "FS=,", "foo.txt"]),
            ["foo.txt"],
        )

    def test_awk_file_flag(self):
        self.assertEqual(
            guard.files_in_command(["awk", "-f", "script.awk", "foo.txt"]),
            ["script.awk", "foo.txt"],
        )

    def test_awk_dash_v_consumes_value(self):
        self.assertEqual(
            guard.files_in_command(["awk", "-v", "x=1", "{print}", "foo.txt"]),
            ["foo.txt"],
        )

    # --- jq ------------------------------------------------------------------

    def test_jq_program_positional(self):
        self.assertEqual(
            guard.files_in_command(["jq", ".foo", "foo.json"]),
            ["foo.json"],
        )

    def test_jq_arg_consumes_two_non_file(self):
        self.assertEqual(
            guard.files_in_command(
                ["jq", "--arg", "name", "value", ".", "main.json"]
            ),
            ["main.json"],
        )

    def test_jq_slurpfile_file_at_index_1(self):
        # --slurpfile VAR FILE -> VAR is not a file, FILE is.
        self.assertEqual(
            guard.files_in_command(
                ["jq", "--slurpfile", "data", "aux.json", ".", "main.json"]
            ),
            ["aux.json", "main.json"],
        )

    def test_jq_rawfile_file_at_index_1(self):
        self.assertEqual(
            guard.files_in_command(
                ["jq", "--rawfile", "data", "aux.txt", ".", "main.json"]
            ),
            ["aux.txt", "main.json"],
        )

    def test_jq_from_file_suppresses_prog(self):
        # -f script.jq -> no prog positional; first positional is a file.
        self.assertEqual(
            guard.files_in_command(
                ["jq", "-f", "script.jq", "main.json"]
            ),
            ["script.jq", "main.json"],
        )

    # --- generic parser behavior --------------------------------------------

    def test_end_of_options_double_dash(self):
        # After `--`, even tokens starting with `-` are positional.
        self.assertEqual(
            guard.files_in_command(["cat", "--", "-foo"]),
            ["-foo"],
        )

    def test_unknown_command_returns_none(self):
        self.assertIsNone(guard.files_in_command(["ls", "/etc"]))

    def test_aliases_resolve(self):
        self.assertEqual(
            guard.files_in_command(["egrep", "PAT", "foo.txt"]),
            ["foo.txt"],
        )
        self.assertEqual(
            guard.files_in_command(["fgrep", "PAT", "foo.txt"]),
            ["foo.txt"],
        )
        self.assertEqual(
            guard.files_in_command(["gawk", "{print}", "foo.txt"]),
            ["foo.txt"],
        )
        self.assertEqual(
            guard.files_in_command(["mawk", "{print}", "foo.txt"]),
            ["foo.txt"],
        )

    # --- rg (dedicated SPEC, not aliased to grep — see Q3) ------------------

    def test_rg_pattern_positional(self):
        self.assertEqual(
            guard.files_in_command(["rg", "PAT", "foo.txt"]),
            ["foo.txt"],
        )

    def test_rg_glob_consumes_value(self):
        # The Q3 motivating case: -g '*.py' must not leak as a positional.
        self.assertEqual(
            guard.files_in_command(["rg", "-g", "*.py", "PAT", "path"]),
            ["path"],
        )

    def test_rg_long_glob_inline_eq(self):
        self.assertEqual(
            guard.files_in_command(["rg", "--glob=*.py", "PAT", "path"]),
            ["path"],
        )

    def test_rg_type_consumes_value(self):
        self.assertEqual(
            guard.files_in_command(["rg", "-t", "py", "PAT", "path"]),
            ["path"],
        )

    def test_rg_prog_suppressed_by_dash_e(self):
        self.assertEqual(
            guard.files_in_command(["rg", "-e", "PAT", "foo.txt"]),
            ["foo.txt"],
        )

    def test_rg_file_flag_short(self):
        self.assertEqual(
            guard.files_in_command(["rg", "-f", "patterns.txt", "foo.txt"]),
            ["patterns.txt", "foo.txt"],
        )

    def test_rg_ignore_file_is_file_flag(self):
        self.assertEqual(
            guard.files_in_command(
                ["rg", "--ignore-file", "ignore.txt", "PAT", "foo.txt"]
            ),
            ["ignore.txt", "foo.txt"],
        )

    def test_rg_max_depth_consumes_value(self):
        self.assertEqual(
            guard.files_in_command(["rg", "--max-depth", "3", "PAT", "path"]),
            ["path"],
        )

    def test_basename_strips_path_prefix(self):
        self.assertEqual(
            guard.files_in_command(["/usr/bin/cat", "foo.txt"]),
            ["foo.txt"],
        )

    def test_split_eq_helper(self):
        self.assertEqual(guard.split_eq("--file=x"), ("--file", "x"))
        self.assertEqual(guard.split_eq("--file"), ("--file", None))
        self.assertEqual(guard.split_eq("-f"), ("-f", None))
        # Short opts with `=` are not parsed as inline.
        self.assertEqual(guard.split_eq("-fx"), ("-fx", None))


class AllowedDeviceTests(unittest.TestCase):
    """Allowlist of well-known device / FD paths."""

    def test_dev_null_allowed(self):
        self.assertTrue(guard.is_allowed_device("/dev/null"))

    def test_standard_streams_allowed(self):
        for p in ("/dev/stdin", "/dev/stdout", "/dev/stderr"):
            self.assertTrue(guard.is_allowed_device(p), p)

    def test_random_sources_allowed(self):
        for p in ("/dev/random", "/dev/urandom", "/dev/zero", "/dev/tty"):
            self.assertTrue(guard.is_allowed_device(p), p)

    def test_dev_fd_numeric_allowed(self):
        self.assertTrue(guard.is_allowed_device("/dev/fd/0"))
        self.assertTrue(guard.is_allowed_device("/dev/fd/63"))

    def test_dev_fd_non_numeric_rejected(self):
        # `/dev/fd/abc` is not a real FD reference — don't allowlist it.
        self.assertFalse(guard.is_allowed_device("/dev/fd/abc"))
        self.assertFalse(guard.is_allowed_device("/dev/fd/"))

    def test_other_dev_paths_rejected(self):
        # Only the explicit allowlist bypasses — `/dev/sda1` etc. still go
        # through the workspace check.
        self.assertFalse(guard.is_allowed_device("/dev/sda1"))
        self.assertFalse(guard.is_allowed_device("/dev/null.bak"))
        self.assertFalse(guard.is_allowed_device("dev/null"))  # relative


def run_hook(cmd, cwd, project_dir=None):
    """Invoke the hook as a subprocess. Returns parsed JSON or None on defer."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = project_dir or cwd
    payload = json.dumps({"tool_input": {"command": cmd}, "cwd": cwd})
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"hook exited {result.returncode}; stderr={result.stderr!r}"
        )
    out = result.stdout.strip()
    return json.loads(out) if out else None


class HookEndToEndTests(unittest.TestCase):
    """Decisions emitted by the script for full command lines."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _decision(self, cmd, expected, *, cwd=None, project_dir=None):
        out = run_hook(cmd, cwd or self.workspace, project_dir=project_dir)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {cmd!r}; got {got!r} "
            f"(reason: {out['hookSpecificOutput'].get('permissionDecisionReason')!r})",
        )
        return out

    def _defer(self, cmd, *, cwd=None, project_dir=None):
        out = run_hook(cmd, cwd or self.workspace, project_dir=project_dir)
        self.assertIsNone(out, f"expected defer for {cmd!r}; got {out!r}")

    # --- workspace files allow ----------------------------------------------

    def test_cat_workspace_file_allow(self):
        self._decision("cat in.txt", "allow")

    def test_grep_workspace_file_allow(self):
        self._decision("grep PAT in.txt", "allow")

    def test_sed_workspace_file_allow(self):
        self._decision("sed 's/a/b/' in.txt", "allow")

    def test_jq_program_only_workspace_allow(self):
        self._decision("jq '.a/.b' in.txt", "allow")

    def test_pipe_chain_workspace_allow(self):
        self._decision("cat in.txt | grep PAT", "allow")

    # --- outside-workspace ask ----------------------------------------------

    def test_cat_outside_ask(self):
        out = self._decision("cat /etc/hosts", "ask")
        self.assertIn(
            "/etc/hosts",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_grep_outside_ask(self):
        self._decision("grep secret /etc/passwd", "ask")

    def test_jq_outside_ask(self):
        self._decision("jq .x /etc/hosts", "ask")

    def test_sed_pattern_file_outside_ask(self):
        # -f /tmp/evil.sed -> pattern file itself is outside.
        self._decision("sed -f /tmp/evil.sed in.txt", "ask")

    def test_grep_prog_suppressed_e_outside_ask(self):
        self._decision("grep -e PAT /etc/hosts", "ask")

    def test_grep_inline_eq_pattern_file_outside_ask(self):
        self._decision("grep --file=/etc/patterns in.txt", "ask")

    def test_jq_slurpfile_outside_ask(self):
        self._decision("jq --slurpfile d /etc/hosts . in.txt", "ask")

    def test_realpath_traversal_outside_ask(self):
        nested = os.path.join(self.workspace, "sub")
        os.mkdir(nested)
        # cwd inside workspace + `..` chain escapes via realpath.
        self._decision("cat ../../../etc/hosts", "ask", cwd=nested)

    # --- redirect capture ---------------------------------------------------

    def test_redirect_target_outside_ask(self):
        out = self._decision("cat in.txt > /tmp/out.txt", "ask")
        self.assertIn(
            "/tmp/out.txt",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_redirect_target_inside_allow(self):
        self._decision("cat in.txt > out.txt", "allow")

    def test_redirect_append_outside_ask(self):
        self._decision("cat in.txt >> /tmp/out.txt", "ask")

    # --- device allowlist ---------------------------------------------------

    def test_cat_dev_null_allow(self):
        self._decision("cat /dev/null", "allow")

    def test_redirect_to_dev_null_allow(self):
        self._decision("cat in.txt > /dev/null", "allow")

    def test_cat_dev_stdin_allow(self):
        # Verifies raw-token match: /dev/stdin realpath-resolves to /dev/fd/0
        # on darwin and /proc/self/fd/0 on Linux, but the literal token is
        # what users write.
        self._decision("cat /dev/stdin", "allow")

    def test_cat_dev_fd_numeric_allow(self):
        self._decision("cat /dev/fd/3", "allow")

    def test_cat_dev_sda_outside_ask(self):
        # Only the explicit allowlist bypasses; other /dev/ paths still ask.
        self._decision("cat /dev/sda1", "ask")

    # --- alias end-to-end ---------------------------------------------------

    def test_egrep_outside_ask(self):
        self._decision("egrep PAT /etc/hosts", "ask")

    def test_gawk_workspace_allow(self):
        self._decision("gawk '{print}' in.txt", "allow")

    # --- rg end-to-end ------------------------------------------------------

    def test_rg_glob_workspace_allow(self):
        # Q3 motivating case: `-g '*.py'` must not flag '*.py' as outside.
        self._decision("rg -g '*.py' PAT in.txt", "allow")

    def test_rg_outside_ask(self):
        self._decision("rg PAT /etc/hosts", "ask")

    def test_rg_type_workspace_allow(self):
        self._decision("rg -t py PAT in.txt", "allow")

    # --- defer paths --------------------------------------------------------

    def test_unguarded_command_defers(self):
        self._defer("ls /etc")

    def test_empty_command_defers(self):
        self._defer("   ")

    def test_unbalanced_quotes_defers(self):
        # shlex raises -> hook defers silently.
        self._defer('cat "unclosed')

    def test_only_redirect_no_guarded_command_defers(self):
        # `ls > /tmp/out` is not a guarded command, so the hook defers
        # even though the redirect target is outside-workspace. The redirect
        # collector only consults `outside` once at least one guarded simple
        # command is present.
        self._defer("ls > /tmp/out.txt")


if __name__ == "__main__":
    unittest.main()
