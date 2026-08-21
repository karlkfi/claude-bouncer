#!/usr/bin/env python3
"""Tests for scripts/bash-workspace-guard.py.

Run with: python3 scripts/run-tests.py
     or:  python3 tests/test_workspace_guard.py

Three layers:
  * Unit tests import `files_in_command` and exercise per-command parsing.
  * End-to-end tests invoke the script as a subprocess and inspect the
    PreToolUse decision JSON it emits.
  * Wiring tests assert the plugin config (hooks.json, plugin.json,
    marketplace.json) is valid and points the hook at the real script.
"""
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib import util
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "bash-workspace-guard.py"


def sh(path):
    """Quote a native path for interpolation into a command-line fixture.

    Windows paths carry backslashes, which the hook's POSIX tokenizer reads as
    escapes — exactly as bash does, so an unquoted native path arrives mangled
    (`C:\\ws\\in.txt` -> `C:wsin.txt`) and lands wherever the mangled name
    resolves. Single-quoting is how a real command names such a path. A no-op
    for the plain POSIX paths this returns elsewhere.
    """
    return shlex.quote(path)


def native(path):
    """POSIX-shaped literal -> this platform's separator, for helper unit tests
    that pass paths straight to a helper instead of through ``realpath``."""
    return path.replace("/", os.sep)


def home_rel(path, home):
    """``path`` relative to ``home``, slash-separated, for interpolating after a
    `~/` in a command fixture. Windows' native `\\` would read as a shell escape;
    bash and the hook's ``os.path.join`` both take `/` on either platform."""
    return os.path.relpath(path, home).replace(os.sep, "/")


def resolved_from(base, *parts):
    """Resolve a path the way the hook will, from ``base``.

    A leading-slash path is drive-relative on Windows, so it only equals what
    the hook computes when resolved against the same cwd the command's own
    arguments resolve against -- and since Q52, only after being read through
    Git Bash's mount table first, which is what the shell will do with it.
    Mirrors ``resolve_from`` in the script. A no-op on POSIX both ways."""
    parts = [guard.msys_to_native(p) for p in parts]
    return os.path.realpath(os.path.join(base, *parts))

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
             "sort", "wc", "diff", "file", "hexdump",
             # Q10: yq (kislyuk + mikefarah variants).
             "yq",
             # Q11 PR1: write/mutation commands (cp, mv, tee).
             "cp", "mv", "tee",
             # Q11 PR2: rm.
             "rm",
             # Q37: readers whose second positional is an output file.
             "uniq", "xxd"},
        )

    def test_documented_aliases_present(self):
        self.assertEqual(
            guard.ALIASES,
            {"egrep": "grep", "fgrep": "grep",
             "gawk": "awk", "mawk": "awk",
             # Q9: pure cat-shape readers aliased to `cat`.
             "less": "cat", "more": "cat",
             # Q37: uniq/xxd left the alias list — their second positional
             # is an output file, so they have their own SPEC rows.
             "tac": "cat", "rev": "cat", "nl": "cat",
             "od": "cat",
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

    # --- yq (Q10: kislyuk + mikefarah variants) -----------------------------

    def test_yq_program_positional(self):
        self.assertEqual(
            guard.files_in_command(["yq", ".foo", "input.yaml"]),
            ["input.yaml"],
        )

    def test_yq_from_file_short_is_file_and_suppresses_prog(self):
        # `-f` is kislyuk's jq-pass-through `--from-file`; suppresses prog
        # so the next positional is a file rather than the program.
        self.assertEqual(
            guard.files_in_command(["yq", "-f", "script.jq", "input.json"]),
            ["script.jq", "input.json"],
        )

    def test_yq_from_file_long_is_file_and_suppresses_prog(self):
        self.assertEqual(
            guard.files_in_command(
                ["yq", "--from-file", "expr.yq", "input.yaml"]
            ),
            ["expr.yq", "input.yaml"],
        )

    def test_yq_arg_consumes_two_non_file(self):
        # `--arg NAME VAL` (kislyuk pass-through) must not leak NAME/VAL.
        self.assertEqual(
            guard.files_in_command(
                ["yq", "--arg", "name", "value", ".x", "main.json"]
            ),
            ["main.json"],
        )

    def test_yq_argjson_consumes_two_non_file(self):
        self.assertEqual(
            guard.files_in_command(
                ["yq", "--argjson", "n", "1", ".x", "main.json"]
            ),
            ["main.json"],
        )

    def test_yq_slurpfile_file_at_index_1(self):
        self.assertEqual(
            guard.files_in_command(
                ["yq", "--slurpfile", "d", "aux.json", ".", "main.json"]
            ),
            ["aux.json", "main.json"],
        )

    def test_yq_rawfile_file_at_index_1(self):
        self.assertEqual(
            guard.files_in_command(
                ["yq", "--rawfile", "d", "aux.txt", ".", "main.json"]
            ),
            ["aux.txt", "main.json"],
        )

    def test_yq_split_exp_file_is_file(self):
        # mikefarah-only flag — file containing the split expression.
        self.assertEqual(
            guard.files_in_command(
                ["yq", "--split-exp-file", "tmpl.txt", ".[]", "input.yaml"]
            ),
            ["tmpl.txt", "input.yaml"],
        )

    def test_yq_expression_long_flag_suppresses_prog(self):
        # mikefarah `--expression .foo input.yaml` — `--expression` is not
        # in consume (falls through as zero-arg) but IS in prog_suppressed_by,
        # so `.foo` is treated as a file (cwd-relative, harmless) and the
        # actual file is still tracked.
        self.assertEqual(
            guard.files_in_command(
                ["yq", "--expression", ".foo", "input.yaml"]
            ),
            [".foo", "input.yaml"],
        )

    def test_yq_mikefarah_output_format_does_not_consume(self):
        # `-o json` is not declared as consume — keeps `json` as the prog
        # positional so the following file is correctly identified. Declaring
        # `-o:1` would let `yq -o json /etc/passwd` slip through.
        self.assertEqual(
            guard.files_in_command(["yq", "-o", "json", "input.yaml"]),
            ["input.yaml"],
        )

    def test_yq_mikefarah_indent_does_not_consume(self):
        self.assertEqual(
            guard.files_in_command(["yq", "-I", "2", "input.yaml"]),
            ["input.yaml"],
        )

    def test_yq_kislyuk_yaml_output_boolean(self):
        # `-y` (kislyuk yaml-output) is boolean — falls through as zero-arg.
        self.assertEqual(
            guard.files_in_command(["yq", "-y", ".foo", "input.json"]),
            ["input.json"],
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

    # --- cp / mv / tee (Q11 PR1) --------------------------------------------

    def test_cp_two_positionals(self):
        # `cp SRC DEST` — both positionals are files (sources and dest both
        # participate in the workspace check).
        self.assertEqual(
            guard.files_in_command(["cp", "a.txt", "b.txt"]),
            ["a.txt", "b.txt"],
        )

    def test_cp_multiple_sources_and_dest(self):
        self.assertEqual(
            guard.files_in_command(["cp", "a.txt", "b.txt", "destdir"]),
            ["a.txt", "b.txt", "destdir"],
        )

    def test_cp_recursive_zero_arg_flag(self):
        # `-r` is zero-arg and falls through; positionals are unchanged.
        self.assertEqual(
            guard.files_in_command(["cp", "-r", "src", "dst"]),
            ["src", "dst"],
        )

    def test_cp_combined_short_flags_zero_arg(self):
        # `-rf` parses as one unknown flag with no value — both positionals
        # remain. (Combined short flags don't need to be decomposed because
        # none of them take separated values in cp's flag set.)
        self.assertEqual(
            guard.files_in_command(["cp", "-rf", "src", "dst"]),
            ["src", "dst"],
        )

    def test_cp_target_directory_short_flag_is_file(self):
        # `cp -t DIR SRC...` — DIR is the destination directory; declare it
        # as file_flag so it participates in the workspace check.
        self.assertEqual(
            guard.files_in_command(["cp", "-t", "/tmp", "a.txt", "b.txt"]),
            ["/tmp", "a.txt", "b.txt"],
        )

    def test_cp_target_directory_long_inline_is_file(self):
        self.assertEqual(
            guard.files_in_command(
                ["cp", "--target-directory=/tmp", "a.txt"]
            ),
            ["/tmp", "a.txt"],
        )

    def test_cp_target_directory_long_separated_is_file(self):
        self.assertEqual(
            guard.files_in_command(
                ["cp", "--target-directory", "/tmp", "a.txt"]
            ),
            ["/tmp", "a.txt"],
        )

    def test_cp_end_of_options_double_dash(self):
        # `cp -- -src -dst` — after `--`, dash-prefixed tokens are positional.
        self.assertEqual(
            guard.files_in_command(["cp", "--", "-src", "-dst"]),
            ["-src", "-dst"],
        )

    def test_mv_two_positionals(self):
        self.assertEqual(
            guard.files_in_command(["mv", "a.txt", "b.txt"]),
            ["a.txt", "b.txt"],
        )

    def test_mv_target_directory_short_flag_is_file(self):
        self.assertEqual(
            guard.files_in_command(["mv", "-t", "/tmp", "a.txt"]),
            ["/tmp", "a.txt"],
        )

    def test_mv_force_flag_zero_arg(self):
        self.assertEqual(
            guard.files_in_command(["mv", "-f", "src", "dst"]),
            ["src", "dst"],
        )

    def test_tee_positional_output_file(self):
        # `tee FILE` — FILE is the output target.
        self.assertEqual(
            guard.files_in_command(["tee", "log.txt"]),
            ["log.txt"],
        )

    def test_tee_multiple_output_files(self):
        self.assertEqual(
            guard.files_in_command(["tee", "a.log", "b.log"]),
            ["a.log", "b.log"],
        )

    def test_tee_append_flag_zero_arg(self):
        self.assertEqual(
            guard.files_in_command(["tee", "-a", "log.txt"]),
            ["log.txt"],
        )

    def test_tee_long_append_flag_zero_arg(self):
        self.assertEqual(
            guard.files_in_command(["tee", "--append", "log.txt"]),
            ["log.txt"],
        )

    # --- rm (Q11 PR2) -------------------------------------------------------

    def test_rm_single_positional(self):
        self.assertEqual(guard.files_in_command(["rm", "foo.txt"]), ["foo.txt"])

    def test_rm_multiple_positionals(self):
        self.assertEqual(
            guard.files_in_command(["rm", "a", "b", "c"]),
            ["a", "b", "c"],
        )

    def test_rm_recursive_flag_zero_arg(self):
        # `-r` is zero-arg; positionals follow unchanged.
        self.assertEqual(
            guard.files_in_command(["rm", "-r", "./build"]),
            ["./build"],
        )

    def test_rm_combined_short_flags_zero_arg(self):
        # `-rf` parses as one unknown flag — none of rm's short flags take
        # values, so combined-short doesn't need decomposition.
        self.assertEqual(
            guard.files_in_command(["rm", "-rf", "./build"]),
            ["./build"],
        )

    def test_rm_long_recursive_flag_zero_arg(self):
        self.assertEqual(
            guard.files_in_command(["rm", "--recursive", "./build"]),
            ["./build"],
        )

    def test_rm_force_and_interactive_combined(self):
        self.assertEqual(
            guard.files_in_command(["rm", "-fI", "./build"]),
            ["./build"],
        )

    def test_rm_end_of_options_double_dash(self):
        # `rm -- -filename` removes a file literally named `-filename`.
        self.assertEqual(
            guard.files_in_command(["rm", "--", "-filename"]),
            ["-filename"],
        )

    def test_rm_preserve_root_inline_value_discarded(self):
        # `--preserve-root=all` — unknown long flag with inline value; value
        # is dropped (not promoted to positional), so only the file remains.
        self.assertEqual(
            guard.files_in_command(["rm", "--preserve-root=all", "-r", "./build"]),
            ["./build"],
        )

    def test_rm_no_preserve_root_zero_arg(self):
        self.assertEqual(
            guard.files_in_command(["rm", "--no-preserve-root", "-rf", "./x"]),
            ["./x"],
        )

    # --- Q9 aliases (cat-shape readers) -------------------------------------

    def test_q9_aliases_resolve_to_cat(self):
        # Each alias should parse identically to bare `cat foo.txt`.
        for cmd in ("less", "more", "tac", "rev", "nl",
                    "od", "strings", "cmp",
                    "zcat", "gzcat", "bzcat", "xzcat"):
            self.assertEqual(
                guard.files_in_command([cmd, "foo.txt"]),
                ["foo.txt"],
                f"alias {cmd!r} did not resolve to cat-shape",
            )

    # --- Q37: uniq / xxd (own SPEC rows; second positional is an output) ----

    def test_uniq_positionals(self):
        self.assertEqual(
            guard.files_in_command(["uniq", "in.txt", "out.txt"]),
            ["in.txt", "out.txt"],
        )

    def test_uniq_consume_flags_do_not_shift_positionals(self):
        # `-f 1` is a field count, not a file — with cat's spec it leaked in
        # as a positional and shifted the output-operand index.
        self.assertEqual(
            guard.files_in_command(["uniq", "-f", "1", "-s", "2", "in.txt"]),
            ["in.txt"],
        )
        self.assertEqual(
            guard.files_in_command(
                ["uniq", "--skip-fields=1", "in.txt", "out.txt"]),
            ["in.txt", "out.txt"],
        )

    def test_xxd_consume_flags_do_not_shift_positionals(self):
        self.assertEqual(
            guard.files_in_command(["xxd", "-l", "16", "-c", "8", "in.bin"]),
            ["in.bin"],
        )
        self.assertEqual(
            guard.files_in_command(["xxd", "-r", "-s", "0x100", "in.hex", "out.bin"]),
            ["in.hex", "out.bin"],
        )

    def test_output_positionals_table_rows_are_index_safe(self):
        # The per-operand write classification indexes files_in_command()'s
        # return by positional order — only sound while these rows have no
        # file_flags and prog 0.
        for cmd in guard.OUTPUT_POSITIONALS:
            spec = guard.SPEC[cmd]
            self.assertEqual(spec["file_flags"], {}, cmd)
            self.assertEqual(spec["prog"], 0, cmd)
            self.assertNotIn(cmd, guard.ALIASES, cmd)

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


class StripShKeywordsTests(unittest.TestCase):
    """Leading shell reserved words are dropped before SPEC lookup (Q28)."""

    def test_single_keyword_stripped(self):
        self.assertEqual(
            guard.strip_sh_keywords(["until", "grep", "PAT", "x"]),
            ["grep", "PAT", "x"],
        )

    def test_if_keyword_stripped(self):
        self.assertEqual(
            guard.strip_sh_keywords(["if", "cat", "x"]),
            ["cat", "x"],
        )

    def test_loop_body_do_keyword_stripped(self):
        self.assertEqual(
            guard.strip_sh_keywords(["do", "tail", "x"]),
            ["tail", "x"],
        )

    def test_multiple_keywords_stripped(self):
        # `if ! grep …` — negation after the conditional keyword.
        self.assertEqual(
            guard.strip_sh_keywords(["if", "!", "grep", "PAT", "x"]),
            ["grep", "PAT", "x"],
        )

    def test_time_keyword_stripped(self):
        self.assertEqual(
            guard.strip_sh_keywords(["time", "cat", "x"]),
            ["cat", "x"],
        )

    def test_stops_at_first_non_keyword(self):
        # A guarded command's own args are never keywords, so stripping stops.
        self.assertEqual(
            guard.strip_sh_keywords(["until", "grep", "in", "x"]),
            ["grep", "in", "x"],
        )

    def test_keyword_only_returns_empty(self):
        # `done`/`fi`/`}` alone carry no command.
        self.assertEqual(guard.strip_sh_keywords(["done"]), [])

    def test_no_keyword_unchanged(self):
        self.assertEqual(
            guard.strip_sh_keywords(["cat", "x"]),
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


class SessionTmpPathTests(unittest.TestCase):
    """Per-session allow for Claude Code's own task-output scratch (Q21)."""

    def setUp(self):
        self.root = guard.claude_tmp_root()
        self.sess = "11111111-2222-3333-4444-555555555555"
        # A realistic per-session task-output path under the temp root.
        self.path = os.path.join(
            self.root, "-Users-me-proj", self.sess, "tasks", "abc.output")

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX-only layout")
    def test_claude_tmp_root_is_realpath_of_uid_dir(self):
        self.assertEqual(
            self.root, os.path.realpath("/tmp/claude-%d" % os.getuid()))

    @unittest.skipIf(hasattr(os, "getuid"), "Windows-only layout")
    def test_claude_tmp_root_is_realpath_of_temp_claude_dir(self):
        # No per-UID suffix on Windows: the per-user temp dir already scopes it.
        self.assertEqual(
            self.root,
            os.path.realpath(os.path.join(tempfile.gettempdir(), "claude")))

    def test_current_session_path_allowed(self):
        self.assertTrue(
            guard.is_session_tmp_path(self.path, self.sess, self.root))

    def test_temp_root_itself_allowed_for_session(self):
        # The root with the session segment somewhere below it is the only
        # match; the bare root has no session segment, so it is not allowed.
        self.assertFalse(
            guard.is_session_tmp_path(self.root, self.sess, self.root))

    def test_other_session_path_not_allowed(self):
        other = "99999999-8888-7777-6666-555555555555"
        self.assertFalse(
            guard.is_session_tmp_path(self.path, other, self.root))

    def test_empty_session_id_disables_allow(self):
        self.assertFalse(guard.is_session_tmp_path(self.path, "", self.root))

    def test_path_outside_temp_root_not_allowed(self):
        # Even though the session id appears as a segment, the path is not under
        # the temp root, so it is not allowed.
        outside = "/var/data/%s/x.output" % self.sess
        self.assertFalse(
            guard.is_session_tmp_path(outside, self.sess, self.root))

    def test_sibling_root_prefix_not_matched(self):
        # `/tmp/claude-501-evil/...` must not match `/tmp/claude-501` via a
        # naive prefix check — the os.sep boundary guards against it.
        sibling = self.root + "-evil/" + self.sess + "/x"
        self.assertFalse(
            guard.is_session_tmp_path(sibling, self.sess, self.root))


class SessionProjectDirTests(unittest.TestCase):
    """claude_session_project_dir() scan for same-project sibling scratch (#61).

    Uses a throwaway temp dir as the scan root (the function takes it as a
    parameter), so nothing touches the real Claude temp root."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.sess = "11111111-2222-3333-4444-555555555555"
        self.slug = "-Users-me-proj"
        self.proj = os.path.join(self.root, self.slug)
        os.makedirs(os.path.join(self.proj, self.sess))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_finds_project_dir_holding_session(self):
        self.assertEqual(
            guard.claude_session_project_dir(self.sess, self.root),
            os.path.realpath(self.proj))

    def test_empty_session_id_returns_none(self):
        self.assertIsNone(guard.claude_session_project_dir("", self.root))

    def test_unknown_session_returns_none(self):
        self.assertIsNone(
            guard.claude_session_project_dir("no-such-session", self.root))

    def test_missing_root_returns_none(self):
        self.assertIsNone(
            guard.claude_session_project_dir(self.sess, self.root + "-absent"))

    def test_ignores_sibling_project_without_this_session(self):
        # A second project dir that does NOT hold this session must not match;
        # the one that holds it wins.
        other = os.path.join(self.root, "-Users-me-other")
        os.makedirs(os.path.join(other, "99999999-0000-0000-0000-000000000000"))
        self.assertEqual(
            guard.claude_session_project_dir(self.sess, self.root),
            os.path.realpath(self.proj))


class WriteModeFlagsUnitTests(unittest.TestCase):
    """Unit tests for has_write_mode_flag() (Q36): flags that flip a
    read-classified command into write mode, disabling the read-prefix
    exemption for the whole invocation."""

    def test_sed_inplace_variants(self):
        for argv in (["sed", "-i", "s/a/b/", "f"],
                     ["sed", "-i.bak", "s/a/b/", "f"],
                     ["sed", "-ni", "s/a/b/p", "f"],
                     ["sed", "--in-place", "s/a/b/", "f"],
                     ["sed", "--in-place=.bak", "s/a/b/", "f"]):
            self.assertTrue(guard.has_write_mode_flag("sed", argv), argv)

    def test_sed_read_only_forms(self):
        for argv in (["sed", "-n", "1p", "f"],
                     ["sed", "-e", "s/a/b/", "f"],
                     # After end-of-options, `-i` is a filename.
                     ["sed", "s/a/b/", "--", "-i"]):
            self.assertFalse(guard.has_write_mode_flag("sed", argv), argv)

    def test_awk_include(self):
        self.assertTrue(guard.has_write_mode_flag(
            "awk", ["awk", "-i", "inplace", "{print}", "f"]))
        self.assertTrue(guard.has_write_mode_flag(
            "awk", ["awk", "--include", "inplace", "{print}", "f"]))
        self.assertFalse(guard.has_write_mode_flag(
            "awk", ["awk", "{print}", "f"]))
        self.assertFalse(guard.has_write_mode_flag(
            "awk", ["awk", "-F:", "{print}", "f"]))

    def test_yq_and_sort(self):
        self.assertTrue(guard.has_write_mode_flag(
            "yq", ["yq", "-i", ".a = 1", "f"]))
        self.assertTrue(guard.has_write_mode_flag(
            "sort", ["sort", "-o", "out", "in"]))
        self.assertTrue(guard.has_write_mode_flag(
            "sort", ["sort", "--output=out", "in"]))
        self.assertFalse(guard.has_write_mode_flag(
            "sort", ["sort", "-r", "in"]))

    def test_unlisted_command_never_matches(self):
        self.assertFalse(guard.has_write_mode_flag("cat", ["cat", "-i"]))
        self.assertFalse(guard.has_write_mode_flag("grep", ["grep", "-i", "x", "f"]))


class AllowedReadPrefixesUnitTests(unittest.TestCase):
    """Unit tests for claude_projects_dir() and allowed_read_prefixes()."""

    def test_claude_projects_dir_under_home(self):
        cpd = guard.claude_projects_dir()
        if cpd is None:
            self.skipTest("home directory not resolvable")
        home = os.path.realpath(os.path.expanduser("~"))
        self.assertTrue(cpd.startswith(home.rstrip(os.sep) + os.sep) or cpd == home,
                        f"expected {cpd!r} under home {home!r}")
        self.assertTrue(cpd.endswith("projects") or "projects" in cpd)

    def test_allowed_read_prefixes_includes_projects_dir(self):
        cpd = guard.claude_projects_dir()
        if cpd is None:
            self.skipTest("home directory not resolvable")
        prefixes = guard.allowed_read_prefixes(os.getcwd())
        self.assertIn(cpd, prefixes)

    def test_allowed_read_prefixes_extras_via_env(self):
        fake = "/fake/read-allow-test"
        old = os.environ.get("WORKSPACE_GUARD_READ_ALLOW_PREFIXES")
        try:
            os.environ["WORKSPACE_GUARD_READ_ALLOW_PREFIXES"] = fake
            prefixes = guard.allowed_read_prefixes(os.getcwd())
        finally:
            if old is None:
                os.environ.pop("WORKSPACE_GUARD_READ_ALLOW_PREFIXES", None)
            else:
                os.environ["WORKSPACE_GUARD_READ_ALLOW_PREFIXES"] = old
        # realpath of /fake/read-allow-test on most systems = itself
        self.assertTrue(any(p.endswith("read-allow-test") for p in prefixes))

    def test_installed_skill_targets_resolves_only_symlinked_skills(self):
        # Issue 167: a skill developed in a repo and symlinked in resolves out
        # of the exempt dir. A real entry needs nothing — the dir itself covers
        # it — and a link to something that isn't a skill is a laundering slot.
        with tempfile.TemporaryDirectory() as td:
            home = os.path.realpath(td)
            skills = os.path.join(home, ".claude", "skills")
            repo = os.path.join(home, "workspace", "skills", "mine")
            os.makedirs(skills)
            os.makedirs(repo)
            open(os.path.join(repo, "SKILL.md"), "w").close()
            os.mkdir(os.path.join(skills, "bundled"))
            os.symlink(repo, os.path.join(skills, "mine"))
            os.symlink(home, os.path.join(skills, "everything"))
            self.assertEqual(guard.installed_skill_targets(home), [repo])

    def test_installed_skill_targets_without_a_skills_dir(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(guard.installed_skill_targets(td), [])

    def test_claude_projects_dir_without_home_env(self):
        # Q40: the hook runs from cmd.exe on Windows, where HOME is unset. The
        # prefix must survive that — expanduser reads USERPROFILE there, and the
        # pwd database on POSIX.
        old_home = os.environ.get("HOME")
        try:
            os.environ.pop("HOME", None)
            cpd = guard.claude_projects_dir()
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        self.assertIsNotNone(cpd)

    def test_claude_projects_dir_unresolvable_home(self):
        # expanduser hands back a bare `~` when no home resolves at all.
        with mock.patch.object(os.path, "expanduser", return_value="~"):
            self.assertIsNone(guard.claude_projects_dir())


class TildeExpansionUnitTests(unittest.TestCase):
    """Unit tests for resolved_home() and expand_tilde() (Q19, Q43)."""

    @contextlib.contextmanager
    def _without_home_env(self):
        old = os.environ.pop("HOME", None)
        try:
            yield
        finally:
            if old is not None:
                os.environ["HOME"] = old

    def test_expand_tilde_without_home_env(self):
        # Q43: on Windows the hook runs under cmd.exe with HOME unset. Reading
        # the variable left `~/x` unexpanded, so a `~` path into the workspace
        # asked and a native tool's `~` path deferred entirely.
        with self._without_home_env():
            self.assertEqual(guard.resolved_home(), os.path.expanduser("~"))
            expanded = guard.expand_tilde("~/q43-fake-target")
            self.assertTrue(os.path.isabs(expanded), expanded)
            self.assertFalse(expanded.startswith("~"), expanded)
            self.assertTrue(expanded.endswith("q43-fake-target"), expanded)
            self.assertTrue(os.path.isabs(guard.expand_tilde("~")))

    def test_native_path_with_tilde_resolves_without_home_env(self):
        # Same regression on the native-tool side: an unexpanded `~` is treated
        # as unresolvable and defers to builtin permissions, so the path went
        # unchecked. It must resolve instead.
        with self._without_home_env():
            p = guard.resolve_native_path("~/q43-fake-target", os.getcwd())
        self.assertIsNotNone(p)
        self.assertTrue(os.path.isabs(p), p)

    def test_expand_tilde_leaves_out_of_scope_prefixes(self):
        # `~user` needs a pwd lookup and `~+`/`~-` need dir-stack state; both
        # stay unexpanded so the caller keeps the runtime-expanded ask.
        for tok in ("~someuser/x", "~+/x", "~-", "~+", "foo~bak"):
            self.assertEqual(guard.expand_tilde(tok), tok)

    def test_expand_tilde_unresolvable_home(self):
        # expanduser hands back a bare `~` when no home resolves at all.
        with mock.patch.object(os.path, "expanduser", return_value="~"):
            self.assertIsNone(guard.resolved_home())
            self.assertEqual(guard.expand_tilde("~/x"), "~/x")
            self.assertEqual(guard.expand_tilde("~"), "~")


class MsysPathFormTests(unittest.TestCase):
    """Q52: a leading-slash path is read through Git Bash's mount table.

    The expectations are the table measured on a windows-latest runner (Git
    2.55, MSYSTEM=MINGW64) via `cygpath -w`, not a reading of the MSYS source.
    `msys_to_native` is a pure string rewrite, so these run on every platform
    with the Windows discriminator patched on; the integration is covered by
    MsysPathFormWindowsTests, which only means anything on Windows.
    """

    FAKE_ROOT = r"C:\Program Files\Git"

    @contextlib.contextmanager
    def _on_windows(self, root=FAKE_ROOT, tmp=r"C:\Users\me\AppData\Local\Temp"):
        with mock.patch.object(guard, "DRIVE_PATHS", True), \
                mock.patch.object(guard, "msys_root", return_value=root), \
                mock.patch.object(guard, "msys_tmp", return_value=tmp):
            yield

    def _expect(self, cases, **kw):
        with self._on_windows(**kw):
            for raw, want in cases.items():
                self.assertEqual(guard.msys_to_native(raw), want, raw)

    def test_drive_forms(self):
        # `C: on /c` — the cygdrive prefix is `/`, and it applies to any single
        # letter, including drives that don't exist (`/x/y` -> `X:\y`).
        self._expect({
            "/c": "C:/",
            "/c/": "C:/",
            "/c/Users/foo": "C:/Users/foo",
            "/C/Users/foo": "C:/Users/foo",
            "/d/a/proj": "D:/a/proj",
            "/x/y": "X:/y",
        })

    def test_tmp_is_the_usertemp_mount(self):
        # `<%TMP%> on /tmp type usertemp` — NOT <root>/tmp. This is the mount
        # that made the host-temp `deny` fire on the right path.
        self._expect({
            "/tmp": r"C:\Users\me\AppData\Local\Temp",
            "/tmp/x": r"C:\Users\me\AppData\Local\Temp/x",
        })

    def test_bin_is_its_own_mount(self):
        # `C:/Program Files/Git/usr/bin on /bin` — one level deeper than the
        # `/` rule would put it.
        self._expect({
            "/bin": r"C:\Program Files\Git/usr/bin",
            "/bin/bash": r"C:\Program Files\Git/usr/bin/bash",
        })

    def test_everything_else_hangs_off_the_root(self):
        self._expect({
            "/etc/passwd": r"C:\Program Files\Git/etc/passwd",
            "/usr/bin/env": r"C:\Program Files\Git/usr/bin/env",
            "/var/tmp/x": r"C:\Program Files\Git/var/tmp/x",
            "/home/me": r"C:\Program Files\Git/home/me",
            # No WSL or cygdrive mount: these are ordinary directories.
            "/mnt/c/foo": r"C:\Program Files\Git/mnt/c/foo",
        })

    def test_untouched_forms(self):
        # Only a leading slash is ambiguous; a native path, a relative path and
        # a `~` that expand_tilde already resolved all pass straight through.
        self._expect({
            r"C:\ws\in.txt": r"C:\ws\in.txt",
            "notes.txt": "notes.txt",
            "../sib/x": "../sib/x",
            "": "",
        })

    def test_no_git_bash_leaves_non_drive_paths_alone(self):
        # Without a locatable root the guard keeps its pre-Q52 drive-relative
        # reading rather than inventing one: an over-prompt, never an allow.
        # Drive and /tmp forms need no root and still resolve.
        self._expect({
            "/etc/passwd": "/etc/passwd",
            "/bin/bash": "/bin/bash",
            "/c/Users/foo": "C:/Users/foo",
            "/tmp/x": r"C:\Users\me\AppData\Local\Temp/x",
        }, root=None)

    def test_posix_is_untouched(self):
        # The discriminator is drive resolution, so on POSIX — where these are
        # real paths — nothing is rewritten.
        with mock.patch.object(guard, "DRIVE_PATHS", False):
            for raw in ("/c/Users/foo", "/tmp/x", "/etc/passwd", "/bin/bash"):
                self.assertEqual(guard.msys_to_native(raw), raw)


class MsysRootDiscoveryTests(unittest.TestCase):
    """Q52: locating Git Bash's `/` from the hook's own (cmd.exe) environment.

    `where.exe bash` on a stock windows-latest runner returns Git's bash first
    and `C:\\Windows\\System32\\bash.exe` — the WSL launcher — second. Without
    Git for Windows only the latter is on PATH, so the marker check is what
    keeps `/etc/passwd` from being reported under `C:\\Windows`.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, guard, "_msys_root_cached", ())
        self.root = os.path.join(self._tmp.name, "Git")
        os.makedirs(os.path.join(self.root, "usr", "bin"))
        os.makedirs(os.path.join(self.root, "bin"))
        os.makedirs(os.path.join(self.root, "cmd"))
        for p in (("usr", "bin", "bash.exe"), ("bin", "bash.exe"),
                  ("cmd", "git.exe")):
            open(os.path.join(self.root, *p), "w").close()

    def _root_from(self, bash=None, git=None, env=None):
        guard._msys_root_cached = ()
        with mock.patch.object(guard.shutil, "which",
                               side_effect=lambda n: {"bash": bash, "git": git}.get(n)), \
                mock.patch.dict(os.environ,
                                {"CLAUDE_CODE_GIT_BASH_PATH": env} if env else {},
                                clear=False):
            if not env:
                os.environ.pop("CLAUDE_CODE_GIT_BASH_PATH", None)
            return guard.msys_root()

    def test_found_from_each_candidate_depth(self):
        # bin/bash.exe, usr/bin/bash.exe and cmd/git.exe sit at different
        # depths; the ancestor walk lands on the same root from all three.
        self.assertEqual(
            self._root_from(bash=os.path.join(self.root, "bin", "bash.exe")),
            self.root)
        self.assertEqual(
            self._root_from(bash=os.path.join(self.root, "usr", "bin", "bash.exe")),
            self.root)
        self.assertEqual(
            self._root_from(git=os.path.join(self.root, "cmd", "git.exe")),
            self.root)

    def test_explicit_git_bash_path_wins(self):
        self.assertEqual(
            self._root_from(env=os.path.join(self.root, "bin", "bash.exe")),
            self.root)

    def test_marker_rejects_a_non_msys_bash(self):
        # The WSL launcher's shape: no usr/bin/bash.exe anywhere above it.
        wsl = os.path.join(self._tmp.name, "Windows", "System32")
        os.makedirs(wsl)
        open(os.path.join(wsl, "bash.exe"), "w").close()
        self.assertIsNone(self._root_from(bash=os.path.join(wsl, "bash.exe")))

    def test_no_bash_at_all(self):
        self.assertIsNone(self._root_from())

    def test_result_is_cached(self):
        first = self._root_from(bash=os.path.join(self.root, "bin", "bash.exe"))
        self.assertEqual(first, self.root)
        # A second call must not re-scan PATH: which() raising proves it didn't.
        with mock.patch.object(guard.shutil, "which",
                               side_effect=AssertionError("re-scanned PATH")):
            self.assertEqual(guard.msys_root(), self.root)


def run_hook(cmd, cwd, project_dir=None, permission_mode=None, session_id=None,
             env_extra=None):
    """Invoke the hook as a subprocess. Returns parsed JSON or None on defer.

    `env_extra` overrides/adds environment variables for the subprocess — used
    to exercise `$TMPDIR` resolution and the `WORKSPACE_GUARD_TMP_*` config
    knobs. A value of None deletes the key from the inherited environment so a
    test can clear an inherited `$TMPDIR`."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = project_dir or cwd
    for k, v in (env_extra or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    data = {"tool_input": {"command": cmd}, "cwd": cwd}
    if permission_mode is not None:
        data["permission_mode"] = permission_mode
    if session_id is not None:
        data["session_id"] = session_id
    payload = json.dumps(data)
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

    def _decision(self, cmd, expected, *, cwd=None, project_dir=None,
                  permission_mode=None, session_id=None):
        out = run_hook(cmd, cwd or self.workspace, project_dir=project_dir,
                       permission_mode=permission_mode, session_id=session_id)
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
        # -f /etc/evil.sed -> pattern file itself is outside.
        self._decision("sed -f /etc/evil.sed in.txt", "ask")

    def test_grep_prog_suppressed_e_outside_ask(self):
        self._decision("grep -e PAT /etc/hosts", "ask")

    def test_grep_inline_eq_pattern_file_outside_ask(self):
        self._decision("grep --file=/etc/patterns in.txt", "ask")

    def test_jq_slurpfile_outside_ask(self):
        self._decision("jq --slurpfile d /etc/hosts . in.txt", "ask")

    # --- permission_mode: ask vs deny for outside paths (Q17) ----------------
    # Verified end-to-end (CLI 2.1.159): a hook `ask` blocks in both headless
    # and `bypassPermissions`, so the boundary holds regardless. In
    # `bypassPermissions` (full-auto, no human) we emit `deny` instead so the
    # model gets recoverable feedback rather than stalling on an unanswerable
    # approval prompt. Every other mode — including absent (interactive) and
    # plain headless `default`, which the hook cannot tell apart — keeps `ask`.

    def test_outside_bypass_permissions_deny(self):
        out = self._decision(
            "cat /etc/q17-fake-target", "deny",
            permission_mode="bypassPermissions",
        )
        self.assertIn(
            "/etc/q17-fake-target",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_outside_default_mode_ask(self):
        self._decision("cat /etc/q17-fake-target", "ask", permission_mode="default")

    def test_outside_no_permission_mode_ask(self):
        # Field absent (interactive sessions don't always send it) -> ask.
        self._decision("cat /etc/q17-fake-target", "ask")

    def test_outside_accept_edits_ask(self):
        # Only bypassPermissions flips to deny; acceptEdits still has a human.
        self._decision("cat /etc/q17-fake-target", "ask", permission_mode="acceptEdits")

    def test_outside_plan_mode_ask(self):
        self._decision("cat /etc/q17-fake-target", "ask", permission_mode="plan")

    def test_workspace_bypass_permissions_still_allow(self):
        # deny only applies to outside paths; in-workspace reads stay allow.
        self._decision("cat in.txt", "allow", permission_mode="bypassPermissions")

    def test_realpath_traversal_outside_ask(self):
        nested = os.path.join(self.workspace, "sub")
        os.mkdir(nested)
        # cwd inside workspace + `..` chain escapes via realpath.
        self._decision("cat ../../../etc/hosts", "ask", cwd=nested)

    # --- redirect capture ---------------------------------------------------

    def test_redirect_target_outside_ask(self):
        out = self._decision("cat in.txt > /etc/out.txt", "ask")
        self.assertIn(
            "/etc/out.txt",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_redirect_target_inside_allow(self):
        self._decision("cat in.txt > out.txt", "allow")

    def test_redirect_append_outside_ask(self):
        self._decision("cat in.txt >> /etc/out.txt", "ask")

    # --- fd-prefixed redirects & fd duplication (Q20) -----------------------
    # `2>file`, `2>&1`, `>&-` tokenize with the fd digit as a bare token glued
    # to nothing (shlex drops adjacency). The digit, the `>&` dup operator, and
    # the dup target (a bare fd number) must not leak as positional file args.

    def test_stderr_redirect_to_dev_null_allow(self):
        self._decision("grep PAT in.txt 2>/dev/null", "allow")

    def test_fd_dup_stderr_to_stdout_allow(self):
        self._decision("grep PAT in.txt 2>&1", "allow")

    def test_combined_redirect_and_fd_dup_allow(self):
        self._decision("grep PAT in.txt >out.txt 2>&1", "allow")

    def test_fd_close_target_allow(self):
        self._decision("grep PAT in.txt 2>&-", "allow")

    def test_fd_digit_not_leaked_after_cd_outside(self):
        # The motivating bug: after a cd-shift the leaked `2`/`>&`/`1` tokens
        # resolve against the new cwd and spuriously flag. Reading an absolute
        # in-workspace file should stay allow despite the cd.
        abs_in = os.path.join(self.workspace, "in.txt")
        self._decision(f"cd /tmp/q20-fake-dir && grep PAT {sh(abs_in)} 2>&1", "allow")

    def test_fd_prefixed_redirect_to_outside_still_ask(self):
        # Dropping the fd digit must not drop the redirect target itself.
        out = self._decision("grep PAT in.txt 2>/etc/q20-fake-out", "ask")
        self.assertIn(
            "/etc/q20-fake-out",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_ampersand_redirect_to_outside_file_still_ask(self):
        # `>&file` (target isn't a bare fd) is a redirect to a file, not a dup.
        out = self._decision("grep PAT in.txt >&/etc/q20-fake-out", "ask")
        self.assertIn(
            "/etc/q20-fake-out",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_ampersand_redirect_to_inside_file_allow(self):
        self._decision("grep PAT in.txt >&out.txt", "allow")

    # --- redirect targets track cd-shifts (Q16) -----------------------------
    # A redirect target attaches to the command group it appears in, so it
    # resolves against that group's cwd — a `cd` earlier in the chain shifts
    # where bash actually opens the file.

    def test_redirect_relative_target_tracks_cd_outside_ask(self):
        # The motivating bug: after `cd /etc`, the relative redirect `evil`
        # resolves to /etc/evil — outside the workspace — even though it looks
        # in-workspace at the chain's original cwd. `/dev/null` is an allowed
        # device, so only the redirect target can flag. (Uses /etc, not /tmp, so
        # the redirect-routing intent isn't entangled with the host-temp deny.)
        out = self._decision("cd /etc && cat /dev/null > evil", "ask")
        self.assertIn(
            "evil",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_redirect_relative_target_no_cd_stays_inside_allow(self):
        # Regression: without a cd, a relative redirect still resolves against
        # the original (workspace) cwd and stays allow.
        self._decision("cat /dev/null > evil", "allow")

    def test_redirect_relative_target_tracks_cd_into_workspace_allow(self):
        # cd into a workspace subdir: the relative redirect resolves inside the
        # workspace and stays allow. Reads an absolute in-workspace source so
        # only the redirect routing is under test.
        os.mkdir(os.path.join(self.workspace, "sub"))
        abs_in = os.path.join(self.workspace, "in.txt")
        self._decision(f"cd sub && cat {sh(abs_in)} > out.txt", "allow")

    def test_fd_prefixed_redirect_target_tracks_cd_outside_ask(self):
        # fd-prefix popping must route the surviving target into the post-cd
        # group: `2>err.log` after `cd /etc` writes /etc/err.log (outside).
        abs_in = os.path.join(self.workspace, "in.txt")
        out = self._decision(f"cd /etc && grep PAT {sh(abs_in)} 2>err.log", "ask")
        self.assertIn(
            "err.log",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_ampersand_redirect_file_target_tracks_cd_outside_ask(self):
        # `>&file` (DUP operator, target is a filename not an fd) routes into
        # the post-cd group too: /etc/dup.out is outside.
        abs_in = os.path.join(self.workspace, "in.txt")
        out = self._decision(f"cd /etc && grep PAT {sh(abs_in)} >&dup.out", "ask")
        self.assertIn(
            "dup.out",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_here_string_after_cd_not_routed_as_redirect_allow(self):
        # `<<<` here-string content is stdin data, not a redirect target — it
        # must stay skipped even after a cd, so a path-like here-string body
        # doesn't spuriously flag. Absolute in-workspace source stays allow.
        abs_in = os.path.join(self.workspace, "in.txt")
        self._decision(f'cd /tmp && cat {sh(abs_in)} <<<"/etc/foo"', "allow")

    def test_top_level_redirect_still_outside_ask(self):
        # Regression: a top-level (no-cd) absolute redirect target is still
        # checked — the per-group routing didn't drop the common case.
        self._decision("cat in.txt > /etc/q16-fake-target", "ask")

    # --- shell expansions (Q5) ----------------------------------------------

    def test_tilde_path_outside_ask(self):
        # Q19: `~/...` is expanded to $HOME, which is outside this tempdir
        # workspace, so it resolves outside and asks. The reason still names
        # the original `~/.ssh/id_rsa` token.
        out = self._decision("cat ~/.ssh/id_rsa", "ask")
        self.assertIn(
            "~/.ssh/id_rsa",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_tilde_user_path_outside_ask(self):
        # `~user` can't be expanded here (needs a pwd lookup) — still ask.
        self._decision("cat ~someuser/.ssh/id_rsa", "ask")

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

    def test_tilde_path_into_workspace_allow(self):
        # Q19: when the project lives under the home directory, `~/<rel>/in.txt`
        # expands to an in-workspace path and should allow (previously a
        # spurious ask).
        home = guard.resolved_home()                      # Q43: not $HOME
        self.assertIsNotNone(home, "no home directory resolves")
        with tempfile.TemporaryDirectory(dir=home) as ws:
            ws = os.path.realpath(ws)
            with open(os.path.join(ws, "in.txt"), "w") as f:
                f.write("hi\n")
            rel = home_rel(ws, home)                       # e.g. "tmpXXXX"
            self._decision(f"cat ~/{rel}/in.txt", "allow", cwd=ws)

    def test_cd_tilde_into_workspace_relative_allow(self):
        # `cd ~/<rel> && cat in.txt` — cd tracks through the expanded home
        # path, so the subsequent relative read resolves in-workspace.
        home = guard.resolved_home()                      # Q43: not $HOME
        self.assertIsNotNone(home, "no home directory resolves")
        with tempfile.TemporaryDirectory(dir=home) as ws:
            ws = os.path.realpath(ws)
            with open(os.path.join(ws, "in.txt"), "w") as f:
                f.write("hi\n")
            rel = home_rel(ws, home)
            self._decision(f"cd ~/{rel} && cat in.txt", "allow", cwd=ws)

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
        self._decision(f"cd {sh(nested)} && cat x.txt", "allow")

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

    def test_cd_tilde_to_home_relative_outside_ask(self):
        # Q19: `cd ~` now tracks to $HOME (no longer untracked), so `in.txt`
        # resolves to $HOME/in.txt — outside this tempdir workspace — and asks.
        self._decision("cd ~ && cat in.txt", "ask")

    def test_cd_tilde_user_taints_subsequent_relative_outside_ask(self):
        # `~user` stays unresolvable, so the cd is untracked and the relative
        # read is treated as outside.
        self._decision("cd ~user && cat in.txt", "ask")

    def test_cd_does_not_taint_absolute_paths(self):
        # `cd /etc && cat /etc/passwd` already had `/etc/passwd` flagged via
        # the absolute path. Q7 doesn't change that — verify it still asks.
        self._decision("cd /etc && cat /etc/passwd", "ask")

    def test_cd_outside_literal_keeps_tracking_names_absolute_path(self):
        # Issue 85: a literal cd OUTSIDE the workspace keeps cwd tracked, so
        # the later relative read prompts as a resolved outside-workspace
        # path — naming where it actually lands — not as an untracked-cd
        # unknown.
        out = self._decision("cd /q85-fake-outside && cat notes.txt", "ask")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Outside-workspace path(s)", reason)
        landed = resolved_from(self.workspace, "/q85-fake-outside", "notes.txt")
        self.assertIn("notes.txt -> %s" % landed, reason)
        self.assertNotIn("untracked", reason)

    def test_cd_outside_literal_redirect_names_absolute_path(self):
        # Same tracking for a redirect target after the outside cd (Q16 +
        # issue 85): the reason shows the resolved absolute landing path.
        out = self._decision(
            "cd /q85-fake-outside && cat in.txt > out.log", "ask")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        landed = resolved_from(self.workspace, "/q85-fake-outside", "out.log")
        self.assertIn("out.log -> %s" % landed, reason)
        self.assertNotIn("untracked", reason)

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
        # The target comes back read the way the shell will read it, so on
        # Windows `/etc` is already the mount-table path (Q52); elsewhere
        # msys_to_native is the identity and these are the literals.
        for tokens, target in ((["cd", "/etc"], "/etc"),
                               (["pushd", "/tmp"], "/tmp"),
                               (["cd", "-L", "/etc"], "/etc")):
            self.assertEqual(guard.classify_cd(tokens),
                             ("arg", guard.msys_to_native(target)))

    def test_classify_cd_helper_unknown(self):
        self.assertEqual(guard.classify_cd(["cd"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["cd", "-"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["cd", "$HOME"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["cd", "~/$VAR"]), ("unknown", None))
        # `~user`/`~+`/`~-` aren't plain `~`/`~/` — still unresolvable (Q19).
        self.assertEqual(guard.classify_cd(["cd", "~user"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["cd", "~+"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["cd", "~-"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["pushd", "+1"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["popd"]), ("unknown", None))
        self.assertEqual(guard.classify_cd(["popd", "+0"]), ("unknown", None))

    def test_classify_cd_helper_expands_tilde(self):
        # Q19: bare `~` and `~/…` expand to the home directory deterministically,
        # so cd tracking follows them instead of dropping to ('unknown', None).
        home = guard.resolved_home()                      # Q43: not $HOME
        self.assertIsNotNone(home, "no home directory resolves")
        self.assertEqual(guard.classify_cd(["cd", "~"]), ("arg", home))
        self.assertEqual(
            guard.classify_cd(["cd", "~/foo"]),
            ("arg", os.path.join(home, "foo")),
        )
        self.assertEqual(
            guard.classify_cd(["pushd", "~/a/b"]),
            ("arg", os.path.join(home, "a/b")),
        )

    def test_classify_cd_helper_not_cd(self):
        self.assertEqual(guard.classify_cd(["cat", "foo"]), (None, None))
        self.assertEqual(guard.classify_cd([]), (None, None))

    # --- whitelisted pure substitutions in cd targets (issue 59) -------------
    # `cd "$(git rev-parse --show-toplevel)"` / `cd "$(pwd)"` are resolved from
    # the tracked cwd instead of dropping tracking, so subsequent in-repo
    # relative paths verify normally. The whitelist is closed: any other
    # substitution keeps the untracked-cd behavior. The hook never executes
    # the substitution text — the toplevel is a filesystem walk to `.git`.

    def test_classify_cd_helper_whitelisted_substs(self):
        self.assertEqual(
            guard.classify_cd(["cd", "$(git rev-parse --show-toplevel)"]),
            ("subst", "toplevel"),
        )
        self.assertEqual(guard.classify_cd(["cd", "$(pwd)"]), ("subst", "pwd"))
        self.assertEqual(
            guard.classify_cd(["pushd", "$(git rev-parse --show-toplevel)"]),
            ("subst", "toplevel"),
        )
        # Option flags before the target are skipped, same as the 'arg' path.
        self.assertEqual(
            guard.classify_cd(["cd", "-P", "$(git rev-parse --show-toplevel)"]),
            ("subst", "toplevel"),
        )

    def test_classify_cd_helper_subst_whitespace_normalized(self):
        # Extra internal whitespace and the optional spaces just inside
        # `$( ... )` are canonicalized before the whitelist lookup.
        self.assertEqual(
            guard.classify_cd(["cd", "$(git  rev-parse   --show-toplevel)"]),
            ("subst", "toplevel"),
        )
        self.assertEqual(
            guard.classify_cd(["cd", "$( git rev-parse --show-toplevel )"]),
            ("subst", "toplevel"),
        )
        self.assertEqual(guard.classify_cd(["cd", "$( pwd )"]), ("subst", "pwd"))

    def test_classify_cd_helper_non_whitelisted_subst_unknown(self):
        # Anything outside the closed whitelist stays untracked.
        self.assertEqual(
            guard.classify_cd(["cd", "$(mktemp -d)"]), ("unknown", None))
        self.assertEqual(
            guard.classify_cd(["cd", "$(git rev-parse --git-dir)"]),
            ("unknown", None),
        )
        # A whitelisted substitution with a suffix is NOT the exact canonical
        # token — no general `$( )` evaluation.
        self.assertEqual(
            guard.classify_cd(["cd", "$(git rev-parse --show-toplevel)/docs"]),
            ("unknown", None),
        )
        self.assertEqual(
            guard.classify_cd(["cd", "$(pwd)/sub"]), ("unknown", None))

    def test_normalize_subst_helper(self):
        self.assertEqual(
            guard.normalize_subst("$( git  rev-parse  --show-toplevel )"),
            "$(git rev-parse --show-toplevel)",
        )
        self.assertEqual(guard.normalize_subst("$(pwd)"), "$(pwd)")
        # Non-substitution tokens pass through (possibly collapsed) unmatched.
        self.assertEqual(guard.normalize_subst("/etc"), "/etc")

    def test_git_toplevel_walks_up_to_dot_git_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(d)
            os.mkdir(os.path.join(root, ".git"))
            nested = os.path.join(root, "a", "b")
            os.makedirs(nested)
            self.assertEqual(guard.git_toplevel(nested), root)
            self.assertEqual(guard.git_toplevel(root), root)

    def test_git_toplevel_dot_git_file_is_boundary(self):
        # Worktrees and submodules use a `.git` *file* (gitdir pointer).
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(d)
            with open(os.path.join(root, ".git"), "w") as f:
                f.write("gitdir: /elsewhere\n")
            nested = os.path.join(root, "sub")
            os.mkdir(nested)
            self.assertEqual(guard.git_toplevel(nested), root)

    def test_git_toplevel_no_boundary_returns_none(self):
        # A tempdir with no `.git` anywhere up its parent chain.
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(guard.git_toplevel(os.path.realpath(d)))

    def test_git_toplevel_discovery_env_disables(self):
        # GIT_DIR & co. can change git's answer — bail to untracked.
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(d)
            os.mkdir(os.path.join(root, ".git"))
            for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CEILING_DIRECTORIES"):
                old = os.environ.get(var)
                try:
                    os.environ[var] = "/somewhere"
                    self.assertIsNone(guard.git_toplevel(root), var)
                finally:
                    if old is None:
                        os.environ.pop(var, None)
                    else:
                        os.environ[var] = old

    def _make_git_workspace(self):
        """Mark the E2E workspace tempdir as a git toplevel."""
        os.mkdir(os.path.join(self.workspace, ".git"))

    def test_cd_git_toplevel_subst_relative_allow(self):
        # The issue-59 motivating case: cwd tracking survives the substitution
        # and the in-repo relative read is allowed instead of untracked-asked.
        self._make_git_workspace()
        self._decision(
            'cd "$(git rev-parse --show-toplevel)" && cat in.txt', "allow")

    def test_cd_git_toplevel_subst_from_subdir_allow(self):
        # From a nested cwd the substitution resolves back to the workspace
        # root, so a root-relative read is allowed.
        self._make_git_workspace()
        nested = os.path.join(self.workspace, "a", "b")
        os.makedirs(nested)
        self._decision(
            'cd "$(git rev-parse --show-toplevel)" && cat in.txt', "allow",
            cwd=nested, project_dir=self.workspace)

    def test_cd_git_toplevel_subst_whitespace_variant_allow(self):
        self._make_git_workspace()
        self._decision(
            'cd "$( git  rev-parse --show-toplevel )" && cat in.txt', "allow")

    def test_cd_git_toplevel_subst_no_repo_stays_untracked_ask(self):
        # No `.git` boundary above the workspace tempdir -> the substitution
        # can't be resolved, so the cd stays untracked and the relative read
        # asks (unchanged secure default).
        self._decision(
            'cd "$(git rev-parse --show-toplevel)" && cat in.txt', "ask")

    def test_cd_git_toplevel_subst_outside_file_still_ask(self):
        # Resolving the cd must not weaken the boundary: an outside path
        # after the tracked cd still asks.
        self._make_git_workspace()
        self._decision(
            'cd "$(git rev-parse --show-toplevel)" && cat /etc/q59-fake-target',
            "ask")

    def test_cd_git_toplevel_subst_outside_repo_relative_blocked(self):
        # cd into an outside repo first: the substitution resolves to that
        # repo's root — outside the workspace — so the relative read is
        # blocked. Whether that block is `ask` or `deny` depends on where the
        # OS places the tempdir (a tempdir under /tmp is host-temp -> deny;
        # elsewhere -> ask); the substitution-resolution behavior under test
        # is the same either way, so assert only that it's not allowed and the
        # relative target is named in the reason.
        with tempfile.TemporaryDirectory() as d:
            outside = os.path.realpath(d)
            os.mkdir(os.path.join(outside, ".git"))
            out = run_hook(
                f'cd {sh(outside)} && cd "$(git rev-parse --show-toplevel)" '
                "&& cat data.txt",
                self.workspace,
            )
            self.assertIsNotNone(out)
            self.assertIn(
                out["hookSpecificOutput"]["permissionDecision"], ("ask", "deny"))
            self.assertIn(
                "data.txt",
                out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_cd_git_toplevel_subst_after_untracked_cd_stays_ask(self):
        # An already-untracked cwd can't seed the walk-up — the substitution
        # must not "re-track" from a wrong starting point.
        self._make_git_workspace()
        self._decision(
            'cd - && cd "$(git rev-parse --show-toplevel)" && cat in.txt',
            "ask")

    def test_cd_pwd_subst_relative_allow(self):
        # `$(pwd)` is the identity on the tracked cwd — no `.git` needed.
        self._decision('cd "$(pwd)" && cat in.txt', "allow")

    def test_cd_pwd_subst_after_cd_outside_relative_ask(self):
        # Identity on a tracked *outside* cwd: relative reads still ask.
        self._decision('cd /etc && cd "$(pwd)" && cat passwd', "ask")

    def test_cd_subst_with_suffix_stays_untracked_ask(self):
        # `$(...)/sub` is not the exact canonical token — still untracked.
        self._make_git_workspace()
        self._decision(
            'cd "$(git rev-parse --show-toplevel)/sub" && cat in.txt', "ask")

    def test_cd_non_whitelisted_subst_stays_untracked_ask(self):
        # A non-whitelisted cd substitution whose body is an UNGUARDED command
        # (readlink) leaves the cwd untracked, so the relative read asks. Body
        # has no guarded command, so substitution recursion adds nothing.
        self._decision('cd "$(readlink foo)" && cat in.txt', "ask")

    def test_cd_mktemp_subst_inner_host_temp_deny(self):
        # The inner `mktemp -d` genuinely creates a host-temp dir; substitution
        # recursion now parses it and denies (host-temp steering), on top of the
        # untracked-cd `ask` for the relative read. (Q33)
        self._decision('cd "$(mktemp -d)" && cat in.txt', "deny")

    def test_cd_git_toplevel_subst_git_dir_env_stays_untracked_ask(self):
        # GIT_DIR in the environment can change git's answer, so the
        # substitution is not resolved and the cd stays untracked.
        self._make_git_workspace()
        out = run_hook(
            'cd "$(git rev-parse --show-toplevel)" && cat in.txt',
            self.workspace,
            env_extra={"GIT_DIR": "/somewhere/else"},
        )
        self.assertIsNotNone(out)
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "ask")

    # --- whitelisted pure substitutions in file operands (issue 84) ----------
    # The issue-59 cd-target registry (CD_SUBST) also resolves when a
    # substitution LEADS a guarded file operand or redirect target, so
    # `cp x "$(git rev-parse --show-toplevel)/backup/"` classifies the real
    # in-repo path instead of asking. Same closed whitelist, same literal
    # matching; the value is string-concatenated with the remainder (bash
    # inserts no separator), so a `$`/`~` left in the remainder still asks.

    def test_resolve_subst_prefix_pwd(self):
        # `$(pwd)` is the identity on the passed cwd; the remainder is
        # concatenated verbatim (a leading `/` is preserved).
        self.assertEqual(
            guard.resolve_subst_prefix("$(pwd)/backup/", "/ws"),
            "/ws/backup/",
        )
        self.assertEqual(guard.resolve_subst_prefix("$(pwd)", "/ws"), "/ws")
        # No path separator is inserted — bash concatenates the output verbatim.
        self.assertEqual(guard.resolve_subst_prefix("$(pwd)x", "/ws"), "/wsx")

    def test_resolve_subst_prefix_git_toplevel(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(d)
            os.mkdir(os.path.join(root, ".git"))
            nested = os.path.join(root, "a", "b")
            os.makedirs(nested)
            self.assertEqual(
                guard.resolve_subst_prefix(
                    "$(git rev-parse --show-toplevel)/backup", nested),
                root + "/backup",
            )

    def test_resolve_subst_prefix_whitespace_variant(self):
        self.assertEqual(
            guard.resolve_subst_prefix("$( pwd )/out", "/ws"),
            "/ws/out",
        )

    def test_resolve_subst_prefix_non_whitelisted_unchanged(self):
        # Not in the registry -> returned verbatim (stays runtime-expanded).
        self.assertEqual(
            guard.resolve_subst_prefix("$(whoami)/x", "/ws"),
            "$(whoami)/x",
        )
        # Not a leading substitution -> unchanged.
        self.assertEqual(
            guard.resolve_subst_prefix("dir/$(pwd)", "/ws"),
            "dir/$(pwd)",
        )

    def test_resolve_subst_prefix_git_no_boundary_unchanged(self):
        # `$(git rev-parse --show-toplevel)` with no `.git` above cwd can't be
        # resolved -> returned verbatim (secure default).
        with tempfile.TemporaryDirectory() as d:
            root = os.path.realpath(d)
            self.assertEqual(
                guard.resolve_subst_prefix(
                    "$(git rev-parse --show-toplevel)/x", root),
                "$(git rev-parse --show-toplevel)/x",
            )

    def test_pwd_subst_file_operand_in_workspace_allow(self):
        # The motivating case for `$(pwd)`: an in-workspace file operand is
        # resolved and allowed instead of asking.
        self._decision('cat "$(pwd)/in.txt"', "allow")

    def test_git_toplevel_subst_file_operand_in_workspace_allow(self):
        self._make_git_workspace()
        nested = os.path.join(self.workspace, "a", "b")
        os.makedirs(nested)
        self._decision(
            'cat "$(git rev-parse --show-toplevel)/in.txt"', "allow",
            cwd=nested, project_dir=self.workspace)

    def test_cp_subst_dest_in_workspace_allow(self):
        # The issue's headline example: a write destination under the resolved
        # toplevel is in-workspace, so the cp is allowed.
        self._make_git_workspace()
        os.mkdir(os.path.join(self.workspace, "backup"))
        self._decision(
            'cp in.txt "$(git rev-parse --show-toplevel)/backup/"', "allow")

    def test_subst_file_operand_outside_still_blocked(self):
        # Resolving the substitution must not weaken the boundary: a `..` in the
        # remainder escapes the workspace and is still blocked. Whether that is
        # `ask` or `deny` depends on where the OS places the tempdir (a parent
        # under /tmp or /var/folders is host-temp -> deny; elsewhere -> ask);
        # assert only that it isn't allowed and the target is named.
        out = run_hook('cat "$(pwd)/../q84-fake-target"', self.workspace)
        self.assertIsNotNone(out)
        self.assertIn(
            out["hookSpecificOutput"]["permissionDecision"], ("ask", "deny"))
        self.assertIn(
            "$(pwd)/../q84-fake-target",
            out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_subst_file_operand_remainder_var_still_ask(self):
        # A `$VAR` left in the remainder is not resolvable -> still asks, naming
        # the original token.
        out = self._decision('cat "$(pwd)/$OTHER"', "ask")
        self.assertIn(
            "$(pwd)/$OTHER",
            out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_non_whitelisted_subst_file_operand_still_ask(self):
        self._decision('cat "$(whoami)/x"', "ask")

    def test_subst_file_operand_after_untracked_cd_stays_ask(self):
        # `$(pwd)` after an untracked cd can't be resolved from a known cwd, so
        # it stays runtime-expanded and asks.
        self._decision('cd - && cat "$(pwd)/in.txt"', "ask")

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

    def test_ln_hard_link_outside_target_then_cat_link_ask(self):
        # Hard-link bypass shape (Q17): identical to the Q8 symlink case
        # without `-s`. Bash hasn't created `link` yet at hook time, so the
        # lexical realpath of `link` lands inside the workspace and would
        # otherwise sneak through. Staging catches it.
        self._decision(
            "ln /tmp/q8-fake-target link && cat link", "ask",
        )

    def test_ln_hard_link_inside_target_then_cat_link_allow(self):
        # Innocent hard link to a workspace file — both target and link stay
        # inside, no staging needed.
        self._decision("ln in.txt link && cat link", "allow")

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

    def test_classify_ln_helper_hard_link_returns_positionals(self):
        # Q11 PR4 / Q17: hard-link form is now classified identically to the
        # symbolic form — the threat model (LINK reads outside file later) is
        # the same.
        self.assertEqual(
            guard.classify_ln(["ln", "/tmp/x", "link"]),
            ("/tmp/x", "link"),
        )

    def test_classify_ln_helper_hard_link_single_positional(self):
        # `ln /tmp/x` (no LINK) — POSIX implicitly creates `x` in cwd, same
        # as the symbolic case.
        self.assertEqual(
            guard.classify_ln(["ln", "/tmp/x"]),
            ("/tmp/x", None),
        )

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

    # --- classify_dd helper (Q11 PR3) ---------------------------------------

    def test_classify_dd_helper_if_and_of(self):
        self.assertEqual(
            guard.classify_dd(["dd", "if=./in", "of=/tmp/out", "bs=1M"]),
            ["./in", "/tmp/out"],
        )

    def test_classify_dd_helper_if_only(self):
        self.assertEqual(
            guard.classify_dd(["dd", "if=/dev/urandom", "count=1"]),
            ["/dev/urandom"],
        )

    def test_classify_dd_helper_of_only(self):
        self.assertEqual(
            guard.classify_dd(["dd", "of=/tmp/out", "bs=1M"]),
            ["/tmp/out"],
        )

    def test_classify_dd_helper_no_operands(self):
        # `dd` alone is still guarded (return [] not None) — main() should
        # mark guarded=True and proceed with an empty file list.
        self.assertEqual(guard.classify_dd(["dd"]), [])

    def test_classify_dd_helper_no_file_operands(self):
        # Only value-bearing operands, no if=/of= — guarded with no files.
        self.assertEqual(
            guard.classify_dd(["dd", "bs=1M", "count=10", "conv=fdatasync"]),
            [],
        )

    def test_classify_dd_helper_lookalike_operands_not_matched(self):
        # `iflag=` / `oflag=` are not `if=` / `of=`; the prefix check is strict.
        self.assertEqual(
            guard.classify_dd(["dd", "iflag=fullblock", "oflag=direct"]),
            [],
        )

    def test_classify_dd_helper_not_dd(self):
        self.assertIsNone(guard.classify_dd(["cat", "if=foo"]))
        self.assertIsNone(guard.classify_dd([]))

    # --- classify_mktemp helper (Q26) ---------------------------------------
    # The default-location cases resolve to `default_temp_dir()`; asserting that
    # value directly keeps the tests independent of the developer's $TMPDIR.

    def test_classify_mktemp_not_mktemp(self):
        self.assertIsNone(guard.classify_mktemp(["cat", "-p", "/tmp"]))
        self.assertIsNone(guard.classify_mktemp([]))

    def test_classify_mktemp_bare_is_default_location(self):
        self.assertEqual(guard.classify_mktemp(["mktemp"]),
                         [guard.default_temp_dir()])

    def test_classify_mktemp_directory_flag_still_default(self):
        self.assertEqual(guard.classify_mktemp(["mktemp", "-d"]),
                         [guard.default_temp_dir()])

    def test_classify_mktemp_bare_name_template_is_default(self):
        self.assertEqual(guard.classify_mktemp(["mktemp", "foo.XXXXXX"]),
                         [guard.default_temp_dir()])

    def test_classify_mktemp_slashed_template_names_its_dir(self):
        self.assertEqual(guard.classify_mktemp(["mktemp", "/tmp/foo.XXXXXX"]),
                         ["/tmp/foo.XXXXXX"])

    def test_classify_mktemp_workspace_template(self):
        self.assertEqual(guard.classify_mktemp(["mktemp", "./foo.XXXXXX"]),
                         ["./foo.XXXXXX"])

    def test_classify_mktemp_dash_p_dir(self):
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "-p", "/tmp", "foo.XXXXXX"]),
            ["/tmp"])

    def test_classify_mktemp_dash_p_glued_dir(self):
        self.assertEqual(guard.classify_mktemp(["mktemp", "-p/tmp", "foo.XX"]),
                         ["/tmp"])

    def test_classify_mktemp_tmpdir_inline(self):
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "--tmpdir=/tmp", "foo.XX"]),
            ["/tmp"])

    def test_classify_mktemp_tmpdir_bare_is_default(self):
        # GNU `--tmpdir` takes an optional arg only when glued with `=`; bare it
        # uses the default location and the next token stays a template.
        self.assertEqual(guard.classify_mktemp(["mktemp", "--tmpdir", "foo.XX"]),
                         [guard.default_temp_dir()])

    def test_classify_mktemp_dash_t_is_default(self):
        # -t: GNU (no arg) and BSD (`-t prefix`) both land in default host temp.
        self.assertEqual(guard.classify_mktemp(["mktemp", "-t", "prefix"]),
                         [guard.default_temp_dir()])

    def test_classify_mktemp_workspace_target_dir(self):
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "-p", "./scratch", "foo.XX"]),
            ["./scratch"])

    def test_classify_mktemp_mixed_templates(self):
        # A slashed template names its own dir; a bare-name one adds the default.
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "/tmp/a.XXX", "b.XXX"]),
            ["/tmp/a.XXX", guard.default_temp_dir()])

    def test_classify_mktemp_cluster_dp_dir(self):
        # Q32: -dp DIR == -d -p DIR; the -p value must not leak to a template.
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "-dp", "./scratch", "x.XXX"]),
            ["./scratch"])

    def test_classify_mktemp_cluster_dp_glued_dir(self):
        # -dpDIR == -d -p DIR (value glued to the cluster).
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "-dp/tmp/q32-fake", "x.XXX"]),
            ["/tmp/q32-fake"])

    def test_classify_mktemp_cluster_boolean_only_is_default(self):
        # -du == -d -u: both boolean, so the target is still the default location.
        self.assertEqual(guard.classify_mktemp(["mktemp", "-du"]),
                         [guard.default_temp_dir()])

    def test_classify_mktemp_cluster_dt_is_default(self):
        # -dt == -d -t: -t forces the default host-temp location.
        self.assertEqual(guard.classify_mktemp(["mktemp", "-dt", "prefix"]),
                         [guard.default_temp_dir()])

    def test_classify_mktemp_version_is_informational(self):
        self.assertIsNone(guard.classify_mktemp(["mktemp", "--version"]))
        self.assertIsNone(guard.classify_mktemp(["mktemp", "-V"]))

    def test_classify_mktemp_end_of_options(self):
        # After `--`, a slashed token is a template even if it looks flag-ish.
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "--", "/tmp/x.XXX"]),
            ["/tmp/x.XXX"])

    # --- inline TMPDIR= override (Q34) --------------------------------------
    # A literal `TMPDIR=<dir>` command prefix relocates mktemp's default
    # location; `inline_tmpdir` captures it and `classify_mktemp` feeds it to
    # the default-location branch (bare / -t / bare-name template) only.

    def test_inline_tmpdir_captures_literal(self):
        self.assertEqual(guard.inline_tmpdir(["TMPDIR=./scratch", "mktemp"]),
                         "./scratch")

    def test_inline_tmpdir_last_assignment_wins(self):
        self.assertEqual(
            guard.inline_tmpdir(["TMPDIR=/a", "TMPDIR=./scratch", "mktemp"]),
            "./scratch")

    def test_inline_tmpdir_among_other_assignments(self):
        self.assertEqual(
            guard.inline_tmpdir(["LC_ALL=C", "TMPDIR=./scratch", "mktemp"]),
            "./scratch")

    def test_inline_tmpdir_none_when_absent(self):
        self.assertIsNone(guard.inline_tmpdir(["LC_ALL=C", "mktemp"]))
        self.assertIsNone(guard.inline_tmpdir(["mktemp", "TMPDIR=./x"]))

    def test_inline_tmpdir_rejects_unexpanded_value(self):
        # A `$`/backtick value would be expanded by bash; not a trusted literal.
        self.assertIsNone(guard.inline_tmpdir(["TMPDIR=$FOO", "mktemp"]))
        self.assertIsNone(guard.inline_tmpdir(["TMPDIR=`pwd`", "mktemp"]))
        self.assertIsNone(guard.inline_tmpdir(["TMPDIR=", "mktemp"]))

    def test_classify_mktemp_default_dir_override(self):
        # Override replaces the default location for bare / -t / bare-name cases.
        self.assertEqual(
            guard.classify_mktemp(["mktemp"], "./scratch"), ["./scratch"])
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "-t", "p"], "./scratch"),
            ["./scratch"])
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "foo.XX"], "./scratch"),
            ["./scratch"])

    def test_classify_mktemp_explicit_dir_beats_override(self):
        # An explicit -p / --tmpdir= wins over the TMPDIR= prefix (real mktemp
        # precedence), and a slashed template names its own dir regardless.
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "-p", "/tmp", "foo.XX"], "./scratch"),
            ["/tmp"])
        self.assertEqual(
            guard.classify_mktemp(["mktemp", "/tmp/foo.XX"], "./scratch"),
            ["/tmp/foo.XX"])

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

    # --- shell-keyword prefix (Q28) -----------------------------------------

    def test_keyword_until_grep_outside_ask(self):
        # `until grep …` — pre-Q28 the `until` keyword masked the SPEC lookup
        # and the whole group deferred.
        out = self._decision("until grep -q PAT /etc/q28-fake-target; do :; done", "ask")
        self.assertIn(
            "/etc/q28-fake-target",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_keyword_if_cat_outside_ask(self):
        self._decision("if cat /etc/q28-fake-target; then :; fi", "ask")

    def test_keyword_if_cat_workspace_allow(self):
        self._decision("if cat in.txt; then :; fi", "allow")

    def test_keyword_prefix_prog_suppression_preserved(self):
        # prog-suppression must still fire after the keyword is stripped.
        self._decision("if grep -e PAT /etc/q28-fake-target; then :; fi", "ask")

    def test_keyword_prefix_env_prefix_combined_ask(self):
        # bash order: reserved word, then inline env assignment, then command.
        self._decision("until LC_ALL=C grep -q PAT /etc/q28-fake-target; do :; done", "ask")

    def test_loop_body_do_prefix_outside_ask(self):
        # The `do cat …` body group is its own token group after `;` splitting.
        self._decision("for f in a; do cat /etc/q28-fake-target; done", "ask")

    def test_oneline_for_do_loopvar_outside_ask(self):
        # Pre-Q28 the `do` on the same line masked the guarded command and the
        # one-line `for … do … done` loop deferred; now the loop variable is
        # resolved and an outside candidate prompts.
        self._decision("for f in /etc/q28-fake-target; do cat $f; done", "ask")

    def test_oneline_for_do_loopvar_workspace_allow(self):
        self._decision("for f in in.txt; do cat $f; done", "allow")

    # --- heredoc / here-string (Q4) -----------------------------------------

    def test_here_string_path_like_content_allow(self):
        # `<<<` content is stdin data, not a file path — must not be flagged
        # even when it looks like an outside-workspace path.
        self._decision('cat <<<"/etc/foo"', "allow")

    def test_heredoc_path_like_delimiter_allow(self):
        # `<<TAG` delimiter is a sentinel string, not a file path. Even when
        # the delimiter resembles an outside path, the hook must not flag it.
        # (Heredoc body lines are dropped before parsing as of Q60 — see
        # StripHeredocBodiesTests and Issue60EndToEndTests.)
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

    # --- Claude per-session temp allow (Q21) --------------------------------
    # Claude Code writes each background task's output to
    # /tmp/claude-<uid>/<encoded-project>/<session-uuid>/tasks/<id>.output and
    # the agent reads it back. That is the agent's own scratch, not the boundary
    # this hook guards, so it's allowed — but ONLY for the current session.
    # Paths come from guard.claude_tmp_root() so they match what the script
    # computes on this platform (the Windows root has no uid suffix); the dirs
    # need not exist (the script resolves lexically and the subprocess never
    # execs the command). Synthetic project/uuid segments, per the repo rule on
    # never using real outside paths in fixtures.

    def _session_tmp(self, session_id, name="abc.output"):
        return os.path.join(
            guard.claude_tmp_root(), "-Users-me-proj",
            session_id, "tasks", name)

    def test_claude_session_tmp_read_allow(self):
        sess = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._decision(f"cat {sh(self._session_tmp(sess))}", "allow",
                       session_id=sess)

    def test_claude_session_tmp_tail_allow(self):
        sess = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._decision(f"tail -20 {sh(self._session_tmp(sess))}", "allow",
                       session_id=sess)

    def test_claude_session_tmp_redirect_target_allow(self):
        # Writing into the current session's scratch via a redirect is allowed.
        sess = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._decision(f"cat in.txt > {sh(self._session_tmp(sess, 'log'))}",
                       "allow", session_id=sess)

    def _session_scratchpad(self, session_id, name="note.txt"):
        return os.path.join(
            guard.claude_tmp_root(), "-Users-me-proj",
            session_id, "scratchpad", name)

    def test_claude_session_scratchpad_write_allow(self):
        # The session's whole scratch tree — not just task output — is exempt
        # for write commands too, which is what the README now promises. (#126)
        sess = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._decision(f"echo hi | tee {sh(self._session_scratchpad(sess))}",
                       "allow", session_id=sess)

    def test_claude_other_session_scratchpad_write_ask(self):
        # The read-write exemption stops at the session boundary.
        owner = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        current = "ffffffff-0000-1111-2222-333333333333"
        self._decision(f"cp in.txt {sh(self._session_scratchpad(owner))}",
                       "ask", session_id=current)

    def test_claude_other_session_tmp_ask(self):
        # A path carrying a DIFFERENT session's uuid must still prompt — this is
        # the cross-session leak the per-session scope prevents.
        owner = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        current = "ffffffff-0000-1111-2222-333333333333"
        out = self._decision(f"cat {sh(self._session_tmp(owner))}", "ask",
                             session_id=current)
        self.assertIn(self._session_tmp(owner),
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_claude_tmp_without_session_id_ask(self):
        # No session_id field (older CLI) -> allow disabled -> still prompts.
        sess = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self._decision(f"cat {sh(self._session_tmp(sess))}", "ask")

    def test_claude_session_tmp_symlink_escape_still_ask(self):
        # Defense-in-depth: an `ln` staging an OUTSIDE target to a link that
        # lives inside the allowed session scratch must still be flagged. The
        # staged-path check runs before the session-tmp allow.
        sess = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        link = self._session_tmp(sess, "link")
        out = self._decision(
            f"ln -s /tmp/q21-fake-target {sh(link)} && cat {sh(link)}", "ask",
            session_id=sess)
        self.assertIn(link,
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    # --- ALLOWED_READ_PREFIXES end-to-end -----------------------------------
    # Claude Code writes workflow journals and sub-agent data under
    # ~/.claude/projects/. Reading them back should not prompt. The tests use a
    # synthetic path under the real ~/.claude/projects/ dir (the dirs need not
    # exist — the hook resolves lexically). Write commands must still prompt.

    def _claude_projects_path(self, *parts):
        """Return a synthetic path under ~/.claude/projects/.

        Plain, like every other path helper here — callers quote it with ``sh()``
        at the point of interpolation. Quoting inside the helper too would double
        it, and the doubled token parses back as a filename that literally
        contains quote characters.
        """
        cpd = guard.claude_projects_dir()
        if cpd is None:
            self.skipTest("home not resolvable, skipping ~/.claude/projects/ tests")
        return os.path.join(cpd, *parts)

    def test_cat_claude_projects_allow(self):
        # Reading a workflow journal under ~/.claude/projects/ is allowed.
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "journal.jsonl")
        self._decision(f"cat {sh(target)}", "allow")

    def test_grep_claude_projects_allow(self):
        target = self._claude_projects_path(
            "-Users-me-proj", "subagents", "data.json")
        self._decision(f"grep 'key' {sh(target)}", "allow")

    def test_head_claude_projects_allow(self):
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "journal.jsonl")
        self._decision(f"head -20 {sh(target)}", "allow")

    def test_tail_claude_projects_allow(self):
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "journal.jsonl")
        self._decision(f"tail -f {sh(target)}", "allow")

    def test_cp_from_claude_projects_ask(self):
        # cp reads source and writes dest — write command; prefix exemption
        # does NOT apply even when the source is under ~/.claude/projects/.
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "journal.jsonl")
        self._decision(f"cp {sh(target)} ./local-copy.jsonl", "ask")

    def test_cp_to_claude_projects_ask(self):
        # Writing into ~/.claude/projects/ is also not exempt.
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "out.txt")
        self._decision(f"cp ./in.txt {sh(target)}", "ask")

    def test_rm_claude_projects_ask(self):
        # Deletion is a write command; exemption does not apply.
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "journal.jsonl")
        self._decision(f"rm {sh(target)}", "ask")

    def test_redirect_to_claude_projects_ask(self):
        # A redirect target is conservative (is_read=False) even for
        # allowed prefixes — the hook can't verify the redirect direction.
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "out.txt")
        self._decision(f"cat in.txt > {sh(target)}", "ask")

    def test_sed_inplace_claude_projects_ask(self):
        # Q36: `sed -i` mutates its file operand — write mode; the read
        # exemption must not apply (memory files feed future sessions'
        # context, so a silent in-place write is an injection vector).
        target = self._claude_projects_path(
            "-Users-me-proj", "memory", "MEMORY.md")
        self._decision(f"sed -i 's/a/b/' {sh(target)}", "ask")

    def test_sed_inplace_cluster_claude_projects_ask(self):
        # `-ni` cluster: the `i` inside a short-option run still counts.
        target = self._claude_projects_path(
            "-Users-me-proj", "memory", "MEMORY.md")
        self._decision(f"sed -ni 's/a/b/p' {sh(target)}", "ask")

    def test_sed_read_only_claude_projects_allow(self):
        # Plain sed (no -i) stays a read — exemption applies.
        target = self._claude_projects_path(
            "-Users-me-proj", "memory", "MEMORY.md")
        self._decision(f"sed -n '1,10p' {sh(target)}", "allow")

    def test_awk_inplace_claude_projects_ask(self):
        target = self._claude_projects_path(
            "-Users-me-proj", "memory", "MEMORY.md")
        self._decision(f"awk -i inplace '{{print}}' {sh(target)}", "ask")

    def test_yq_inplace_claude_projects_ask(self):
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "data.yaml")
        self._decision(f"yq -i '.a = 1' {sh(target)}", "ask")

    def test_sort_output_claude_projects_ask(self):
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "out.txt")
        self._decision(f"sort -o {sh(target)} ./in.txt", "ask")

    def test_sort_read_claude_projects_allow(self):
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "journal.jsonl")
        self._decision(f"sort {sh(target)}", "allow")

    def test_sed_inplace_workspace_allow(self):
        # Write-mode detection only disables the exemption; in-workspace
        # files are unaffected.
        self._decision("sed -i 's/a/b/' ./notes.txt", "allow")

    def test_uniq_output_claude_projects_ask(self):
        # Q37: `uniq IN OUT` writes the second positional — write context,
        # exemption must not apply to the output even under the read prefix.
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "out.txt")
        self._decision(f"uniq ./in.txt {sh(target)}", "ask")

    def test_uniq_read_claude_projects_allow(self):
        # Single operand is a pure read — exemption still applies.
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "journal.jsonl")
        self._decision(f"uniq {sh(target)}", "allow")

    def test_uniq_exempt_input_workspace_output_allow(self):
        # Per-operand classification: the input keeps the read exemption
        # while the in-workspace output is fine — no prompt.
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "journal.jsonl")
        self._decision(f"uniq {sh(target)} ./deduped.txt", "allow")

    def test_uniq_flag_value_not_an_output_allow(self):
        # `-f 1` is consumed as a field count; with the old cat alias it
        # became a positional and shifted the operand indices.
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "journal.jsonl")
        self._decision(f"uniq -f 1 {sh(target)}", "allow")

    def test_xxd_output_claude_projects_ask(self):
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "dump.hex")
        self._decision(f"xxd ./in.bin {sh(target)}", "ask")

    def test_xxd_reverse_output_claude_projects_ask(self):
        # `xxd -r IN OUT` also writes the second positional; `-s 0x10` is
        # consumed so the operand indices stay aligned.
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "rebuilt.bin")
        self._decision(f"xxd -r -s 0x10 ./dump.hex {sh(target)}", "ask")

    def test_xxd_read_claude_projects_allow(self):
        target = self._claude_projects_path(
            "-Users-me-proj", "wf_abc123", "journal.jsonl")
        self._decision(f"xxd -l 64 {sh(target)}", "allow")

    def test_uniq_output_workspace_allow(self):
        # Both operands in-workspace: unaffected by the write classification.
        self._decision("uniq ./in.txt ./out.txt", "allow")

    def test_read_allow_prefixes_env_var(self):
        # WORKSPACE_GUARD_READ_ALLOW_PREFIXES lets users add their own prefixes.
        with tempfile.TemporaryDirectory() as td:
            td = os.path.realpath(td)
            target = os.path.join(td, "safe-data.json")
            out = run_hook(f"cat {sh(target)}", self.workspace,
                           env_extra={"WORKSPACE_GUARD_READ_ALLOW_PREFIXES": td})
            self.assertIsNotNone(out)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_read_allow_prefix_does_not_exempt_write_command(self):
        # Even with a user-configured prefix, write commands must still prompt.
        # Use a synthetic path outside /tmp to avoid the host-temp deny path.
        fake_prefix = "/var/fake-read-allow-test"
        target = fake_prefix + "/safe-data.json"
        out = run_hook(f"cp ./in.txt {sh(target)}", self.workspace,
                       env_extra={"WORKSPACE_GUARD_READ_ALLOW_PREFIXES": fake_prefix})
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

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

    # --- yq end-to-end (Q10) ------------------------------------------------

    def test_yq_workspace_allow(self):
        self._decision("yq .foo in.txt", "allow")

    def test_yq_outside_ask(self):
        out = self._decision("yq .x /etc/hosts", "ask")
        self.assertIn(
            "/etc/hosts",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_yq_from_file_outside_ask(self):
        # Both kislyuk and mikefarah read the program/expression from FILE.
        self._decision("yq --from-file /etc/evil.yq in.txt", "ask")

    def test_yq_short_f_outside_ask(self):
        # kislyuk's jq-pass-through -f. For mikefarah this is --front-matter
        # (a string value), but an absolute outside path is unusual there and
        # asking is the secure default.
        self._decision("yq -f /etc/evil.jq in.txt", "ask")

    def test_yq_slurpfile_outside_ask(self):
        self._decision("yq --slurpfile d /etc/hosts . in.txt", "ask")

    def test_yq_mikefarah_output_format_outside_file_ask(self):
        # The motivating mikefarah-aware case: expression omitted, flag value
        # is a format name. If `-o` were declared as consume:1, the value
        # would be eaten and the outside file silently allowed.
        out = self._decision("yq -o json /etc/passwd", "ask")
        self.assertIn(
            "/etc/passwd",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_yq_mikefarah_indent_outside_file_ask(self):
        self._decision("yq -I 2 /etc/passwd", "ask")

    def test_yq_kislyuk_arg_outside_ask(self):
        # `--arg NAME VAL` must consume cleanly so the trailing file is the
        # one that gets flagged — not NAME or VAL.
        out = self._decision("yq --arg n v .x /etc/hosts", "ask")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("/etc/hosts", reason)
        self.assertNotIn(" n,", reason)
        self.assertNotIn(" v,", reason)

    def test_yq_program_only_workspace_allow(self):
        # `.a/.b` is a yq expression, not a path — same shape as the jq
        # decision-table row.
        self._decision("yq '.a/.b' in.txt", "allow")

    def test_yq_inplace_workspace_allow(self):
        # mikefarah `-i` (boolean inplace) — falls through as zero-arg.
        self._decision("yq -i .foo in.txt", "allow")

    def test_yq_pipe_chain_workspace_allow(self):
        self._decision("cat in.txt | yq .foo", "allow")

    # --- Q9: cat-family commands (dedicated rows + aliases) -----------------

    def test_sort_workspace_allow(self):
        self._decision("sort in.txt", "allow")

    def test_sort_output_outside_ask(self):
        # `-o /etc/out.txt` writes outside — must ask, citing /etc/out.txt.
        out = self._decision("sort -o /etc/out.txt in.txt", "ask")
        self.assertIn(
            "/etc/out.txt",
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
        self._decision("file -f /etc/list.txt", "ask")

    def test_hexdump_workspace_allow(self):
        self._decision("hexdump in.txt", "allow")

    def test_hexdump_outside_ask(self):
        self._decision("hexdump /etc/passwd", "ask")

    def test_hexdump_format_file_outside_ask(self):
        self._decision("hexdump -f /etc/fmt.txt in.txt", "ask")

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
        self._decision("zcat /etc/archive.gz", "ask")

    def test_cmp_outside_ask(self):
        self._decision("cmp in.txt /etc/hosts", "ask")

    # --- Q11 PR1: cp / mv / tee end-to-end ----------------------------------

    def test_cp_inside_workspace_allow(self):
        # `cp SRC DEST` where both are inside the workspace — must allow.
        with open(os.path.join(self.workspace, "src.txt"), "w") as f:
            f.write("hi\n")
        self._decision("cp src.txt dst.txt", "allow")

    def test_cp_outside_source_ask(self):
        # `cp /etc/passwd ./local` — outside source must ask.
        out = self._decision("cp /etc/passwd ./local", "ask")
        self.assertIn(
            "/etc/passwd",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_cp_outside_dest_ask(self):
        # `cp ./in.txt /etc/exfil` — outside dest must ask (the net-new
        # coverage Q11 adds).
        out = self._decision("cp ./in.txt /etc/exfil", "ask")
        self.assertIn(
            "/etc/exfil",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_cp_target_directory_outside_ask(self):
        # `cp -t /etc a.txt` — DIR must be checked. (Non-temp outside dir so the
        # target-directory parsing intent isn't entangled with host-temp deny.)
        out = self._decision("cp -t /etc in.txt", "ask")
        self.assertIn(
            "/etc",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_cp_target_directory_inline_outside_ask(self):
        self._decision("cp --target-directory=/etc in.txt", "ask")

    def test_cp_recursive_outside_ask(self):
        self._decision("cp -r ./dir /etc/exfil", "ask")

    def test_cp_after_cd_relative_outside_ask(self):
        # `cd /etc && cp passwd out` — both positionals resolve outside
        # the workspace via Q7's cd-tracking (/etc/passwd, /etc/out).
        self._decision("cd /etc && cp passwd out", "ask")

    def test_mv_inside_workspace_allow(self):
        with open(os.path.join(self.workspace, "src.txt"), "w") as f:
            f.write("hi\n")
        self._decision("mv src.txt dst.txt", "allow")

    def test_mv_outside_dest_tilde_ask(self):
        # `mv .env ~/leaked` — `~` is runtime-expanded; secure-by-default ask.
        out = self._decision("mv in.txt ~/leaked", "ask")
        self.assertIn(
            "~/leaked",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_mv_outside_source_ask(self):
        self._decision("mv /etc/payload ./app.py", "ask")

    def test_tee_inside_workspace_allow(self):
        self._decision("tee log.txt", "allow")

    def test_tee_outside_ask(self):
        out = self._decision("tee /etc/hosts", "ask")
        self.assertIn(
            "/etc/hosts",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_tee_append_outside_ask(self):
        self._decision("tee -a /var/log/syslog", "ask")

    def test_pipe_into_tee_inside_allow(self):
        # `echo foo | tee log.txt` — pipe source is unguarded (echo is not in
        # SPEC), tee target is inside workspace. Decision is allow.
        self._decision("echo foo | tee log.txt", "allow")

    def test_pipe_into_tee_outside_ask(self):
        self._decision("echo foo | tee /etc/hosts", "ask")

    # --- Q11 PR2: rm end-to-end ---------------------------------------------

    def test_rm_inside_workspace_allow(self):
        # `rm ./build` inside the workspace — allow.
        self._decision("rm -rf ./build", "allow")

    def test_rm_outside_absolute_ask(self):
        out = self._decision("rm -rf /etc/q11-fake-target", "ask")
        self.assertIn(
            "/etc/q11-fake-target",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_rm_traversal_outside_ask(self):
        # `rm -rf ../../../…/etc/foo` from inside the workspace escapes via
        # realpath. Over-traversal clamps at `/`, so this deterministically
        # resolves to /etc/foo (outside, and not host-temp) on any platform —
        # the temp workspace itself may live under /tmp or /var/folders.
        nested = os.path.join(self.workspace, "sub")
        os.mkdir(nested)
        self._decision("rm -rf ../../../../../../../../etc/foo", "ask", cwd=nested)

    def test_rm_tilde_outside_ask(self):
        out = self._decision("rm ~/secret", "ask")
        self.assertIn(
            "~/secret",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_rm_after_cd_outside_ask(self):
        # `cd /etc && rm passwd` — Q7 cd-tracking re-roots `passwd` to /etc.
        self._decision("cd /etc && rm passwd", "ask")

    def test_rm_mixed_positionals_one_outside_ask(self):
        # Mixed list — any outside positional triggers ask, citing only it.
        out = self._decision("rm -rf in.txt /etc/q11-fake-target", "ask")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("/etc/q11-fake-target", reason)
        self.assertNotIn("in.txt", reason)

    def test_rm_double_dash_then_outside_ask(self):
        # `rm -- /etc/passwd` — end-of-options doesn't change the workspace
        # check; absolute outside path still asks.
        self._decision("rm -- /etc/q11-fake-target", "ask")

    # --- Q11 PR3: dd end-to-end ---------------------------------------------

    def test_dd_inside_workspace_allow(self):
        # `dd if=./in of=./out` — both operands inside workspace.
        self._decision("dd if=./in of=./out bs=1M", "allow")

    def test_dd_outside_of_ask(self):
        out = self._decision(
            "dd if=/dev/urandom of=/etc/q11-fake-target bs=1M count=1", "ask",
        )
        self.assertIn(
            "/etc/q11-fake-target",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_dd_outside_if_ask(self):
        out = self._decision("dd if=/etc/passwd of=./out", "ask")
        self.assertIn(
            "/etc/passwd",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_dd_dev_allowlisted_of_inside_allow(self):
        # `/dev/null` is allowlisted; ./out inside workspace.
        self._decision("dd if=./in of=/dev/null", "allow")

    def test_dd_no_operands_allow(self):
        # Bare `dd` is guarded but has no file operands — must allow, not defer.
        self._decision("dd", "allow")

    def test_dd_only_value_operands_allow(self):
        self._decision("dd bs=1M count=10", "allow")

    def test_dd_iflag_lookalike_not_treated_as_file_allow(self):
        # `iflag=fullblock` must not be parsed as `if=lag=fullblock`.
        self._decision("dd if=./in of=./out iflag=fullblock", "allow")

    def test_dd_after_cd_relative_outside_ask(self):
        # `cd /etc && dd if=passwd of=./out` — Q7 cd-tracking re-roots `passwd`
        # to /etc, which is outside the workspace.
        self._decision("cd /etc && dd if=passwd of=./out", "ask")

    def test_dd_tilde_outside_ask(self):
        out = self._decision("dd if=./in of=~/leaked", "ask")
        self.assertIn(
            "~/leaked",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    # --- defer paths --------------------------------------------------------

    def test_unguarded_command_defers(self):
        self._defer("ls /etc")

    def test_empty_command_defers(self):
        self._defer("   ")

    def test_unbalanced_quotes_defers(self):
        # shlex raises -> hook defers silently.
        self._defer('cat "unclosed')

    def test_unguarded_command_redirect_outside_ask(self):
        # A redirect is a shell-level write the hook resolves regardless of the
        # command word, so `ls > /etc/out.txt` — an unguarded command — is
        # honored on its redirect target and asks for the outside path (Q26).
        out = self._decision("ls > /etc/out.txt", "ask")
        self.assertIn(
            "/etc/out.txt",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_unguarded_command_positional_outside_still_defers(self):
        # Only redirect *targets* are checked for unguarded commands, not their
        # positionals: `ls /etc` has no SPEC row and no redirect, so it defers.
        self._defer("ls /etc")


class SplitOperatorRunsTests(unittest.TestCase):
    """`split_operator_runs` decomposes a glued operator-run token into its
    individual operators (Q27) — including the newline peel it subsumes (Q18) —
    while leaving word tokens (quoted strings containing an operator char or a
    newline) untouched."""

    # --- newline peel (formerly split_newline_separators, Q18) ---------------

    def test_semicolon_newline_run_split(self):
        self.assertEqual(
            guard.split_operator_runs([";\n"]), [";", "\n"])

    def test_pipe_newline_run_split(self):
        self.assertEqual(
            guard.split_operator_runs(["|\n"]), ["|", "\n"])

    def test_blank_line_double_newline_split(self):
        self.assertEqual(
            guard.split_operator_runs(["\n\n"]), ["\n", "\n"])

    def test_redirect_newline_run_split(self):
        self.assertEqual(
            guard.split_operator_runs(["&>\n"]), ["&>", "\n"])

    def test_operator_without_newline_unchanged(self):
        self.assertEqual(guard.split_operator_runs(["&&"]), ["&&"])

    def test_quoted_word_with_newline_left_intact(self):
        # A filename/string from quotes contains non-punctuation chars, so it
        # is a word token and must NOT be split even though it has a newline.
        self.assertEqual(
            guard.split_operator_runs(["line1\nline2"]), ["line1\nline2"])

    def test_plain_tokens_pass_through(self):
        self.assertEqual(
            guard.split_operator_runs(["grep", "PAT", "f.txt"]),
            ["grep", "PAT", "f.txt"])

    # --- glued operator runs (Q27) -------------------------------------------

    def test_close_paren_semicolon_split(self):
        self.assertEqual(guard.split_operator_runs([");"]), [")", ";"])

    def test_double_open_paren_split(self):
        self.assertEqual(guard.split_operator_runs(["(("]), ["(", "("])

    def test_close_paren_paren_semicolon_split(self):
        self.assertEqual(
            guard.split_operator_runs(["));"]), [")", ")", ";"])

    def test_close_paren_andand_greedy_longest(self):
        # `)&&` -> `)` then the 2-char `&&`, not `)` `&` `&`.
        self.assertEqual(guard.split_operator_runs([")&&"]), [")", "&&"])

    def test_here_string_beats_double_less(self):
        # Longest-first: `<<<` wins over `<<` + `<`.
        self.assertEqual(guard.split_operator_runs(["<<<"]), ["<<<"])

    def test_ampersand_append_redirect_greedy(self):
        # Longest-first: `&>>` wins over `&>` + `>`.
        self.assertEqual(guard.split_operator_runs(["&>>"]), ["&>>"])

    def test_pipe_open_paren_split(self):
        self.assertEqual(guard.split_operator_runs(["|("]), ["|", "("])


class NewlineSeparatorEndToEndTests(unittest.TestCase):
    """Newline-only command boundaries split into separate groups (Q18).

    Before the fix `shlex` swallowed `\\n` as whitespace, merging a command
    with the one on the next line into a single group — producing both false
    positives (the next command's tokens read as file args) and a false
    negative (a guarded command after an unguarded one escaped the guard)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _decision(self, cmd, expected):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {cmd!r}; got {got!r} "
            f"(reason: {out['hookSpecificOutput'].get('permissionDecisionReason')!r})",
        )
        return out

    def test_false_negative_guarded_after_unguarded_newline_ask(self):
        # The security regression: `echo` (unguarded) then a newline then a
        # guarded `grep` of an outside path. Pre-fix the whole thing merged
        # into the unguarded `echo` group and deferred (silent allow).
        out = self._decision("echo hi\ngrep PAT /etc/q18-fake-target", "ask")
        self.assertIn(
            "/etc/q18-fake-target",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_false_positive_next_line_echo_not_a_file_arg(self):
        # The reported case (inverted order): a guarded `grep` of an outside
        # path, then a newline, then `echo` with an outside-looking string.
        # Pre-fix `echo` and its string merged into the grep group and were
        # flagged as file args. Post-fix only the real grep target is named.
        out = self._decision(
            'grep PAT /etc/q18-real-target\necho "/tmp/q18-echo-string"', "ask")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("/etc/q18-real-target", reason)
        self.assertNotIn("/tmp/q18-echo-string", reason)
        self.assertNotIn("echo", reason)

    def test_newline_separates_guarded_groups_like_semicolon(self):
        # `cat in.txt <newline> cat OUTSIDE` — first reads a workspace file,
        # only the second is flagged. Mirrors the `;`-separator behavior.
        out = self._decision("cat in.txt\ncat /etc/q18-fake-target", "ask")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("/etc/q18-fake-target", reason)
        self.assertNotIn("in.txt", reason)

    def test_workspace_only_multiline_allow(self):
        self._decision("cat in.txt\ngrep PAT in.txt", "allow")

    def test_trailing_semicolon_then_newline_allow(self):
        # Exercises the `;\n` combined-run token path end-to-end.
        self._decision("cat in.txt;\ngrep PAT in.txt", "allow")


class GluedOperatorRunEndToEndTests(unittest.TestCase):
    """Glued operator runs split into command boundaries (Q27).

    shlex's `punctuation_chars` returns adjacent operators as one token
    (`(cd x); …` -> `);`, `((cat …` -> `((`, `… ));`). Pre-fix none matched the
    separator vocab, so the group merged and the guarded command inside was
    never isolated — the whole string deferred (a silent pass to builtin
    permissions). Post-fix each operator is its own token, so the guarded
    command is checked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _decision(self, cmd, expected):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {cmd!r}; got {got!r} "
            f"(reason: {out['hookSpecificOutput'].get('permissionDecisionReason')!r})",
        )
        return out

    def test_close_paren_semicolon_guards_following_cat(self):
        # `);` glued: pre-fix the whole string merged into the subshell group
        # and deferred, so `cat OUTSIDE` escaped the guard.
        out = self._decision(
            "(echo hi); cat /etc/q27-fake-target", "ask")
        self.assertIn(
            "/etc/q27-fake-target",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_double_paren_subshell_guarded(self):
        # `((` and `))` both glued (nested subshell). The guarded `cat` inside
        # is still isolated and its outside target flagged.
        out = self._decision(
            "((cat /etc/q27-fake-target))", "ask")
        self.assertIn(
            "/etc/q27-fake-target",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_glued_operators_workspace_only_allow(self):
        # Same glued shapes but every target is in-workspace -> allow, not a
        # spurious prompt from the split introducing phantom file args.
        self._decision("(cat in.txt); grep PAT in.txt", "allow")

    def test_trailing_comment_removed_newline_kept(self):
        self.assertEqual(
            guard.strip_comments("tee log # note\nEXIT=1"),
            "tee log \nEXIT=1")

    def test_full_line_comment_removed(self):
        self.assertEqual(
            guard.strip_comments("# note\ncat f"), "\ncat f")

    def test_hash_in_single_quotes_kept(self):
        self.assertEqual(
            guard.strip_comments("grep '#include' f"), "grep '#include' f")

    def test_hash_in_double_quotes_kept(self):
        self.assertEqual(
            guard.strip_comments('echo "a # b"'), 'echo "a # b"')

    def test_midword_hash_not_a_comment(self):
        # bash only starts a comment at a word boundary; `file#1` is literal.
        self.assertEqual(guard.strip_comments("cat file#1"), "cat file#1")

    def test_dollar_hash_not_a_comment(self):
        # `$#` (positional count) — the `#` follows `$`, not a word boundary.
        self.assertEqual(guard.strip_comments("echo $#"), "echo $#")

    def test_escaped_hash_kept(self):
        self.assertEqual(guard.strip_comments(r"cat foo\#bar"), r"cat foo\#bar")

    def test_comment_after_operator(self):
        # `#` right after a `|`/`;` operator still starts a comment.
        self.assertEqual(guard.strip_comments("cat f |# c\ngrep x f"),
                         "cat f |\ngrep x f")


class StripHeredocBodiesTests(unittest.TestCase):
    """`strip_heredoc_bodies` drops heredoc body text from the raw command
    string, before shlex, so body content (HTML, scripts, path-like text,
    unbalanced quotes) is never tokenized as commands/file args (Q60, issue 83).
    The `<<WORD` operator and delimiter stay on the command line."""

    def test_body_dropped_terminator_removed(self):
        self.assertEqual(
            guard.strip_heredoc_bodies("cat <<EOF\n</div>\nEOF"), "cat <<EOF\n")

    def test_redirect_on_heredoc_line_survives(self):
        # `cat <<EOF > out` — the redirect is on the command line, not the body.
        self.assertEqual(
            guard.strip_heredoc_bodies("cat <<EOF > out\nbody\nEOF"),
            "cat <<EOF > out\n")

    def test_quoted_delimiter_effective_form_matches(self):
        # `<<'EOF'` terminates on a line `EOF` (quotes removed for the match);
        # the operator/delimiter chars are preserved verbatim in the output.
        self.assertEqual(
            guard.strip_heredoc_bodies("cat <<'EOF'\ndon't\nEOF"),
            "cat <<'EOF'\n")

    def test_tab_strip_dash_delimiter_matches(self):
        # `<<-EOF` lets a tab-indented terminator match `EOF`.
        self.assertEqual(
            guard.strip_heredoc_bodies("cat <<-EOF\n\tx\n\tEOF"), "cat <<-EOF\n")

    def test_command_after_heredoc_preserved(self):
        self.assertEqual(
            guard.strip_heredoc_bodies("cat <<EOF\nb\nEOF\ncat x"),
            "cat <<EOF\ncat x")

    def test_unbalanced_quote_body_stripped(self):
        # The body's lone quote would abort shlex; stripping it leaves clean
        # shell syntax with the command-line redirect intact.
        self.assertEqual(
            guard.strip_heredoc_bodies("cat <<'EOF' > out\nit's a test\nEOF"),
            "cat <<'EOF' > out\n")

    def test_multiple_heredocs_consumed_in_order(self):
        self.assertEqual(
            guard.strip_heredoc_bodies("cat <<A <<B\naaa\nA\nbbb\nB"),
            "cat <<A <<B\n")

    def test_unterminated_body_swallowed_to_end(self):
        self.assertEqual(
            guard.strip_heredoc_bodies("cat <<EOF\nbody line\nno terminator"),
            "cat <<EOF\n")

    def test_here_string_not_treated_as_heredoc(self):
        # `<<<` is a distinct operator — never arms a delimiter.
        self.assertEqual(
            guard.strip_heredoc_bodies('cat <<<"/etc/foo"'), 'cat <<<"/etc/foo"')

    def test_quoted_double_less_not_heredoc(self):
        # A `<<` inside quotes is literal text, not a heredoc operator.
        self.assertEqual(
            guard.strip_heredoc_bodies('echo "a<<b"\ncat x'), 'echo "a<<b"\ncat x')

    def test_arithmetic_shift_not_heredoc(self):
        # `$((1<<2))` is a shift; the following line must NOT be eaten as a body.
        self.assertEqual(
            guard.strip_heredoc_bodies("echo $((1<<2))\ncat x"),
            "echo $((1<<2))\ncat x")

    def test_double_paren_arithmetic_shift_not_heredoc(self):
        self.assertEqual(
            guard.strip_heredoc_bodies("((1<<2))\ncat x"), "((1<<2))\ncat x")

    def test_double_less_in_comment_not_heredoc(self):
        # A `<<EOF` inside a `#` comment must not arm; the next line survives.
        self.assertEqual(
            guard.strip_heredoc_bodies("grep foo bar # <<EOF\ncat x"),
            "grep foo bar # <<EOF\ncat x")

    def test_no_heredoc_passthrough(self):
        self.assertEqual(
            guard.strip_heredoc_bodies("grep PAT f.txt\ncat g.txt"),
            "grep PAT f.txt\ncat g.txt")

    # --- expanded: hand back the bodies bash expands (Q35, Q50) --------------

    def collect(self, cmd):
        """(stripped command, bodies bash would expand) for `cmd`."""
        expanded = []
        return guard.strip_heredoc_bodies(cmd, expanded=expanded), expanded

    def test_expanded_skips_single_quoted_delimiter_body(self):
        self.assertEqual(
            self.collect("cat <<'EOF'\n$(id)\nEOF"), ("cat <<'EOF'\n", []))

    def test_expanded_skips_double_quoted_delimiter_body(self):
        self.assertEqual(
            self.collect('cat <<"EOF"\n$(id)\nEOF'), ('cat <<"EOF"\n', []))

    def test_expanded_skips_backslash_delimiter_body(self):
        # `<<\EOF` is quoting too — bash leaves the body literal.
        self.assertEqual(
            self.collect("cat <<\\EOF\n$(id)\nEOF"), ("cat <<\\EOF\n", []))

    def test_expanded_collects_unquoted_delimiter_body(self):
        self.assertEqual(
            self.collect("cat <<EOF\n$(id)\nEOF"),
            ("cat <<EOF\n", ["$(id)\nEOF"]))

    def test_expanded_skips_partially_quoted_delimiter_body(self):
        # `<<E'O'F` — any quoting in the word makes the whole delimiter quoted.
        self.assertEqual(
            self.collect("cat <<E'O'F\n$(id)\nEOF"), ("cat <<E'O'F\n", []))

    def test_expanded_mixed_delimiters_collect_only_unquoted(self):
        self.assertEqual(
            self.collect("cat <<'A' <<B\naaa\nA\nbbb\nB"),
            ("cat <<'A' <<B\n", ["bbb\nB"]))

    def test_expanded_command_after_collected_body_survives(self):
        self.assertEqual(
            self.collect("cat <<EOF\nbody\nEOF\ncat x"),
            ("cat <<EOF\ncat x", ["body\nEOF\n"]))

    def test_expanded_unterminated_body(self):
        self.assertEqual(
            self.collect("cat <<EOF\nbody line\nno terminator"),
            ("cat <<EOF\n", ["body line\nno terminator"]))

    def test_expanded_omitted_still_strips(self):
        # Q50: the body never stays in the returned string, so an odd quote in
        # it cannot color the scan of what follows.
        self.assertEqual(
            guard.strip_heredoc_bodies("cat <<EOF\ndon't\nEOF\ncat x"),
            "cat <<EOF\ncat x")

    # --- a substitution opens a fresh quote context (issue 169) -------------

    def test_heredoc_inside_quoted_substitution_stripped(self):
        # `$(…)` re-opens quoting in bash, so this `<<` is unquoted to the
        # shell even with the enclosing `"` still open. Tracked flat, the body
        # survived and its lone `"` aborted the whole parse.
        self.assertEqual(
            guard.strip_heredoc_bodies(
                'git commit -F "$(cat <<\'M\'\nhe said "hi\nM\n)" && cat x'),
            'git commit -F "$(cat <<\'M\'\n)" && cat x')

    def test_heredoc_inside_quoted_backticks_stripped(self):
        self.assertEqual(
            guard.strip_heredoc_bodies(
                'git commit -F "`cat <<\'M\'\nhe said "hi\nM\n`" && cat x'),
            'git commit -F "`cat <<\'M\'\n`" && cat x')

    def test_subshell_paren_does_not_close_substitution(self):
        # The inner `)` closes the subshell; only the outer one ends the body.
        self.assertEqual(
            guard.strip_heredoc_bodies(
                'echo "$( (cat <<\'M\'\nbody "x\nM\n) )" && cat x'),
            'echo "$( (cat <<\'M\'\n) )" && cat x')

    def test_quoted_arithmetic_shift_still_not_heredoc(self):
        # `"$((1<<2))"` is a shift inside quotes — the fresh context must not
        # reach it, or the next line would be eaten as a body.
        self.assertEqual(
            guard.strip_heredoc_bodies('echo "$((1<<2))"\ncat x'),
            'echo "$((1<<2))"\ncat x')

    def test_single_quoted_double_less_in_substitution_not_heredoc(self):
        self.assertEqual(
            guard.strip_heredoc_bodies('echo "$(grep -c \'<<\' f)"\ncat x'),
            'echo "$(grep -c \'<<\' f)"\ncat x')

    def test_nested_quotes_in_substitution_not_heredoc(self):
        self.assertEqual(
            guard.strip_heredoc_bodies('echo "$(echo "a<<b")"\ncat x'),
            'echo "$(echo "a<<b")"\ncat x')

    def test_expanded_collects_body_inside_quoted_substitution(self):
        # An unquoted delimiter is expanded wherever it sits, so the nested
        # body still reaches the command-substitution scan (Q35).
        self.assertEqual(
            self.collect('echo "$(cat <<M\n$(id)\nM\n)"'),
            ('echo "$(cat <<M\n)"', ["$(id)\nM\n"]))


class GlueDollarParenTests(unittest.TestCase):
    """`glue_dollar_paren` re-attaches `(` to a preceding `$` so `$(...)`
    reads as a runtime expansion, not a bare literal `$` filename (Q60)."""

    def test_dollar_paren_glued(self):
        self.assertEqual(
            guard.glue_dollar_paren(["cat", "$", "(", "echo", "x", ")"]),
            ["cat", "$(", "(", "echo", "x", ")"])

    def test_bare_dollar_not_glued_without_paren(self):
        self.assertEqual(
            guard.glue_dollar_paren(["grep", "foo", "bar", "$"]),
            ["grep", "foo", "bar", "$"])

    def test_subshell_paren_not_glued_to_word(self):
        # A `(` after a normal word (subshell group) is left alone.
        self.assertEqual(
            guard.glue_dollar_paren(["(", "cat", "f", ")"]),
            ["(", "cat", "f", ")"])


class CommandSubstitutionsTests(unittest.TestCase):
    """`command_substitutions` extracts the substitution bodies bash evaluates —
    `$(…)` and backtick — in unquoted/double-quoted context, skipping
    single-quoted literals and `$((…))` arithmetic (Q33)."""

    def test_double_quoted_dollar_paren(self):
        self.assertEqual(
            guard.command_substitutions('echo "$(mktemp)"'), ["mktemp"])

    def test_bare_dollar_paren(self):
        self.assertEqual(
            guard.command_substitutions("cat $(mktemp)"), ["mktemp"])

    def test_backtick(self):
        self.assertEqual(
            guard.command_substitutions("x=`mktemp -d`"), ["mktemp -d"])

    def test_single_quoted_skipped(self):
        # bash performs no substitution inside single quotes.
        self.assertEqual(
            guard.command_substitutions("echo '$(mktemp)'"), [])

    def test_backtick_in_single_quotes_skipped(self):
        self.assertEqual(
            guard.command_substitutions("echo '`mktemp`'"), [])

    def test_arithmetic_skipped(self):
        # `$((…))` is arithmetic expansion, not a command.
        self.assertEqual(
            guard.command_substitutions('echo "$((1 + 2))"'), [])

    def test_nested_returns_outermost_only(self):
        # The caller recurses into the returned body to find the inner one.
        self.assertEqual(
            guard.command_substitutions('echo "$(echo "$(cat f)")"'),
            ['echo "$(cat f)"'])

    def test_inner_double_quotes_in_body(self):
        self.assertEqual(
            guard.command_substitutions('echo "$(grep "a b" f)"'),
            ['grep "a b" f'])

    def test_unterminated_dollar_paren_yields_nothing(self):
        self.assertEqual(
            guard.command_substitutions('echo "$(cat f'), [])

    def test_unterminated_backtick_yields_nothing(self):
        self.assertEqual(guard.command_substitutions("echo `cat f"), [])

    def test_escaped_dollar_paren_not_a_substitution(self):
        # A backslash-escaped `$` is a literal, not a substitution.
        self.assertEqual(
            guard.command_substitutions(r'echo \$(mktemp)'), [])

    def test_two_substitutions(self):
        self.assertEqual(
            guard.command_substitutions('echo "$(a)" `b`'), ["a", "b"])

    def test_no_substitution(self):
        self.assertEqual(guard.command_substitutions("cat foo bar"), [])

    # --- quotes=False: how bash reads a heredoc body (Q50) -------------------

    def test_quotes_off_apostrophe_does_not_hide_substitution(self):
        self.assertEqual(
            guard.command_substitutions("don't $(cat f)", quotes=False),
            ["cat f"])

    def test_quotes_off_single_quoted_substitution_is_live(self):
        self.assertEqual(
            guard.command_substitutions("'$(cat f)'", quotes=False), ["cat f"])

    def test_quotes_off_odd_double_quote_does_not_hide_substitution(self):
        self.assertEqual(
            guard.command_substitutions('say "hi $(cat f)', quotes=False),
            ["cat f"])

    def test_quotes_off_backslash_still_escapes(self):
        self.assertEqual(
            guard.command_substitutions(r"don't \$(cat f)", quotes=False), [])

    def test_quotes_off_backtick_still_found(self):
        self.assertEqual(
            guard.command_substitutions("don't `cat f`", quotes=False),
            ["cat f"])

    def test_quotes_off_arithmetic_still_skipped(self):
        self.assertEqual(
            guard.command_substitutions("don't $((1 + 2))", quotes=False), [])


class QuotedSubstBodyEndToEndTests(unittest.TestCase):
    """A guarded command hidden in a quoted `"$(…)"` or backtick substitution is
    now parsed and its file ops flagged — closing the gap that the bare `$(…)`
    subshell split already covered (Q33). Substitution analysis only ever ADDS
    offenders: a clean body never turns a deferring outer command into `allow`,
    and a single-quoted literal stays a defer."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _decision(self, cmd, expected):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {cmd!r}; got {got!r} "
            f"(reason: {out['hookSpecificOutput'].get('permissionDecisionReason')!r})",
        )
        return out

    def _defer(self, cmd):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        self.assertIsNone(out, f"expected defer for {cmd!r}, got {out!r}")

    def test_quoted_mktemp_in_unguarded_echo_denies(self):
        # echo is unguarded, so pre-fix the inner mktemp host-temp write was
        # invisible and the whole string deferred.
        self._decision('echo "$(mktemp -p /tmp q33.XXXX)"', "deny")

    def test_quoted_mktemp_assignment_denies(self):
        self._decision('x="$(mktemp)"', "deny")

    def test_backtick_mktemp_assignment_denies(self):
        self._decision("x=`mktemp`", "deny")

    def test_backtick_outside_read_asks(self):
        out = self._decision("echo `cat /etc/q33-fake-target`", "ask")
        self.assertIn("/etc/q33-fake-target",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_quoted_grep_outside_read_names_inner_path(self):
        out = self._decision('cat "$(grep foo /etc/q33-fake-target)"', "ask")
        self.assertIn("/etc/q33-fake-target",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_redirect_inside_subst_body_flagged(self):
        out = self._decision(
            'echo "$(cat in.txt > /etc/q33-fake-target)"', "ask")
        self.assertIn("/etc/q33-fake-target",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_nested_substitution_outside_read_asks(self):
        out = self._decision(
            'echo "$(echo "$(cat /etc/q33-fake-target)")"', "ask")
        self.assertIn("/etc/q33-fake-target",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_single_quoted_literal_defers(self):
        # bash does NOT substitute inside single quotes — the body is a literal
        # string, so no command runs and the hook must not fabricate an offender.
        self._defer("echo '$(mktemp -p /tmp q33.XXXX)'")

    def test_clean_in_workspace_subst_does_not_allow(self):
        # A clean guarded command inside a substitution must NOT flip the
        # deferring outer echo into an `allow` (substitution guarded is dropped).
        self._defer('echo "$(cat in.txt)"')

    def test_arithmetic_expansion_defers(self):
        self._defer('echo "$((1 + 2))"')


class SubstBodyVarPropagationTests(unittest.TestCase):
    """Q66: a substitution body inherits the string's literal variables.

    `f=in.txt; echo "$(cat "$f")"` asked on an unresolvable `$f` where the same
    command without the `$( )` allowed. The body scan runs once for the whole
    string, so it gets only the names holding one literal throughout — a
    reassigned or poisoned name stays unresolvable and keeps its `ask`.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _defer(self, cmd):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        self.assertIsNone(out, f"expected defer for {cmd!r}, got {out!r}")

    def _asks_about(self, cmd, path):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(got, "ask", f"expected ask for {cmd!r}; got {got!r}")
        self.assertIn(path, out["hookSpecificOutput"]["permissionDecisionReason"])

    # --- the motivating shapes stop prompting --------------------------------

    def test_quoted_subst_resolves_tracked_var(self):
        self._defer('f=in.txt; echo "$(cat "$f")"')

    def test_backtick_subst_resolves_tracked_var(self):
        self._defer('f=in.txt; echo "`cat "$f"`"')

    def test_var_holding_a_directory_prefix_resolves(self):
        self._defer('d=.; echo "$(cat $d/in.txt)"')

    def test_nested_subst_inherits_too(self):
        # `allow` rather than the defer the single-level case gets: the
        # alternating quotes of a nested substitution let shlex split the inner
        # `cat` out as a top-level group, so its `guarded` is not the
        # substitution recursion's to drop. Pre-fix this asked on `$f`.
        out = run_hook('f=in.txt; echo "$(echo "$(cat "$f")")"',
                       self.workspace, project_dir=self.workspace)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_nested_subst_outside_value_still_asks(self):
        self._asks_about('f=/etc/q66-fake-target; echo "$(echo "$(cat "$f")")"',
                         "/etc/q66-fake-target")

    # --- an outside value still blocks ---------------------------------------

    def test_outside_value_inside_subst_asks(self):
        self._asks_about('f=/etc/q66-fake-target; echo "$(cat "$f")"',
                         "/etc/q66-fake-target")

    # --- only string-wide stable names carry in ------------------------------

    def test_later_reassignment_does_not_launder_the_earlier_value(self):
        # The body runs while `f` still holds the outside path; substituting the
        # later in-workspace literal would drop the offender entirely. Being
        # unstable, `f` is not substituted at all, so the block survives as the
        # generic runtime-expanded `ask` rather than one naming the path.
        self._asks_about('f=/etc/q66-fake-target; echo "$(cat "$f")"; f=in.txt',
                         "$f")

    def test_reassignment_leaves_the_var_unresolvable(self):
        # Both values are in-workspace, so nothing is hidden — but the name is
        # no longer stable, so the body keeps the runtime-expanded `ask`.
        self._asks_about('f=in.txt; echo "$(cat "$f")"; f=other.txt', "$f")

    def test_poisoned_var_stays_unresolvable(self):
        self._asks_about('f=in.txt; read f; echo "$(cat "$f")"', "$f")

    def test_ifs_clobber_stops_propagation_into_the_body(self):
        self._asks_about('f=in.txt; IFS=:; echo "$(cat "$f")"', "$f")

    def test_pipeline_segment_assignment_does_not_reach_the_body(self):
        # `f=…` in a pipeline segment runs in a subshell, so bash never sets it
        # for the substitution that follows.
        self._asks_about('true | f=in.txt; echo "$(cat "$f")"', "$f")

    def test_single_quoted_body_is_still_a_literal(self):
        self._defer("f=/etc/q66-fake-target; echo '$(cat \"$f\")'")

    # --- heredoc bodies get the map too, now that Q67 keeps it alive ---------

    def test_expanded_heredoc_body_subst_resolves_the_var(self):
        self._asks_about('f=/etc/q66-fake-target\ncat <<EOF\n$(cat "$f")\nEOF',
                         "/etc/q66-fake-target")

    def test_quoted_heredoc_body_stays_literal(self):
        # A `<<'EOF'` body is data to bash, so its `$(…)` never runs and the
        # seeded map must not conjure an offender out of it.
        out = run_hook("f=/etc/q66-fake-target\ncat <<'EOF'\n$(cat \"$f\")\nEOF",
                       self.workspace, project_dir=self.workspace)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")


class SubstDepthCapTests(unittest.TestCase):
    """`MAX_SUBST_DEPTH` bounds the substitution recursion (Q63).

    It went unenforced for as long as `_analyze_command` read the group loop's
    paren-nesting counter — which shadowed the `depth` parameter and is 0 for
    any balanced command — so every level recursed as depth 1 and the cap never
    fired. Deep enough nesting then exhausted Python's own stack, and a hook
    that dies mid-decision is a hook Claude Code treats as a non-blocking
    error: the guard would enforce nothing at all."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _nest(levels, inner="cat /etc/q63-fake-target"):
        for _ in range(levels):
            inner = 'echo "$(%s)"' % inner
        return inner

    def test_recursion_stops_at_the_cap(self):
        # Pre-fix this maxed out at 1 for every input; the assertion below is
        # two-sided so neither the shadowing bug nor a runaway cap slips back.
        depths = []
        real = guard._analyze_command

        def spy(cmd, ctx, base_cwd, depth=0, *a, **kw):
            depths.append(depth)
            return real(cmd, ctx, base_cwd, depth, *a, **kw)

        ctx = guard.build_context(
            {"cwd": self.workspace, "tool_input": {}})
        with mock.patch.object(guard, "_analyze_command", spy):
            guard.analyze_command(self._nest(60), ctx, self.workspace)
        self.assertEqual(max(depths), guard.MAX_SUBST_DEPTH)

    def test_shallow_nesting_recurses_one_level_per_substitution(self):
        depths = []
        real = guard._analyze_command

        def spy(cmd, ctx, base_cwd, depth=0, *a, **kw):
            depths.append(depth)
            return real(cmd, ctx, base_cwd, depth, *a, **kw)

        ctx = guard.build_context(
            {"cwd": self.workspace, "tool_input": {}})
        with mock.patch.object(guard, "_analyze_command", spy):
            guard.analyze_command(self._nest(5), ctx, self.workspace)
        self.assertEqual(max(depths), 5)

    def test_pathological_nesting_does_not_kill_the_hook(self):
        # 1000 levels overflowed the interpreter stack pre-fix, so the hook
        # exited non-zero with no decision at all. `run_hook` raises on a
        # non-zero exit, which is what fails this test if the cap regresses.
        out = run_hook(self._nest(1000), self.workspace,
                       project_dir=self.workspace)
        self.assertIsNotNone(out)

    def test_capping_recursion_does_not_hide_the_inner_read(self):
        # The cap stops the recursion, not the analysis: shlex does not track
        # quote nesting through `$(…)`, so the innermost command still surfaces
        # in an outer level's token stream and is flagged there.
        out = run_hook(self._nest(60), self.workspace,
                       project_dir=self.workspace)
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("/etc/q63-fake-target",
                      out["hookSpecificOutput"]["permissionDecisionReason"])


class ExpansionRegexTests(unittest.TestCase):
    """EXPANSION_RE distinguishes a real `$`-expansion from a literal `$`."""

    def test_variables_are_expansions(self):
        for tok in ("$HOME", "${HOME}", "$1", "$?", "$@", "$(cmd)", "a/$x"):
            self.assertTrue(guard.EXPANSION_RE.search(tok), tok)

    def test_literal_dollars_not_expansions(self):
        for tok in ("$", "foo$", "a$.b", "price$", "$/tmp"):
            self.assertFalse(guard.EXPANSION_RE.search(tok), tok)


class Issue60EndToEndTests(unittest.TestCase):
    """False-positive tokens from issue #60 no longer prompt, while every
    real outside-workspace read/write in the same shapes still does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _decision(self, cmd, expected):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {cmd!r}; got {got!r} "
            f"(reason: {out['hookSpecificOutput'].get('permissionDecisionReason')!r})",
        )
        return out

    # --- assignment word / PIPESTATUS after a trailing comment --------------

    def test_pipestatus_after_comment_line_allow(self):
        # The reported case: `tee log # note` then a newline then an
        # assignment word. Pre-fix shlex swallowed the newline, merging
        # `EXIT=${PIPESTATUS[0]}` into the tee group as a runtime-expanded arg.
        self._decision(
            "make test 2>&1 | tee build.log # show\nEXIT=${PIPESTATUS[0]}",
            "allow")

    # --- HTML/SVG heredoc body content --------------------------------------

    def test_heredoc_html_body_not_flagged_allow(self):
        # `</div>` etc. in a heredoc body previously parsed as a `<` redirect
        # whose target `/div` looked like an outside path.
        self._decision(
            "cat > page.html <<'EOF'\n<div>hi</div>\n<script>x</script>\nEOF",
            "allow")

    def test_heredoc_svg_body_not_flagged_allow(self):
        self._decision(
            "cat > i.svg <<'EOF'\n<svg><defs><radialGradient/></defs></svg>\nEOF",
            "allow")

    # --- prose in echo/log strings after a comment --------------------------

    def test_echo_prose_after_comment_allow(self):
        self._decision(
            'grep -q ready in.txt # check\necho "worker pod: ${pod:-none}"',
            "allow")

    # --- bare / literal dollar ----------------------------------------------

    def test_lone_dollar_arg_allow(self):
        self._decision("grep foo in.txt $", "allow")

    # --- security preserved: real outside targets in the same shapes --------

    def test_outside_read_with_trailing_comment_ask(self):
        out = self._decision("cat /etc/q60-fake # peek", "ask")
        self.assertIn("/etc/q60-fake",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_guarded_after_commented_line_ask(self):
        # Comment line must not hide a following guarded outside read.
        self._decision("echo note # x\ncat /etc/q60-fake", "ask")

    def test_heredoc_line_redirect_outside_ask(self):
        # The heredoc body is skipped, but a redirect on the command line to
        # an outside path is still checked.
        self._decision("cat <<EOF > /etc/q60-fake\nbody\nEOF", "ask")

    def test_command_after_heredoc_outside_ask(self):
        self._decision(
            "cat > f.txt <<EOF\nbody\nEOF\ncat /etc/q60-fake", "ask")

    def test_command_substitution_still_conservative_ask(self):
        # `$(...)` is unresolvable — must stay ask, not slip through as a
        # literal `$`.
        self._decision("cat $(echo /etc/q60-fake)", "ask")

    def test_quoted_hash_pattern_outside_ask(self):
        # A `#` inside quotes is a pattern char, not a comment — the outside
        # file after it must still be checked.
        self._decision("grep '#include' /etc/q60-fake", "ask")


class Issue83HeredocEndToEndTests(unittest.TestCase):
    """Heredoc bodies are stripped from the raw string before shlex, so body
    content — even an unbalanced quote that would abort the parse — never hides
    a real outside-workspace redirect on the command line (issue 83)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _decision(self, cmd, expected):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {cmd!r}; got {got!r} "
            f"(reason: {out['hookSpecificOutput'].get('permissionDecisionReason')!r})",
        )
        return out

    def test_apostrophe_body_does_not_hide_outside_redirect(self):
        # The core bug: an apostrophe in the body made shlex abort with
        # ValueError, so the whole command deferred and the outside redirect
        # target went unchecked. Now the body is gone before shlex sees it.
        self._decision(
            "cat <<'EOF' > /etc/q83-fake\ndon't do this\nEOF", "ask")

    def test_apostrophe_body_safe_target_allow(self):
        # Same body, but writing to the workspace (stdout here) — allow.
        self._decision("cat <<'EOF'\ndon't do this\nEOF", "allow")

    def test_unbalanced_paren_body_does_not_hide_outside_redirect(self):
        self._decision(
            "cat <<EOF > /etc/q83-fake\nfunc foo( {\nEOF", "ask")

    def test_html_body_workspace_target_allow(self):
        self._decision(
            "cat > page.html <<'EOF'\n<div>x</div>\n<a href=\"/x\">\nEOF", "allow")

    def test_arithmetic_shift_no_longer_arms_delimiter(self):
        # `$((1<<2))` is a shift, not a heredoc — the following outside read must
        # be checked, not swallowed as a bogus body.
        self._decision("echo $((1<<2))\ncat /etc/q83-fake", "ask")

    def test_double_paren_arithmetic_shift_checks_following(self):
        self._decision("((1<<2))\ncat /etc/q83-fake", "ask")

    def test_double_less_in_comment_does_not_arm(self):
        # `<<EOF` inside a trailing comment must not swallow the next command.
        self._decision("echo hi # <<EOF\ncat /etc/q83-fake", "ask")

    def test_tab_strip_heredoc_outside_redirect_ask(self):
        self._decision("cat <<-EOF > /etc/q83-fake\n\thi\n\tEOF", "ask")

    def test_multiple_heredocs_then_outside_read_ask(self):
        self._decision(
            "cat <<A <<B\naaa\nA\nbbb\nB\ncat /etc/q83-fake", "ask")

    # --- `$(…)` in a body: expanded only under an unquoted delimiter (Q35) ---

    def test_substitution_in_quoted_delimiter_body_allow(self):
        # A quoted delimiter makes the body literal text, so the `$(…)` never
        # runs — flagging its outside read was a spurious prompt.
        self._decision(
            "cat > doc.md <<'EOF'\nrun $(cat /etc/q35-fake) to see\nEOF", "allow")

    def test_substitution_in_backslash_delimiter_body_allow(self):
        self._decision(
            "cat > doc.md <<\\EOF\nrun $(cat /etc/q35-fake)\nEOF", "allow")

    def test_substitution_in_unquoted_delimiter_body_ask(self):
        # No quoting: bash expands the body, so the read is real.
        out = self._decision(
            "cat > doc.md <<EOF\nrun $(cat /etc/q35-fake)\nEOF", "ask")
        self.assertIn("/etc/q35-fake",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_backtick_in_quoted_delimiter_body_allow(self):
        self._decision(
            "cat > doc.md <<'EOF'\nrun `cat /etc/q35-fake`\nEOF", "allow")

    def test_substitution_on_quoted_heredoc_command_line_still_ask(self):
        # Only the BODY is literal; the command line around it still expands.
        self._decision(
            "cat > \"$(cat /etc/q35-fake)\" <<'EOF'\nplain\nEOF", "ask")

    def test_substitution_after_quoted_heredoc_still_ask(self):
        self._decision(
            "cat <<'EOF'\n$(true)\nEOF\necho $(cat /etc/q35-fake)", "ask")

    # --- an odd quote in an expanded body colors nothing (Q50) --------------

    def test_apostrophe_before_substitution_in_body_ask(self):
        # A heredoc body carries no quoting, so the `'` in `don't` is text. Read
        # inline it opened a quoted run that swallowed the live `$(…)` after it.
        out = self._decision(
            "cat > doc.md <<EOF\ndon't run $(cat /etc/q50-fake)\nEOF", "ask")
        self.assertIn("/etc/q50-fake",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_apostrophe_in_body_then_substitution_on_later_line_ask(self):
        # The body's odd quote must not reach the command line that follows it.
        self._decision(
            "cat <<EOF\ndon't\nEOF\necho $(cat /etc/q50-fake)", "ask")

    def test_odd_double_quote_before_substitution_in_body_ask(self):
        self._decision(
            'cat <<EOF\nsay "hi\n$(cat /etc/q50-fake)\nEOF', "ask")

    def test_apostrophe_before_backtick_in_body_ask(self):
        self._decision(
            "cat <<EOF\ndon't\n`cat /etc/q50-fake`\nEOF", "ask")

    def test_escaped_substitution_in_body_after_apostrophe_allow(self):
        # `\$(` is quoted even in a body — bash writes it literally, so the
        # quote-inert scan must still honour the backslash.
        self._decision(
            "cat > doc.md <<EOF\ndon't run \\$(cat /etc/q50-fake)\nEOF", "allow")

    # --- a heredoc in a quoted substitution hides nothing (issue 169) -------

    def test_odd_quote_in_nested_body_still_checks_later_command(self):
        # The shape of a multi-paragraph commit message. The body's lone `"`
        # aborted shlex, so the whole command deferred and the `cp` target went
        # unchecked — a silent pass on an outside-workspace write.
        out = self._decision(
            'git commit -aqF "$(cat <<\'MSG\'\nhe said "hello\nMSG\n)"'
            " && cp notes.md /etc/q169-fake", "ask")
        self.assertIn("/etc/q169-fake",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_odd_quote_in_nested_backtick_body_still_checks(self):
        self._decision(
            'git commit -aqF "`cat <<\'MSG\'\nhe said "hello\nMSG\n`"'
            " && cp notes.md /etc/q169-fake", "ask")

    def test_odd_quote_in_nested_subshell_body_still_checks(self):
        self._decision(
            'echo "$( (cat <<\'MSG\'\nhe said "hello\nMSG\n) )"'
            " && cp notes.md /etc/q169-fake", "ask")

    def test_guarded_command_inside_the_substitution_still_checked(self):
        self._decision(
            'echo "$(cat /etc/q169-fake <<\'MSG\'\nhe said "hello\nMSG\n)"',
            "ask")

    def test_nested_heredoc_with_in_workspace_command_allow(self):
        self._decision(
            'git commit -aqF "$(cat <<\'MSG\'\nhe said "hello\nMSG\n)"'
            " && cat README.md", "allow")


class OffenderDisplayTests(unittest.TestCase):
    """Relative offender tokens are shown with their resolved landing path."""

    def test_relative_token_shows_resolved_path(self):
        self.assertEqual(
            guard.offender_display("notes.txt", "/outside/notes.txt"),
            "notes.txt -> /outside/notes.txt")

    def test_absolute_token_unchanged(self):
        # A leading slash is drive-relative on Windows, not absolute, so the
        # token has to be fully qualified for this branch to be the one under
        # test — resolving it is how the caller gets there anyway.
        tok = resolved_from(os.getcwd(), "/outside/notes.txt")
        self.assertEqual(guard.offender_display(tok, tok), tok)


class BuildReasonTests(unittest.TestCase):
    """The decision reason names offenders AND tailors the fix per category."""

    def test_outside_category_names_path_and_fix(self):
        # Issue 90: the fix must NOT suggest switching to the native
        # Read/Grep/Glob tools as a way to avoid the prompt — those are
        # guarded too (since 1.5.0), so an outside path prompts either way.
        r = guard.build_reason([("/etc/hosts", "outside")])
        self.assertIn("/etc/hosts", r)
        self.assertIn("inside the project root", r)
        self.assertIn("same check", r)
        self.assertNotIn("instead of bash", r)

    def test_expand_category_distinct_advice(self):
        # `~`/`$` tokens get the "write a literal path" advice, not the plain
        # outside-path advice — they may in fact land inside the root.
        r = guard.build_reason([("$HOME/.aws/credentials", "expand")])
        self.assertIn("$HOME/.aws/credentials", r)
        self.assertIn("literal path", r)
        self.assertNotIn("Outside-workspace path(s)", r)

    def test_untracked_category_mentions_cd(self):
        r = guard.build_reason([("data.txt", "untracked")])
        self.assertIn("data.txt", r)
        self.assertIn("cd", r)

    def test_categories_combine_in_stable_order(self):
        r = guard.build_reason([
            ("rel.txt", "untracked"),
            ("$X/y", "expand"),
            ("/etc/hosts", "outside"),
        ])
        # outside, then expand, then untracked — independent of input order.
        self.assertLess(r.index("/etc/hosts"), r.index("$X/y"))
        self.assertLess(r.index("$X/y"), r.index("rel.txt"))

    def test_tokens_deduplicated_and_sorted(self):
        r = guard.build_reason([
            ("/b", "outside"), ("/a", "outside"), ("/a", "outside"),
        ])
        self.assertEqual(r.count("/a"), 1)
        self.assertLess(r.index("/a"), r.index("/b"))

    def test_sibling_category_names_checkout_branch_and_fix(self):
        r = guard.build_reason([(
            "/repo/main/cmd/x.go", "sibling",
            {"root": "/repo/main", "branch": "main",
             "corrected": "/repo/wt/cmd/x.go"},
        )])
        self.assertIn("Sibling-checkout", r)
        self.assertIn("/repo/main", r)
        self.assertIn("on branch main", r)
        self.assertIn("/repo/wt/cmd/x.go", r)
        self.assertIn("WORKSPACE_GUARD_OVERRIDE", r)

    def test_sibling_override_wording_downgrades(self):
        r = guard.build_reason(
            [("/repo/main/x", "sibling",
              {"root": "/repo/main", "branch": "main",
               "corrected": "/repo/wt/x"})],
            override="deliberate")
        self.assertIn("prompting because", r)
        self.assertIn("deliberate", r)

    def test_kill_category_names_pattern_and_both_rewrites(self):
        r = guard.build_reason([(
            "pkill", "kill",
            {"cmd": "pkill", "pattern": "ginkgo", "root": "/repo/wt"},
        )])
        self.assertIn("Unanchored process kill", r)
        self.assertIn("ginkgo", r)
        self.assertIn("pgrep -fl", r)
        self.assertIn("/repo/wt", r)
        self.assertIn("WORKSPACE_GUARD_OVERRIDE", r)

    def test_kill_without_a_pattern_says_so(self):
        r = guard.build_reason([(
            "pkill", "kill", {"cmd": "pkill", "pattern": None, "root": "/repo/wt"},
        )])
        self.assertIn("no pattern at all", r)

    def test_kill_override_wording_downgrades(self):
        r = guard.build_reason(
            [("pkill", "kill",
              {"cmd": "pkill", "pattern": "ginkgo", "root": "/repo/wt"})],
            override="stuck harness")
        self.assertIn("prompting because", r)
        self.assertIn("stuck harness", r)


class ReasonAdviceEndToEndTests(unittest.TestCase):
    """The emitted reason carries actionable advice end-to-end (subprocess)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _reason(self, cmd):
        out = run_hook(cmd, self.workspace)
        self.assertIsNotNone(out, f"expected a decision for {cmd!r}")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_outside_path_reason_has_fix(self):
        r = self._reason("cat /etc/hosts")
        self.assertIn("/etc/hosts", r)
        self.assertIn("same check", r)

    def test_tilde_home_token_reason_uses_outside_advice(self):
        # Q19: `~/…` now expands to the home directory, which is outside this
        # tempdir workspace, so the offender lands in the 'outside' bucket (not
        # 'expand') and gets the outside-path advice.
        self.assertIsNotNone(guard.resolved_home(), "no home directory resolves")
        r = self._reason("cat ~/.ssh/id_rsa")
        self.assertIn("~/.ssh/id_rsa", r)
        self.assertIn("same check", r)

    def test_tilde_user_token_reason_uses_expand_advice(self):
        # `~user` isn't deterministically resolvable (needs a pwd lookup), so
        # it still defers to the runtime-expanded advice path.
        r = self._reason("cat ~someuser/secret")
        self.assertIn("~someuser/secret", r)
        self.assertIn("literal path", r)

    def test_dollar_token_reason_uses_expand_advice(self):
        r = self._reason("cat $HOME/.aws/credentials")
        self.assertIn("$HOME/.aws/credentials", r)
        self.assertIn("literal path", r)

    def test_untracked_cd_reason_mentions_cd(self):
        r = self._reason("popd && cat data.txt")
        self.assertIn("data.txt", r)
        self.assertIn("untracked cd", r)


class DenyAttributionTests(unittest.TestCase):
    """Deny reasons open with the guard's name; ask reasons stay bare.

    A deny is refused before the tool runs, so the reason text is the only
    trace of which hook blocked the command. The opener is a cross-guard
    convention (foreground-guard's friction report keys its deny attribution on
    it), so the contract regex is pinned here alongside the literal prefix.
    Every deny driver is exercised through `decide`, the one place that emits
    the pair, rather than through each handler's fixture.
    """

    # foreground-guard 0.5.1, scripts/friction-report.py: DENY_TEXT. The
    # optional `Error: ` is Claude Code's own wrapping, not part of our string.
    DENY_TEXT = re.compile(r'^(?:Error:\s*)?([a-z0-9-]+-guard):\s')

    SIBLING = ("/repo/main/x", "sibling",
               {"root": "/repo/main", "branch": "main",
                "corrected": "/repo/wt/x"})
    KILL = ("pkill", "kill",
            {"cmd": "pkill", "pattern": "ginkgo", "root": "/repo/wt"})
    OUTSIDE = ("/q5-fake-target", "outside", None)
    HOSTTEMP = ("/tmp/q5-fake-target", "hosttemp", None)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _decide(self, offenders, bypass=False, **ctx_kw):
        fields = dict(
            proj=self.workspace, cwd=self.workspace, session_id="",
            session_tmp_root=os.path.join(self.workspace, "no-such-tmp-root"),
            session_proj_dir=None, tmp_roots=(), tmp_allow=(),
            tmp_action="deny", read_prefixes=(), session_wt=None,
            override=None, kill_anchor=None)
        fields.update(ctx_kw)
        return guard.decide(offenders, guard.Ctx(**fields), bypass)

    def _assert_attributed(self, reason):
        m = self.DENY_TEXT.match(reason)
        self.assertIsNotNone(
            m, f"deny reason does not match the cross-guard opener: {reason!r}")
        self.assertEqual(m.group(1), "workspace-guard")
        self.assertEqual(reason.count(guard.DENY_ATTRIBUTION), 1)

    def test_bypass_outside_deny_is_attributed(self):
        decision, reason = self._decide([self.OUTSIDE], bypass=True)
        self.assertEqual(decision, "deny")
        self._assert_attributed(reason)

    def test_host_temp_deny_is_attributed(self):
        decision, reason = self._decide([self.HOSTTEMP])
        self.assertEqual(decision, "deny")
        self._assert_attributed(reason)

    def test_sibling_checkout_deny_is_attributed(self):
        decision, reason = self._decide([self.SIBLING])
        self.assertEqual(decision, "deny")
        self._assert_attributed(reason)

    def test_unanchored_kill_deny_is_attributed(self):
        decision, reason = self._decide([self.KILL])
        self.assertEqual(decision, "deny")
        self._assert_attributed(reason)

    def test_advice_survives_the_prefix(self):
        # The prefix leads; the categorized body and its fix follow unchanged.
        _, reason = self._decide([self.OUTSIDE], bypass=True)
        self.assertTrue(reason.startswith(
            guard.DENY_ATTRIBUTION + "Outside-workspace path(s): "), reason)
        self.assertIn("/q5-fake-target", reason)
        self.assertIn("Fix:", reason)

    def test_ask_reason_is_not_attributed(self):
        decision, reason = self._decide([self.OUTSIDE])
        self.assertEqual(decision, "ask")
        self.assertIsNone(self.DENY_TEXT.match(reason))
        self.assertTrue(reason.startswith("Outside-workspace path(s): "), reason)

    def test_host_temp_downgraded_to_ask_is_not_attributed(self):
        decision, reason = self._decide([self.HOSTTEMP], tmp_action="ask")
        self.assertEqual(decision, "ask")
        self.assertNotIn(guard.DENY_ATTRIBUTION, reason)

    def test_overridden_cross_workspace_ask_is_not_attributed(self):
        for offender in (self.SIBLING, self.KILL):
            decision, reason = self._decide([offender], override="deliberate")
            self.assertEqual(decision, "ask")
            self.assertNotIn(guard.DENY_ATTRIBUTION, reason)


class DenyAttributionEndToEndTests(unittest.TestCase):
    """The attributed reason reaches the emitted JSON (subprocess)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _out(self, cmd, expected, **kw):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace, **kw)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], expected, cmd)
        return hso["permissionDecisionReason"]

    def test_bypass_outside_deny(self):
        r = self._out("cat /q5-fake-target", "deny",
                      permission_mode="bypassPermissions")
        self.assertTrue(r.startswith("workspace-guard: "), r)

    def test_host_temp_deny(self):
        r = self._out("cat /tmp/q5-fake-target", "deny")
        self.assertTrue(r.startswith("workspace-guard: "), r)

    def test_unanchored_kill_deny(self):
        r = self._out("pkill -f ginkgo", "deny")
        self.assertTrue(r.startswith("workspace-guard: "), r)

    def test_outside_ask_stays_bare(self):
        r = self._out("cat /q5-fake-target", "ask")
        self.assertFalse(r.startswith("workspace-guard: "), r)


class HostTempHelperTests(unittest.TestCase):
    """Unit coverage for the host-temp classification helpers."""

    def test_path_at_or_under_boundary(self):
        # Callers pass realpaths, which carry the platform separator.
        self.assertTrue(guard.path_at_or_under(native("/tmp"), native("/tmp")))
        self.assertTrue(guard.path_at_or_under(native("/tmp/x"), native("/tmp")))
        self.assertTrue(guard.path_at_or_under(native("/tmp/a/b"), native("/tmp")))
        # Sibling lookalikes must NOT match (the os.sep boundary).
        self.assertFalse(guard.path_at_or_under(native("/tmpfoo"), native("/tmp")))
        self.assertFalse(guard.path_at_or_under(native("/tmpfs/x"), native("/tmp")))
        self.assertFalse(
            guard.path_at_or_under(native("/var/tmpx"), native("/var/tmp")))

    def test_is_host_temp_with_explicit_roots(self):
        roots = {native("/tmp"), native("/var/tmp")}
        self.assertTrue(guard.is_host_temp(native("/tmp/out"), roots))
        self.assertTrue(guard.is_host_temp(native("/var/tmp/x"), roots))
        self.assertFalse(guard.is_host_temp(native("/etc/passwd"), roots))
        self.assertFalse(guard.is_host_temp(native("/tmpfoo/x"), roots))

    def test_split_pathlist_colon_and_comma(self):
        # `:` is the POSIX list separator but part of a Windows drive letter,
        # so the split is on os.pathsep — `;` there — plus a comma everywhere.
        self.assertEqual(
            guard._split_pathlist("/a%s/b,/c" % os.pathsep), ["/a", "/b", "/c"])
        self.assertEqual(guard._split_pathlist(""), [])
        self.assertEqual(guard._split_pathlist("  /a , , /b "), ["/a", "/b"])

    def test_matches_allowlist_exact_and_prefix(self):
        # Callers always pass an already-resolved realpath, so mirror that
        # (on macOS /tmp/ok -> /private/tmp/ok); the pattern stays user-written.
        base = os.getcwd()
        ok = resolved_from(base, "/tmp/ok")
        self.assertTrue(guard.matches_allowlist(ok, ["/tmp/ok"], base))
        self.assertTrue(
            guard.matches_allowlist(os.path.join(ok, "x"), ["/tmp/ok"], base))
        self.assertFalse(guard.matches_allowlist(
            resolved_from(base, "/tmp/nope"), ["/tmp/ok"], base))
        self.assertFalse(guard.matches_allowlist(ok, [], base))

    def test_matches_allowlist_glob(self):
        # The resolved realpath (possibly /private-prefixed on macOS) still
        # matches a user-written /tmp glob.
        base = os.getcwd()
        self.assertTrue(guard.matches_allowlist(
            resolved_from(base, "/tmp/build-123"), ["/tmp/build-*"], base))
        self.assertFalse(guard.matches_allowlist(
            resolved_from(base, "/tmp/other"), ["/tmp/build-*"], base))

    def test_host_temp_roots_includes_defaults(self):
        roots = guard.host_temp_roots(os.getcwd())
        # Defaults are always present (resolved), regardless of env.
        self.assertIn(os.path.realpath("/tmp"), roots)
        self.assertIn(os.path.realpath("/var/tmp"), roots)

    def test_host_temp_roots_includes_platform_temp_dir(self):
        # The directory this tier exists to catch. On POSIX it is
        # $TMPDIR-or-/tmp and already among the defaults; on Windows it is
        # %TMP%, which the POSIX names miss entirely.
        roots = guard.host_temp_roots(os.getcwd())
        self.assertIn(os.path.realpath(tempfile.gettempdir()), roots)

    def test_build_scratch_hint_present_vs_absent(self):
        with tempfile.TemporaryDirectory() as proj:
            # No scratch dir yet -> "create it" guidance.
            absent = guard.build_scratch_hint(proj, "tmp/")
            self.assertIn("Create a gitignored `tmp`", absent)
            self.assertIn(".gitignore", absent)
            self.assertIn("WORKSPACE_GUARD_TMP_ACTION=ask", absent)
            # Once present -> names it concretely.
            os.mkdir(os.path.join(proj, "tmp"))
            present = guard.build_scratch_hint(proj, "tmp/")
            self.assertIn("Use the repo-local scratch dir `./tmp/`", present)

    def test_build_scratch_hint_names_session_scratchpad(self):
        # Q56: the second legitimate destination. Absent by default so the
        # message never points at a directory the harness didn't create.
        with tempfile.TemporaryDirectory() as proj:
            self.assertNotIn("scratchpad",
                             guard.build_scratch_hint(proj, "tmp/"))
            pad = os.path.join(proj, "pad")
            hint = guard.build_scratch_hint(proj, "tmp/", pad)
            self.assertIn(pad, hint)
            self.assertIn("allowed read-write", hint)
            # Still steers to the repo-local dir first, knobs still last.
            self.assertLess(hint.index("tmp"), hint.index(pad))
            self.assertIn("WORKSPACE_GUARD_TMP_ALLOW", hint)

    def test_session_scratchpad_requires_an_existing_dir(self):
        with tempfile.TemporaryDirectory() as root:
            sess = "aaaaaaaa-1111-2222-3333-444444444444"
            self.assertIsNone(guard.session_scratchpad(sess, root))
            self.assertIsNone(guard.session_scratchpad("", root))
            self.assertIsNone(guard.session_scratchpad(sess, None))
            pad = os.path.join(root, sess, "scratchpad")
            os.makedirs(pad)
            self.assertEqual(guard.session_scratchpad(sess, root), pad)


class HostTempDenyTests(unittest.TestCase):
    """Host-wide temp (/tmp, /var/tmp, $TMPDIR) is denied (default) and steered
    to a repo-local scratch dir — reusing the same path extraction/resolution as
    the outside-workspace check, so text mentions and lookalike paths don't fire.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, cmd, **kw):
        return run_hook(cmd, self.workspace, project_dir=self.workspace, **kw)

    def _expect(self, cmd, expected, **kw):
        out = self._run(cmd, **kw)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {cmd!r}; got {got!r} "
            f"(reason: {out['hookSpecificOutput'].get('permissionDecisionReason')!r})")
        return out

    # --- DENY cases ---------------------------------------------------------

    def test_cat_tmp_deny(self):
        out = self._expect("cat /tmp/q-hosttemp-out", "deny")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("/tmp/q-hosttemp-out", reason)
        self.assertIn("./tmp/", reason)                 # steers to repo-local
        self.assertIn("WORKSPACE_GUARD_TMP_ACTION=ask", reason)

    def test_rm_quoted_tmp_deny(self):
        self._expect('rm "/tmp/q-hosttemp-x"', "deny")

    def test_var_tmp_deny(self):
        self._expect("cat /var/tmp/q-hosttemp-x", "deny")

    def test_sort_output_tmp_deny(self):
        # `-o /tmp/...` is a write target — host temp, denied.
        self._expect("sort -o /tmp/q-hosttemp-out in.txt", "deny")

    def test_redirect_to_tmp_deny(self):
        # Redirect target under /tmp, with a guarded command present.
        self._expect("cat in.txt > /tmp/q-hosttemp-log", "deny")

    def test_cd_tmp_then_relative_redirect_deny(self):
        # `cd /tmp && cat /dev/null > evil` -> /tmp/evil (host temp).
        self._expect("cd /tmp && cat /dev/null > evil", "deny")

    def test_mktemp_style_dest_under_tmp_deny(self):
        # `cp` into /tmp is the common "scratch file" pattern -> deny.
        self._expect("cp ./in.txt /tmp/q-hosttemp-copy", "deny")

    def test_tmpdir_resolved_path_deny(self):
        # macOS-style: $TMPDIR resolves under /var/folders/...; a path under it
        # is host temp. Simulated cross-platform via a custom TMPDIR root.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = os.path.realpath(tmpdir)
            target = os.path.join(tmpdir, "session-scratch")
            self._expect(f"cat {sh(target)}", "deny",
                         env_extra={"TMPDIR": tmpdir})

    # --- NO-MATCH cases (must NOT deny) -------------------------------------

    def test_repo_local_dot_tmp_allow(self):
        self._expect("cat ./tmp/out.txt", "allow")

    def test_relative_tmp_allow(self):
        self._expect("cat tmp/out.txt", "allow")

    def test_tmp_in_middle_of_relative_path_allow(self):
        self._expect("cat foo/tmp/bar", "allow")

    def test_tmpfs_lookalike_not_host_temp_ask(self):
        # `/tmpfs` is a different absolute path — outside, but NOT host temp,
        # so it asks (the generic outside decision), it does not deny.
        self._expect("cat /tmpfs/x", "ask")

    def test_tmpfoo_lookalike_not_host_temp_ask(self):
        self._expect("cat /tmpfoo/x", "ask")

    def test_home_tmp_not_host_temp_ask(self):
        # `~/tmp` expands to $HOME/tmp — outside this workspace but not under a
        # host-temp root, so it asks rather than denies.
        self._expect("cat ~/tmp", "ask")

    def test_url_with_tmp_component_not_a_path_allow(self):
        # A URL is not an absolute path token; it resolves cwd-relative and
        # lands in-workspace -> allow (never treated as host temp).
        self._expect("cat https://host/tmp/x", "allow")

    def test_tmp_as_grep_pattern_text_allow(self):
        # `/tmp` as the search *pattern* is not a file argument — the only file
        # is the in-workspace in.txt. No deny, no false positive.
        self._expect("grep /tmp in.txt", "allow")

    def test_tmp_as_sed_program_text_allow(self):
        self._expect("sed 's#/tmp#X#' in.txt", "allow")

    # --- config knobs -------------------------------------------------------

    def test_action_ask_softens_to_prompt(self):
        self._expect("cat /tmp/q-hosttemp-x", "ask",
                     env_extra={"WORKSPACE_GUARD_TMP_ACTION": "ask"})

    def test_unknown_action_falls_back_to_deny(self):
        self._expect("cat /tmp/q-hosttemp-x", "deny",
                     env_extra={"WORKSPACE_GUARD_TMP_ACTION": "bogus"})

    def test_allowlist_exact_path_escapes_to_allow(self):
        self._expect("cat /tmp/ok-scratch", "allow",
                     env_extra={"WORKSPACE_GUARD_TMP_ALLOW": "/tmp/ok-scratch"})

    def test_allowlist_does_not_exempt_other_tmp_paths(self):
        self._expect("cat /tmp/not-listed", "deny",
                     env_extra={"WORKSPACE_GUARD_TMP_ALLOW": "/tmp/ok-scratch"})

    def test_allowlist_glob_escapes_to_allow(self):
        self._expect("cat /tmp/build-42/log", "allow",
                     env_extra={"WORKSPACE_GUARD_TMP_ALLOW": "/tmp/build-*"})

    def test_extra_root_extends_deny(self):
        # An additional root makes a non-default location host temp too.
        with tempfile.TemporaryDirectory() as extra:
            extra = os.path.realpath(extra)
            target = os.path.join(extra, "x")
            self._expect(f"cat {sh(target)}", "deny",
                         env_extra={"WORKSPACE_GUARD_TMP_ROOTS": extra})

    def test_scratch_dir_name_config_in_reason(self):
        out = self._expect("cat /tmp/q-hosttemp-x", "deny",
                           env_extra={"WORKSPACE_GUARD_SCRATCH_DIR": "scratch/"})
        self.assertIn("./scratch/",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_present_scratch_dir_named_concretely(self):
        os.mkdir(os.path.join(self.workspace, "tmp"))
        out = self._expect("cat /tmp/q-hosttemp-x", "deny")
        self.assertIn("Use the repo-local scratch dir `./tmp/`",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    # --- interactions -------------------------------------------------------

    def test_claude_managed_other_session_stays_ask_not_deny(self):
        # Another session's task output under /tmp/claude-<uid> is a
        # cross-session decision for a human (`ask`), NOT the host-temp deny —
        # its "use ./tmp/" message would be wrong there.
        owner = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        current = "ffffffff-0000-1111-2222-333333333333"
        path = os.path.join(guard.claude_tmp_root(), "-Users-me-proj",
                            owner, "tasks", "abc.output")
        self._expect(f"cat {sh(path)}", "ask", session_id=current)

    def test_deny_message_names_this_sessions_scratchpad(self):
        # Q56: the deny steers to two destinations, not one — an agent told only
        # about `./tmp/` infers the harness scratchpad is off-limits too.
        sess = "5e551011-1111-2222-3333-444444444444"
        proj_dir = os.path.join(guard.claude_tmp_root(),
                                "-guardtest-q56-%d" % os.getpid())
        pad = os.path.join(proj_dir, sess, "scratchpad")
        try:
            os.makedirs(pad, exist_ok=True)
            out = self._expect("cat in.txt > /tmp/q56-hosttemp-log", "deny",
                               session_id=sess)
            reason = out["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn(pad, reason)
            self.assertIn("./tmp/", reason)     # still names the repo-local dir
        finally:
            shutil.rmtree(proj_dir, ignore_errors=True)

    def test_deny_message_omits_scratchpad_when_absent(self):
        # No session_id -> nothing to name; the message is unchanged.
        out = self._expect("cat in.txt > /tmp/q56-hosttemp-log", "deny")
        self.assertNotIn(
            "scratchpad", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unguarded_command_to_tmp_still_defers(self):
        # The capability only upgrades paths the hook already extracts. An
        # unguarded command (no SPEC row) is untouched, even targeting /tmp.
        out = self._run("ls /tmp/whatever")
        self.assertIsNone(out, f"expected defer, got {out!r}")

    def test_bare_redirect_from_unguarded_to_tmp_deny(self):
        # Q26: a redirect target is a shell-level write the hook resolves even
        # when the command word is unguarded, so `echo hi > /tmp/x` now denies
        # (host temp) instead of deferring.
        out = self._expect("echo hi > /tmp/whatever", "deny")
        self.assertIn("/tmp/whatever",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_printf_redirect_from_unguarded_to_tmp_deny(self):
        self._expect("printf hi > /tmp/q26-hosttemp-log", "deny")

    def test_cd_tmp_then_unguarded_redirect_deny(self):
        # cd into host temp + an unguarded command's relative redirect: the
        # target resolves to /tmp/out.txt via cwd tracking (Q26).
        out = self._expect("cd /tmp && echo x > out.txt", "deny")
        self.assertIn("out.txt",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    # --- mktemp (Q26) -------------------------------------------------------
    # mktemp's default location is host temp, so a bare/`-d`/`-t`/`-p /tmp`
    # invocation denies; an explicit in-workspace target allows.

    def test_mktemp_bare_default_deny(self):
        # Clear $TMPDIR so the default resolves to /tmp deterministically.
        out = self._expect("mktemp", "deny", env_extra={"TMPDIR": None})
        self.assertIn("/tmp",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_mktemp_directory_flag_default_deny(self):
        self._expect("mktemp -d", "deny", env_extra={"TMPDIR": None})

    def test_mktemp_dash_t_prefix_deny(self):
        # -t resolves to the default host-temp location on both GNU and BSD.
        self._expect("mktemp -t q26-prefix", "deny", env_extra={"TMPDIR": None})

    def test_mktemp_dash_p_tmp_deny(self):
        self._expect("mktemp -p /tmp q26.XXXXXX", "deny")

    def test_mktemp_tmpdir_inline_tmp_deny(self):
        self._expect("mktemp --tmpdir=/tmp q26.XXXXXX", "deny")

    def test_mktemp_slashed_template_under_tmp_deny(self):
        self._expect("mktemp /tmp/q26.XXXXXX", "deny")

    def test_mktemp_workspace_target_dir_allow(self):
        # -p into a repo-local dir is the steered-to pattern -> allow.
        self._expect("mktemp -p ./scratch q26.XXXXXX", "allow")

    def test_mktemp_workspace_slashed_template_allow(self):
        self._expect("mktemp ./q26.XXXXXX", "allow")

    def test_mktemp_cluster_dp_workspace_allow(self):
        # Q32: -dp ./scratch is -d -p ./scratch -> repo-local target -> allow
        # (previously false-denied because -dp was read as one unknown flag).
        self._expect("mktemp -dp ./scratch q32.XXXXXX", "allow")

    def test_mktemp_cluster_dp_tmp_deny(self):
        # -dp into host temp is still an explicit host-temp target -> deny.
        self._expect("mktemp -dp /tmp q32.XXXXXX", "deny")

    def test_mktemp_version_defers(self):
        # Informational invocation creates nothing -> defer to normal perms.
        out = self._run("mktemp --version")
        self.assertIsNone(out, f"expected defer, got {out!r}")

    # --- inline TMPDIR= relocates the default location (Q34) ----------------

    def test_mktemp_inline_tmpdir_workspace_allow(self):
        # `TMPDIR=./scratch mktemp` writes into the repo -> allow (pre-Q34 the
        # prefix was stripped and it false-denied to host temp).
        self._expect("TMPDIR=./scratch mktemp", "allow")

    def test_mktemp_inline_tmpdir_workspace_dir_flag_allow(self):
        self._expect("TMPDIR=./scratch mktemp -d", "allow")

    def test_mktemp_inline_tmpdir_workspace_dash_t_allow(self):
        # -t uses the (now repo-local) default location.
        self._expect("TMPDIR=./scratch mktemp -t q34-prefix", "allow")

    def test_mktemp_inline_tmpdir_host_temp_deny(self):
        self._expect("TMPDIR=/tmp/q34-fake mktemp", "deny")

    def test_mktemp_inline_tmpdir_unexpanded_deny(self):
        # A `$`-bearing value isn't a trusted literal -> host-temp default deny.
        self._expect("TMPDIR=$SOMEDIR mktemp", "deny", env_extra={"TMPDIR": None})

    def test_mktemp_inline_tmpdir_explicit_p_still_wins(self):
        # Explicit -p into host temp wins over the workspace TMPDIR= prefix.
        self._expect("TMPDIR=./scratch mktemp -p /tmp/q34-fake x.XX", "deny")


def _have_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


@unittest.skipUnless(_have_git(), "git not available")
class SiblingCheckoutTests(unittest.TestCase):
    """Worktree-aware sibling-checkout deny (issue 62).

    Builds a REAL git repo with linked worktrees (so the parser is exercised
    against git's actual on-disk metadata, not a hand-rolled mock) and asserts
    the Bash + Edit/Write deny behavior. The fixture lives under $HOME rather
    than a system tempdir so its paths are not classified as host-temp — that
    keeps the sibling decision cleanly separated from the /tmp deny.
    """

    def _git(self, args, cwd):
        env = os.environ.copy()
        # Isolate from the developer's global/system git config (templates,
        # hooksPath, signing) and supply a deterministic identity.
        env.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_TERMINAL_PROMPT": "0",
        })
        return subprocess.run(["git"] + args, cwd=cwd, env=env,
                              capture_output=True, text=True, check=True)

    def setUp(self):
        home = guard.resolved_home()                      # Q43: not $HOME
        self.assertTrue(home and os.path.isdir(home),
                        f"no home directory to build the fixture under: {home!r}")
        self._tmp = tempfile.TemporaryDirectory(dir=home)
        self.base = os.path.realpath(self._tmp.name)
        self.main = os.path.join(self.base, "main")
        os.mkdir(self.main)
        self._git(["init"], self.main)
        with open(os.path.join(self.main, "root.txt"), "w") as f:
            f.write("x\n")
        self._git(["add", "."], self.main)
        self._git(["commit", "-m", "init"], self.main)
        self.main_branch = self._git(
            ["rev-parse", "--abbrev-ref", "HEAD"], self.main).stdout.strip()
        self.wt = os.path.join(self.base, "wt-a")
        self._git(["worktree", "add", "-b", "feat-a", self.wt], self.main)
        self.other = os.path.join(self.base, "wt-b")
        self._git(["worktree", "add", "-b", "feat-b", self.other], self.main)
        self.main = os.path.realpath(self.main)
        self.wt = os.path.realpath(self.wt)
        self.other = os.path.realpath(self.other)
        # Links living OUTSIDE every checkout, standing in for the documented
        # `~/.claude/skills/<name> -> <repo>/<name>` install layout.
        self.links = os.path.join(self.base, "links")
        os.mkdir(self.links)
        os.symlink(os.path.join(self.main, "root.txt"),
                   os.path.join(self.links, "live"))
        os.symlink(os.path.join(self.main, "gone.txt"),
                   os.path.join(self.links, "dangling"))
        os.symlink(self.main, os.path.join(self.links, "dirlink"))
        # And one inside the session's own checkout, pointing at the sibling.
        os.symlink(os.path.join(self.main, "root.txt"),
                   os.path.join(self.wt, "inlink"))

    def tearDown(self):
        # Worktrees hold no locks once the subprocesses exit; plain cleanup is
        # enough (the whole tree is removed).
        self._tmp.cleanup()

    def _run(self, data, proj=None, cwd=None, env_extra=None):
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = proj or self.wt
        for k, v in (env_extra or {}).items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        data = dict(data)
        data.setdefault("cwd", cwd or self.wt)
        r = subprocess.run(
            [sys.executable, str(SCRIPT)], input=json.dumps(data),
            capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(r.returncode, 0, f"hook errored: {r.stderr!r}")
        out = r.stdout.strip()
        return json.loads(out) if out else None

    def _bash(self, cmd, **kw):
        return self._run({"tool_input": {"command": cmd}}, **kw)

    def _edit(self, tool, file_path, **kw):
        return self._run(
            {"tool_name": tool, "tool_input": {"file_path": file_path}}, **kw)

    def _decision(self, out):
        self.assertIsNotNone(out, "expected a decision, got defer")
        return out["hookSpecificOutput"]["permissionDecision"]

    def _reason(self, out):
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    # --- unit: detection helpers --------------------------------------------

    def test_resolve_session_worktree_in_worktree(self):
        s = guard.resolve_session_worktree(self.wt)
        self.assertIsNotNone(s)
        self.assertTrue(s["in_worktree"])
        self.assertEqual(s["root"], self.wt)
        self.assertEqual(s["common"], os.path.realpath(
            os.path.join(self.main, ".git")))

    def test_resolve_session_worktree_main_checkout_not_worktree(self):
        s = guard.resolve_session_worktree(self.main)
        self.assertIsNotNone(s)
        self.assertFalse(s["in_worktree"])

    def test_resolve_checkout_non_repo_returns_none(self):
        self.assertIsNone(guard._resolve_checkout(self.base))

    def test_sibling_checkout_for_primary(self):
        s = guard.resolve_session_worktree(self.wt)
        root, branch = guard.sibling_checkout_for(
            os.path.join(self.main, "cmd", "main.go"), s)
        self.assertEqual(root, self.main)
        self.assertEqual(branch, self.main_branch)

    def test_sibling_checkout_for_other_worktree(self):
        s = guard.resolve_session_worktree(self.wt)
        root, branch = guard.sibling_checkout_for(
            os.path.join(self.other, "x.py"), s)
        self.assertEqual(root, self.other)
        self.assertEqual(branch, "feat-b")

    def test_sibling_checkout_for_own_workspace_is_none(self):
        s = guard.resolve_session_worktree(self.wt)
        self.assertIsNone(
            guard.sibling_checkout_for(os.path.join(self.wt, "x"), s))

    def test_sibling_checkout_for_unrelated_repo_is_none(self):
        # A different git repo entirely -> different common-dir -> not a sibling.
        with tempfile.TemporaryDirectory(dir=self.base) as other_repo:
            other_repo = os.path.realpath(other_repo)
            self._git(["init"], other_repo)
            s = guard.resolve_session_worktree(self.wt)
            self.assertIsNone(guard.sibling_checkout_for(
                os.path.join(other_repo, "x"), s))

    def test_branch_label_reads_head(self):
        admin = os.path.realpath(os.path.join(self.main, ".git"))
        self.assertEqual(guard._branch_label(admin), self.main_branch)

    # --- Bash: writes into a sibling checkout deny --------------------------

    def test_bash_redirect_into_primary_deny(self):
        target = os.path.join(self.main, "root.txt")
        out = self._bash(f"cat /dev/null > {sh(target)}")
        self.assertEqual(self._decision(out), "deny")
        r = self._reason(out)
        self.assertIn("Sibling-checkout", r)
        self.assertIn(self.main, r)
        self.assertIn(self.main_branch, r)
        # Names the corrected in-session path (same relative path).
        self.assertIn(os.path.join(self.wt, "root.txt"), r)

    def test_bash_cp_into_other_worktree_deny(self):
        target = os.path.join(self.other, "copy.txt")
        out = self._bash(f"cp root.txt {sh(target)}")
        self.assertEqual(self._decision(out), "deny")
        self.assertIn("feat-b", self._reason(out))

    def test_bash_tee_into_primary_deny(self):
        out = self._bash(f"echo hi | tee {sh(os.path.join(self.main, 'log.txt'))}")
        self.assertEqual(self._decision(out), "deny")

    def test_bash_rm_in_sibling_deny(self):
        out = self._bash(f"rm -f {sh(os.path.join(self.main, 'root.txt'))}")
        self.assertEqual(self._decision(out), "deny")

    # --- Bash: an operand that IS a link is judged by the link ---------------
    # `rm link` unlinks the link and never writes the target, so resolving the
    # operand through to a sibling checkout denied a removal that lands nothing
    # on the wrong branch.

    def test_bash_rm_of_link_into_sibling_is_not_denied(self):
        out = self._bash(f"rm {sh(os.path.join(self.links, 'live'))}")
        self.assertEqual(self._decision(out), "ask")
        self.assertNotIn("Sibling-checkout", self._reason(out))

    def test_bash_rm_of_dangling_link_into_sibling_is_not_denied(self):
        # The target does not exist, so there is nothing there to protect.
        out = self._bash(f"rm {sh(os.path.join(self.links, 'dangling'))}")
        self.assertEqual(self._decision(out), "ask")
        self.assertNotIn("Sibling-checkout", self._reason(out))

    def test_bash_rm_of_link_to_sibling_checkout_root_is_not_denied(self):
        out = self._bash(f"rm {sh(os.path.join(self.links, 'dirlink'))}")
        self.assertEqual(self._decision(out), "ask")
        self.assertNotIn("Sibling-checkout", self._reason(out))

    def test_bash_mv_of_link_source_into_sibling_is_not_denied(self):
        src = os.path.join(self.links, "live")
        dst = os.path.join(self.links, "renamed")
        out = self._bash(f"mv {sh(src)} {sh(dst)}")
        self.assertEqual(self._decision(out), "ask")
        self.assertNotIn("Sibling-checkout", self._reason(out))

    def test_bash_rm_of_link_inside_own_checkout_is_allowed(self):
        # The link lives in this session's worktree; unlinking it is an
        # in-workspace write however far its target points.
        out = self._bash("rm ./inlink")
        self.assertEqual(self._decision(out), "allow")

    # The protection this must not weaken: a *directory* link in the path, with
    # a real file inside the sibling as the operand.
    def test_bash_rm_through_directory_link_still_denies(self):
        target = os.path.join(self.links, "dirlink", "root.txt")
        out = self._bash(f"rm {sh(target)}")
        self.assertEqual(self._decision(out), "deny")
        self.assertIn("Sibling-checkout", self._reason(out))

    def test_bash_mv_destination_still_follows_link_into_sibling(self):
        # `mv src dirlink/x` writes INTO the checkout the link names, so the
        # destination keeps resolving. Only `mv`'s sources are entry operands.
        dst = os.path.join(self.links, "dirlink", "copy.txt")
        out = self._bash(f"mv root.txt {sh(dst)}")
        self.assertEqual(self._decision(out), "deny")
        self.assertIn("Sibling-checkout", self._reason(out))

    # --- Bash: a checkout ROOT is inside its own checkout -------------------
    # The walk used to start at dirname(), which for a checkout root is one
    # level too high, so removing a whole sibling escaped the rule while
    # removing one file inside it was denied.

    def test_bash_rm_of_sibling_worktree_root_denies(self):
        out = self._bash(f"rm -rf {sh(self.other)}")
        self.assertEqual(self._decision(out), "deny")
        r = self._reason(out)
        self.assertIn("Sibling-checkout", r)
        self.assertIn("feat-b", r)

    def test_bash_rm_of_primary_checkout_root_denies(self):
        out = self._bash(f"rm -rf {sh(self.main)}")
        self.assertEqual(self._decision(out), "deny")
        self.assertIn(self.main_branch, self._reason(out))

    def test_bash_cp_into_sibling_checkout_root_denies(self):
        # Same walk change, reached by a content operand: the destination names
        # the checkout directory itself.
        out = self._bash(f"cp root.txt {sh(self.other)}")
        self.assertEqual(self._decision(out), "deny")
        self.assertIn("feat-b", self._reason(out))

    # --- Bash: reads keep today's behavior (ask, not deny) ------------------

    def test_bash_read_of_sibling_asks_not_deny(self):
        out = self._bash(f"cat {sh(os.path.join(self.main, 'root.txt'))}")
        self.assertEqual(self._decision(out), "ask")
        self.assertNotIn("Sibling-checkout", self._reason(out))

    def test_bash_grep_of_sibling_asks(self):
        out = self._bash(f"grep x {sh(os.path.join(self.other, 'root.txt'))}")
        self.assertEqual(self._decision(out), "ask")

    # --- Bash: override downgrades to ask -----------------------------------

    def test_bash_override_downgrades_deny_to_ask(self):
        target = os.path.join(self.main, "root.txt")
        out = self._bash(f"cat /dev/null > {sh(target)}",
                         env_extra={"WORKSPACE_GUARD_OVERRIDE": "deliberate sync"})
        self.assertEqual(self._decision(out), "ask")
        r = self._reason(out)
        self.assertIn("WORKSPACE_GUARD_OVERRIDE", r)
        self.assertIn("deliberate sync", r)

    # --- Bash: no-op when the session isn't in a worktree -------------------

    def test_bash_main_session_write_into_worktree_is_ask_not_deny(self):
        # Session is the main checkout (not a worktree): sibling detection is a
        # no-op, so a write into a linked worktree gets the generic outside ask.
        target = os.path.join(self.other, "x.txt")
        out = self._bash(f"cat /dev/null > {sh(target)}",
                         proj=self.main, cwd=self.main)
        self.assertEqual(self._decision(out), "ask")
        self.assertNotIn("Sibling-checkout", self._reason(out))

    # --- Edit/Write/MultiEdit/NotebookEdit ----------------------------------

    def test_edit_into_primary_deny(self):
        target = os.path.join(self.main, "cmd", "main.go")
        out = self._edit("Edit", target)
        self.assertEqual(self._decision(out), "deny")
        r = self._reason(out)
        self.assertIn(self.main_branch, r)
        self.assertIn(os.path.join(self.wt, "cmd", "main.go"), r)

    def test_write_into_other_worktree_deny(self):
        out = self._edit("Write", os.path.join(self.other, "new.txt"))
        self.assertEqual(self._decision(out), "deny")
        self.assertIn("feat-b", self._reason(out))

    def test_multiedit_into_primary_deny(self):
        out = self._edit("MultiEdit", os.path.join(self.main, "root.txt"))
        self.assertEqual(self._decision(out), "deny")

    def test_notebook_edit_notebook_path_into_primary_deny(self):
        out = self._run({"tool_name": "NotebookEdit",
                         "tool_input": {"notebook_path":
                                        os.path.join(self.main, "nb.ipynb")}})
        self.assertEqual(self._decision(out), "deny")

    def test_edit_override_downgrades_to_ask(self):
        out = self._edit("Write", os.path.join(self.main, "root.txt"),
                         env_extra={"WORKSPACE_GUARD_OVERRIDE": "porting"})
        self.assertEqual(self._decision(out), "ask")
        self.assertIn("porting", self._reason(out))

    def test_edit_inside_session_workspace_defers(self):
        out = self._edit("Write", os.path.join(self.wt, "in-session.txt"))
        self.assertIsNone(out, f"expected defer, got {out!r}")

    def test_edit_unrelated_outside_path_asks(self):
        # Outside the repo and not a sibling checkout -> the widened Edit hook
        # treats it as a generic outside write and asks (was defer before the
        # multi-tool widening; the sibling *deny* stays a no-op here).
        out = self._edit("Write", os.path.join(self.base, "stray.txt"))
        self.assertEqual(self._decision(out), "ask")

    def test_edit_main_session_outside_asks(self):
        # Not in a worktree -> the sibling deny is a no-op, but writing into a
        # sibling worktree is still outside proj(main), so the generic outside
        # `ask` applies.
        out = self._edit("Write", os.path.join(self.other, "x.txt"),
                         proj=self.main, cwd=self.main)
        self.assertEqual(self._decision(out), "ask")

    def test_edit_unresolved_expansion_defers(self):
        # A `$`/`~user` path can't be resolved here (native tools don't
        # shell-expand) -> defer to builtin permissions.
        out = self._edit("Write", "$HOME/somewhere/x.txt")
        self.assertIsNone(out, f"expected defer, got {out!r}")


class EntryOperandMaskTests(unittest.TestCase):
    """Which file operands name a directory entry rather than file contents."""

    def _mask(self, cmd):
        toks = shlex.split(cmd)
        mask = guard.entry_operand_mask(toks)
        # The mask is only usable if it lines up with the file list it labels.
        self.assertEqual(len(mask), len(guard.files_in_command(toks) or []),
                         f"mask does not line up with files for {cmd!r}")
        return mask

    def test_rm_marks_every_operand(self):
        self.assertEqual(self._mask("rm a b c"), [True, True, True])

    def test_rm_flags_do_not_shift_the_mask(self):
        self.assertEqual(self._mask("rm -rf -- a b"), [True, True])

    def test_mv_marks_sources_but_not_the_destination(self):
        self.assertEqual(self._mask("mv a b"), [True, False])
        self.assertEqual(self._mask("mv a b destdir"), [True, True, False])

    def test_mv_target_directory_flag_makes_every_positional_a_source(self):
        # `-t DIR` is a file_flag, so DIR leads the file list and there is no
        # positional destination to exclude.
        self.assertEqual(self._mask("mv -t destdir a b"), [False, True, True])
        self.assertEqual(self._mask("mv --target-directory=destdir a b"),
                         [False, True, True])

    def test_content_commands_mark_nothing(self):
        self.assertEqual(self._mask("cat a b"), [False, False])
        self.assertEqual(self._mask("cp a b"), [False, False])
        self.assertEqual(self._mask("tee a"), [False])

    def test_unguarded_command_has_no_mask(self):
        self.assertEqual(guard.entry_operand_mask(["ls", "a"]), [])


class EntryRealpathTests(unittest.TestCase):
    """`realpath` with the final component left alone."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._tmp.name)
        self.real = os.path.join(self.base, "real")
        os.mkdir(self.real)
        with open(os.path.join(self.real, "f.txt"), "w") as f:
            f.write("x\n")
        os.symlink(os.path.join(self.real, "f.txt"),
                   os.path.join(self.base, "link"))
        os.symlink(os.path.join(self.base, "nope"),
                   os.path.join(self.base, "dangling"))
        os.symlink(self.real, os.path.join(self.base, "dirlink"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_final_symlink_is_not_followed(self):
        p = os.path.join(self.base, "link")
        self.assertEqual(guard.entry_realpath(p), p)
        self.assertEqual(os.path.realpath(p), os.path.join(self.real, "f.txt"))

    def test_dangling_final_symlink_is_not_followed(self):
        p = os.path.join(self.base, "dangling")
        self.assertEqual(guard.entry_realpath(p), p)

    def test_directory_symlink_in_the_middle_still_resolves(self):
        p = os.path.join(self.base, "dirlink", "f.txt")
        self.assertEqual(guard.entry_realpath(p),
                         os.path.join(self.real, "f.txt"))

    def test_plain_path_matches_realpath(self):
        p = os.path.join(self.real, "f.txt")
        self.assertEqual(guard.entry_realpath(p), os.path.realpath(p))

    def test_trailing_slash_dot_and_dotdot_fall_back_to_realpath(self):
        # None of these name an entry within a directory.
        for raw in (os.path.join(self.base, "dirlink") + os.sep,
                    os.path.join(self.base, "dirlink", os.curdir),
                    os.path.join(self.base, "dirlink", os.pardir)):
            self.assertEqual(guard.entry_realpath(raw), os.path.realpath(raw),
                             f"diverged from realpath for {raw!r}")


class ClassifyPkillTests(unittest.TestCase):
    """Operand extraction for `pkill`/`killall` (issue 125)."""

    def test_non_kill_command_returns_none(self):
        self.assertIsNone(guard.classify_pkill(["pgrep", "-f", "make"]))
        self.assertIsNone(guard.classify_pkill(["kill", "1234"]))
        self.assertIsNone(guard.classify_pkill([]))

    def test_pattern_operand(self):
        self.assertEqual(guard.classify_pkill(["pkill", "-f", "make check"]),
                         ("pkill", ["make check"]))

    def test_signal_flag_is_not_a_value_flag(self):
        # `-9`/`-TERM` are signal selectors, not value-taking options: the
        # pattern after them must survive as an operand.
        self.assertEqual(guard.classify_pkill(["pkill", "-9", "-f", "ginkgo"]),
                         ("pkill", ["ginkgo"]))
        self.assertEqual(guard.classify_pkill(["pkill", "-TERM", "ginkgo"]),
                         ("pkill", ["ginkgo"]))

    def test_value_flags_consume_their_value(self):
        self.assertEqual(guard.classify_pkill(["pkill", "-u", "karl", "node"]),
                         ("pkill", ["node"]))
        self.assertEqual(
            guard.classify_pkill(["pkill", "--signal", "TERM", "-f", "node"]),
            ("pkill", ["node"]))

    def test_inline_value_flag_does_not_consume_next_token(self):
        self.assertEqual(
            guard.classify_pkill(["pkill", "--signal=TERM", "-f", "node"]),
            ("pkill", ["node"]))

    def test_selector_only_invocation_has_no_operands(self):
        # Nothing ties `pkill -u karl` or `pkill -P 1234` to this workspace, so
        # the caller must still deny — an empty list, not None.
        self.assertEqual(guard.classify_pkill(["pkill", "-u", "karl"]),
                         ("pkill", []))
        self.assertEqual(guard.classify_pkill(["pkill", "-P", "1234"]),
                         ("pkill", []))

    def test_end_of_options(self):
        self.assertEqual(guard.classify_pkill(["pkill", "-f", "--", "-weird"]),
                         ("pkill", ["-weird"]))

    def test_clustered_boolean_flags_fall_through(self):
        self.assertEqual(guard.classify_pkill(["pkill", "-fx", "ginkgo"]),
                         ("pkill", ["ginkgo"]))

    def test_killall_and_absolute_command_path(self):
        self.assertEqual(guard.classify_pkill(["killall", "-u", "karl", "node"]),
                         ("killall", ["node"]))
        self.assertEqual(guard.classify_pkill(["/usr/bin/pkill", "-f", "x"]),
                         ("pkill", ["x"]))


class ClassifyTaskkillTests(unittest.TestCase):
    """Selection classification for Windows' `taskkill` (Q58)."""

    def classify(self, text):
        return guard.classify_taskkill(shlex.split(text))

    def test_non_kill_command_returns_none(self):
        self.assertIsNone(self.classify("tasklist /IM node.exe"))
        self.assertIsNone(self.classify("kill 1234"))
        self.assertIsNone(guard.classify_taskkill([]))

    def test_command_word_is_normalized_like_windows_resolves_it(self):
        for head in ("taskkill", "TASKKILL", "taskkill.exe", "TaskKill.EXE",
                     "/c/Windows/System32/taskkill.exe"):
            self.assertEqual(self.classify("%s /IM node.exe" % head),
                             ("other", [2]), head)

    def test_every_flag_prefix_binds(self):
        # `/IM` is the documented form, `-IM` is accepted too, and `//IM` is what
        # Git Bash's MSYS path mangling requires.
        for flag in ("/IM", "//IM", "-IM", "/im", "-Im"):
            self.assertEqual(self.classify("taskkill %s node.exe" % flag),
                             ("other", [2]), flag)

    def test_image_name_and_filter_are_selection(self):
        self.assertEqual(self.classify('taskkill /FI "IMAGENAME eq node.exe"'),
                         ("other", [2]))
        self.assertEqual(
            self.classify('taskkill /IM node.exe /FI "STATUS eq RUNNING"'),
            ("other", [2, 4]))

    def test_literal_pid_needs_no_anchor(self):
        for text in ("taskkill /PID 1234", "taskkill /F /T /PID 1234",
                     "taskkill //PID 1234"):
            self.assertEqual(self.classify(text), ("pid", []), text)

    def test_expandable_pid_is_not_a_pid(self):
        # The `Stop-Process -Id $p.Id` case: host-wide, and the hook cannot tell
        # it from a pid the agent looked up.
        self.assertEqual(self.classify("taskkill /PID $p"), ("other", [2]))

    def test_mixed_pid_and_image_reads_as_selection(self):
        self.assertEqual(self.classify("taskkill /PID 1234 /IM node.exe"),
                         ("other", [4]))

    def test_switches_are_not_selection(self):
        self.assertEqual(self.classify("taskkill /F /T"), ("other", []))

    def test_remote_flags_consume_their_value(self):
        # Without `consume` handling, `box` lands in the operand stream and drags
        # a by-pid kill into a deny.
        self.assertEqual(
            self.classify("taskkill /S box /U karl /P pw /PID 1234"),
            ("pid", []))

    def test_no_selector_at_all_is_denied_selection(self):
        self.assertEqual(self.classify("taskkill"), ("other", []))

    def test_help_invocation_kills_nothing(self):
        self.assertIsNone(self.classify("taskkill /?"))

    def test_bare_operand_is_selection_the_hook_cannot_vouch_for(self):
        # `taskkill node.exe` is a syntax error to Windows — nothing here is
        # positional — but reading it as selection keeps the deny direction.
        self.assertEqual(self.classify("taskkill node.exe"), ("other", [1]))


class WorkspaceAnchorTests(unittest.TestCase):
    """The path fragment that pins a kill pattern to this workspace (issue 125)."""

    def _anchored(self, proj, pattern):
        return guard.workspace_anchor_re(proj).search(pattern) is not None

    def test_component_with_separator_anchors(self):
        for pattern in ("repo/bin/server", "/ws/repo/bin", "bin/repo",
                        "x\\repo\\bin"):
            self.assertTrue(self._anchored("/ws/repo", pattern), pattern)

    def test_bare_word_does_not_anchor(self):
        # A pattern is a substring match against a command line, not a path;
        # without a separator there is nothing making it a path anchor.
        self.assertFalse(self._anchored("/ws/repo", "repo"))

    def test_partial_component_does_not_anchor(self):
        # The whole point: a sibling worktree whose name merely contains the
        # root's name must not read as this workspace.
        for pattern in ("repo-branch1/bin", "x/repo-branch1/bin",
                        "/ws/myrepo/bin", "repo.old/bin", "/ws/repo_2/bin"):
            self.assertFalse(self._anchored("/ws/repo", pattern), pattern)

    def test_trailing_separator_on_root_is_ignored(self):
        self.assertTrue(self._anchored("/ws/repo/", "repo/bin"))

    def test_filesystem_root_has_no_anchor(self):
        self.assertIsNone(guard.workspace_anchor_re(os.sep))


class KillOperandAnchorTests(unittest.TestCase):
    """Expansion handling in the anchor check (issue 125)."""

    def setUp(self):
        self.anchor = guard.workspace_anchor_re("/ws/repo")

    def _ok(self, tok, cwd="/ws/repo", unknown=False):
        return guard.kill_operand_anchored(tok, self.anchor, cwd, unknown)

    def test_literal_path_anchors(self):
        self.assertTrue(self._ok("/ws/repo/bin/server"))

    def test_unresolved_expansion_never_anchors(self):
        # `$HOME/repo/bin` contains the literal text `/repo/`, but bash decides
        # at runtime where it lands, so the text proves nothing.
        self.assertFalse(self._ok("$HOME/repo/bin"))
        self.assertFalse(self._ok("~someuser/repo/bin"))

    def test_trailing_dollar_is_a_regex_anchor_not_an_expansion(self):
        self.assertTrue(self._ok("repo/bin$"))

    def test_pwd_substitution_prefix_resolves(self):
        self.assertTrue(self._ok("$(pwd)/bin/server"))

    def test_pwd_substitution_needs_a_tracked_cwd(self):
        self.assertFalse(self._ok("$(pwd)/bin/server", unknown=True))

    def test_no_anchor_available_denies_everything(self):
        self.assertFalse(
            guard.kill_operand_anchored("/ws/repo/bin", None, "/ws/repo", False))


class UnanchoredKillEndToEndTests(unittest.TestCase):
    """Decisions for `pkill`/`killall` end-to-end (issue 125).

    The workspace is a directory with a distinctive name so an anchored pattern
    is unambiguous, and `wt-b` stands in for a sibling worktree. No process is
    ever signalled: the hook reads the command as a JSON string and the test
    never invokes bash on it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._tmp.name)
        self.workspace = os.path.join(self.base, "wt-a")
        os.mkdir(self.workspace)

    def tearDown(self):
        self._tmp.cleanup()

    def _out(self, cmd, **kw):
        return run_hook(cmd, self.workspace, project_dir=self.workspace, **kw)

    def _decision(self, cmd, expected, **kw):
        out = self._out(cmd, **kw)
        if expected == "defer":
            self.assertIsNone(out, f"expected defer for {cmd!r}, got {out!r}")
            return None
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(got, expected, f"expected {expected!r} for {cmd!r}")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_bare_program_pattern_denies(self):
        for cmd in ('pkill -f ginkgo', 'pkill -f "make check"',
                    'pkill -9 -f "usr/bin/make check"', 'killall node'):
            self._decision(cmd, "deny")

    def test_sibling_worktree_name_denies(self):
        # The motivating case: a pattern naming a *different* worktree.
        self._decision('pkill -f "wt-b/.build/ginkgo"', "deny")

    def test_anchored_pattern_defers(self):
        # Defer, not allow: an anchored kill is out of this hook's scope, and an
        # `allow` would short-circuit the user's own permission settings on a
        # destructive command.
        self._decision('pkill -f "wt-a/.build/ginkgo"', "defer")
        self._decision('pkill -f %s' % sh(os.path.join(
            self.workspace, "bin", "server")), "defer")

    def test_selector_without_pattern_denies(self):
        r = self._decision('pkill -u karl', "deny")
        self.assertIn("no pattern at all", r)

    def test_kill_by_pid_is_untouched(self):
        # Killing by pid is the rewrite the deny message recommends.
        self._decision('kill 1234', "defer")
        self._decision('pgrep -fl ginkgo', "defer")

    def test_reason_names_the_pattern_and_both_rewrites(self):
        r = self._decision('pkill -f ginkgo', "deny")
        self.assertIn("ginkgo", r)
        self.assertIn("pgrep -fl", r)
        self.assertIn(self.workspace, r)
        self.assertIn("WORKSPACE_GUARD_OVERRIDE", r)

    def test_override_downgrades_to_ask(self):
        out = self._out('pkill -f ginkgo',
                        env_extra={"WORKSPACE_GUARD_OVERRIDE": "stuck harness"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("stuck harness",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_propagated_literal_variable_is_checked(self):
        self._decision('P=ginkgo; pkill -f $P', "deny")

    def test_kill_inside_command_substitution_is_caught(self):
        self._decision('echo "$(pkill -f ginkgo)"', "deny")

    def test_unanchored_kill_beats_a_clean_guarded_command(self):
        # A clean `cat` would emit `allow` for the whole string; the kill's deny
        # must win rather than being laundered into a blanket allow.
        self._decision('cat ./in.txt && pkill -f ginkgo', "deny")

    def test_bypass_mode_still_denies(self):
        self._decision('pkill -f ginkgo', "deny",
                       permission_mode="bypassPermissions")


class TaskkillEndToEndTests(unittest.TestCase):
    """Decisions for `taskkill` through the Bash frontend (Q58).

    Git Bash is where a bash-tool session reaches Windows' own kill. No process
    is ever signalled: the hook reads the command as a JSON string and the test
    never invokes a shell on it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._tmp.name)
        self.workspace = os.path.join(self.base, "wt-a")
        os.mkdir(self.workspace)

    def tearDown(self):
        self._tmp.cleanup()

    def _out(self, cmd, **kw):
        return run_hook(cmd, self.workspace, project_dir=self.workspace, **kw)

    def _decision(self, cmd, expected, **kw):
        out = self._out(cmd, **kw)
        if expected == "defer":
            self.assertIsNone(out, f"expected defer for {cmd!r}, got {out!r}")
            return None
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(got, expected, f"expected {expected!r} for {cmd!r}")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_image_name_denies(self):
        # `taskkill /IM node.exe` is `killall node` — every checkout's node.
        for cmd in ("taskkill //IM node.exe", "taskkill /IM node.exe",
                    "taskkill -IM node.exe", "taskkill /F /T //IM node.exe",
                    "TASKKILL.EXE //IM node.exe"):
            self._decision(cmd, "deny")

    def test_filter_denies(self):
        self._decision('taskkill //FI "IMAGENAME eq node.exe"', "deny")

    def test_no_selector_denies(self):
        r = self._decision("taskkill", "deny")
        self.assertIn("no pattern at all", r)

    def test_kill_by_literal_pid_is_untouched(self):
        for cmd in ("taskkill //PID 1234", "taskkill /F //PID 1234",
                    "taskkill //S box //U karl //PID 1234", "taskkill /?"):
            self._decision(cmd, "defer")

    def test_expandable_pid_denies(self):
        self._decision("taskkill //PID $p", "deny")

    def test_anchored_filter_defers(self):
        # Defer, not allow: an anchored kill is out of this hook's scope, and an
        # `allow` would short-circuit the user's own permission settings.
        self._decision('taskkill //FI %s' % sh(
            'WINDOWTITLE eq %s' % os.path.join(self.workspace, '*')), "defer")

    def test_sibling_worktree_filter_denies(self):
        self._decision('taskkill //FI %s' % sh(
            'WINDOWTITLE eq %s' % os.path.join(self.base, 'wt-b', '*')), "deny")

    def test_reason_names_the_taskkill_rewrites(self):
        r = self._decision("taskkill //IM node.exe", "deny")
        self.assertIn("node.exe", r)
        self.assertIn("taskkill /PID", r)
        self.assertIn("tasklist", r)
        self.assertIn("WORKSPACE_GUARD_OVERRIDE", r)
        self.assertNotIn("pgrep", r)          # the pkill rewrite, wrong command

    def test_clean_read_never_launders_a_taskkill(self):
        # `allow` speaks for the whole string, so a clean `cat` must not carry
        # even the by-pid form past the user's own permission settings.
        self._decision("cat ./in.txt && taskkill //IM node.exe", "deny")
        self._decision("cat ./in.txt && taskkill //PID 1234", "defer")

    def test_kill_inside_command_substitution_is_caught(self):
        self._decision('echo "$(taskkill //IM node.exe)"', "deny")

    def test_override_downgrades_to_ask(self):
        out = self._out("taskkill //IM node.exe",
                        env_extra={"WORKSPACE_GUARD_OVERRIDE": "stuck harness"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("stuck harness",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_bypass_mode_still_denies(self):
        self._decision("taskkill //IM node.exe", "deny",
                       permission_mode="bypassPermissions")


class SignalCommandTests(unittest.TestCase):
    """Which groups signal a process, and which of those could be pattern-fed."""

    def test_pattern_kill_commands_are_never_launderable(self):
        # pkill/killall carry their own anchor rule; folding them in would let an
        # unrelated unanchored pattern deny a correctly anchored one.
        self.assertEqual(guard.signal_command(["pkill", "-f", "x"]),
                         ("pkill", False))
        self.assertEqual(guard.signal_command(["killall", "node"]),
                         ("killall", False))

    def test_literal_pid_operands_are_not_launderable(self):
        for toks in (["kill", "1234"], ["kill", "-9", "1234", "5678"],
                     ["kill", "-0", "4321"], ["kill", "%1"], ["kill", "%+"],
                     ["kill", "-l"]):
            self.assertEqual(guard.signal_command(toks), ("kill", False), toks)

    def test_non_literal_operands_are_launderable(self):
        for toks in (["kill", "$p"], ["kill", "$("], ["kill", "-9", "$pids"]):
            self.assertEqual(guard.signal_command(toks), ("kill", True), toks)

    def test_xargs_running_a_signal_is_launderable(self):
        for toks in (["xargs", "-r", "kill"], ["xargs", "kill", "-9"],
                     ["xargs", "-I{}", "kill", "{}"],
                     ["xargs", "-r", "/bin/kill"]):
            self.assertEqual(guard.signal_command(toks), ("kill", True), toks)

    def test_a_signal_zero_probe_is_not_launderable(self):
        # `kill -0` sends no signal, so its pid source doesn't matter (Q62).
        for toks in (["kill", "-0", "$p"], ["kill", "-s", "0", "$(pgrep -f x)"],
                     ["kill", "-n", "0", "$pids"], ["kill", "-0", "--", "$p"],
                     ["xargs", "kill", "-0"], ["xargs", "-r", "kill", "-s", "0"]):
            self.assertEqual(guard.signal_command(toks), ("kill", False), toks)

    def test_a_second_signal_selector_forfeits_the_exemption(self):
        # `kill -0 -s 9` really does SIGKILL; `xargs -0` is xargs' own NUL flag.
        for toks in (["kill", "-0", "-s", "9", "$p"],
                     ["kill", "-9", "-s", "0", "$p"],
                     ["kill", "-0", "-9", "$p"],
                     ["kill", "-s0", "$p"],
                     ["xargs", "-0", "kill"]):
            self.assertEqual(guard.signal_command(toks), ("kill", True), toks)

    def test_non_signal_commands_return_none(self):
        for toks in (["pgrep", "-f", "x"], ["xargs", "wc", "-l"],
                     ["grep", "kill", "f.txt"], ["ps", "aux"], []):
            self.assertIsNone(guard.signal_command(toks), toks)

    def test_absolute_command_path(self):
        self.assertEqual(guard.signal_command(["/bin/kill", "1234"]),
                         ("kill", False))


class ShellCGroupTests(unittest.TestCase):
    """Which groups carry a shell `-c` body the hook cannot read (Q60)."""

    def test_the_shell_spellings(self):
        for toks in (["sh", "-c", "kill 1"], ["bash", "-c", "x"],
                     ["zsh", "-c", "x"], ["dash", "-c", "x"],
                     ["ksh", "-c", "x"], ["/bin/sh", "-c", "x"],
                     ["bash", "-lc", "x"], ["bash", "-euc", "x"]):
            self.assertTrue(guard.shell_c_group(toks), toks)

    def test_the_shell_is_usually_not_the_command_word(self):
        # The wrappers that actually appear in real command strings.
        for toks in (["timeout", "5", "bash", "-c", "x"],
                     ["xargs", "-I{}", "sh", "-c", "kill {}"],
                     ["find", ".", "-exec", "sh", "-c", "cat {}", ";"],
                     ["env", "FOO=1", "sh", "-c", "x"]):
            self.assertTrue(guard.shell_c_group(toks), toks)

    def test_a_long_option_is_not_the_c_flag(self):
        # `--version` and `--config=…` both contain a `c`; neither is `-c`.
        for toks in (["bash", "--version"], ["sh", "--help"],
                     ["bash", "--config=x"], ["bash", "script.sh"],
                     ["bash"]):
            self.assertFalse(guard.shell_c_group(toks), toks)

    def test_a_c_flag_on_a_non_shell_does_not_fire(self):
        for toks in (["grep", "-c", "foo", "f.txt"], ["sort", "-c", "f.txt"],
                     ["ps", "aux"], []):
            self.assertFalse(guard.shell_c_group(toks), toks)


class ShellCBodyExtractionTests(unittest.TestCase):
    """Which bodies come back for re-analysis, and which are left alone (Q61)."""

    def test_the_body_is_the_token_after_the_shells_own_c_flag(self):
        for toks in (["sh", "-c", "cat f"], ["bash", "-lc", "cat f"],
                     ["bash", "-euc", "cat f"], ["/bin/sh", "-c", "cat f"],
                     ["bash", "--norc", "-c", "cat f"],
                     ["sh", "-c", "cat f", "arg0", "arg1"]):
            self.assertEqual(guard.shell_c_bodies(toks), ["cat f"], toks)

    def test_local_wrappers_pass_the_body_through(self):
        for toks in (["timeout", "5", "bash", "-c", "cat f"],
                     ["xargs", "-I{}", "sh", "-c", "cat f"],
                     ["find", ".", "-exec", "sh", "-c", "cat f", ";"],
                     ["env", "FOO=1", "sh", "-c", "cat f"],
                     ["/usr/bin/time", "-p", "sh", "-c", "cat f"],
                     ["nohup", "sh", "-c", "cat f"]):
            self.assertEqual(guard.shell_c_bodies(toks), ["cat f"], toks)

    def test_a_body_that_does_not_run_on_this_host_is_left_alone(self):
        # The paths in these bodies belong to another filesystem. Naming the
        # local wrappers rather than the remote ones is what makes an unlisted
        # runtime (`sudo`, a container tool nobody has heard of) safe by
        # omission — it reads as remote and the body stays unanalyzed.
        for toks in (["docker", "exec", "c", "sh", "-c", "cat f"],
                     ["kubectl", "exec", "p", "--", "sh", "-c", "cat f"],
                     ["podman", "exec", "c", "sh", "-c", "cat f"],
                     ["ssh", "host", "sh", "-c", "cat f"],
                     ["sudo", "sh", "-c", "cat f"]):
            self.assertEqual(guard.shell_c_bodies(toks), [], toks)

    def test_the_c_must_be_an_option_of_the_shell_itself(self):
        # `shell_c_group` scans every token and over-reports on purpose; picking
        # a body cannot afford that. `^FAIL` is a grep pattern, not a command,
        # and feeding it to the tokenizer invents offenders out of text.
        for toks in (["bash", "run.sh", "|", "grep", "-c", "^FAIL"],
                     ["sh", "foo.sh", "-c", "cat f"],
                     ["bash", "--version"], ["bash", "--config=x"],
                     ["bash"], []):
            self.assertEqual(guard.shell_c_bodies(toks), [], toks)

    def test_a_c_flag_with_no_body_after_it(self):
        self.assertEqual(guard.shell_c_bodies(["sh", "-c"]), [])


class PidSourcePatternTests(unittest.TestCase):
    """Pattern extraction from the commands that produce a pid list."""

    def test_pgrep_reuses_the_pkill_flag_table(self):
        self.assertEqual(guard.pgrep_operands(["pgrep", "-f", "make check"]),
                         ["make check"])
        self.assertEqual(guard.pgrep_operands(["pgrep", "-u", "karl", "node"]),
                         ["node"])
        self.assertEqual(guard.pgrep_operands(["pgrep", "-fl", "ginkgo"]),
                         ["ginkgo"])

    def test_pgrep_only(self):
        self.assertIsNone(guard.pgrep_operands(["pkill", "-f", "x"]))
        self.assertIsNone(guard.pgrep_operands([]))

    def test_grep_positional_pattern(self):
        self.assertEqual(guard.grep_pattern_operands(["grep", "ginkgo"]),
                         ["ginkgo"])
        self.assertEqual(
            guard.grep_pattern_operands(["egrep", "-i", "ginkgo"]), ["ginkgo"])

    def test_grep_e_values_are_collected(self):
        self.assertEqual(
            guard.grep_pattern_operands(["grep", "-e", "a", "-e", "b"]),
            ["a", "b"])
        self.assertEqual(guard.grep_pattern_operands(["grep", "--regexp=a"]),
                         ["a"])

    def test_a_file_operand_is_never_read_as_a_pattern(self):
        # The unsafe direction: a file whose path carries the root's name would
        # otherwise anchor a pipeline it has nothing to do with.
        self.assertEqual(
            guard.grep_pattern_operands(["grep", "-e", "x", "wt-a/list.txt"]),
            ["x"])
        self.assertEqual(
            guard.grep_pattern_operands(
                ["grep", "-f", "pats.txt", "wt-a/list.txt"]),
            [guard.UNREADABLE_PATTERN])

    def test_flag_values_are_not_patterns(self):
        self.assertEqual(
            guard.grep_pattern_operands(["grep", "-m", "1", "ginkgo"]),
            ["ginkgo"])
        self.assertEqual(
            guard.grep_pattern_operands(["grep", "--include", "*.py", "gink"]),
            ["gink"])

    def test_inverting_grep_contributes_no_anchorable_pattern(self):
        # `-v` excludes rather than selects, so its pattern is not what the kill
        # receives; counting it would let an exclusion anchor the pipeline.
        for toks in (["grep", "-v", "wt-a/skip"], ["grep", "-iv", "wt-a/skip"],
                     ["grep", "--invert-match", "wt-a/skip"],
                     ["grep", "--invert", "wt-a/skip"]):
            self.assertEqual(guard.grep_pattern_operands(toks),
                             [guard.UNREADABLE_PATTERN], toks)

    def test_non_grep_commands_return_none(self):
        for toks in (["awk", "{print $1}"], ["ps", "aux"], ["cat", "f"]):
            self.assertIsNone(guard.grep_pattern_operands(toks), toks)


class PatternFedKillEndToEndTests(unittest.TestCase):
    """A kill that derives its pids from a pattern gets the pattern's verdict.

    The five laundering shapes measured against the hook at 13bb4d1, plus the
    safe forms that must stay untouched. No process is ever signalled: the hook
    reads the command as a JSON string and the test never invokes bash on it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._tmp.name)
        self.workspace = os.path.join(self.base, "wt-a")
        os.mkdir(self.workspace)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _out(self, cmd, **kw):
        return run_hook(cmd, self.workspace, project_dir=self.workspace, **kw)

    def _decision(self, cmd, expected, **kw):
        out = self._out(cmd, **kw)
        if expected == "defer":
            self.assertIsNone(out, f"expected defer for {cmd!r}, got {out!r}")
            return None
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(got, expected, f"expected {expected!r} for {cmd!r}")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_the_five_measured_laundering_shapes_deny(self):
        for cmd in (
                'pkill -f ginkgo',
                'kill $(pgrep -f ginkgo)',
                'pgrep -f ginkgo | xargs -r kill',
                "ps -eo pid,command | grep ginkgo | grep -v grep "
                "| awk '{print $1}' | xargs -r kill",
                'for p in $(pgrep -f ginkgo); do kill $p; done'):
            self._decision(cmd, "deny")

    def test_the_clean_guarded_pipeline_no_longer_allows(self):
        # The worst case: `grep` and `awk` are clean guarded commands, so the
        # whole string used to come back `allow` — the guard green-lit the
        # laundered kill rather than merely missing it.
        r = self._decision(
            "ps -eo pid,command | grep ginkgo | grep -v grep "
            "| awk '{print $1}' | xargs -r kill", "deny")
        self.assertIn("ginkgo", r)
        self.assertIn("pgrep -fl", r)

    def test_a_clean_guarded_command_never_speaks_for_a_kill(self):
        # Part 1 on its own: no pattern source, so no deny — but `allow` would
        # short-circuit the user's permission settings for the kill too.
        for cmd in ('cat ./in.txt && kill 1234',
                    'grep foo ./in.txt; kill $pid',
                    'cat ./in.txt && pkill -f "wt-a/.build/ginkgo"'):
            self._decision(cmd, "defer")

    def test_safe_kill_forms_stay_untouched(self):
        for cmd in ('kill 1234', 'kill -9 1234 5678', 'kill -0 1234',
                    'kill %1', 'kill $pid', 'sleep 5 & pid=$!; kill $pid'):
            self._decision(cmd, "defer")

    def test_a_literal_pid_kill_is_not_laundering(self):
        # The proof the literal-pid rule buys: inspecting by pattern and then
        # killing a pid you already know is the rewrite the deny recommends.
        self._decision('pgrep -f ginkgo; kill 1234', "defer")
        self._decision('ps aux | grep ginkgo; kill 1234', "defer")

    def test_a_signal_zero_probe_is_not_laundering(self):
        # A liveness wait sends no signal, so an unanchored pid source in it is
        # not a kill this workspace has to answer for (Q62).
        for cmd in ('kill -0 $(pgrep -f ginkgo)',
                    'while kill -0 $(pgrep -f ginkgo); do sleep 1; done',
                    'pgrep -f ginkgo | xargs kill -0',
                    'kill -s 0 $(pgrep -f ginkgo)',
                    "ps -eo pid,command | grep ginkgo | awk '{print $1}' "
                    "| xargs kill -0"):
            self._decision(cmd, "defer")

    def test_a_signal_zero_probe_that_also_kills_still_denies(self):
        # `-s 9` overrides the earlier `-0`, and `xargs -0` is xargs' own flag.
        for cmd in ('kill -0 -s 9 $(pgrep -f ginkgo)',
                    'pgrep -f ginkgo | xargs -0 kill'):
            self.assertIn("ginkgo", self._decision(cmd, "deny"))

    def test_anchored_patterns_defer(self):
        for cmd in ('kill $(pgrep -f "wt-a/bin/server")',
                    'pgrep -f "wt-a/bin/server" | xargs -r kill',
                    "ps -eo pid,command | grep 'wt-a/bin/server' "
                    "| grep -v grep | awk '{print $1}' | xargs -r kill"):
            self._decision(cmd, "defer")

    def test_an_exclusion_cannot_anchor_the_pipeline(self):
        # `grep -v wt-a/skip` names this workspace but *removes* those pids, so
        # what the kill receives is every OTHER checkout's ginkgo.
        self._decision("ps aux | grep ginkgo | grep -v 'wt-a/skip' "
                       "| xargs kill", "deny")
        self._decision("ps aux | grep -v foo | xargs kill", "deny")

    def test_an_unreadable_pattern_cannot_clear_the_pipeline(self):
        # `grep -f` takes its patterns from a file the hook can't read; that is
        # the case it cannot clear, not evidence there is no pid source.
        r = self._decision("ps aux | grep -f ./in.txt | awk '{print $1}' "
                           "| xargs kill", "deny")
        self.assertIn(guard.UNREADABLE_PATTERN, r)

    def test_grep_not_fed_by_ps_is_not_a_pid_source(self):
        self._decision('grep ginkgo ./in.txt; kill $pid', "defer")
        self._decision('cat ./in.txt | grep ginkgo | xargs kill', "defer")

    def test_a_ps_pipeline_with_no_kill_still_allows(self):
        for cmd in ('ps -eo pid,command | grep ginkgo',
                    "ps aux | grep ginkgo | awk '{print $1}'"):
            self._decision(cmd, "allow")

    def test_source_and_kill_on_opposite_sides_of_a_substitution(self):
        # Quoting hides the body from the outer tokenizer, so neither half is an
        # offender on its own — the facts have to cross the recursion.
        self._decision('kill "$(pgrep -f ginkgo)"', "deny")

    def test_reason_reuses_the_kill_category(self):
        r = self._decision('pgrep -f ginkgo | xargs -r kill', "deny")
        self.assertIn("Unanchored process kill", r)
        self.assertIn("ginkgo", r)
        self.assertIn(self.workspace, r)
        self.assertIn("WORKSPACE_GUARD_OVERRIDE", r)

    def test_override_downgrades_to_ask(self):
        out = self._out('pgrep -f ginkgo | xargs -r kill',
                        env_extra={"WORKSPACE_GUARD_OVERRIDE": "stuck harness"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("stuck harness",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_bypass_mode_still_denies(self):
        self._decision('pgrep -f ginkgo | xargs -r kill', "deny",
                       permission_mode="bypassPermissions")


class PsPidSourceEndToEndTests(unittest.TestCase):
    """`ps` is the pid source, so the filter reading it need not be a grep (Q60).

    No process is ever signalled: the hook reads the command as a JSON string
    and the test never invokes bash on it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._tmp.name)
        self.workspace = os.path.join(self.base, "wt-a")
        os.mkdir(self.workspace)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _out(self, cmd, **kw):
        return run_hook(cmd, self.workspace, project_dir=self.workspace, **kw)

    def _decision(self, cmd, expected, **kw):
        out = self._out(cmd, **kw)
        if expected == "defer":
            self.assertIsNone(out, f"expected defer for {cmd!r}, got {out!r}")
            return None
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(got, expected, f"expected {expected!r} for {cmd!r}")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_a_filter_that_is_not_grep_no_longer_escapes(self):
        # Each measured at d076eb4 as a defer. All kill the same processes as the
        # already-denied `pkill -f ginkgo`.
        for cmd in ("ps -eo pid,command | awk '/ginkgo/ {print $1}' "
                    "| xargs -r kill",
                    "ps aux | awk '/ginkgo/ {print $2}' | xargs kill -9",
                    "ps -eo pid,command | sed -n '/ginkgo/s/^ *\\([0-9]*\\).*"
                    "/\\1/p' | xargs kill",
                    "ps -eo pid,command | cut -d' ' -f1 | xargs kill",
                    "ps -eo pid,command | head -5 | awk '{print $1}' "
                    "| xargs kill"):
            self._decision(cmd, "deny")

    def test_a_pipeline_with_no_filter_at_all_is_caught(self):
        # The shape the grep-as-source framing could never see: nothing to read.
        self._decision('ps -eo pid= | xargs kill', "deny")
        self._decision('ps -eo pid= | xargs -r kill -9', "deny")

    def test_an_anchored_grep_still_clears_the_pipeline(self):
        # The ps stand-in never anchors, but the unchanged any-pattern-anchors
        # rule means a readable grep pattern in the same pipeline still does.
        for cmd in ("ps -eo pid,command | grep 'wt-a/bin/server' "
                    "| awk '{print $1}' | xargs -r kill",
                    "ps aux | grep wt-a/api | cut -d' ' -f1 | xargs kill"):
            self._decision(cmd, "defer")

    def test_an_anchored_awk_program_still_denies(self):
        # The documented cost. The hook cannot read an awk program, and reading
        # one would be unsafe rather than imprecise: an inverting program
        # (`!/wt-a/`) would read as anchored while killing every other checkout.
        self._decision("ps -eo pid,command | awk '/wt-a\\/ginkgo/ {print $1}' "
                       "| xargs -r kill", "deny")

    def test_the_background_child_idiom_is_untouched(self):
        # `ps -p $pid` CONSUMES a pid the shell already knows; it is in its own
        # group, so no pids flow to the kill. Two real corpus commands have this
        # shape — without the same-pipeline rule the guard would deny them.
        for cmd in ('./run.sh & pid=$!; sleep 2; kill -TERM $pid; '
                    'ps -p $pid >/dev/null && echo alive',
                    'make check & p=$!; kill $p; ps -p $p',
                    'ps aux > ./procs.txt; kill $p',
                    'ps aux; kill $p'):
            self._decision(cmd, "defer")

    def test_a_ps_pipeline_with_no_kill_is_unaffected(self):
        # No launderable kill, so the ps contributes nothing and each pipeline
        # keeps the verdict its own commands earn: `awk` is guarded, `cut` isn't.
        self._decision("ps aux | awk '{print $1}'", "allow")
        self._decision("ps -eo pid,command | cut -d' ' -f1", "defer")

    def test_awk_outside_a_ps_pipeline_is_not_a_pid_source(self):
        self._decision("awk '/ginkgo/ {print $1}' ./in.txt", "allow")
        self._decision("cat ./in.txt | awk '{print $1}' | xargs kill", "defer")

    def test_the_substitution_boundary(self):
        # A body's output is consumed by the enclosing command by definition, so
        # a `ps` anywhere in one reaches an outer launderable kill.
        self._decision('kill $(ps -eo pid= | head -1)', "deny")
        self._decision('kill "$(ps aux | awk \'/ginkgo/ {print $1}\')"', "deny")
        self._decision("kill $(ps -eo pid,command | grep wt-a/api "
                       "| awk '{print $1}')", "defer")

    def test_a_literal_pid_kill_is_not_laundering(self):
        self._decision("ps -eo pid,command | awk '{print $1}'; kill 1234",
                       "defer")

    def test_reason_names_the_unreadable_source(self):
        r = self._decision("ps -eo pid= | xargs kill", "deny")
        self.assertIn("Unanchored process kill", r)
        self.assertIn(guard.UNREADABLE_PATTERN, r)
        self.assertIn("WORKSPACE_GUARD_OVERRIDE", r)

    def test_override_downgrades_to_ask(self):
        out = self._out("ps -eo pid= | xargs kill",
                        env_extra={"WORKSPACE_GUARD_OVERRIDE": "stuck harness"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_bypass_mode_still_denies(self):
        self._decision("ps -eo pid= | xargs kill", "deny",
                       permission_mode="bypassPermissions")


class ShellCSuppressesAllowTests(unittest.TestCase):
    """A shell `-c` body never earns the string an `allow` (Q60).

    Measured at d076eb4: `cat in.txt; sh -c 'cat /q60-fake-target'` came back
    `allow`, green-lighting an unreadable outside read. Q61 went on to analyze
    the bodies the hook can read, but the suppression is what covers the ones it
    can't — so every body here is clean or unreadable, and the assertion is that
    none of them reaches `allow`. Targets are synthetic (repo rule); nothing here
    is ever executed.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _decision(self, cmd, expected):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        if expected == "defer":
            self.assertIsNone(out, f"expected defer for {cmd!r}, got {out!r}")
            return None
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(got, expected, f"expected {expected!r} for {cmd!r}")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_a_clean_guarded_command_no_longer_speaks_for_a_body(self):
        # A body with nothing to flag still costs the string its `allow`: the
        # body's own `guarded` is discarded, so reading a workspace file inside
        # one buys nothing.
        for cmd in ("cat in.txt; sh -c 'cat in.txt'",
                    "grep foo in.txt && sh -c 'kill 1234'",
                    "cat in.txt | xargs -I{} sh -c 'kill {}'",
                    "cat in.txt; timeout 5 bash -c 'kill 1'",
                    "cat in.txt; find . -exec sh -c 'kill 1' \\;"):
            self._decision(cmd, "defer")

    def test_a_body_the_hook_declines_to_read_is_still_suppressed(self):
        # Q61 does not analyze these — a container path is not a host path — so
        # the Q60 suppression is the only thing standing between the clean `cat`
        # and a blanket `allow`.
        for cmd in ("cat in.txt; docker exec c sh -c 'cat /q61-fake-target'",
                    "cat in.txt; kubectl exec p -- sh -c 'pkill -f ginkgo'",
                    "cat in.txt; ssh host sh -c 'cat /q61-fake-target'"):
            self._decision(cmd, "defer")

    def test_a_body_on_its_own_with_nothing_to_flag_defers(self):
        for cmd in ("sh -c 'kill 1234'", "sh -c 'cat in.txt'",
                    "sh -c 'echo hello'"):
            self._decision(cmd, "defer")

    def test_a_shell_without_the_c_flag_still_allows(self):
        for cmd in ("cat in.txt; bash --version", "cat in.txt; sh --help",
                    "cat in.txt; bash ./script.sh"):
            self._decision(cmd, "allow")

    def test_an_ordinary_guarded_command_still_allows(self):
        self._decision("cat in.txt", "allow")
        self._decision("grep -c foo in.txt", "allow")


class InterpreterSuppressesAllowTests(unittest.TestCase):
    """A clean guarded command never speaks for interpreter code (Q72).

    `shell_c_group`'s rule one layer out. Measured at 0219b04:
    `cat README.md && python3 -c '…'` came back `allow`, so the hook was
    short-circuiting the user's own permission settings on arbitrary code — the
    same defect Q60 fixed for `sh -c`, in a spelling `SHELL_C_CMDS` misses.

    Interpreters stay out of `SPEC`: deferring on a bare `python3 x.py` is the
    documented threat model. Only the blanket `allow` is withdrawn, and only for
    code the hook cannot read — a script resolving *inside* the workspace is
    repo-resident and still allows. Targets are synthetic (repo rule); nothing
    here is ever executed.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _decision(self, cmd, expected):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        if expected == "defer":
            self.assertIsNone(out, f"expected defer for {cmd!r}, got {out!r}")
            return
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(got, expected, f"expected {expected!r} for {cmd!r}")

    def test_inline_code_suppresses_the_allow(self):
        # `-c` for python, `-e` for the perl/ruby/node family, and both read off
        # short-option clusters so `perl -pe` and `perl -0pi -e` fire too.
        for cmd in ("cat in.txt && python3 -c 'import os'",
                    "cat in.txt && perl -e 'print 1'",
                    "cat in.txt && perl -pe 's/a/b/' in.txt",
                    "cat in.txt && perl -0pi -e 's/a/b/' in.txt",
                    "cat in.txt && ruby -e 'puts 1'",
                    "cat in.txt && node -e 'console.log(1)'"):
            self._decision(cmd, "defer")

    def test_stdin_and_heredoc_bodies_suppress_the_allow(self):
        # The heredoc body is stripped before shlex, so the hook has nothing to
        # read — the same blind spot as a `-c` operand, and far more common.
        for cmd in ("cat in.txt && python3 - <<'PY'\nimport os\nPY",
                    "cat in.txt && python3 <<'PY'\nimport os\nPY",
                    "cat in.txt && python3"):
            self._decision(cmd, "defer")

    # An outside script is no longer merely suppressed — it is a real offender.
    # See test_a_script_outside_the_workspace_is_a_real_offender below.

    def test_a_workspace_resident_script_still_allows(self):
        # Repo-resident code is what the boundary already trusts; exempting it
        # is what keeps this rule from costing far more friction than it buys.
        for cmd in ("cat in.txt && python3 ./scripts/run.py",
                    "cat in.txt && bash ./script.sh",
                    "cat in.txt && node subdir/app.js"):
            self._decision(cmd, "allow")

    def test_an_interpreter_on_another_filesystem_still_allows(self):
        # Locality is decided the way `shell_c_bodies` decides it: the code runs
        # on another host, so this workspace's boundary has nothing to say.
        for cmd in ("cat in.txt && kubectl exec p -- python3 -c 'import os'",
                    "cat in.txt && ssh host python3 /q72-fake-target/x.py",
                    "cat in.txt && docker exec c ruby -e 'puts 1'"):
            self._decision(cmd, "allow")

    def test_a_local_wrapper_still_reaches_the_interpreter(self):
        for cmd in ("cat in.txt && timeout 5 python3 -c 'import os'",
                    "cat in.txt && env python3 -c 'import os'",
                    "cat in.txt && nohup perl -e 'print 1'"):
            self._decision(cmd, "defer")

    def test_a_query_flag_runs_no_code_and_still_allows(self):
        for cmd in ("cat in.txt; python3 --version", "cat in.txt; node --version",
                    "cat in.txt; perl --help", "cat in.txt; python3 -V"):
            self._decision(cmd, "allow")

    def test_an_interpreter_name_in_a_pattern_does_not_suppress(self):
        # The rule keys on the group's command word. A loose token scan would
        # read these as interpreter invocations and defer a clean grep.
        for cmd in ("grep -n 'kindest/node' in.txt",
                    "grep -rn python3 in.txt",
                    "cat in.txt | grep -c perl"):
            self._decision(cmd, "allow")

    def test_inline_code_on_its_own_still_defers(self):
        # No guarded command, so there was never an `allow` to suppress, and
        # inline code carries no path to check. Interpreters remain outside
        # `SPEC`; only a script operand is treated as a file argument.
        for cmd in ("python3 -c 'import os'", "perl -e 'print 1'",
                    "python3 ./scripts/run.py"):
            self._decision(cmd, "defer")

    def test_an_outside_read_still_asks(self):
        # The suppression withdraws `allow`; it must not mask a real offender.
        self._decision("cat in.txt && cat /q72-fake-target/secret", "ask")

    def test_a_script_outside_the_workspace_is_a_real_offender(self):
        # Not merely suppressed: a script operand is a file the interpreter
        # reads, so it is checked like any other read and yields `ask`. That is
        # the part that survives the permission-mode matrix — a suppression only
        # withholds `allow`, which still runs under `auto`/`acceptEdits`/
        # `bypassPermissions` (docs/permission-modes.md).
        for cmd in ("python3 /q72-fake-target/x.py",
                    "bash /q72-fake-target/x.sh",
                    "cat in.txt && ruby /q72-fake-target/x.rb"):
            self._decision(cmd, "ask")

    def test_an_outside_script_denies_under_bypass(self):
        out = run_hook("python3 /q72-fake-target/x.py", self.workspace,
                       project_dir=self.workspace,
                       permission_mode="bypassPermissions")
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_a_remote_interpreter_script_is_not_an_offender(self):
        # The path names another filesystem, so checking it here would block a
        # file this host never touches.
        self._decision("cat in.txt && ssh host python3 /q72-fake-target/x.py",
                       "allow")


class InstalledExtensionReadExemptionTests(unittest.TestCase):
    """Installed plugin/skill code is read-exempt (Q72).

    82% of outside-root interpreter script arguments in a measured corpus were
    plugin scripts launched by a hook or skill, so without this the interpreter
    check would prompt on the extension ecosystem and be turned off. Reads only.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        self._home = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self._home.name)
        self.env = {"HOME": self.home, "USERPROFILE": self.home}
        for name in ("plugins", "skills"):
            os.makedirs(os.path.join(self.home, ".claude", name), exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()
        self._home.cleanup()

    def _decision(self, cmd):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace,
                       env_extra=self.env)
        return None if out is None else \
            out["hookSpecificOutput"]["permissionDecision"]

    # The exempt case is tier-stable: `classify_outside` consults the read
    # prefixes before the host-temp rule, so it answers the same on a platform
    # where this fixture's temp home is itself host-temp. The two negative cases
    # are not, so they assert only that SOME blocking tier was reached.
    def test_reading_installed_plugin_code_is_exempt(self):
        p = os.path.join(self.home, ".claude", "plugins", "p", "run.sh")
        self.assertNotIn(self._decision(f"bash {sh(p)}"), ("ask", "deny"))

    def test_writing_installed_plugin_code_is_not_exempt(self):
        p = os.path.join(self.home, ".claude", "plugins", "p", "run.sh")
        self.assertIn(self._decision(f"cp ./in.txt {sh(p)}"), ("ask", "deny"))

    def test_an_unrelated_outside_script_is_not_exempt(self):
        p = os.path.join(self.home, "elsewhere", "run.sh")
        self.assertIn(self._decision(f"bash {sh(p)}"), ("ask", "deny"))

    # A skill you wrote yourself is installed by symlinking the repo it lives
    # in, which resolves out of the dir above (issue 167). It gets the same
    # read exemption a bundled one does, and nothing more.
    def _install_symlinked_skill(self, name="mine", manifest=True):
        repo = os.path.join(self.home, "workspace", "skills")
        os.makedirs(os.path.join(repo, name))
        if manifest:
            open(os.path.join(repo, name, "SKILL.md"), "w").close()
        os.symlink(os.path.join(repo, name),
                   os.path.join(self.home, ".claude", "skills", name))
        return repo

    def test_reading_a_symlinked_skills_script_is_exempt(self):
        repo = self._install_symlinked_skill()
        p = os.path.join(repo, "mine", "lint.py")
        self.assertNotIn(self._decision(f"python3 {sh(p)}"), ("ask", "deny"))
        self.assertNotIn(self._decision(f"cat {sh(p)}"), ("ask", "deny"))

    def test_writing_into_a_symlinked_skill_is_not_exempt(self):
        repo = self._install_symlinked_skill()
        p = os.path.join(repo, "mine", "lint.py")
        self.assertIn(self._decision(f"cp ./in.txt {sh(p)}"), ("ask", "deny"))

    def test_a_symlinked_skill_exempts_its_target_and_not_its_neighbours(self):
        repo = self._install_symlinked_skill()
        p = os.path.join(repo, "theirs", "lint.py")
        self.assertIn(self._decision(f"cat {sh(p)}"), ("ask", "deny"))

    def test_an_entry_that_is_not_a_skill_launders_nothing(self):
        # No SKILL.md at the target, so the link is a slot someone dropped a
        # path into rather than a skill anyone installed.
        repo = self._install_symlinked_skill(manifest=False)
        p = os.path.join(repo, "mine", "lint.py")
        self.assertIn(self._decision(f"cat {sh(p)}"), ("ask", "deny"))


class ShellCBodyAnalysisTests(unittest.TestCase):
    """A shell `-c` body the host runs is checked like any command string (Q61).

    Measured at 8ebdc10, every one of these came back `defer`. The bar the
    change was held to: each body gets exactly the decision the shipped hook
    already gives it written unwrapped — `sh -c` buys an exemption, and this
    takes it away. Targets are synthetic (repo rule); nothing here is executed.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def _decision(self, cmd, expected):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace)
        if expected == "defer":
            self.assertIsNone(out, f"expected defer for {cmd!r}, got {out!r}")
            return None
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"],
                         expected, f"expected {expected!r} for {cmd!r}")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_outside_read_inside_a_body(self):
        for cmd in ("sh -c 'cat /q61-fake-target'",
                    "cat in.txt; sh -c 'cat /q61-fake-target'",
                    "timeout 5 bash -c 'cat /q61-fake-target'",
                    "xargs -I{} sh -c 'cat /q61-fake-target'",
                    "find . -exec sh -c 'cat /q61-fake-target' \\;",
                    "env FOO=1 sh -c 'cat /q61-fake-target'"):
            self._decision(cmd, "ask")

    def test_an_outside_write_inside_a_body(self):
        reason = self._decision("cat in.txt; sh -c 'echo x > /q61-fake-target'",
                                "ask")
        self.assertIn("q61-fake-target", reason)

    def test_an_unanchored_kill_inside_a_body(self):
        for cmd in ("sh -c 'pkill -f ginkgo'",
                    "cat in.txt; sh -c 'pkill -f ginkgo'",
                    "bash -c 'kill $(pgrep -f ginkgo)'",
                    "cat in.txt; sh -c 'ps -eo pid= | xargs kill'"):
            self._decision(cmd, "deny")

    def test_an_anchored_kill_inside_a_body_is_not_an_offender(self):
        self._decision(f"sh -c 'pkill -f {os.path.basename(self.workspace)}/x'",
                       "defer")

    def test_the_body_resolves_against_its_own_groups_cwd(self):
        # `cd` runs before the body does, so a relative path inside it lands
        # where the `cd` left the shell, not where the string started.
        self._decision("cd /etc && sh -c 'cat passwd'", "ask")

    def test_a_nested_body_is_reached(self):
        self._decision("""cat in.txt; sh -c 'sh -c "cat /q61-fake-target"'""",
                       "ask")

    def test_a_body_inside_a_substitution_is_reached(self):
        # `in_subst` carries into the body, so the enclosing `kill` still gets
        # `ps` as its pid source.
        self._decision("kill $(sh -c 'ps -eo pid= | head -1')", "deny")

    def test_a_bare_ps_in_a_body_is_not_a_pid_source_on_its_own(self):
        # The commonest debugging idiom there is: background a child, kill it,
        # confirm it died. A `sh -c` body is not a pipe, so the `ps` inside one
        # stays a CONSUMER of an already-known pid.
        self._decision("./run.sh & p=$!; kill $p; sh -c 'ps -p $p'", "defer")

    def test_a_body_the_hook_cannot_tokenize_defers(self):
        self._decision("cat in.txt; sh -c 'cat \"unbalanced'", "defer")

    def test_an_untracked_cwd_leaves_the_body_alone(self):
        # After a `cd` the hook could not follow, a relative path in the body
        # would resolve against a stale directory and read as in-workspace. No
        # answer beats a wrong clean one.
        self._decision("cd - && sh -c 'cat passwd'", "defer")


class SiblingSessionScratchE2ETests(unittest.TestCase):
    """#61 end-to-end: read-only guarded commands on a SAME-project sibling
    session's Claude scratch are allowed (the dispatcher-tails-worker case);
    writes, redirect targets, and cross-project reads still prompt.

    Creates a synthetic ``<tmp_root>/<slug>/<session>/`` layout under the real
    Claude temp root so the hook's directory scan (claude_session_project_dir)
    can anchor on the current session; cleaned up in tearDown. The slug and both
    session ids carry os.getpid() to avoid colliding with real session dirs or
    parallel runs. No real outside paths are used as targets (repo rule)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")
        self.root = guard.claude_tmp_root()
        self.slug = "-guardtest-sibling-%d" % os.getpid()
        self.proj_dir = os.path.join(self.root, self.slug)
        # The scan anchors on the session id, not on the slug, so a sharded run
        # where two workers plant the same id under their own slugs resolves to
        # whichever listdir returns first. Both ids carry the pid for that.
        tag = "%012x" % os.getpid()
        self.current = "cccccccc-1111-2222-3333-" + tag
        self.worker = "wwwwwwww-1111-2222-3333-" + tag
        # The current session's own scratch dir (scan anchor) and a sibling
        # worker session's dir, both under the same project slug.
        os.makedirs(os.path.join(self.proj_dir, self.current, "tasks"),
                    exist_ok=True)
        os.makedirs(os.path.join(self.proj_dir, self.worker, "tasks"),
                    exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()
        shutil.rmtree(self.proj_dir, ignore_errors=True)

    def _sibling(self, session_id, name="out.output"):
        return os.path.join(self.proj_dir, session_id, "tasks", name)

    def _expect(self, cmd, expected, **kw):
        out = run_hook(cmd, self.workspace, project_dir=self.workspace, **kw)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {cmd!r}; got {got!r} "
            f"(reason: {out['hookSpecificOutput'].get('permissionDecisionReason')!r})")
        return out

    def test_sibling_worker_tail_allow(self):
        # The motivating case: a dispatcher tailing a worker's task output.
        self._expect(f"tail -20 {sh(self._sibling(self.worker))}", "allow",
                     session_id=self.current)

    def test_sibling_worker_grep_allow(self):
        self._expect(f'grep -q "EXIT=" {sh(self._sibling(self.worker))}', "allow",
                     session_id=self.current)

    def test_own_session_read_allow(self):
        # Own-session scratch is allowed here too (via the per-session rule).
        self._expect(f"cat {sh(self._sibling(self.current))}", "allow",
                     session_id=self.current)

    def test_sibling_worker_cp_write_still_ask(self):
        # Copying INTO a sibling session's scratch is a write -> not exempt.
        self._expect(f"cp ./in.txt {sh(self._sibling(self.worker))}", "ask",
                     session_id=self.current)

    def test_redirect_into_sibling_worker_still_ask(self):
        # Redirect targets pass is_read=False, so they stay guarded.
        self._expect(f"cat in.txt > {sh(self._sibling(self.worker))}", "ask",
                     session_id=self.current)

    def test_rm_sibling_worker_still_ask(self):
        self._expect(f"rm {sh(self._sibling(self.worker))}", "ask",
                     session_id=self.current)

    def test_no_session_id_still_ask(self):
        # Without session_id the scan can't anchor -> exemption off -> ask.
        self._expect(f"tail -20 {sh(self._sibling(self.worker))}", "ask")

    def test_cross_project_sibling_still_ask(self):
        # A different project slug (not holding the current session) is NOT
        # exempt even for reads — Option 1 is same-project-scoped.
        other_proj = os.path.join(self.root, "-guardtest-other-%d" % os.getpid())
        other_path = os.path.join(other_proj, self.worker, "tasks", "out.output")
        try:
            os.makedirs(os.path.dirname(other_path), exist_ok=True)
            self._expect(f"cat {sh(other_path)}", "ask", session_id=self.current)
        finally:
            shutil.rmtree(other_proj, ignore_errors=True)

    def test_sibling_symlink_escape_still_ask(self):
        # The ln-staging defense runs before the sibling-read exemption: a link
        # inside the sibling scratch pointing outside is still flagged.
        link = self._sibling(self.worker, "link")
        out = self._expect(
            f"ln -s /tmp/q61-fake-target {sh(link)} && cat {sh(link)}", "ask",
            session_id=self.current)
        self.assertIn(link,
                      out["hookSpecificOutput"]["permissionDecisionReason"])


class LiteralAssignmentValueTests(unittest.TestCase):
    """Purity check for assignment RHS values (issue 58)."""

    def test_plain_relative_path_pure(self):
        self.assertEqual(guard.literal_assignment_value("sub/x.txt"), "sub/x.txt")

    def test_plain_absolute_path_pure(self):
        self.assertEqual(guard.literal_assignment_value("/opt/x"), "/opt/x")

    def test_empty_value_impure(self):
        # `f=(a b)` tokenizes as `f=` + a paren run; treating the empty
        # scalar as the value would miss the array's real $f.
        self.assertIsNone(guard.literal_assignment_value(""))

    def test_dollar_impure(self):
        self.assertIsNone(guard.literal_assignment_value("$HOME/x"))

    def test_backtick_impure(self):
        self.assertIsNone(guard.literal_assignment_value("`cmd`"))

    def test_glob_chars_impure(self):
        for v in ("*.txt", "a?b", "a[0]"):
            self.assertIsNone(guard.literal_assignment_value(v), v)

    def test_whitespace_impure(self):
        # An unquoted use would word-split on default IFS.
        for v in ("a b", "a\tb", "a\nb"):
            self.assertIsNone(guard.literal_assignment_value(v), repr(v))

    def test_colon_impure(self):
        # PATH-style values are excluded so an IFS the hook didn't see can't
        # split them into pieces the single-token check misses.
        self.assertIsNone(guard.literal_assignment_value("/a:/b"))

    def test_drive_prefix_pure_only_where_drives_resolve(self):
        # Q48: the drive colon is the one `:` that isn't a list separator. On
        # POSIX `C:/proj/x` is a directory literally named `C:`, so the rule
        # above stands there and nothing changes.
        for v in ("C:/proj/x", r"C:\proj\x", r"c:\proj"):
            self.assertEqual(guard.literal_assignment_value(v),
                             v if guard.DRIVE_PATHS else None, v)

    def test_second_colon_impure_even_after_drive_prefix(self):
        # The exemption covers the prefix, not the rest of the value.
        self.assertIsNone(guard.literal_assignment_value("C:/proj:/etc"))

    def test_drive_letter_without_separator_impure(self):
        # Bash tilde-expands after a `:` in an assignment RHS, so `C:~/x` is
        # really `C:/Users/…` — never the literal. `C:` alone is drive-relative,
        # which resolves against a cwd the hook doesn't track.
        for v in ("C:~/x", "C:", "C:proj"):
            self.assertIsNone(guard.literal_assignment_value(v), v)

    def test_leading_tilde_slash_expands_to_home(self):
        home = guard.resolved_home()                      # Q43: not $HOME
        self.assertIsNotNone(home, "no home directory resolves")
        # A Windows home carries a drive colon; Q48 exempts that prefix, so the
        # expansion is usable as a literal on both platforms.
        self.assertEqual(guard.literal_assignment_value("~/x"),
                         os.path.join(home, "x"))

    def test_tilde_user_impure(self):
        self.assertIsNone(guard.literal_assignment_value("~someuser/x"))

    def test_embedded_tilde_pure(self):
        # Bash only tilde-expands at value start; `x~y` is literal.
        self.assertEqual(guard.literal_assignment_value("x~y"), "x~y")


class SubstituteVarsTests(unittest.TestCase):
    """$NAME / ${NAME} substitution against known literals (issue 58)."""

    MAP = {"SP": "/opt/scratch", "f": "in.txt"}

    def test_plain_use_substituted(self):
        self.assertEqual(
            guard.substitute_vars("$SP/q.csv", self.MAP), "/opt/scratch/q.csv")

    def test_braced_use_substituted(self):
        self.assertEqual(
            guard.substitute_vars("${SP}.bak", self.MAP), "/opt/scratch.bak")

    def test_name_boundary_respected(self):
        # `$SPX` is a different variable — must not match `SP`.
        self.assertEqual(guard.substitute_vars("$SPX", self.MAP), "$SPX")

    def test_unknown_name_left_in_place(self):
        self.assertEqual(guard.substitute_vars("$nope/x", self.MAP), "$nope/x")

    def test_expansion_operator_not_substituted(self):
        # `${f%.txt}` is a parameter expansion the hook can't evaluate.
        self.assertEqual(
            guard.substitute_vars("${f%.txt}", self.MAP), "${f%.txt}")

    def test_backtick_token_untouched(self):
        self.assertEqual(
            guard.substitute_vars("$f`cmd`", self.MAP), "$f`cmd`")

    def test_empty_map_untouched(self):
        self.assertEqual(guard.substitute_vars("$SP", {}), "$SP")


class ApplyAssignmentGroupTests(unittest.TestCase):
    """Recognition and folding of assignment-only groups (issue 58)."""

    def test_single_assignment_sets(self):
        m = {}
        self.assertEqual(
            guard.apply_assignment_group(["f=in.txt"], m, True), ["f"])
        self.assertEqual(m, {"f": "in.txt"})

    def test_multiple_assignments_set_sequentially(self):
        # bash applies assignment-only commands left to right: `a=x b=$a`.
        m = {}
        guard.apply_assignment_group(["a=sub", "b=$a/x.txt"], m, True)
        self.assertEqual(m, {"a": "sub", "b": "sub/x.txt"})

    def test_export_form_sets(self):
        m = {}
        self.assertEqual(
            guard.apply_assignment_group(["export", "f=in.txt"], m, True),
            ["f"])
        self.assertEqual(m, {"f": "in.txt"})

    def test_export_bare_name_is_noop(self):
        # `export NAME` re-exports without changing the value.
        m = {"f": "in.txt"}
        self.assertEqual(
            guard.apply_assignment_group(["export", "f"], m, True), [])
        self.assertEqual(m, {"f": "in.txt"})

    def test_impure_value_poisons(self):
        m = {"f": "in.txt"}
        guard.apply_assignment_group(["f=$(cmd)"], m, True)
        self.assertEqual(m, {})

    def test_non_persisting_group_poisons(self):
        # Subshell / pipeline-segment / backgrounded assignment: pop, not set.
        m = {"f": "old.txt"}
        guard.apply_assignment_group(["f=new.txt"], m, False)
        self.assertEqual(m, {})

    def test_special_names_never_propagate(self):
        m = {}
        guard.apply_assignment_group(["RANDOM=5", "PWD=/x", "_=/y"], m, True)
        self.assertEqual(m, {})

    def test_command_with_prefix_assignment_not_an_assignment_group(self):
        m = {}
        self.assertIsNone(
            guard.apply_assignment_group(["f=x", "cat", "y"], m, True))
        self.assertEqual(m, {})

    def test_plain_command_not_an_assignment_group(self):
        self.assertIsNone(
            guard.apply_assignment_group(["cat", "x"], {}, True))


class PoisonVarsTests(unittest.TestCase):
    """Conservative invalidation for groups that might mutate variables."""

    def test_eval_clears_map(self):
        m = {"f": "x", "g": "y"}
        guard.poison_vars(["eval", "echo"], m)
        self.assertEqual(m, {})

    def test_source_and_dot_clear_map(self):
        for cmd in ("source", "."):
            m = {"f": "x"}
            guard.poison_vars([cmd, "lib.sh"], m)
            self.assertEqual(m, {}, cmd)

    def test_read_poisons_named_vars(self):
        m = {"f": "x", "g": "y"}
        guard.poison_vars(["read", "-r", "f"], m)
        self.assertEqual(m, {"g": "y"})

    def test_read_with_dollar_arg_clears_map(self):
        # `read $n` assigns to a variable the hook can't name.
        m = {"f": "x"}
        guard.poison_vars(["read", "$n"], m)
        self.assertEqual(m, {})

    def test_read_clobbers_reply(self):
        m = {"REPLY": "x", "g": "y"}
        guard.poison_vars(["read"], m)
        self.assertEqual(m, {"g": "y"})

    def test_keyword_prefix_skipped_before_dispatch(self):
        # `while read -r f` — the `while` keyword must not hide `read`.
        m = {"f": "x"}
        guard.poison_vars(["while", "read", "-r", "f"], m)
        self.assertEqual(m, {})

    def test_for_poisons_loop_var(self):
        m = {"f": "x"}
        guard.poison_vars(["for", "f", "in", "a", "b"], m)
        self.assertNotIn("f", m)

    def test_env_prefix_skipped_before_dispatch(self):
        # Q69: an inline env assignment must not hide the assigning command.
        for pre in (["LC_ALL=C"], ["LC_ALL=C", "TZ=UTC"], ["while", "LC_ALL=C"]):
            for cmd in (["read", "-r", "f"], ["printf", "-v", "f", "%s", "y"],
                        ["unset", "f"], ["source", "lib.sh"], ["eval", "echo"]):
                with self.subTest(prefix=pre, cmd=cmd):
                    m = {"f": "x"}
                    guard.poison_vars(pre + cmd, m)
                    self.assertEqual(m, {})

    def test_env_prefix_name_still_poisoned(self):
        # A special builtin under `set -o posix` keeps the prefix assignment.
        m = {"f": "x", "g": "y"}
        guard.poison_vars(["f=/y", "read", "g"], m)
        self.assertEqual(m, {})

    def test_prefix_assignment_poisons(self):
        m = {"f": "x"}
        guard.poison_vars(["f=/y", "cat", "z"], m)
        self.assertEqual(m, {})

    def test_append_and_array_and_increment_poison(self):
        for tok in ("f+=/y", "f[0]=/y", "f++"):
            m = {"f": "x"}
            guard.poison_vars([tok], m)
            self.assertEqual(m, {}, tok)

    def test_torn_arithmetic_assignment_poisons(self):
        # `(( f = x ))` tokenizes with `f` and `=` as separate tokens.
        m = {"f": "x"}
        guard.poison_vars(["f", "=", "5"], m)
        self.assertEqual(m, {})

    def test_plain_command_leaves_map_alone(self):
        m = {"f": "x"}
        guard.poison_vars(["grep", "PAT", "y.txt"], m)
        self.assertEqual(m, {"f": "x"})

    def test_printf_without_v_leaves_map_alone(self):
        # Q65: only `-v` assigns, and bash stops reading options at the format,
        # so nothing after it can name a variable.
        for args in (["%s\n", "f"], ["%s\n", "$UNSET"], ["value: %s", "$HOME"],
                     ["--", "-v", "f"], ["%s", "-v", "f"]):
            with self.subTest(args=args):
                m = {"f": "x"}
                guard.poison_vars(["printf"] + args, m)
                self.assertEqual(m, {"f": "x"})

    def test_printf_v_forms_still_poison(self):
        for args in (["-v", "f", "%s", "y"], ["-vf", "%s", "y"]):
            with self.subTest(args=args):
                m = {"f": "x"}
                guard.poison_vars(["printf"] + args, m)
                self.assertEqual(m, {})

    def test_printf_option_region_dollar_clears_map(self):
        # Unquoted, `$fmt` word-splits into `-v f` and does assign.
        m = {"f": "x"}
        guard.poison_vars(["printf", "$fmt", "%s", "y"], m)
        self.assertEqual(m, {})


class PrintfAssignsTests(unittest.TestCase):
    """`printf` assigns only under `-v`, read from the option region (Q65)."""

    def test_assigning_forms(self):
        for args in (["-v", "f", "%s"], ["-vf", "%s"], ["$fmt", "%s"],
                     ["-v", "$n", "%s"]):
            self.assertTrue(guard.printf_assigns(args), args)

    def test_non_assigning_forms(self):
        for args in ([], ["%s\n", "f"], ["%s", "$f"], ["--", "-v", "f"],
                     ["%s", "-v", "f"]):
            self.assertFalse(guard.printf_assigns(args), args)

    def test_unglue_v(self):
        self.assertEqual(guard.unglue_printf_v("-vf"), "f")
        self.assertEqual(guard.unglue_printf_v("-v"), "-v")
        self.assertEqual(guard.unglue_printf_v("%s"), "%s")


class ClobbersIfsTests(unittest.TestCase):
    """Groups that set IFS outside the plain/`export` assignment forms (Q49)."""

    def test_arg_assigners_naming_ifs(self):
        for cmd in (["declare", "IFS=x"], ["local", "IFS=x"],
                    ["typeset", "IFS=x"], ["readonly", "IFS=x"],
                    ["declare", "-x", "IFS=x"], ["read", "IFS"],
                    ["printf", "-v", "IFS", "x"], ["printf", "-vIFS", "x"],
                    ["for", "IFS", "in", "a"]):
            self.assertTrue(guard.clobbers_ifs(cmd), cmd)

    def test_printf_without_v_does_not_clobber_ifs(self):
        # Q65: reading `$IFS` is not setting it.
        for cmd in (["printf", "%s", "$IFS"], ["printf", "%s\n", "IFS"]):
            self.assertFalse(guard.clobbers_ifs(cmd), cmd)

    def test_eval_and_source_clobber(self):
        for cmd in ("eval", "source", "."):
            self.assertTrue(guard.clobbers_ifs([cmd, "lib.sh"]), cmd)

    def test_unnameable_arg_clobbers(self):
        # `declare $n=x` could name IFS.
        self.assertTrue(guard.clobbers_ifs(["declare", "$n=x"]))

    def test_keyword_prefix_skipped_before_dispatch(self):
        self.assertTrue(guard.clobbers_ifs(["while", "read", "IFS"]))

    def test_env_prefix_skipped_before_dispatch(self):
        # Q69: `LC_ALL=C read IFS` sets IFS for every later group.
        for cmd in (["LC_ALL=C", "read", "IFS"], ["LC_ALL=C", "source", "lib.sh"],
                    ["LC_ALL=C", "TZ=UTC", "declare", "IFS=x"],
                    ["while", "LC_ALL=C", "read", "IFS"]):
            self.assertTrue(guard.clobbers_ifs(cmd), cmd)

    def test_ifs_env_prefix_alone_does_not_clobber(self):
        # The assignment is scoped to `cat`, which bash splits with the old IFS.
        self.assertFalse(guard.clobbers_ifs(["IFS=x", "cat", "y.txt"]))

    def test_unset_is_exempt(self):
        # bash splits on the default IFS while IFS is unset, which is the
        # behaviour the hook already models.
        self.assertFalse(guard.clobbers_ifs(["unset", "IFS"]))

    def test_other_names_do_not_clobber(self):
        for cmd in (["declare", "IFSX=1"], ["read", "-r", "f"],
                    ["for", "f", "in", "a"], ["grep", "PAT", "y.txt"], []):
            self.assertFalse(guard.clobbers_ifs(cmd), cmd)


class LiteralForItemTests(unittest.TestCase):
    """Purity check for `for VAR in <list>` items (issue 70)."""

    def test_plain_word_literal(self):
        self.assertEqual(guard.literal_for_item("unit-test"), "unit-test")

    def test_relative_path_literal(self):
        self.assertEqual(guard.literal_for_item("docs/plan"), "docs/plan")

    def test_dollar_item_impure(self):
        self.assertIsNone(guard.literal_for_item("$x"))

    def test_glob_item_kept_as_pattern(self):
        # A pattern resolves where every path it expands to resolves (issue 99).
        for v in ("*.md", "docs/*.md", "doc?/plan", "docs/[ab]*.md"):
            self.assertEqual(guard.literal_for_item(v), v)

    def test_escaping_glob_kept_and_resolves_outside(self):
        # `../*.md` needs no separate containment rule — kept as the pattern, it
        # resolves above the root and the caller prompts on it.
        self.assertEqual(guard.literal_for_item("../*.md"), "../*.md")
        self.assertEqual(guard.literal_for_item("/etc/*.conf"), "/etc/*.conf")

    def test_glob_with_expansion_still_impure(self):
        # Keeping `*?[` doesn't relax anything else in the purity test.
        for v in ("$x/*.md", "a b/*.md", "`x`/*.md", "a:b/*.md"):
            self.assertIsNone(guard.literal_for_item(v), v)

    def test_glob_assignment_rhs_still_impure(self):
        # The relaxation is for-list items only; an assignment RHS is unchanged.
        self.assertIsNone(guard.literal_assignment_value("docs/*.md"))

    def test_brace_item_impure(self):
        # Unlike an assignment RHS, a for-list item IS brace-expanded by bash,
        # so `{a,b}` must not be treated as the literal string `{a,b}`.
        for v in ("{a,b}", "a{1..3}", "x{y"):
            self.assertIsNone(guard.literal_for_item(v), v)

    def test_tilde_slash_expands(self):
        home = guard.resolved_home()                      # Q43: not $HOME
        self.assertIsNotNone(home, "no home directory resolves")
        self.assertEqual(guard.literal_for_item("~/x"),
                         os.path.join(home, "x"))         # drive prefix: Q48


class ForLoopBindingTests(unittest.TestCase):
    """Classification of `for NAME in <list>` headers (issue 70)."""

    def test_all_literal_list_binds(self):
        self.assertEqual(
            guard.for_loop_binding(["for", "f", "in", "a", "b", "c"], {}),
            ("f", ["a", "b", "c"]))

    def test_braced_name_form_returns_poison(self):
        # `for f in a $x` — a non-literal item poisons the variable.
        self.assertEqual(
            guard.for_loop_binding(["for", "f", "in", "a", "$x"], {}),
            ("f", None))

    def test_glob_item_binds_to_pattern(self):
        self.assertEqual(
            guard.for_loop_binding(["for", "f", "in", "docs/*.md"], {}),
            ("f", ["docs/*.md"]))

    def test_mixed_literal_and_glob_list_binds(self):
        self.assertEqual(
            guard.for_loop_binding(["for", "f", "in", "a", "docs/*.md"], {}),
            ("f", ["a", "docs/*.md"]))

    def test_for_name_without_in_poisons(self):
        # `for f; do …` iterates "$@" — unresolvable.
        self.assertEqual(guard.for_loop_binding(["for", "f"], {}), ("f", None))

    def test_empty_list_poisons(self):
        self.assertEqual(guard.for_loop_binding(["for", "f", "in"], {}), ("f", None))

    def test_special_name_not_bound(self):
        self.assertIsNone(guard.for_loop_binding(["for", "IFS", "in", "a"], {}))

    def test_arithmetic_form_not_a_binding(self):
        # `for ((i=0;…))` tokenizes with `(` at index 1 — not `for NAME in`.
        self.assertIsNone(guard.for_loop_binding(["for", "(", "(", "i=0"], {}))

    def test_non_for_group_ignored(self):
        self.assertIsNone(guard.for_loop_binding(["grep", "PAT", "x"], {}))

    def test_item_using_outer_loop_var_binds_cross_product(self):
        # Q41: `for d in a b; do for f in "$d"/*.md` — one candidate per
        # (outer candidate, item) pair.
        self.assertEqual(
            guard.for_loop_binding(["for", "f", "in", "$d/*.md"],
                                   {"d": ["a", "b"]}),
            ("f", ["a/*.md", "b/*.md"]))

    def test_item_using_unbound_var_still_poisons(self):
        self.assertEqual(
            guard.for_loop_binding(["for", "f", "in", "$d/*.md"],
                                   {"other": ["a"]}),
            ("f", None))

    def test_expanded_item_with_brace_poisons(self):
        # The brace check runs on the expanded item.
        self.assertEqual(
            guard.for_loop_binding(["for", "f", "in", "$d/x"],
                                   {"d": ["a", "{b,c}"]}),
            ("f", None))

    def test_over_cap_candidate_set_poisons(self):
        items = ["a%d" % i for i in range(guard.MAX_LOOP_CANDIDATES + 1)]
        self.assertEqual(
            guard.for_loop_binding(["for", "f", "in"] + items, {}),
            ("f", None))

    def test_at_cap_candidate_set_binds(self):
        items = ["a%d" % i for i in range(guard.MAX_LOOP_CANDIDATES)]
        name, values = guard.for_loop_binding(["for", "f", "in"] + items, {})
        self.assertEqual(len(values), guard.MAX_LOOP_CANDIDATES)

    def test_cross_product_over_cap_poisons(self):
        # Depth multiplies: the cap bounds the nested cross product too.
        outer = ["d%d" % i for i in range(20)]
        items = ["$d/x%d" % i for i in range(20)]
        self.assertEqual(
            guard.for_loop_binding(["for", "f", "in"] + items, {"d": outer}),
            ("f", None))

    def test_single_over_cap_item_poisons(self):
        # One item can be over-cap on its own — `$a/$b` over two full outer
        # loops is 65536 pairs. The answer was always poison; Q46 is what
        # reaches it without materialising the 65536 first.
        n = guard.MAX_LOOP_CANDIDATES
        loopmap = {"a": ["x%d" % i for i in range(n)],
                   "b": ["y%d" % i for i in range(n)]}
        self.assertEqual(
            guard.for_loop_binding(["for", "f", "in", "$a/$b"], loopmap),
            ("f", None))


class ExpandLoopCandidatesTests(unittest.TestCase):
    """Cross-product expansion of loop-variable file tokens (issue 70)."""

    def test_single_var_expands_each_candidate(self):
        self.assertEqual(
            guard.expand_loop_candidates("wf/$f.yml", {"f": ["a", "b"]}),
            ["wf/a.yml", "wf/b.yml"])

    def test_braced_use_expands(self):
        self.assertEqual(
            guard.expand_loop_candidates("wf/${f}.yml", {"f": ["a"]}),
            ["wf/a.yml"])

    def test_no_loop_var_unchanged(self):
        self.assertEqual(
            guard.expand_loop_candidates("$other/x", {"f": ["a"]}),
            ["$other/x"])

    def test_two_vars_cross_product(self):
        self.assertEqual(
            guard.expand_loop_candidates("$a/$b", {"a": ["x", "y"],
                                                   "b": ["p", "q"]}),
            ["x/p", "x/q", "y/p", "y/q"])

    def test_backtick_token_not_expanded(self):
        self.assertEqual(
            guard.expand_loop_candidates("$f`cmd`", {"f": ["a"]}),
            ["$f`cmd`"])

    def test_empty_map_unchanged(self):
        self.assertEqual(guard.expand_loop_candidates("$f", {}), ["$f"])

    def test_at_cap_cross_product_expands(self):
        n = guard.MAX_LOOP_CANDIDATES
        loopmap = {"a": ["x%d" % i for i in range(n // 2)], "b": ["p", "q"]}
        self.assertEqual(len(guard.expand_loop_candidates("$a/$b", loopmap)), n)

    def test_over_cap_cross_product_returns_none(self):
        # Q46: the product is known from the per-variable candidate counts, so
        # an over-cap token is rejected without expanding anything. None is a
        # poison — the caller keeps the runtime-expanded `ask`.
        n = guard.MAX_LOOP_CANDIDATES
        loopmap = {"a": ["x%d" % i for i in range(n)], "b": ["p", "q"]}
        self.assertIsNone(guard.expand_loop_candidates("$a/$b", loopmap))


class VarPropagationEndToEndTests(unittest.TestCase):
    """Issue 58: `VAR=literal; use $VAR` resolves through the workspace check.

    Every uncertain shape must land on today's `ask` (or the pre-existing
    defer) — the feature is a precision improvement, never a new allow for a
    path bash could resolve differently.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _decision(self, cmd, expected, *, cwd=None):
        out = run_hook(cmd, cwd or self.workspace)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {cmd!r}; got {got!r} "
            f"(reason: {out['hookSpecificOutput'].get('permissionDecisionReason')!r})",
        )
        return out

    # --- the motivating shapes resolve ---------------------------------------

    def test_literal_var_inside_workspace_allow(self):
        self._decision("f=in.txt; cat $f", "allow")

    def test_braced_use_inside_workspace_allow(self):
        self._decision("f=in.txt; cat ${f}", "allow")

    def test_export_literal_var_allow(self):
        self._decision("export f=in.txt; cat $f", "allow")

    def test_literal_var_outside_workspace_ask(self):
        out = self._decision("f=/etc/q58-fake-target; cat $f", "ask")
        # The resolved path (not the $f token) is named in the reason.
        self.assertIn(
            "/etc/q58-fake-target",
            out["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_absolute_workspace_path_var_allow(self):
        # The value is the whole workspace path, which on Windows carries a
        # drive colon — impure before Q48, so propagation was dead there.
        target = os.path.join(self.workspace, "in.txt")
        self._decision("f=%s; cat $f" % sh(target), "allow")

    def test_absolute_outside_path_var_ask(self):
        # Same shape, outside the workspace: Q48 lets the path resolve, it does
        # not exempt it from the boundary check.
        drive = os.path.splitdrive(self.workspace)[0]
        target = os.path.join(drive + os.sep, "q48-fake-target")
        self._decision("f=%s; cat $f" % sh(target), "ask")

    def test_literal_var_host_temp_deny(self):
        # The issue's motivating example: `SP=/tmp/...; tail -5 $SP/x.csv`.
        self._decision("SP=/tmp/q58-fake-dir; tail -5 $SP/q265.csv", "deny")

    def test_chained_assignment_allow(self):
        # `b=$a/…` sees the already-known literal `a` (bash does the same).
        self._decision("a=sub; b=$a/x.txt; cat $b", "allow")

    def test_cd_through_literal_var_allow(self):
        # cd tracking benefits too: `cd $d` re-roots later relative paths.
        self._decision("d=sub; cd $d && cat x.txt", "allow")

    def test_cd_through_literal_var_outside_ask(self):
        self._decision("d=/etc; cd $d && cat q58-fake", "ask")

    def test_redirect_target_through_literal_var_allow(self):
        self._decision("o=out.txt; cat in.txt > $o", "allow")

    def test_redirect_target_through_literal_var_outside_ask(self):
        self._decision("o=/etc/q58-fake-target; cat in.txt > $o", "ask")

    def test_use_inside_subshell_allow(self):
        # The subshell inherits the parent's variables.
        self._decision("f=in.txt; (cat $f)", "allow")

    def test_expanded_command_name_becomes_guarded(self):
        # Closes a former silent bypass: `C=cat; $C /etc/x` used to defer.
        self._decision("C=cat; $C /etc/q58-fake-target", "ask")
        self._decision("C=cat; $C in.txt", "allow")

    # --- uncertainty keeps today's ask ----------------------------------------

    def test_unknown_var_still_ask(self):
        self._decision("cat $q58_unset_var", "ask")

    def test_command_substitution_value_still_ask(self):
        self._decision('f="x$(cmd)"; cat $f', "ask")

    def test_reassigned_literal_uses_last_value(self):
        self._decision("f=in.txt; f=/etc/q58-fake-target; cat $f", "ask")

    def test_impure_reassignment_poisons(self):
        self._decision('f=in.txt; f="x$(cmd)"; cat $f', "ask")

    def test_pipeline_segment_assignment_does_not_persist(self):
        self._decision("true | f=in.txt; cat $f", "ask")

    def test_backgrounded_assignment_does_not_persist(self):
        self._decision("f=in.txt & cat $f", "ask")

    def test_read_poisons_var(self):
        self._decision("f=in.txt; read f; cat $f", "ask")

    def test_keyword_hidden_read_poisons_var(self):
        self._decision("f=in.txt; while read -r f; do :; done; cat $f", "ask")

    def test_eval_clears_all(self):
        self._decision("f=in.txt; eval echo hi; cat $f", "ask")

    def test_env_prefix_does_not_hide_the_poisoning_command(self):
        # Q69: the prefix left `read`/`source` unrecognised, so `f` kept its
        # stale in-workspace literal and the later `cat` allowed.
        for cmd in ("read f", "read -r f", "printf -v f /etc/q69-fake",
                    "unset f", "source lib.sh", "eval echo hi"):
            with self.subTest(cmd=cmd):
                self._decision(f"f=in.txt; LC_ALL=C {cmd}; cat $f", "ask")

    def test_unset_poisons_var(self):
        self._decision("f=in.txt; unset f; cat $f", "ask")

    def test_declare_reassignment_poisons(self):
        self._decision("f=in.txt; declare f=/etc/q58-fake; cat $f", "ask")

    def test_printf_v_poisons(self):
        self._decision("f=in.txt; printf -v f /etc/q58-fake; cat $f", "ask")

    def test_printf_v_glued_poisons(self):
        # bash accepts the name glued to the flag: `printf -vf x` sets f.
        self._decision("f=in.txt; printf -vf /etc/q58-fake; cat $f", "ask")

    def test_printf_without_v_keeps_var(self):
        # Q65: a plain printf assigns nothing, so the map survives it.
        for pf in ('printf "%s\\n" f', 'printf "%s\\n" "$q65_unset"',
                   'printf "value: %s" "$HOME"', "printf -- -v f",
                   'printf "%s" -v f'):
            with self.subTest(pf=pf):
                self._decision(f"f=in.txt; {pf}; cat $f", "allow")

    def test_printf_unresolvable_format_still_poisons(self):
        # Unquoted, `$fmt` word-splits into `-v f` and does assign.
        self._decision('f=in.txt; printf $fmt "%s" y; cat $f', "ask")

    def test_array_element_assignment_poisons(self):
        # `f[0]=…` mutates f (a scalar f is f[0]).
        self._decision("f=in.txt; f[0]=/etc/q58-fake; cat $f", "ask")

    def test_function_body_assignment_poisons(self):
        self._decision("f=in.txt; g() { f=/etc/q58-fake; }; g; cat $f", "ask")

    def test_value_with_space_not_propagated(self):
        # An unquoted use would word-split into multiple paths.
        self._decision('f="a b"; cat $f', "ask")

    def test_value_with_glob_not_propagated(self):
        self._decision("f=*.txt; cat $f", "ask")

    def test_expansion_operator_still_ask(self):
        self._decision("f=in.txt; cat ${f%.txt}", "ask")

    def test_ifs_reassignment_disables_propagation(self):
        self._decision("IFS=,; f=in.txt; cat $f", "ask")

    def test_arg_assigner_setting_ifs_disables_propagation(self):
        # Q49: these reach IFS without going through apply_assignment_group.
        for setter in ("declare IFS=x", "local IFS=x", "typeset IFS=x",
                       "readonly IFS=x", "read IFS", "printf -v IFS x",
                       "for IFS in a b; do :; done", "eval 'IFS=x'"):
            with self.subTest(setter=setter):
                self._decision(f"{setter}; f=in.txt; cat $f", "ask")

    def test_ifs_split_reaches_a_second_outside_word(self):
        # The reason the rule exists: bash splits `docs/x/opt/q49-fake-target`
        # into `docs/` and `/opt/q49-fake-target` under IFS=x, so a value that
        # resolves inside the workspace reads a file outside it.
        self._decision(
            "declare IFS=x; f=docs/x/opt/q49-fake-target; cat $f", "ask")

    def test_env_prefixed_ifs_setter_disables_propagation(self):
        # Q69: same setters, hidden behind an inline env assignment.
        for setter in ("declare IFS=x", "read IFS", "eval 'IFS=x'"):
            with self.subTest(setter=setter):
                self._decision(f"LC_ALL=C {setter}; f=in.txt; cat $f", "ask")

    def test_ifs_env_prefix_keeps_propagation(self):
        # The assignment is scoped to `true`, and bash splits that command's
        # own words with the old IFS, so later groups are unaffected.
        self._decision("IFS=x true; f=in.txt; cat $f", "allow")

    def test_unset_ifs_keeps_propagation(self):
        # Unsetting IFS restores the default splitting the hook models.
        self._decision("unset IFS; f=in.txt; cat $f", "allow")

    def test_heredoc_keeps_propagation(self):
        # Q67: bodies are stripped from the raw string before shlex, so no body
        # line reaches the group loop and the map survives the `<<`.
        self._decision('f=in.txt; cat $f <<EOF\nx\nEOF', "allow")

    def test_heredoc_before_use_keeps_propagation(self):
        self._decision('f=in.txt\ncat <<EOF > out.txt\nx\nEOF\ncat $f', "allow")

    def test_heredoc_body_assignment_does_not_seed_map(self):
        # The body is data: `f=in.txt` written there assigns nothing, so `$f`
        # must stay unresolved rather than being laundered into an allow.
        self._decision('cat <<EOF > out.txt\nf=in.txt\nEOF\ncat "$f"', "ask")

    def test_heredoc_keeps_outside_value_flagged(self):
        self._decision(
            'f=/etc/q67-fake-target\ncat <<EOF > out.txt\nx\nEOF\ncat $f', "ask")

    def test_arithmetic_shift_keeps_propagation(self):
        # `$((1<<2))` leaves a `<<` token in the stream but is a shift, not a
        # heredoc — the old token scan turned propagation off for it too.
        self._decision('f=in.txt; n=$((1<<2)); cat $f', "allow")

    def test_heredoc_then_ifs_still_disables_propagation(self):
        self._decision('f=in.txt\ncat <<EOF > out.txt\nx\nEOF\nIFS=,\ncat $f',
                       "ask")

    def test_prefix_assignment_does_not_persist(self):
        # `F=… cat …` exports F only into cat's environment; a later $F is
        # NOT the assigned value (and F is poisoned, not propagated).
        self._decision(
            "F=/etc/q58-fake cat in.txt; cat $F", "ask")

    def test_expanded_token_cannot_form_assignment(self):
        # bash decides what is an assignment before expansion: `$g` expanding
        # to `f=…` runs a command named `f=…`, it does not assign f. The map
        # must not be polluted by the expanded token (f stays in.txt here;
        # conservative poisoning keeps this at ask, never allow-with-wrong-f).
        out = run_hook("f=/etc/q58-fake-target; g=f=in.txt; $g; cat $f",
                       self.workspace)
        self.assertIsNotNone(out)
        self.assertNotEqual(
            out["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_subshell_assignment_overwrite_not_trusted(self):
        # `f=out; (f=in); cat $f` — bash reads the OUTER f. The inner
        # assignment must not overwrite the map with the "safe" value.
        out = run_hook(
            "f=/etc/q58-fake-target; (f=in.txt; cat $f); cat $f",
            self.workspace)
        self.assertIsNotNone(out)
        self.assertNotEqual(
            out["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_array_assignment_not_treated_as_empty_scalar(self):
        # `f=(x)` tokenizes as `f=` + parens; the empty-looking scalar must
        # not enter the map (bash's $f is the first array element). The glued
        # `);` run keeps this a pre-existing defer — assert only that it can
        # never become allow.
        out = run_hook("f=(/etc/q58-fake-target); cat $f", self.workspace)
        if out is not None:
            self.assertNotEqual(
                out["hookSpecificOutput"]["permissionDecision"], "allow")


class ForLoopPropagationEndToEndTests(unittest.TestCase):
    """Issue 70: `for VAR in <literal list>` binds VAR's candidate set instead
    of poisoning it, so `$VAR` in a file arg is checked against every value.

    As with issue 58, every uncertain shape must keep today's `ask`/defer — a
    superset of candidates can prompt but never wrongly allow.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        os.makedirs(os.path.join(self.workspace, "wf"))
        for n in ("a", "b", "c"):
            with open(os.path.join(self.workspace, "wf", n + ".yml"), "w") as f:
                f.write("x\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _decision(self, cmd, expected):
        out = run_hook(cmd, self.workspace)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {cmd!r}; got {got!r} "
            f"(reason: {out['hookSpecificOutput'].get('permissionDecisionReason')!r})",
        )
        return out

    # --- the motivating shape resolves ---------------------------------------

    def test_all_candidates_inside_allow(self):
        # The issue's motivating example (body on its own line after `do`).
        self._decision(
            "for f in a b c\ndo\n  grep -n x wf/$f.yml\ndone", "allow")

    def test_outside_candidate_ask_names_resolved_path(self):
        out = self._decision(
            "for f in a /etc/q70-fake-target\ndo\n  cat $f\ndone", "ask")
        # The resolved candidate (not the $f token) is named in the reason.
        self.assertIn(
            "/etc/q70-fake-target",
            out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_host_temp_candidate_deny(self):
        self._decision(
            "for f in a /tmp/q70-fake-target\ndo\n  cat $f\ndone", "deny")

    def test_scalar_var_resolved_into_list_allow(self):
        # `SP=wf; for f in $SP/a.yml …` — the list items resolve via issue 58
        # before binding, so the candidate set matches what bash iterates.
        self._decision(
            "SP=wf; for f in $SP/a.yml $SP/b.yml\ndo\n  cat $f\ndone", "allow")

    def test_braced_use_of_loop_var_allow(self):
        self._decision(
            "for f in a b\ndo\n  cat wf/${f}.yml\ndone", "allow")

    # --- glob items resolve as their own pattern (issue 99) -------------------

    def test_glob_item_allow(self):
        # The issue's largest prompt source: an in-workspace survey loop.
        self._decision("for f in wf/*.yml\ndo\n  cat $f\ndone", "allow")
        self._decision(
            'for f in docs/*.md\ndo\n  echo "=== $f"\n  grep -E "^# " "$f"\ndone',
            "allow")

    def test_glob_item_other_metachars_allow(self):
        self._decision("for f in w?/[ab]*.yml\ndo\n  cat $f\ndone", "allow")

    def test_escaping_glob_item_ask(self):
        # A pattern that resolves above the root prompts on the pattern itself —
        # no expansion needed to see it escapes.
        out = self._decision(
            "for f in ../../../etc/q99-fake/*.conf\ndo\n  cat $f\ndone", "ask")
        self.assertIn(
            "/etc/q99-fake",
            out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_absolute_glob_item_ask(self):
        self._decision("for f in /etc/q99-fake/*.conf\ndo\n  cat $f\ndone", "ask")

    def test_glob_item_escaping_body_path_flagged(self):
        # `$f` is in-workspace, but the body climbs out of it. The pattern has
        # the same segment count as every path it expands to, so it climbs
        # exactly as far as bash will — here to the fixture's parent, which is
        # the host temp dir (deny), not merely outside (ask).
        out = self._decision(
            "for f in wf/*.yml\ndo\n  cat $f/../../../q99-fake\ndone", "deny")
        self.assertIn(
            "wf/*.yml/../../../q99-fake",
            out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_glob_item_reassigned_in_body_ask(self):
        # The glob binding is poisoned by the same rules as a literal one.
        self._decision(
            "for f in wf/*.yml\ndo\n  read f\n  cat $f\ndone", "ask")

    def test_glob_item_outside_sibling_in_list_ask(self):
        # One outside item taints the loop, glob or not.
        self._decision(
            "for f in wf/*.yml /etc/q99-fake\ndo\n  cat $f\ndone", "ask")

    def test_glob_item_after_cd_outside_deny(self):
        # The pattern is relative, so it tracks the cd like any relative path.
        self._decision(
            "cd /tmp/q99-fake && for f in *.log\ndo\n  cat $f\ndone", "deny")

    # --- a nested loop's list may use the outer variable (Q41) ---------------

    def test_nested_glob_over_glob_allow(self):
        # Q41's motivating shape: both levels glob, and the inner list is built
        # from the outer variable. The inner binding is `wf/*/*.yml`.
        self._decision(
            'for d in wf/*\ndo\n  for f in "$d"/*.yml\n  do\n    cat "$f"\n'
            '  done\ndone', "allow")

    def test_nested_loop_one_line_allow(self):
        # Same shape written on one line — the inner header shares its group
        # with the enclosing `do`.
        self._decision(
            'for d in wf/*; do for f in "$d"/*.yml; do cat "$f"; done; done',
            "allow")

    def test_nested_literal_outer_binds_cross_product_allow(self):
        self._decision(
            'for d in wf sub\ndo\n  for f in "$d"/*.yml\n  do\n    cat "$f"\n'
            '  done\ndone', "allow")

    def test_nested_three_deep_allow(self):
        self._decision(
            'for a in wf\ndo\n  for b in "$a"/*\n  do\n    for c in "$b"/*.yml\n'
            '    do\n      cat "$c"\n    done\n  done\ndone', "allow")

    def test_nested_outer_candidate_outside_ask(self):
        # An outside outer candidate carries into every inner binding built
        # from it, so the inner loop's reads prompt.
        out = self._decision(
            'for d in wf /etc/q41-fake\ndo\n  for f in "$d"/*.yml\n  do\n'
            '    cat "$f"\n  done\ndone', "ask")
        self.assertIn(
            "/etc/q41-fake",
            out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_nested_inner_item_climbs_out_ask(self):
        # The inner list escapes the root even though the outer one doesn't.
        self._decision(
            'for d in wf\ndo\n  for f in "$d"/../../../etc/q41-fake/*\n  do\n'
            '    cat "$f"\n  done\ndone', "ask")

    def test_nested_poisoned_outer_poisons_inner_ask(self):
        # `$d` is unresolvable, so `"$d"/*.yml` keeps its `$` and poisons `f`.
        self._decision(
            'for d in $x\ndo\n  for f in "$d"/*.yml\n  do\n    cat "$f"\n'
            '  done\ndone', "ask")

    def test_nested_brace_in_inner_item_poisons_ask(self):
        # Brace rejection applies to the expanded item, not just a bare one.
        self._decision(
            'for d in wf\ndo\n  for f in "$d"/{a,b}.yml\n  do\n    cat "$f"\n'
            '  done\ndone', "ask")

    def test_nested_inner_reusing_outer_name_allow(self):
        # `for d in "$d"/*.yml` reads the OUTER d to build the list, then
        # rebinds it — the expansion must happen before the rebind.
        self._decision(
            'for d in wf/*\ndo\n  for d in "$d"/*.yml\n  do\n    cat "$d"\n'
            '  done\ndone', "allow")

    # --- the candidate cross product is bounded (Q46) ------------------------

    def _cap_lists(self, *prefixes):
        n = guard.MAX_LOOP_CANDIDATES
        return tuple(" ".join("%s%d" % (p, i) for i in range(n))
                     for p in prefixes)

    def test_deep_cross_product_asks_without_hanging(self):
        # Three nested loops over the cap's worth of literals each make
        # `$a/$b/$c` stand for 16.7M paths. Enumerating them ran past two
        # minutes, and a hook that never answers is a non-blocking error — the
        # guard would enforce nothing at all. run_hook's timeout fails this on
        # a hang rather than wedging the suite.
        self._decision(
            "for a in %s; do for b in %s; do for c in %s; do cat $a/$b/$c; "
            "done; done; done" % self._cap_lists("a", "b", "c"), "ask")

    def test_deep_cross_product_in_inner_list_asks(self):
        # The same blowup one level up, where the over-cap product is a
        # for-LIST item: the bound has to apply before the list is built, not
        # only when a file arg is checked.
        self._decision(
            "for a in %s; do for b in %s; do for c in %s; do "
            "for d in $a/$b/$c; do cat $d; done; done; done; done"
            % self._cap_lists("a", "b", "c"), "ask")

    def test_at_cap_cross_product_still_resolves_allow(self):
        # The bound is on the product, not the nesting depth: a cap-sized outer
        # list times a single inner candidate is exactly at the cap, so it
        # still expands and an in-workspace read allows as before.
        outer, = self._cap_lists("a")
        self._decision(
            "for a in %s; do for b in wf; do cat $b/$a; done; done" % outer,
            "allow")

    def test_over_cap_cross_product_poisons_rather_than_truncates(self):
        # One candidate past the cap and the same in-workspace read prompts.
        # Over-cap must poison: truncating to the first N candidates would
        # check a prefix and silently allow whatever the rest resolved to.
        outer, = self._cap_lists("a")
        self._decision(
            "for a in %s; do for b in wf sub; do cat $b/$a; done; done" % outer,
            "ask")

    # --- uncertainty keeps today's ask ---------------------------------------

    def test_brace_item_poisons(self):
        # bash brace-expands a for-list item; treating `{a,b}` literally would
        # miss the real paths, so the variable is poisoned.
        self._decision("for f in wf/{a,b}.yml\ndo\n  cat $f\ndone", "ask")

    def test_nonliteral_item_poisons(self):
        self._decision("for f in a $x\ndo\n  cat wf/$f.yml\ndone", "ask")

    def test_for_name_without_in_poisons(self):
        self._decision("for f\ndo\n  cat $f\ndone", "ask")

    def test_reassigned_loop_var_poisons(self):
        self._decision(
            "for f in a b\ndo\n  f=/etc/q70-fake\n  cat $f\ndone", "ask")

    def test_read_in_body_poisons_loop_var(self):
        self._decision(
            "for f in a b\ndo\n  read f\n  cat $f\ndone", "ask")

    def test_eval_in_body_poisons_loop_var(self):
        self._decision(
            "for f in a b\ndo\n  eval echo hi\n  cat $f\ndone", "ask")

    def test_arithmetic_for_form_poisons(self):
        # `for ((i=0;i<3;i++))` isn't a `for NAME in` list — the loop var keeps
        # today's poison behavior.
        self._decision(
            "for ((i=0;i<3;i++))\ndo\n  cat $i\ndone", "ask")

    def test_heredoc_keeps_loop_propagation(self):
        # Q67: the stripped body can't pollute the maps, so loopmap survives a
        # heredoc the same way varmap does.
        self._decision(
            "for f in a b\ndo\n  cat $f <<EOF\nx\nEOF\ndone", "allow")

    def test_heredoc_keeps_outside_candidate_tainting(self):
        self._decision(
            "for f in a ../../../etc/q67-fake\ndo\n  cat $f <<EOF\nx\nEOF\ndone",
            "ask")

    def test_one_outside_candidate_taints_whole_loop(self):
        # bash visits every value, so a single outside candidate must prompt
        # even when the rest are in-workspace.
        self._decision(
            "for f in a b c ../../../etc/q70-fake\ndo\n  cat wf/$f.yml\ndone",
            "ask")


class PluginWiringTests(unittest.TestCase):
    """The config plumbing that connects the script to Claude Code.

    Unit/e2e tests can be green while the plugin silently fails to load
    because a config file has a typo, a bad hook path, or invalid JSON.
    These tests assert the wiring itself.
    """

    def _load_json(self, relpath):
        path = REPO / relpath
        self.assertTrue(path.is_file(), f"missing config file: {relpath}")
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            self.fail(f"{relpath} is not valid JSON: {e}")

    # --- hooks/hooks.json ----------------------------------------------------

    def _shell_matcher(self, data):
        pre = data.get("hooks", {}).get("PreToolUse")
        self.assertIsInstance(pre, list, "hooks.PreToolUse must be a list")
        matchers = [e.get("matcher") for e in pre]
        matcher = next((m for m in matchers if m and "Bash" in m), None)
        self.assertIsNotNone(matcher, "no PreToolUse entry matches Bash")
        return matcher

    def test_hooks_json_registers_pretooluse_shell_tools(self):
        # Q51: PowerShell is the shell tool on a Windows box without Git for
        # Windows. Unmatched, the plugin loads, reports itself active, and
        # checks no shell command at all.
        data = self._load_json("hooks/hooks.json")
        matcher = self._shell_matcher(data)
        for tool in ("Bash", "PowerShell"):
            self.assertIn(tool, matcher,
                          f"{tool} not covered by matcher {matcher!r}")

    def test_hooks_json_registers_pretooluse_edit_tools(self):
        # Issue 62: the sibling-checkout deny also hooks the file-editing tools.
        data = self._load_json("hooks/hooks.json")
        pre = data.get("hooks", {}).get("PreToolUse")
        self.assertIsInstance(pre, list)
        matchers = [e.get("matcher") for e in pre]
        edit_matcher = next(
            (m for m in matchers if m and "Edit" in m and "Write" in m), None)
        self.assertIsNotNone(
            edit_matcher, "no PreToolUse entry matches the Edit/Write tools")
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            self.assertIn(tool, edit_matcher,
                          f"{tool} not covered by matcher {edit_matcher!r}")

    def test_hooks_json_registers_pretooluse_read_tools(self):
        # Q29: native read/search tools get the outside-workspace check too.
        data = self._load_json("hooks/hooks.json")
        pre = data.get("hooks", {}).get("PreToolUse")
        self.assertIsInstance(pre, list)
        matchers = [e.get("matcher") for e in pre]
        read_matcher = next(
            (m for m in matchers if m and "Read" in m and "Grep" in m), None)
        self.assertIsNotNone(
            read_matcher, "no PreToolUse entry matches the Read/Grep/Glob tools")
        for tool in ("Read", "Grep", "Glob"):
            self.assertIn(tool, read_matcher,
                          f"{tool} not covered by matcher {read_matcher!r}")

    def test_hooks_json_command_path_exists(self):
        data = self._load_json("hooks/hooks.json")
        matcher = self._shell_matcher(data)
        entry = next(
            e for e in data["hooks"]["PreToolUse"] if e.get("matcher") == matcher
        )
        commands = [
            h["command"] for h in entry["hooks"]
            if h.get("type") == "command" and "command" in h
        ]
        self.assertTrue(commands, "shell matcher has no command-type hook")
        # Every referenced "${CLAUDE_PLUGIN_ROOT}/<rel>" path must exist in the
        # repo (the plugin root is the repo root at install time).
        marker = "${CLAUDE_PLUGIN_ROOT}/"
        found_ref = False
        for cmd in commands:
            idx = cmd.find(marker)
            while idx != -1:
                rest = cmd[idx + len(marker):]
                # The path runs until the next quote/space that ends the token.
                rel = rest.split('"')[0].split("'")[0].split()[0]
                self.assertTrue(
                    (REPO / rel).is_file(),
                    f"hook command references missing file: {rel}",
                )
                found_ref = True
                idx = cmd.find(marker, idx + 1)
        self.assertTrue(
            found_ref, "no hook command references ${CLAUDE_PLUGIN_ROOT}/"
        )
        # Sanity: the load-bearing script is the one that's wired up.
        self.assertTrue(
            any("bash-workspace-guard.py" in c for c in commands),
            "the guard script is not registered as a hook command",
        )

    # --- scripts/run-python-hook.cmd -----------------------------------------

    def test_hook_shim_is_executable(self):
        # The hook command execs the shim directly, so a missing execute bit
        # means exit 126. Claude Code treats that as a non-blocking hook error,
        # which leaves the guard enforcing nothing with no visible symptom.
        if os.name == "nt":
            self.skipTest("POSIX permission bits")
        shim = REPO / "scripts" / "run-python-hook.cmd"
        self.assertTrue(shim.is_file(), "missing scripts/run-python-hook.cmd")
        self.assertTrue(os.access(shim, os.X_OK),
                        "run-python-hook.cmd is not executable")

    def test_hook_shim_has_lf_line_endings(self):
        # CRLF makes the POSIX half of the polyglot a syntax error.
        shim = REPO / "scripts" / "run-python-hook.cmd"
        self.assertNotIn(b"\r", shim.read_bytes(),
                         "run-python-hook.cmd must stay LF-only")

    def test_gitattributes_pins_cmd_line_endings(self):
        # Without the pin, a clone under core.autocrlf=true rewrites the shim.
        path = REPO / ".gitattributes"
        self.assertTrue(path.is_file(), "missing .gitattributes")
        self.assertIn("*.cmd text eol=lf", path.read_text())

    def test_hook_shim_emits_the_same_decision_as_a_direct_run(self):
        if os.name == "nt":
            self.skipTest("the shim runs through cmd.exe on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = os.path.realpath(tmp)
            payload = json.dumps({
                "tool_name": "Bash",
                "cwd": workspace,
                "tool_input": {"command": "cat ../q94-fake-target"},
            })
            env = os.environ.copy()
            env["CLAUDE_PROJECT_DIR"] = workspace
            shim = REPO / "scripts" / "run-python-hook.cmd"
            # Claude Code runs hook commands through a shell, and the shim has
            # no shebang -- it relies on the shell retrying an ENOEXEC exec as
            # /bin/sh. Launching it any other way doesn't exercise that.
            outputs = []
            for argv in ([sys.executable, str(SCRIPT)],
                         ["/bin/sh", "-c", f'"{shim}" bash-workspace-guard.py']):
                r = subprocess.run(argv, input=payload, capture_output=True,
                                   text=True, env=env, timeout=10)
                self.assertEqual(r.returncode, 0,
                                 f"{argv[0]} exited {r.returncode}: "
                                 f"{r.stderr!r}")
                outputs.append(r.stdout)
            # Two silent defers would compare equal, so pin the decision too.
            self.assertIn("permissionDecision", outputs[1])
            self.assertEqual(outputs[0], outputs[1])

    # --- .claude-plugin/*.json -----------------------------------------------

    def test_plugin_json_valid_and_named(self):
        data = self._load_json(".claude-plugin/plugin.json")
        self.assertEqual(data.get("name"), "workspace-guard")
        self.assertIn("version", data)

    def test_marketplace_json_valid_and_lists_plugin(self):
        data = self._load_json(".claude-plugin/marketplace.json")
        names = [p.get("name") for p in data.get("plugins", [])]
        self.assertIn(
            "workspace-guard", names,
            "marketplace.json does not list the workspace-guard plugin",
        )


class CIWiringTests(unittest.TestCase):
    """Q45: a skipped test reads as OK, so a plain `unittest discover` on
    Windows can't tell "passes there" from "quietly stopped running there".
    The job runs through the skip ceiling instead."""

    def test_windows_job_runs_the_suite_through_the_skip_ceiling(self):
        self.assertTrue((REPO / "scripts" / "run-tests.py").is_file(),
                        "missing scripts/run-tests.py")
        workflow = (REPO / ".github" / "workflows" / "tests.yml").read_text()
        self.assertRegex(
            workflow, r"run-tests\.py --max-skips \d+",
            "the Windows job must run the suite through --max-skips",
        )

    def test_windows_suite_also_runs_under_git_bash(self):
        # Q44: the hook process and the shell it parses for are different
        # processes with different environments. run-python-hook.cmd gives the
        # hook a cmd.exe environment (what the default-shell job covers), while
        # the commands it reads were written for Git Bash. Dropping the Git Bash
        # job leaves every environment-reading helper verified in one of the two
        # Windows environments it has to be right in.
        workflow = (REPO / ".github" / "workflows" / "tests.yml").read_text()
        self.assertRegex(
            workflow, r"run-tests\.py --max-skips \d+\n\s+shell: bash",
            "a Windows job must run the suite under Git Bash (shell: bash)",
        )


@unittest.skipUnless(guard.DRIVE_PATHS, "MSYS forms only differ where paths "
                                        "carry drive letters")
class MsysPathFormWindowsTests(unittest.TestCase):
    """Q52 end-to-end: the decision a real command in MSYS form gets on Windows.

    MsysPathFormTests covers the rewrite itself everywhere; these run the whole
    chain -- tokenizer, cwd tracking, config resolution, decision -- and so only
    mean anything where a leading slash is genuinely ambiguous.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = os.path.realpath(self._tmp.name)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hello\n")

    @staticmethod
    def _msys(path):
        """Native path -> the form a command would be written in under Git Bash."""
        drive, rest = os.path.splitdrive(path)
        return "/" + drive[0].lower() + rest.replace("\\", "/")

    def _decision(self, cmd, expected, **kw):
        out = run_hook(cmd, self.workspace, **kw)
        self.assertIsNotNone(out, f"expected a decision, got defer for: {cmd!r}")
        hook = out["hookSpecificOutput"]
        self.assertEqual(hook["permissionDecision"], expected,
                         f"{cmd!r} -> {hook!r}")
        return hook.get("permissionDecisionReason", "")

    def test_workspace_file_in_msys_form_is_allowed(self):
        # The drive mapping's whole point: this names a file inside the root,
        # and before Q52 it resolved to `<drive>\c\...` and prompted.
        self._decision("cat %s" % self._msys(
            os.path.join(self.workspace, "in.txt")), "allow")

    def test_cd_in_msys_form_keeps_tracking(self):
        self._decision("cd %s && cat in.txt" % self._msys(self.workspace),
                       "allow")

    def test_outside_prompt_names_the_path_bash_will_open(self):
        reason = self._decision("cat /c/Users/nobody/q52-fake-target", "ask")
        self.assertIn(r"C:\Users\nobody\q52-fake-target", reason)
        self.assertNotIn(r"\c\Users\nobody", reason)

    def test_tmp_deny_names_the_real_temp_dir(self):
        # `/tmp` is the usertemp mount, not <drive>\tmp. The deny already fired
        # before Q52 -- on a path the command was never going to write.
        reason = self._decision("echo hi > /tmp/q52-fake-target", "deny")
        self.assertIn(
            os.path.join(os.path.realpath(tempfile.gettempdir()),
                         "q52-fake-target"),
            reason)

    def test_read_allow_prefix_in_msys_form_matches(self):
        # The dead-knob half of Q52: an entry written the way a Git Bash user
        # writes paths resolved onto another drive and exempted nothing.
        outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        target = os.path.join(outside, "shared.txt")
        with open(target, "w") as f:
            f.write("x\n")
        self._decision("cat %s" % sh(target), "allow", env_extra={
            "WORKSPACE_GUARD_READ_ALLOW_PREFIXES": self._msys(outside)})


class MsysEnvironmentTests(unittest.TestCase):
    """Q44: what the guard reads from the environment under Git Bash.

    MSYS rewrites path-shaped variables on the way into a native binary: the
    shell holds `HOME=/c/Users/x` and `TMP=/tmp`, and the Python it launches
    sees `C:\\Users\\x` and the real temp directory. Every path the guard
    derives from the environment rides on that conversion, and a leading-slash
    path is not absolute under ntpath -- so if it ever stopped, `resolved_home`
    would return None (no tilde expansion, Q43 undone) and the real temp
    directory would drop out of the host-temp roots, both silently.

    These assert the conversion, not the guard's parsing, and only mean
    anything in the environment that performs it.
    """

    def setUp(self):
        if os.name != "nt" or not os.environ.get("MSYSTEM"):
            self.skipTest("not running under MSYS/Git Bash on Windows")

    def test_home_arrives_in_native_form(self):
        self.assertIsNotNone(
            guard.resolved_home(),
            "resolved_home() is None under Git Bash: $HOME reached Python in "
            "MSYS form, which ntpath does not consider absolute",
        )

    def test_platform_temp_dir_is_a_host_temp_root(self):
        # tempfile.gettempdir() reads %TMP%, which is `/tmp` in the shell. In
        # MSYS form it resolves to <drive>\tmp -- a directory that does not
        # exist -- and the real temp dir stops being a host-temp root at all,
        # downgrading its `deny` tier to a plain `ask`.
        real_temp = os.path.realpath(tempfile.gettempdir())
        self.assertTrue(os.path.isabs(real_temp))
        self.assertIn(real_temp, guard.host_temp_roots(os.getcwd()))


class NativeToolTests(unittest.TestCase):
    """Q29: the native Read/Grep/Glob (read) and Edit/Write (write) tools get the
    same outside-workspace verdict as the equivalent bash command, routed through
    the shared classify_outside/decide core.

    The workspace lives under $HOME so an outside path is NOT classified as
    host-temp — that keeps the plain outside `ask` cleanly separated from the
    /tmp `deny`. No real sensitive paths are used as targets (repo rule)."""

    def setUp(self):
        home = guard.resolved_home()                      # Q43: not $HOME
        self.assertTrue(home and os.path.isdir(home),
                        f"no home directory to build the fixture under: {home!r}")
        self._tmp = tempfile.TemporaryDirectory(dir=home)
        self.base = os.path.realpath(self._tmp.name)
        self.workspace = os.path.join(self.base, "proj")
        os.mkdir(self.workspace)
        with open(os.path.join(self.workspace, "in.txt"), "w") as f:
            f.write("hi\n")
        self.outside_dir = os.path.join(self.base, "outside")
        os.makedirs(self.outside_dir, exist_ok=True)
        self.outside = os.path.join(self.outside_dir, "secret.txt")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, tool, tool_input, env_extra=None, permission_mode=None,
             session_id=None, cwd=None):
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = self.workspace
        for k, v in (env_extra or {}).items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        data = {"tool_name": tool, "tool_input": tool_input,
                "cwd": cwd or self.workspace}
        if permission_mode is not None:
            data["permission_mode"] = permission_mode
        if session_id is not None:
            data["session_id"] = session_id
        r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(data),
                           capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(r.returncode, 0, f"hook errored: {r.stderr!r}")
        out = r.stdout.strip()
        return json.loads(out) if out else None

    def _decision(self, out):
        self.assertIsNotNone(out, "expected a decision, got defer")
        return out["hookSpecificOutput"]["permissionDecision"]

    # --- reads: Read / Grep / Glob ------------------------------------------

    def test_read_outside_asks(self):
        out = self._run("Read", {"file_path": self.outside})
        self.assertEqual(self._decision(out), "ask")

    def test_read_inside_defers(self):
        out = self._run("Read",
                        {"file_path": os.path.join(self.workspace, "in.txt")})
        self.assertIsNone(out, f"expected defer, got {out!r}")

    def test_grep_outside_path_asks(self):
        out = self._run("Grep", {"pattern": "x", "path": self.outside_dir})
        self.assertEqual(self._decision(out), "ask")

    def test_glob_outside_path_asks(self):
        out = self._run("Glob",
                        {"pattern": "**/*.txt", "path": self.outside_dir})
        self.assertEqual(self._decision(out), "ask")

    def test_read_host_temp_denies(self):
        # Mirrors bash `cat /tmp/…`: host-temp is a steered deny, reads included.
        out = self._run("Read", {"file_path": "/tmp/q29-fake-target"})
        self.assertEqual(self._decision(out), "deny")

    def test_read_read_prefix_exempt_defers(self):
        # WORKSPACE_GUARD_READ_ALLOW_PREFIXES exempts READS under the prefix.
        allow = os.path.join(self.base, "allowed")
        os.makedirs(allow, exist_ok=True)
        out = self._run("Read", {"file_path": os.path.join(allow, "x.txt")},
                        env_extra={"WORKSPACE_GUARD_READ_ALLOW_PREFIXES": allow})
        self.assertIsNone(out, f"expected defer, got {out!r}")

    def test_read_own_session_output_defers(self):
        # The agent reading back its own background-task output is exempt, same
        # as a bash `tail` of it (see SiblingSessionScratchE2ETests).
        root = guard.claude_tmp_root()
        slug = "-guardtest-native-%d" % os.getpid()
        session = "aaaaaaaa-1111-2222-3333-555555555555"
        d = os.path.join(root, slug, session, "tasks")
        os.makedirs(d, exist_ok=True)
        self.addCleanup(shutil.rmtree, os.path.join(root, slug), True)
        out = self._run("Read", {"file_path": os.path.join(d, "out.output")},
                        session_id=session)
        self.assertIsNone(out, f"expected defer, got {out!r}")

    def test_read_unresolved_expansion_defers(self):
        # Native tools don't shell-expand; a `$` path is unresolvable -> defer.
        out = self._run("Read", {"file_path": "$HOME/x.txt"})
        self.assertIsNone(out, f"expected defer, got {out!r}")

    def test_grep_missing_path_defers(self):
        # No path -> Grep searches cwd (in workspace) -> nothing to flag.
        out = self._run("Grep", {"pattern": "x"})
        self.assertIsNone(out, f"expected defer, got {out!r}")

    def test_read_outside_bypass_denies(self):
        # bypassPermissions has no human to answer an ask -> deny for feedback.
        out = self._run("Read", {"file_path": self.outside},
                        permission_mode="bypassPermissions")
        self.assertEqual(self._decision(out), "deny")

    # --- writes: Edit / Write / NotebookEdit --------------------------------

    def test_write_outside_asks(self):
        out = self._run("Write", {"file_path": self.outside})
        self.assertEqual(self._decision(out), "ask")

    def test_edit_outside_asks(self):
        out = self._run("Edit", {"file_path": self.outside})
        self.assertEqual(self._decision(out), "ask")

    def test_write_host_temp_denies(self):
        out = self._run("Write", {"file_path": "/tmp/q29-fake-target"})
        self.assertEqual(self._decision(out), "deny")

    def test_write_inside_defers(self):
        out = self._run("Edit",
                        {"file_path": os.path.join(self.workspace, "in.txt")})
        self.assertIsNone(out, f"expected defer, got {out!r}")

    def test_write_read_prefix_not_exempt_asks(self):
        # The read-prefix exemption is READ-only: a WRITE under it still asks.
        allow = os.path.join(self.base, "allowed")
        os.makedirs(allow, exist_ok=True)
        out = self._run("Write", {"file_path": os.path.join(allow, "x.txt")},
                        env_extra={"WORKSPACE_GUARD_READ_ALLOW_PREFIXES": allow})
        self.assertEqual(self._decision(out), "ask")

    def test_notebook_edit_outside_asks(self):
        out = self._run("NotebookEdit",
                        {"notebook_path": os.path.join(self.outside_dir, "n.ipynb")})
        self.assertEqual(self._decision(out), "ask")


# --- Q51: the PowerShell tool ------------------------------------------------

# Lets a fixture pass `tool_input=None` to mean "omit the key entirely", which
# `None` as a default could not express.
_UNSET = object()


def ps(path):
    """Quote a native path for a PowerShell fixture — the `sh()` of this
    frontend, and deliberately not `sh()` itself.

    `shlex.quote` is POSIX quoting: it leaves a backslash bare, which is right
    for bash and wrong here only in the other direction — what actually breaks a
    Windows-CI fixture is a space in the interpolated path. Single quotes are
    literal in PowerShell, so they carry the backslashes through intact and
    survive a `C:\\Program Files`-shaped home.
    """
    return "'" + path.replace("'", "''") + "'"


class PowerShellSpecShapeTests(unittest.TestCase):
    """Invariants the PS_SPEC table has to hold for the binder to be correct."""

    def test_spec_covers_documented_cmdlets(self):
        self.assertEqual(
            set(guard.PS_SPEC.keys()),
            {"get-content", "select-string", "import-csv", "import-clixml",
             "set-content", "add-content", "out-file", "tee-object",
             "export-csv", "export-clixml",
             "copy-item", "move-item", "remove-item", "rename-item"},
        )

    def test_positional_slots_are_declared_parameter_names(self):
        # A slot is a parameter name, not a role: binding `-Pattern` by name has
        # to close slot 0 so the file lands in slot 1. A typo'd slot name would
        # silently never match a file parameter and never be checked.
        rows = dict(guard.PS_SPEC, __location__=guard.PS_LOCATION_SPEC)
        for name, row in rows.items():
            for slot in row["positional"]:
                self.assertIn(slot, row["names"],
                              f"{name}: positional slot {slot!r} is not a "
                              f"declared parameter")

    def test_file_roles_are_read_or_write(self):
        for name, row in guard.PS_SPEC.items():
            for param, role in row["files"].items():
                self.assertIn(role, ("read", "write"), f"{name}.-{param}")

    def test_file_and_consume_are_disjoint(self):
        # A parameter declared both ways would have its path swallowed as a
        # value — the silent-allow direction.
        for name, row in guard.PS_SPEC.items():
            self.assertFalse(set(row["files"]) & row["consume"],
                             f"{name}: parameter is both a file and a value")

    def test_aliases_resolve_to_real_rows(self):
        known = set(guard.PS_SPEC) | guard.PS_LOCATION_CMDS | guard.PS_KILL_CMDS
        for alias, target in guard.PS_ALIASES.items():
            self.assertIn(target, known, f"alias {alias!r} -> unknown {target!r}")

    def test_kill_selectors_are_declared_values(self):
        # A selector missing from `consume` would leave its value in the
        # positional stream, where a literal pid reads as the safe 'pid' mode —
        # `Stop-Process -Name 1234` would defer.
        self.assertTrue(guard.PS_KILL_SELECTORS <= guard.PS_KILL_SPEC["consume"])

    def test_posix_lookalike_aliases_do_not_reach_the_bash_spec(self):
        # `cat`, `rm`, `cp`, `mv`, `tee` name cmdlets with entirely different
        # flag sets here. Routing them to the SPEC row of the same name is the
        # aliasing mistake Q3 recorded.
        for alias in ("cat", "rm", "cp", "mv", "tee", "sc", "type"):
            self.assertIn(alias, guard.PS_ALIASES)
            self.assertIn(guard.PS_ALIASES[alias], guard.PS_SPEC)


class PowerShellTokenizerTests(unittest.TestCase):
    """The tokenizer is the load-bearing difference from the bash frontend."""

    def words(self, text):
        toks = guard.ps_tokenize(text)
        self.assertIsNotNone(toks, f"unexpected defer for {text!r}")
        return [t[1] for t in toks if t[0] == "word"]

    def test_backslash_is_a_path_character_not_an_escape(self):
        # The whole reason this is not shlex. In POSIX mode `C:\Users\x`
        # tokenizes to `C:Usersx`, which resolves INSIDE the project root.
        self.assertEqual(self.words(r"Get-Content C:\Users\bob\x"),
                         ["Get-Content", r"C:\Users\bob\x"])

    def test_shlex_would_have_eaten_the_backslashes(self):
        # Pins the premise above rather than trusting it.
        self.assertEqual(shlex.split(r"Get-Content C:\Users\bob\x"),
                         ["Get-Content", "C:Usersbobx"])

    def test_backtick_is_the_escape_character(self):
        # An escaped space joins the token; an unescaped one still separates.
        self.assertEqual(self.words("Get-Content a` b"), ["Get-Content", "a b"])
        self.assertEqual(self.words("Get-Content a b"), ["Get-Content", "a", "b"])

    def test_backtick_before_a_path_letter_is_the_letter(self):
        self.assertEqual(self.words(r"Get-Content `C:\x"), ["Get-Content", r"C:\x"])

    def test_single_quotes_are_literal(self):
        self.assertEqual(self.words(r"Get-Content 'C:\a b\$x'"),
                         ["Get-Content", r"C:\a b\$x"])

    def test_doubled_quote_inside_a_quoted_run(self):
        self.assertEqual(self.words("Get-Content 'it''s'"), ["Get-Content", "it's"])

    def test_unbalanced_quote_defers(self):
        self.assertIsNone(guard.ps_tokenize('Get-Content "unterminated'))
        self.assertIsNone(guard.ps_tokenize("Get-Content 'unterminated"))

    def test_dollar_marks_a_token_expandable(self):
        toks = guard.ps_tokenize(r'Get-Content $env:USERPROFILE\x')
        self.assertTrue(toks[1][2], "token with $ should be expandable")

    def test_dollar_inside_single_quotes_is_not_expandable(self):
        toks = guard.ps_tokenize(r"Get-Content '$env:USERPROFILE'")
        self.assertFalse(toks[1][2])

    def test_dollar_inside_double_quotes_is_expandable(self):
        toks = guard.ps_tokenize(r'Get-Content "$env:USERPROFILE"')
        self.assertTrue(toks[1][2])

    def test_stream_selector_glues_to_the_redirect(self):
        toks = guard.ps_tokenize(r"Get-Content a 2> C:\x")
        self.assertIn(("redir", "2>", False, False), toks)
        self.assertEqual([t[1] for t in toks if t[0] == "word"],
                         ["Get-Content", "a", r"C:\x"])

    def test_separators_split_segments(self):
        toks = guard.ps_tokenize("a | b; c && d")
        self.assertEqual([t[1] for t in toks if t[0] == "op"], ["|", ";", "&&"])

    def test_line_comment_is_dropped(self):
        self.assertEqual(self.words("Get-Content a # Get-Content C:\\x"),
                         ["Get-Content", "a"])

    def test_hash_inside_a_token_is_not_a_comment(self):
        self.assertEqual(self.words("Get-Content 'a#b'"), ["Get-Content", "a#b"])

    def test_block_comment_is_dropped(self):
        self.assertEqual(self.words("<# note #> Get-Content a"),
                         ["Get-Content", "a"])

    def test_backtick_newline_continues_the_line(self):
        self.assertEqual(self.words("Get-Content `\na"), ["Get-Content", "a"])


class PowerShellHereStringTests(unittest.TestCase):
    def test_literal_here_string_body_is_dropped(self):
        out = guard.ps_strip_here_strings("Set-Content x @'\n$(evil)\n'@\n")
        self.assertNotIn("evil", out)

    def test_expandable_body_survives_the_literal_only_pass(self):
        # It has to: PowerShell runs a `$(…)` written in an @"…"@ body, so the
        # subexpression scan needs to see it.
        text = 'Set-Content x @"\n$(Get-Content C:\\x)\n"@\n'
        self.assertIn("Get-Content C:", guard.ps_strip_here_strings(
            text, literal_only=True))
        self.assertNotIn("Get-Content C:", guard.ps_strip_here_strings(text))

    def test_unterminated_here_string_defers(self):
        self.assertIsNone(guard.ps_strip_here_strings('Set-Content x @"\nbody'))


class PowerShellSubexpressionTests(unittest.TestCase):
    def test_body_is_extracted_and_masked(self):
        masked, bodies = guard.ps_subexpressions(r"Write-Output $(Get-Content C:\x)")
        self.assertEqual(bodies, [r"Get-Content C:\x"])
        self.assertEqual(masked, "Write-Output $")

    def test_body_inside_double_quotes_is_extracted(self):
        _, bodies = guard.ps_subexpressions(r'Write-Output "$(Get-Content C:\x)"')
        self.assertEqual(bodies, [r"Get-Content C:\x"])

    def test_paren_in_quoted_prose_does_not_close_the_body(self):
        _, bodies = guard.ps_subexpressions(r'$(Get-Content "a)b" C:\x)')
        self.assertEqual(bodies, [r'Get-Content "a)b" C:\x'])

    def test_only_the_outermost_body_is_returned(self):
        # Descendants are the caller's business: it re-scans each body it
        # recurses into. Returning them here as well made that recursion
        # quadratic in bodies and exponential in nesting depth (Q64).
        _, bodies = guard.ps_subexpressions(r"$(a $(Get-Content C:\x))")
        self.assertEqual(bodies, [r"a $(Get-Content C:\x)"])

    def test_a_nested_body_is_found_by_re_scanning(self):
        _, bodies = guard.ps_subexpressions(r"$(a $(Get-Content C:\x))")
        self.assertEqual(guard.ps_subexpressions(bodies[0])[1],
                         [r"Get-Content C:\x"])

    def test_unbalanced_quote_is_not_closed_for_the_tokenizer(self):
        # Fabricating the missing quote here would hand the tokenizer a
        # balanced string and turn a defer into a parse.
        masked, _ = guard.ps_subexpressions('Get-Content "unterminated')
        self.assertIsNone(guard.ps_tokenize(masked))


class PowerShellSubexpressionRecursionTests(unittest.TestCase):
    """Nested `$(…)` costs one analysis per level, not 2^n (Q64).

    `ps_subexpressions` used to return every descendant body alongside the
    outermost ones, and `_ps_analyze_command` then recursed into each and
    re-flattened it — so a body `n` deep was analyzed once per ancestor. 20
    levels meant 1,048,576 analyses and about 9 seconds, and a hook that stalls
    is one Claude Code abandons as a non-blocking error, leaving the guard
    enforcing nothing. Counting calls rather than clocking them keeps this
    honest on a loaded CI box, where a timing assertion is either flaky or too
    loose to catch a regression.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self._tmp.name)
        self.ctx = guard.build_context(
            {"cwd": self.workspace, "tool_input": {}})

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _nest(levels, inner="Get-Content /q64-fake-target"):
        return "$(" * levels + inner + ")" * levels

    def _depths(self, command):
        depths = []
        real = guard._ps_analyze_command

        def spy(cmd, ctx, base_cwd, depth=0):
            depths.append(depth)
            return real(cmd, ctx, base_cwd, depth)

        with mock.patch.object(guard, "_ps_analyze_command", spy):
            guard.ps_analyze_command(command, self.ctx, self.workspace)
        return depths

    def test_each_nesting_level_is_analyzed_exactly_once(self):
        # Pre-fix: 4096 calls for 12 levels. One per level plus the outer
        # command is the whole budget.
        self.assertEqual(len(self._depths(self._nest(12))), 13)

    def test_recursion_stops_at_the_cap(self):
        # Two-sided, matching the bash side's cap test: neither a cap that
        # stopped firing nor one that runs away slips back in.
        self.assertEqual(max(self._depths(self._nest(60))),
                         guard.MAX_SUBST_DEPTH)

    def test_an_offender_nested_at_the_cap_is_still_found(self):
        offenders, guarded = guard.ps_analyze_command(
            self._nest(guard.MAX_SUBST_DEPTH), self.ctx, self.workspace)
        self.assertTrue(offenders)
        self.assertFalse(guarded)

    def test_nesting_past_the_cap_defers_rather_than_allows(self):
        # The unanalyzed tail must never read as clean: `allow` speaks for the
        # whole string, and past the cap the guard has not looked at it.
        offenders, guarded = guard.ps_analyze_command(
            self._nest(guard.MAX_SUBST_DEPTH + 1), self.ctx, self.workspace)
        self.assertFalse(offenders)
        self.assertFalse(guarded)


class PowerShellBindArgsTests(unittest.TestCase):
    """Parameter binding — the layer where a shifted operand becomes a silent
    allow."""

    def bind(self, cmdlet, text):
        toks = guard.ps_tokenize(text)
        return [(t, role) for t, _, _, role in
                guard.ps_bind_args(toks[1:], guard.PS_SPEC[cmdlet])]

    def test_positional_path(self):
        self.assertEqual(self.bind("get-content", r"Get-Content C:\x"),
                         [(r"C:\x", "read")])

    def test_named_path(self):
        self.assertEqual(self.bind("get-content", r"Get-Content -Path C:\x"),
                         [(r"C:\x", "read")])

    def test_literalpath_is_a_file_parameter(self):
        self.assertEqual(self.bind("get-content", r"Get-Content -LiteralPath C:\x"),
                         [(r"C:\x", "read")])

    def test_colon_bound_value(self):
        self.assertEqual(self.bind("get-content", r"Get-Content -Path:C:\x"),
                         [(r"C:\x", "read")])

    def test_unambiguous_prefix_resolves(self):
        self.assertEqual(self.bind("get-content", r"Get-Content -Pat C:\x"),
                         [(r"C:\x", "read")])

    def test_ambiguous_prefix_falls_back_to_a_switch(self):
        # `-P` matches Path and PipelineVariable. Unresolved, it reads as a
        # switch and the value stays a positional — so the file is still
        # checked, which is the safe way to be wrong.
        self.assertEqual(self.bind("get-content", r"Get-Content -P C:\x"),
                         [(r"C:\x", "read")])

    def test_value_taking_parameter_does_not_shift_the_operand(self):
        # Undeclared, `-Encoding` would leak UTF8 into slot 0 (-Path) and push
        # the real target into slot 1 (-Value), where it is never checked.
        self.assertEqual(
            self.bind("set-content", r"Set-Content -Encoding UTF8 C:\x hi"),
            [(r"C:\x", "write")])

    def test_named_binding_closes_its_positional_slot(self):
        # `-Pattern` bound by name means the file takes slot 1 (-Path), whatever
        # the order. A one-pass binder gives it slot 0 and never checks it.
        self.assertEqual(
            self.bind("select-string", r"Select-String -Pattern foo C:\x"),
            [(r"C:\x", "read")])
        self.assertEqual(
            self.bind("select-string", r"Select-String C:\x -Pattern foo"),
            [(r"C:\x", "read")])

    def test_destination_bound_by_name_frees_the_source_slot(self):
        self.assertEqual(
            self.bind("copy-item", r"Copy-Item -Destination C:\d C:\s"),
            [(r"C:\d", "write"), (r"C:\s", "read")])

    def test_pattern_positional_is_not_a_file(self):
        self.assertEqual(self.bind("select-string", r"Select-String foo C:\x"),
                         [(r"C:\x", "read")])

    def test_value_positional_is_not_a_file(self):
        self.assertEqual(self.bind("set-content", r"Set-Content C:\x C:\y"),
                         [(r"C:\x", "write")])

    def test_trailing_operands_repeat_the_last_slot(self):
        self.assertEqual(self.bind("remove-item", r"Remove-Item C:\a C:\b C:\c"),
                         [(r"C:\a", "write"), (r"C:\b", "write"),
                          (r"C:\c", "write")])

    def test_switches_do_not_consume_the_operand(self):
        self.assertEqual(
            self.bind("remove-item", r"Remove-Item -Recurse -Force C:\x"),
            [(r"C:\x", "write")])

    def test_stop_parsing_marker_ends_binding(self):
        self.assertEqual(self.bind("get-content", r"Get-Content --% C:\x"), [])

    def test_negative_number_is_not_a_parameter(self):
        self.assertEqual(self.bind("get-content", r"Get-Content -Tail -5 C:\x"),
                         [(r"C:\x", "read")])


class PowerShellPathPartsTests(unittest.TestCase):
    def test_unquoted_array_operand_splits(self):
        self.assertEqual(guard.ps_path_parts(r"a,C:\x", False), ["a", r"C:\x"])

    def test_quoted_operand_keeps_its_comma(self):
        self.assertEqual(guard.ps_path_parts(r"a,b.txt", True), [r"a,b.txt"])


class PowerShellKillClassifyTests(unittest.TestCase):
    """Selection classification for `Stop-Process` (Q57)."""

    def classify(self, text):
        toks = guard.ps_tokenize(text)
        self.assertIsNotNone(toks, f"unexpected defer for {text!r}")
        return guard.ps_classify_kill([t for t in toks if t[0] == "word"])

    def test_pid_list_accepts_only_literal_digits(self):
        for tok in ("1234", "12,34"):
            self.assertTrue(guard.ps_pid_list(tok), tok)
        for tok in ("", "node", "12,", "12a"):
            self.assertFalse(guard.ps_pid_list(tok), tok)

    def test_non_kill_returns_none(self):
        self.assertIsNone(self.classify("Get-Process -Name node"))
        self.assertIsNone(self.classify("Get-Content in.txt"))
        self.assertIsNone(guard.ps_classify_kill([]))

    def test_aliases_and_case_are_kills(self):
        for text in ("Stop-Process -Name node", "stop-process -Name node",
                     "kill -Name node", "spps -Name node"):
            self.assertEqual(self.classify(text), ("name", ["node"]), text)

    def test_literal_pid_needs_no_anchor(self):
        for text in ("Stop-Process -Id 1234", "Stop-Process 1234",
                     "Stop-Process -Id 1234,5678", "Stop-Process -Id:1234"):
            self.assertEqual(self.classify(text)[0], "pid", text)

    def test_expandable_pid_is_not_a_pid(self):
        # `$p = Get-Process -Name node; Stop-Process -Id $p.Id` is host-wide, and
        # the hook cannot tell it from a pid the agent looked up.
        self.assertEqual(self.classify("Stop-Process -Id $p.Id"),
                         ("other", ["$p.Id"]))

    def test_name_binds_by_prefix_and_by_colon(self):
        for text in ("Stop-Process -Na node", "Stop-Process -Name:node"):
            self.assertEqual(self.classify(text), ("name", ["node"]), text)

    def test_no_selector_reads_as_pipeline_input(self):
        self.assertEqual(self.classify("Stop-Process"), ("other", []))
        self.assertEqual(self.classify("Stop-Process -Force"), ("other", []))

    def test_inputobject_is_anchorable_selection(self):
        self.assertEqual(self.classify("Stop-Process -InputObject $p"),
                         ("other", ["$p"]))

    def test_ordinary_value_parameter_is_not_selection(self):
        # Without `consume` handling, `SilentlyContinue` lands in the positional
        # stream and drags a by-pid kill into a deny.
        self.assertEqual(
            self.classify("Stop-Process -ErrorAction SilentlyContinue -Id 1234"),
            ("pid", []))

    def test_switches_are_not_selection(self):
        self.assertEqual(
            self.classify("Stop-Process -Id 1234 -Force -PassThru -WhatIf"),
            ("pid", []))

    def test_stop_parsing_tail_is_selection_it_cannot_vouch_for(self):
        self.assertEqual(self.classify("Stop-Process --% /IM node.exe")[0],
                         "other")


class PowerShellKillAnchorTests(unittest.TestCase):
    """Expansion handling in the PowerShell anchor check (Q57)."""

    def setUp(self):
        # A slash-separated root, so `os.path.basename` finds `repo` on a POSIX
        # test host as well as on Windows. Given `C:\ws\repo`, POSIX basename
        # returns the whole string and every assertion below tests the wrong
        # regex.
        self.anchor = guard.workspace_anchor_re("/ws/repo")

    def test_literal_path_anchors_with_either_separator(self):
        for tok in (r"C:\ws\repo\bin\*", "C:/ws/repo/bin/*", r"repo\bin"):
            self.assertTrue(
                guard.ps_kill_operand_anchored(tok, False, self.anchor), tok)

    def test_expandable_token_never_anchors(self):
        # PowerShell decides at runtime where this lands, so the `\repo\` in it
        # proves nothing — the same rule the file checks apply.
        self.assertFalse(guard.ps_kill_operand_anchored(
            r"$env:USERPROFILE\repo\bin", True, self.anchor))

    def test_partial_component_does_not_anchor(self):
        self.assertFalse(guard.ps_kill_operand_anchored(
            r"C:\ws\repo-branch1\bin", False, self.anchor))

    def test_unresolved_tilde_never_anchors(self):
        self.assertFalse(guard.ps_kill_operand_anchored(
            r"~someone\repo\bin", False, self.anchor))

    def test_no_anchor_available_denies_everything(self):
        self.assertFalse(guard.ps_kill_operand_anchored(
            r"C:\ws\repo\bin", False, None))


class PowerShellKillFixture(object):
    """Workspace and hook-invocation harness for the PowerShell kill suites.

    `wt-a` is this session's workspace and `wt-b` stands in for a sibling
    worktree. No process is ever signalled: the hook reads the command as a JSON
    string and the tests never invoke a shell on it. A mixin rather than a base
    TestCase so the loader doesn't collect it, and so neither suite re-runs the
    other's cases.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.realpath(self._tmp.name)
        self.workspace = os.path.join(self.base, "wt-a")
        os.mkdir(self.workspace)

    def tearDown(self):
        self._tmp.cleanup()

    def _glob(self, *parts):
        return ps(os.path.join(self.base, *parts, "*"))

    def _out(self, command, permission_mode=None, env_extra=None):
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = self.workspace
        env.update(env_extra or {})
        data = {"tool_name": "PowerShell", "cwd": self.workspace,
                "tool_input": {"command": command}}
        if permission_mode is not None:
            data["permission_mode"] = permission_mode
        r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(data),
                           capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(r.returncode, 0, f"hook errored: {r.stderr!r}")
        out = r.stdout.strip()
        return json.loads(out) if out else None

    def _decision(self, command, expected, **kw):
        out = self._out(command, **kw)
        if expected == "defer":
            self.assertIsNone(out, f"expected defer for {command!r}, got {out!r}")
            return None
        self.assertIsNotNone(out, f"expected {expected}, got defer for {command!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(got, expected, f"expected {expected!r} for {command!r}")
        return out["hookSpecificOutput"]["permissionDecisionReason"]


class PowerShellUnanchoredKillEndToEndTests(PowerShellKillFixture,
                                            unittest.TestCase):
    """Decisions the script emits for a PowerShell `Stop-Process` (Q57)."""

    def test_kill_by_name_denies(self):
        for cmd in ("Stop-Process -Name node", "kill -Name node",
                    "spps -Name node -Force"):
            self._decision(cmd, "deny")

    def test_pipeline_kill_denies(self):
        # The idiomatic form, and the one an agent denied on -Name reaches for
        # next. Leaving it would make the -Name deny train the bypass.
        self._decision("Get-Process node | Stop-Process", "deny")

    def test_anchored_pipeline_defers(self):
        # Defer, not allow: an anchored kill is out of this hook's scope, and an
        # `allow` would short-circuit the user's own permission settings on a
        # destructive command.
        self._decision(
            "Get-Process | Where-Object { $_.Path -like %s } | Stop-Process"
            % self._glob("wt-a"), "defer")

    def test_sibling_worktree_filter_denies(self):
        self._decision(
            "Get-Process | Where-Object { $_.Path -like %s } | Stop-Process"
            % self._glob("wt-b"), "deny")

    def test_name_kill_denies_even_inside_an_anchored_pipeline(self):
        # A process name carries no path, so nothing around it can scope it.
        self._decision(
            "Get-Process | Where-Object { $_.Path -like %s } | "
            "Stop-Process -Name node" % self._glob("wt-a"), "deny")

    def test_kill_by_literal_pid_is_untouched(self):
        for cmd in ("Stop-Process -Id 1234", "Stop-Process 1234",
                    "Get-Process -Name node"):
            self._decision(cmd, "defer")

    def test_expandable_pid_denies(self):
        self._decision("Stop-Process -Id $p.Id", "deny")

    def test_anchor_does_not_carry_across_a_statement_boundary(self):
        for sep in (";", "\n", "&&"):
            self._decision(
                "Get-ChildItem %s%s Get-Process node | Stop-Process"
                % (ps(self.workspace), sep), "deny")

    def test_kill_in_a_script_block_is_seen(self):
        self._decision(
            "Get-Process node | ForEach-Object { Stop-Process -Id $_.Id }",
            "deny")
        self._decision(
            "Get-Process | Where-Object { $_.Path -like %s } | "
            "ForEach-Object { Stop-Process -Id $_.Id }" % self._glob("wt-a"),
            "defer")

    def test_kill_inside_a_subexpression_is_caught(self):
        self._decision("Write-Output $(Stop-Process -Name node)", "deny")

    def test_reason_names_the_powershell_rewrites(self):
        r = self._decision("Stop-Process -Name node", "deny")
        self.assertIn("node", r)
        self.assertIn("Stop-Process -Id", r)
        self.assertIn("Where-Object", r)
        self.assertIn(self.workspace, r)
        self.assertIn("WORKSPACE_GUARD_OVERRIDE", r)
        self.assertNotIn("pgrep", r)      # the bash rewrite, wrong shell

    def test_override_downgrades_to_ask(self):
        out = self._out("Stop-Process -Name node",
                        env_extra={"WORKSPACE_GUARD_OVERRIDE": "stuck harness"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("stuck harness",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_bypass_mode_still_denies(self):
        self._decision("Stop-Process -Name node", "deny",
                       permission_mode="bypassPermissions")

    # --- Q59: a clean guarded cmdlet never speaks for the kill ---------------

    def test_a_clean_guarded_cmdlet_never_speaks_for_a_kill(self):
        # These kills earn no offender — by literal pid, or anchored — but
        # `allow` speaks for the WHOLE string and short-circuits the user's own
        # permission settings, so the cmdlet must not launder them into one.
        for cmd in (r"Get-Content .\in.txt; Stop-Process -Id 1234",
                    r"Stop-Process -Id 1234; Get-Content .\in.txt",
                    r"Get-Content .\in.txt && kill 1234",
                    r"Get-Content .\in.txt; Get-Process | Where-Object "
                    r"{ $_.Path -like %s } | Stop-Process" % self._glob("wt-a"),
                    r"Get-Content .\in.txt; Get-Process | Where-Object "
                    r"{ $_.Path -like %s } | ForEach-Object "
                    r"{ Stop-Process -Id $_.Id }" % self._glob("wt-a")):
            self._decision(cmd, "defer")

    def test_a_kill_in_a_subexpression_suppresses_the_allow(self):
        # The body is masked out of the outer text, so the clean cmdlet and the
        # kill sit on opposite sides of the recursion.
        self._decision(r"Get-Content .\in.txt; $(Stop-Process -Id 1234)",
                       "defer")

    def test_a_guarded_cmdlet_with_no_kill_still_allows(self):
        self._decision(r"Get-Content .\in.txt", "allow")

    def test_an_offending_kill_outranks_a_clean_cmdlet(self):
        # Suppression removes the `allow`; it must not also swallow the deny.
        self._decision(r"Get-Content .\in.txt; Stop-Process -Name node", "deny")


class PowerShellTaskkillEndToEndTests(PowerShellKillFixture, unittest.TestCase):
    """Decisions for `taskkill` through the PowerShell frontend (Q58).

    The same verdicts the Bash frontend gives the same command — `taskkill` is
    one program, and the two frontends must not disagree about it.
    """

    def test_image_name_denies(self):
        for cmd in ("taskkill /IM node.exe", "taskkill /F /T /IM node.exe",
                    "TASKKILL.EXE /IM node.exe"):
            self._decision(cmd, "deny")

    def test_filter_denies(self):
        self._decision('taskkill /FI "IMAGENAME eq node.exe"', "deny")

    def test_no_selector_denies(self):
        self._decision("taskkill", "deny")

    def test_kill_by_literal_pid_is_untouched(self):
        for cmd in ("taskkill /PID 1234", "taskkill /F /PID 1234",
                    "taskkill /?"):
            self._decision(cmd, "defer")

    def test_expandable_pid_denies(self):
        self._decision("taskkill /PID $p", "deny")

    def test_anchored_filter_defers(self):
        self._decision("taskkill /FI %s"
                       % ps("WINDOWTITLE eq %s"
                            % os.path.join(self.base, "wt-a", "*")), "defer")

    def test_sibling_worktree_filter_denies(self):
        self._decision("taskkill /FI %s"
                       % ps("WINDOWTITLE eq %s"
                            % os.path.join(self.base, "wt-b", "*")), "deny")

    def test_upstream_anchor_does_not_reach_a_taskkill(self):
        # The divergence from `Stop-Process`: `taskkill` reads no pipeline, so a
        # filter written upstream of it is not what selects the processes.
        self._decision(
            "Get-Process | Where-Object { $_.Path -like %s } | "
            "taskkill /IM node.exe" % self._glob("wt-a"), "deny")

    def test_reason_names_the_taskkill_rewrites(self):
        r = self._decision("taskkill /IM node.exe", "deny")
        self.assertIn("node.exe", r)
        self.assertIn("taskkill /PID", r)
        self.assertIn("tasklist", r)
        self.assertNotIn("Where-Object", r)   # the Stop-Process rewrite

    def test_override_downgrades_to_ask(self):
        out = self._out("taskkill /IM node.exe",
                        env_extra={"WORKSPACE_GUARD_OVERRIDE": "stuck harness"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_bypass_mode_still_denies(self):
        self._decision("taskkill /IM node.exe", "deny",
                       permission_mode="bypassPermissions")

    def test_a_clean_guarded_cmdlet_never_speaks_for_a_taskkill(self):
        # Q59's suppression has to reach this kill too: `taskkill /PID 1234`
        # earns no offender, which is exactly what leaves it available for a
        # clean `Get-Content` to launder into a blanket `allow`.
        for cmd in (r"Get-Content .\in.txt; taskkill /PID 1234",
                    r"taskkill /PID 1234; Get-Content .\in.txt",
                    r"Get-Content .\in.txt | taskkill /PID 1234",
                    r"Get-Content .\in.txt; $(taskkill /PID 1234)"):
            self._decision(cmd, "defer")

    def test_a_help_invocation_does_not_suppress_the_allow(self):
        # `taskkill /?` kills nothing, so there is no kill to speak for.
        self._decision(r"Get-Content .\in.txt; taskkill /?", "allow")

    def test_an_offending_taskkill_outranks_a_clean_cmdlet(self):
        # Suppression removes the `allow`; it must not also swallow the deny.
        self._decision(r"Get-Content .\in.txt; taskkill /IM node.exe", "deny")


class PowerShellEndToEndTests(unittest.TestCase):
    """Decisions the script emits for `tool_name: PowerShell`.

    The workspace lives under $HOME so an outside sibling is a plain `outside`
    ask rather than a host-temp deny. Windows-native targets are synthetic
    placeholders (repo rule) — the check is lexical, so nothing needs to exist.
    """

    OUT = r"C:\q51-fake-target\x"

    def setUp(self):
        home = guard.resolved_home()                      # Q43: not $HOME
        self.assertTrue(home and os.path.isdir(home),
                        f"no home directory to build the fixture under: {home!r}")
        self._tmp = tempfile.TemporaryDirectory(dir=home)
        self.base = os.path.realpath(self._tmp.name)
        self.workspace = os.path.join(self.base, "proj")
        os.makedirs(os.path.join(self.workspace, "docs"))
        for rel in ("in.txt", "docs/note.md"):
            with open(os.path.join(self.workspace, rel), "w") as f:
                f.write("hi\n")
        self.outside_dir = os.path.join(self.base, "outside")
        os.makedirs(self.outside_dir)
        self.outside = os.path.join(self.outside_dir, "secret.txt")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, command, *, tool="PowerShell", tool_input=_UNSET,
             permission_mode=None, cwd=None):
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = self.workspace
        data = {"tool_name": tool, "cwd": cwd or self.workspace,
                "tool_input": {"command": command}
                if tool_input is _UNSET else tool_input}
        if tool_input is None:
            data.pop("tool_input")
        if permission_mode is not None:
            data["permission_mode"] = permission_mode
        r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(data),
                           capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(r.returncode, 0, f"hook errored: {r.stderr!r}")
        out = r.stdout.strip()
        return json.loads(out) if out else None

    def _decide(self, command, expected, **kw):
        out = self._run(command, **kw)
        self.assertIsNotNone(out, f"expected {expected}, got defer for {command!r}")
        got = out["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(
            got, expected,
            f"expected {expected!r} for {command!r}; got {got!r} (reason: "
            f"{out['hookSpecificOutput'].get('permissionDecisionReason')!r})")
        return out

    def _defer(self, command, **kw):
        out = self._run(command, **kw)
        self.assertIsNone(out, f"expected defer for {command!r}, got {out!r}")

    # --- the gap Q51 closes --------------------------------------------------

    def test_native_windows_path_is_not_mangled_into_the_workspace(self):
        # Routed through the POSIX tokenizer this becomes `C:q51-fake-targetx`,
        # resolves under the project root, and allows silently.
        self._decide(r"Get-Content C:\q51-fake-target\x", "ask")

    def test_powershell_never_reaches_the_bash_handler(self):
        # `cat` is Get-Content here. Under the bash SPEC row the same string
        # would parse with cat's (empty) flag set — a different grammar.
        self._decide(r"cat C:\q51-fake-target\x", "ask")

    def test_missing_command_field_asks_rather_than_defers(self):
        # The field name is read off the installed binary, not documented
        # schema. Deferring here would be indistinguishable from the wiring bug
        # it would be hiding: a guard that reports itself active and checks
        # nothing.
        out = self._run(None, tool_input={"timeout": 5})
        self.assertIsNotNone(out, "a PowerShell call with no command must not defer")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("tool_input.command",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_absent_tool_input_asks(self):
        out = self._run(None, tool_input=None)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")

    # --- parity with the bash frontend ---------------------------------------

    def test_read_outside_asks(self):
        self._decide("Get-Content %s" % ps(self.outside), "ask")

    def test_read_inside_allows(self):
        self._decide("Get-Content in.txt", "allow")

    def test_write_outside_asks(self):
        self._decide("Set-Content %s hi" % ps(self.outside), "ask")

    def test_out_file_outside_asks(self):
        self._decide("Out-File -FilePath %s" % ps(self.outside), "ask")

    def test_select_string_outside_asks(self):
        self._decide("Select-String -Pattern foo %s" % ps(self.outside), "ask")

    def test_copy_item_outside_destination_asks(self):
        self._decide("Copy-Item in.txt %s" % ps(self.outside), "ask")

    def test_redirect_target_outside_asks_whatever_the_command(self):
        # Q26 parity: a redirect is a shell-level write, honored even though
        # Write-Output is not a guarded cmdlet.
        self._decide("Write-Output hi > %s" % ps(self.outside), "ask")

    def test_redirect_target_inside_defers(self):
        self._defer("Write-Output hi > out.txt")

    def test_unguarded_cmdlet_defers(self):
        self._defer(r"Get-ChildItem C:\q51-fake-target")

    def test_bypass_permissions_denies(self):
        self._decide("Get-Content %s" % ps(self.outside), "deny",
                     permission_mode="bypassPermissions")

    # --- location tracking ---------------------------------------------------

    def test_set_location_moves_the_resolution_base(self):
        self._decide("Set-Location %s; Get-Content secret.txt"
                     % ps(self.outside_dir), "ask")

    def test_set_location_inside_still_allows(self):
        self._decide("Set-Location docs; Get-Content note.md", "allow")

    def test_untracked_location_flags_relative_operands(self):
        out = self._decide("Set-Location $d; Get-Content secret.txt", "ask")
        self.assertIn("untracked",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_bare_cd_drops_tracking(self):
        self._decide("cd; Get-Content secret.txt", "ask")

    def test_pop_location_drops_tracking(self):
        self._decide("Push-Location docs; Pop-Location; Get-Content note.md", "ask")

    # --- deferring, and what must not defer ----------------------------------

    def test_empty_command_defers(self):
        self._defer("   ")

    def test_unbalanced_quote_defers(self):
        self._defer('Get-Content "unterminated')

    def test_commented_out_command_defers(self):
        self._defer(r"# Get-Content C:\q51-fake-target\x")

    def test_expandable_operand_asks(self):
        out = self._decide(r"Get-Content $env:USERPROFILE\x", "ask")
        self.assertIn("Runtime-expanded",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_subexpression_body_is_analyzed(self):
        self._decide('Write-Output "$(Get-Content %s)"' % ps(self.outside), "ask")

    def test_script_block_body_is_analyzed(self):
        self._decide("Get-ChildItem | ForEach-Object { Get-Content %s }"
                     % ps(self.outside), "ask")

    def test_literal_here_string_body_is_inert(self):
        self._decide("Set-Content in.txt @'\nGet-Content %s\n'@" % ps(self.outside),
                     "allow")

    def test_expandable_here_string_body_is_analyzed(self):
        self._decide('Set-Content in.txt @"\n$(Get-Content %s)\n"@'
                     % ps(self.outside), "ask")

    def test_assignment_head_is_stripped(self):
        self._decide("$x = Get-Content %s" % ps(self.outside), "ask")

    def test_array_operand_flags_the_outside_element(self):
        # An unquoted comma-joined operand, so a synthetic Windows-native
        # target rather than the fixture path: quoting it would suppress the
        # split this is about, and the check is lexical either way.
        self._decide("Get-Content -Path in.txt,%s" % self.OUT, "ask")


if __name__ == "__main__":
    unittest.main()
