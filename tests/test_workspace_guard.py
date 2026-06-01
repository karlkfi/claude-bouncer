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
            {"grep", "rg", "sed", "awk", "jq", "cat", "head", "tail",
             # Q9: cat-shape commands with file-naming flags.
             "sort", "wc", "diff", "file", "hexdump"},
        )

    def test_documented_aliases_present(self):
        self.assertEqual(
            guard.ALIASES,
            {"egrep": "grep", "fgrep": "grep",
             "gawk": "awk", "mawk": "awk",
             # Q9: pure cat-shape readers aliased to `cat`.
             "less": "cat", "more": "cat",
             "tac": "cat", "rev": "cat", "nl": "cat",
             "uniq": "cat", "xxd": "cat", "od": "cat",
             "strings": "cat", "cmp": "cat",
             "zcat": "cat", "gzcat": "cat",
             "bzcat": "cat", "xzcat": "cat"},
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

    # --- sort / wc / diff / file / hexdump (Q9) -----------------------------

    def test_sort_positional_files(self):
        self.assertEqual(
            guard.files_in_command(["sort", "a.txt", "b.txt"]),
            ["a.txt", "b.txt"],
        )

    def test_sort_output_short_flag_is_file(self):
        # `-o FILE` writes to FILE — must be tracked, not consumed.
        self.assertEqual(
            guard.files_in_command(["sort", "-o", "out.txt", "in.txt"]),
            ["out.txt", "in.txt"],
        )

    def test_sort_output_long_flag_is_file(self):
        self.assertEqual(
            guard.files_in_command(["sort", "--output", "out.txt", "in.txt"]),
            ["out.txt", "in.txt"],
        )

    def test_sort_output_inline_eq_is_file(self):
        self.assertEqual(
            guard.files_in_command(["sort", "--output=out.txt", "in.txt"]),
            ["out.txt", "in.txt"],
        )

    def test_sort_files0_from_is_file(self):
        self.assertEqual(
            guard.files_in_command(["sort", "--files0-from=list.txt"]),
            ["list.txt"],
        )

    def test_sort_field_separator_consumes_value(self):
        # `-t :` and `-k 1` must not leak as positional files.
        self.assertEqual(
            guard.files_in_command(
                ["sort", "-t", ":", "-k", "1", "in.txt"]
            ),
            ["in.txt"],
        )

    def test_wc_positional_file(self):
        self.assertEqual(guard.files_in_command(["wc", "in.txt"]), ["in.txt"])

    def test_wc_files0_from_is_file(self):
        self.assertEqual(
            guard.files_in_command(["wc", "--files0-from=list.txt"]),
            ["list.txt"],
        )

    def test_wc_boolean_flag_not_consumed(self):
        # `-l` takes no value — file is in.txt, not ""
        self.assertEqual(
            guard.files_in_command(["wc", "-l", "in.txt"]),
            ["in.txt"],
        )

    def test_diff_two_positional_files(self):
        self.assertEqual(
            guard.files_in_command(["diff", "a.txt", "b.txt"]),
            ["a.txt", "b.txt"],
        )

    def test_diff_unified_consumes_value(self):
        self.assertEqual(
            guard.files_in_command(["diff", "-U", "3", "a.txt", "b.txt"]),
            ["a.txt", "b.txt"],
        )

    def test_diff_from_file_is_file(self):
        self.assertEqual(
            guard.files_in_command(
                ["diff", "--from-file=base.txt", "new.txt"]
            ),
            ["base.txt", "new.txt"],
        )

    def test_diff_to_file_is_file(self):
        self.assertEqual(
            guard.files_in_command(
                ["diff", "--to-file", "target.txt", "src.txt"]
            ),
            ["target.txt", "src.txt"],
        )

    def test_file_positional(self):
        self.assertEqual(
            guard.files_in_command(["file", "foo.bin"]),
            ["foo.bin"],
        )

    def test_file_dash_f_reads_file_list(self):
        # `file -f LIST` reads filenames to test from LIST — LIST is a file.
        self.assertEqual(
            guard.files_in_command(["file", "-f", "list.txt"]),
            ["list.txt"],
        )

    def test_hexdump_positional(self):
        self.assertEqual(
            guard.files_in_command(["hexdump", "data.bin"]),
            ["data.bin"],
        )

    def test_hexdump_dash_f_reads_format_file(self):
        # `hexdump -f FILE` reads format spec from FILE.
        self.assertEqual(
            guard.files_in_command(
                ["hexdump", "-f", "fmt.txt", "data.bin"]
            ),
            ["fmt.txt", "data.bin"],
        )

    def test_hexdump_dash_e_consumes_value(self):
        # `-e FORMAT_STRING` consumes the inline format.
        self.assertEqual(
            guard.files_in_command(
                ["hexdump", "-e", '"%x"', "data.bin"]
            ),
            ["data.bin"],
        )

    # --- Q9 aliases (cat-shape readers) -------------------------------------

    def test_q9_aliases_resolve_to_cat(self):
        # Each alias should parse identically to bare `cat foo.txt`.
        for cmd in ("less", "more", "tac", "rev", "nl", "uniq",
                    "xxd", "od", "strings", "cmp",
                    "zcat", "gzcat", "bzcat", "xzcat"):
            self.assertEqual(
                guard.files_in_command([cmd, "foo.txt"]),
                ["foo.txt"],
                f"alias {cmd!r} did not resolve to cat-shape",
            )

    def test_alias_unknown_flag_treats_value_as_positional(self):
        # Documented false-positive: `tac -s SEP foo.txt` — cat doesn't know
        # `-s`, so SEP becomes a positional file. In practice SEP resolves
        # lexically inside cwd (harmless allow); only flagged when it looks
        # like an absolute outside path.
        self.assertEqual(
            guard.files_in_command(["tac", "-s", ",", "foo.txt"]),
            [",", "foo.txt"],
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


class StripEnvPrefixTests(unittest.TestCase):
    """POSIX command-prefix assignments are dropped before SPEC lookup (Q6)."""

    def test_single_assignment_stripped(self):
        self.assertEqual(
            guard.strip_env_prefix(["LC_ALL=C", "cat", "/etc/passwd"]),
            ["cat", "/etc/passwd"],
        )

    def test_multiple_assignments_stripped(self):
        self.assertEqual(
            guard.strip_env_prefix(["FOO=1", "BAR=2", "cat", "x"]),
            ["cat", "x"],
        )

    def test_empty_value_assignment_stripped(self):
        self.assertEqual(
            guard.strip_env_prefix(["FOO=", "cat", "x"]),
            ["cat", "x"],
        )

    def test_underscore_leading_name_stripped(self):
        self.assertEqual(
            guard.strip_env_prefix(["_X=1", "cat", "x"]),
            ["cat", "x"],
        )

    def test_assignment_only_returns_empty(self):
        # `FOO=bar` alone is a pure shell assignment with no command.
        self.assertEqual(guard.strip_env_prefix(["FOO=bar"]), [])

    def test_stops_at_first_non_assignment(self):
        # `FOO=1 cat BAR=2 baz` — BAR=2 is an operand to cat, not stripped.
        self.assertEqual(
            guard.strip_env_prefix(["FOO=1", "cat", "BAR=2", "baz"]),
            ["cat", "BAR=2", "baz"],
        )

    def test_invalid_name_not_stripped(self):
        # `1FOO=bar` is not a valid POSIX variable name; leave it alone.
        self.assertEqual(
            guard.strip_env_prefix(["1FOO=bar", "cat"]),
            ["1FOO=bar", "cat"],
        )

    def test_flag_not_stripped(self):
        self.assertEqual(
            guard.strip_env_prefix(["--foo=bar", "cat"]),
            ["--foo=bar", "cat"],
        )

    def test_no_equals_not_stripped(self):
        self.assertEqual(
            guard.strip_env_prefix(["cat", "x"]),
            ["cat", "x"],
        )


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

    # --- shell expansions (Q5) ----------------------------------------------

    def test_tilde_path_outside_ask(self):
        # `~/...` is expanded by bash to $HOME at runtime; shlex leaves it
        # literal. Lexical resolution would put it inside cwd — must ask.
        out = self._decision("cat ~/.ssh/id_rsa", "ask")
        self.assertIn(
            "~/.ssh/id_rsa",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_dollar_var_path_outside_ask(self):
        out = self._decision("cat $HOME/.aws/credentials", "ask")
        self.assertIn(
            "$HOME/.aws/credentials",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_quoted_dollar_var_outside_ask(self):
        # Double quotes preserve `$` expansion in bash; shlex strips the
        # quotes but the literal `$HOME` remains in the token. Still ask.
        self._decision('cat "$HOME/secret"', "ask")

    def test_curly_dollar_var_outside_ask(self):
        self._decision("cat ${HOME}/secret", "ask")

    def test_redirect_to_tilde_outside_ask(self):
        self._decision("cat in.txt > ~/evil", "ask")

    def test_redirect_to_dollar_var_outside_ask(self):
        self._decision("cat in.txt > $LOG/evil", "ask")

    def test_tilde_in_middle_of_token_allowed(self):
        # `~` only triggers when it's the leading character — bash only
        # tilde-expands at word start. A literal `foo~bak` inside workspace
        # should still allow.
        self._decision("cat foo~bak", "allow")

    # --- cd / pushd / popd shift cwd (Q7) -----------------------------------

    def test_cd_then_relative_outside_ask(self):
        # `cd /etc && cat passwd` — bash runs cat in /etc, so `passwd` is
        # /etc/passwd. The pre-Q7 hook resolved against the original cwd and
        # returned allow.
        out = self._decision("cd /etc && cat passwd", "ask")
        self.assertIn(
            "passwd",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_pushd_then_relative_outside_ask(self):
        self._decision("pushd /etc && cat passwd", "ask")

    def test_cd_with_semicolon_separator_outside_ask(self):
        self._decision("cd /etc; cat passwd", "ask")

    def test_cd_into_subshell_outside_ask(self):
        # `(cd /etc; cat passwd)` — subshell restores cwd for the parent but
        # we still flag the inner `cat passwd` against /etc.
        self._decision("(cd /etc; cat passwd)", "ask")

    def test_cd_workspace_subdir_relative_allow(self):
        # `cd subdir && cat in.txt` where both subdir and in.txt are inside
        # the workspace — re-rooting keeps this an allow.
        nested = os.path.join(self.workspace, "sub")
        os.mkdir(nested)
        with open(os.path.join(nested, "x.txt"), "w") as f:
            f.write("hi\n")
        self._decision("cd sub && cat x.txt", "allow")

    def test_cd_absolute_workspace_path_allow(self):
        # `cd <workspace>/sub && cat x.txt` — absolute cd into workspace
        # still allows subsequent in-workspace reads.
        nested = os.path.join(self.workspace, "sub")
        os.mkdir(nested)
        with open(os.path.join(nested, "x.txt"), "w") as f:
            f.write("hi\n")
        self._decision(f"cd {nested} && cat x.txt", "allow")

    def test_popd_taints_subsequent_relative_outside_ask(self):
        # popd's effect can't be tracked; any subsequent relative path in a
        # guarded group is treated as outside.
        self._decision("popd && cat in.txt", "ask")

    def test_bare_cd_taints_subsequent_relative_outside_ask(self):
        # `cd` with no arg goes to $HOME — we can't track precisely.
        self._decision("cd && cat in.txt", "ask")

    def test_cd_dash_taints_subsequent_relative_outside_ask(self):
        # `cd -` toggles to OLDPWD — same untracked situation.
        self._decision("cd - && cat in.txt", "ask")

    def test_cd_dollar_var_taints_subsequent_relative_outside_ask(self):
        # cd target with `$` can't be resolved at hook time.
        self._decision("cd $HOME && cat in.txt", "ask")

    def test_cd_tilde_taints_subsequent_relative_outside_ask(self):
        self._decision("cd ~ && cat in.txt", "ask")

    def test_cd_does_not_taint_absolute_paths(self):
        # `cd /etc && cat /etc/passwd` already had `/etc/passwd` flagged via
        # the absolute path. Q7 doesn't change that — verify it still asks.
        self._decision("cd /etc && cat /etc/passwd", "ask")

    def test_cd_only_command_defers(self):
        # `cd /etc` alone has no guarded command — must defer.
        self._defer("cd /etc")

    def test_first_group_unaffected_by_later_cd(self):
        # `cat in.txt; cd /etc; cat passwd` — first cat reads workspace file,
        # only the second is flagged. Decision is the union, so still ask,
        # but the outside list must not contain `in.txt`.
        out = self._decision("cat in.txt; cd /etc; cat passwd", "ask")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("passwd", reason)
        self.assertNotIn("in.txt", reason)

    def test_classify_cd_helper_arg(self):
        self.assertEqual(guard.classify_cd(["cd", "/etc"]), ("arg", "/etc"))
        self.assertEqual(guard.classify_cd(["pushd", "/tmp"]), ("arg", "/tmp"))
        self.assertEqual(guard.classify_cd(["cd", "-L", "/etc"]), ("arg", "/etc"))

    def test_classify_cd_helper_unknown(self):
        self.assertEqual(guard.classify_cd(["cd"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["cd", "-"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["cd", "~/foo"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["cd", "$HOME"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["pushd", "+1"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["popd"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["popd", "+0"]), ("unknown", None))

    def test_classify_cd_helper_not_cd(self):
        self.assertEqual(guard.classify_cd(["cat", "foo"]), (None, None))
        self.assertEqual(guard.classify_cd([]), (None, None))

    # --- ln -s symlink staging (Q8) -----------------------------------------

    def test_ln_outside_target_then_cat_link_ask(self):
        # The Q8 motivating case: `ln -s OUTSIDE link && cat link`. Pre-Q8,
        # `link` didn't exist at hook time so realpath kept it lexically inside
        # the workspace and the whole chain was allowed.
        out = self._decision(
            "ln -s /tmp/q8-fake-target link && cat link", "ask",
        )
        self.assertIn(
            "link",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_ln_inside_target_then_cat_link_allow(self):
        # Innocent in-workspace symlink — staging must not false-positive.
        self._decision("ln -s in.txt link && cat link", "allow")

    def test_ln_long_symbolic_flag_staged(self):
        self._decision(
            "ln --symbolic /tmp/q8-fake-target link && cat link", "ask",
        )

    def test_ln_combined_short_flags_staged(self):
        # `-fs` / `-fns` — symbolic mode hides inside the combined flag.
        self._decision(
            "ln -fs /tmp/q8-fake-target link && cat link", "ask",
        )
        self._decision(
            "ln -fns /tmp/q8-fake-target link && cat link", "ask",
        )

    def test_ln_without_dash_s_not_staged(self):
        # Hard link is a separate threat (same TOCTOU shape, single-filesystem
        # only); Q8 stays scoped to `-s`. Verify the staging logic doesn't
        # silently catch the hard-link form and lull a future reader.
        self._decision(
            "ln /tmp/q8-fake-target link && cat link", "allow",
        )

    def test_ln_omitted_link_uses_basename(self):
        # `ln -s /tmp/q8-fake-target` creates `q8-fake-target` in cwd.
        self._decision(
            "ln -s /tmp/q8-fake-target && cat q8-fake-target", "ask",
        )

    def test_ln_absolute_outside_link_caught_by_existing_check(self):
        # Link itself is outside-workspace; the cat already asks via the
        # absolute-path rule, staging is a no-op. Decision is still ask.
        self._decision(
            "ln -s /tmp/q8-fake-target /tmp/q8-link && cat /tmp/q8-link",
            "ask",
        )

    def test_ln_after_cd_stages_against_shifted_cwd(self):
        # `cd /tmp && ln -s OUTSIDE link && cat link` — link lives in /tmp,
        # so the staged path is /tmp/link. The cat must still ask.
        self._decision(
            "cd /tmp && ln -s /tmp/q8-fake-target link && cat link", "ask",
        )

    def test_ln_inside_target_relative_link_outside_workspace(self):
        # `ln -s ./in.txt /tmp/out` — target inside, link outside. Staging
        # skips (target inside), but the resulting symlink lives outside, so
        # no later guarded read in the workspace would be affected. This
        # scenario stays allow because there's no later cat inside-workspace.
        # The `ln` itself isn't guarded yet (that's Q11's scope).
        self._defer("ln -s ./in.txt /tmp/out")

    def test_ln_subdir_link_path_stages_correctly(self):
        # `ln -s OUTSIDE ./sub/link && cat ./sub/link` — staged path is
        # <cwd>/sub/link; the cat must match it.
        nested = os.path.join(self.workspace, "sub")
        os.mkdir(nested)
        self._decision(
            "ln -s /tmp/q8-fake-target ./sub/link && cat ./sub/link", "ask",
        )

    def test_ln_dollar_target_stages_link_as_outside(self):
        # `$HOME` target can't be resolved at hook time; secure-by-default
        # treats it as outside, so link gets staged.
        self._decision(
            "ln -s $HOME/secret link && cat link", "ask",
        )

    def test_ln_dollar_link_not_staged_but_cat_asks_anyway(self):
        # `link` with `$` is unresolvable — staging can't pin it down. The
        # later `cat $X` still asks via the existing $/~ rule.
        self._decision(
            "ln -s /tmp/q8-fake-target $LINK && cat $LINK", "ask",
        )

    def test_ln_only_command_defers(self):
        # `ln -s OUTSIDE link` alone has no guarded command — must defer
        # (ln itself isn't guarded; that's Q11).
        self._defer("ln -s /tmp/q8-fake-target link")

    def test_classify_ln_helper_basic(self):
        self.assertEqual(
            guard.classify_ln(["ln", "-s", "/tmp/x", "link"]),
            ("/tmp/x", "link"),
        )

    def test_classify_ln_helper_omitted_link(self):
        self.assertEqual(
            guard.classify_ln(["ln", "-s", "/tmp/x"]),
            ("/tmp/x", None),
        )

    def test_classify_ln_helper_long_flag(self):
        self.assertEqual(
            guard.classify_ln(["ln", "--symbolic", "/tmp/x", "link"]),
            ("/tmp/x", "link"),
        )

    def test_classify_ln_helper_combined_flags(self):
        self.assertEqual(
            guard.classify_ln(["ln", "-fs", "/tmp/x", "link"]),
            ("/tmp/x", "link"),
        )
        self.assertEqual(
            guard.classify_ln(["ln", "-fns", "/tmp/x", "link"]),
            ("/tmp/x", "link"),
        )

    def test_classify_ln_helper_hard_link_returns_none(self):
        # No `-s` → not symbolic → Q8 staging shouldn't apply.
        self.assertIsNone(guard.classify_ln(["ln", "/tmp/x", "link"]))

    def test_classify_ln_helper_multi_source_returns_none(self):
        # 3+ positionals — multi-source-to-directory form is out of scope.
        self.assertIsNone(
            guard.classify_ln(["ln", "-s", "a", "b", "destdir"]),
        )

    def test_classify_ln_helper_target_directory_flag_consumed(self):
        # `-t DIR` consumes DIR as a value, not a positional.
        self.assertEqual(
            guard.classify_ln(["ln", "-s", "-t", "destdir", "/tmp/x"]),
            ("/tmp/x", None),
        )

    def test_classify_ln_helper_not_ln(self):
        self.assertIsNone(guard.classify_ln(["cat", "-s", "/tmp/x"]))
        self.assertIsNone(guard.classify_ln([]))

    # --- inline env-var prefix (Q6) -----------------------------------------

    def test_env_prefix_outside_ask(self):
        # `LC_ALL=C cat /etc/passwd` — pre-Q6 the assignment masked the
        # command name and the hook deferred entirely.
        out = self._decision("LC_ALL=C cat /etc/passwd", "ask")
        self.assertIn(
            "/etc/passwd",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_env_prefix_workspace_allow(self):
        self._decision("LC_ALL=C cat in.txt", "allow")

    def test_multiple_env_prefix_outside_ask(self):
        self._decision("FOO=1 BAR=2 cat /etc/passwd", "ask")

    def test_env_prefix_before_grep_outside_ask(self):
        # Make sure prog-suppression still works after stripping env prefix.
        self._decision("LC_ALL=C grep -e PAT /etc/passwd", "ask")

    def test_env_prefix_only_defers(self):
        # `FOO=bar` alone is a pure shell assignment — no command, defer.
        self._defer("FOO=bar")

    def test_env_prefix_in_second_group_outside_ask(self):
        # Prefix on a later group in a chain is still stripped.
        self._decision("cat in.txt && LC_ALL=C cat /etc/passwd", "ask")

    # --- heredoc / here-string (Q4) -----------------------------------------

    def test_here_string_path_like_content_allow(self):
        # `<<<` content is stdin data, not a file path — must not be flagged
        # even when it looks like an outside-workspace path.
        self._decision('cat <<<"/etc/foo"', "allow")

    def test_heredoc_path_like_delimiter_allow(self):
        # `<<TAG` delimiter is a sentinel string, not a file path. Even when
        # the delimiter resembles an outside path, the hook must not flag it.
        # (The heredoc body still tokenizes as positional args — that's a
        # separate limitation of stdlib shlex, not in scope for Q4.)
        self._decision("cat <</etc/passwd\nbody\n", "allow")

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

    # --- Q9: cat-family commands (dedicated rows + aliases) -----------------

    def test_sort_workspace_allow(self):
        self._decision("sort in.txt", "allow")

    def test_sort_output_outside_ask(self):
        # `-o /tmp/out.txt` writes outside — must ask, citing /tmp/out.txt.
        out = self._decision("sort -o /tmp/out.txt in.txt", "ask")
        self.assertIn(
            "/tmp/out.txt",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_sort_output_inside_allow(self):
        self._decision("sort -o sorted.txt in.txt", "allow")

    def test_sort_files0_from_outside_ask(self):
        self._decision("sort --files0-from=/etc/hosts", "ask")

    def test_sort_separator_does_not_leak_as_file(self):
        # Regression: -t : -k 1 must consume values, not flag them.
        self._decision("sort -t : -k 1 in.txt", "allow")

    def test_wc_workspace_allow(self):
        self._decision("wc -l in.txt", "allow")

    def test_wc_outside_ask(self):
        self._decision("wc -l /etc/passwd", "ask")

    def test_wc_files0_from_outside_ask(self):
        # Inline `=` form: pre-Q9 cat-alias would have silently dropped it.
        self._decision("wc --files0-from=/etc/list", "ask")

    def test_diff_workspace_allow(self):
        with open(os.path.join(self.workspace, "other.txt"), "w") as f:
            f.write("hi\n")
        self._decision("diff in.txt other.txt", "allow")

    def test_diff_outside_ask(self):
        self._decision("diff in.txt /etc/hosts", "ask")

    def test_diff_from_file_outside_ask(self):
        self._decision("diff --from-file=/etc/hosts in.txt", "ask")

    def test_file_workspace_allow(self):
        self._decision("file in.txt", "allow")

    def test_file_outside_ask(self):
        self._decision("file /etc/passwd", "ask")

    def test_file_dash_f_outside_ask(self):
        self._decision("file -f /tmp/list.txt", "ask")

    def test_hexdump_workspace_allow(self):
        self._decision("hexdump in.txt", "allow")

    def test_hexdump_outside_ask(self):
        self._decision("hexdump /etc/passwd", "ask")

    def test_hexdump_format_file_outside_ask(self):
        self._decision("hexdump -f /tmp/fmt.txt in.txt", "ask")

    # Cat-shape aliases: pick a couple of representative end-to-end checks
    # rather than re-testing each alias — the alias resolution table is
    # already covered by SpecShapeTests.
    def test_less_outside_ask(self):
        self._decision("less /var/log/syslog", "ask")

    def test_tac_workspace_allow(self):
        self._decision("tac in.txt", "allow")

    def test_zcat_workspace_allow(self):
        self._decision("zcat in.txt", "allow")

    def test_zcat_outside_ask(self):
        self._decision("zcat /tmp/archive.gz", "ask")

    def test_cmp_outside_ask(self):
        self._decision("cmp in.txt /etc/hosts", "ask")

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
