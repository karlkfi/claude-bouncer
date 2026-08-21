#!/usr/bin/env python3
"""Tests for scripts/friction-report.py.

Run with: python3 scripts/run-tests.py

Covers the pure parsing/normalization helpers and an end-to-end pass over a
synthetic transcript so the attachment-parsing and toolUseID join are pinned.
"""
import collections
import contextlib
import datetime as dt
import io
import json
import subprocess
import sys
import tempfile
import unittest
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "friction-report.py"

_spec = util.spec_from_file_location("friction_report", SCRIPT)
fr = util.module_from_spec(_spec)
_spec.loader.exec_module(fr)


class CategorizeTests(unittest.TestCase):
    def test_outside_bucket(self):
        reason = ("Outside-workspace path(s): /etc/passwd, ../x. Fix: use a "
                  "path inside the project root, or read the file with the "
                  "Read/Grep/Glob tools instead of bash.")
        self.assertEqual(fr.categorize(reason),
                         {'outside': ['/etc/passwd', '../x']})

    def test_all_three_buckets_concatenated(self):
        reason = ("Outside-workspace path(s): a. Fix: x. "
                  "Runtime-expanded arg(s) bash resolves but the hook can't: "
                  "$f. Fix: y. "
                  "Relative path(s) after an untracked cd: b. Fix: z.")
        cats = fr.categorize(reason)
        self.assertEqual(set(cats), {'outside', 'expand', 'untracked'})
        self.assertEqual(cats['expand'], ['$f'])

    def test_deny_attribution_prefix_still_categorizes(self):
        # Deny reasons lead with `workspace-guard: `. REASON_PATTERNS are
        # applied unanchored, so the categories survive the prefix rather than
        # falling into 'other'.
        reason = ("workspace-guard: Outside-workspace path(s): /q5-fake. "
                  "Fix: use a path inside the project root.")
        self.assertEqual(fr.categorize(reason), {'outside': ['/q5-fake']})

    def test_unrecognized_reason_buckets_as_other(self):
        self.assertEqual(fr.categorize("Guarded commands target workspace/pipe only"),
                         {'other': []})

    def test_another_guards_reason_buckets_as_other(self):
        self.assertEqual(fr.categorize("Command runs against the production cluster."),
                         {'other': []})


class NormalizeTests(unittest.TestCase):
    def test_collapses_per_session_temp_path(self):
        a = fr.normalize_path("/private/tmp/claude-501/-Users-karl-proj/x")
        b = fr.normalize_path("/private/tmp/claude-999/-Users-karl-other/x")
        self.assertEqual(a, b)

    def test_collapses_tooluse_and_uuid(self):
        self.assertEqual(
            fr.normalize_path("sess/toolu_01ABCdef/out.json"),
            fr.normalize_path("sess/toolu_99ZZZ/out.json"))

    def test_leaves_plain_path_alone(self):
        self.assertEqual(fr.normalize_path("docs/STATUS.md"), "docs/STATUS.md")


class GuardNameTests(unittest.TestCase):
    def test_strips_bash_prefix_and_suffix(self):
        self.assertEqual(
            fr.guard_name('python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bash-workspace-guard.py"'),
            'workspace-guard')

    def test_non_guard_command_is_none(self):
        self.assertIsNone(fr.guard_name('grep foo bar'))


class SinceTests(unittest.TestCase):
    def test_relative_window(self):
        cut = fr.parse_since('2d')
        now = dt.datetime.now(dt.timezone.utc)
        self.assertLess(abs((now - cut).total_seconds() - 2 * 86400), 5)

    def test_iso_date(self):
        self.assertEqual(fr.parse_since('2026-06-01').year, 2026)


class VersionTupleTests(unittest.TestCase):
    def test_dotted_release(self):
        self.assertEqual(fr.version_tuple("1.5.0"), (1, 5, 0))

    def test_prerelease_folds_to_base(self):
        self.assertEqual(fr.version_tuple("1.5.0-rc1"), (1, 5, 0))

    def test_ordering(self):
        self.assertLess(fr.version_tuple("1.3.0"), fr.version_tuple("1.5.0"))
        self.assertLess(fr.version_tuple("1.5.0"), fr.version_tuple("1.5.1"))

    def test_empty_and_nonnumeric(self):
        self.assertIsNone(fr.version_tuple(""))
        self.assertIsNone(fr.version_tuple(None))
        self.assertIsNone(fr.version_tuple("dev"))


class StalenessTests(unittest.TestCase):
    """A synthetic plugins dir standing in for ~/.claude/plugins."""

    def _plugins_dir(self, tmp, installed, available, marketplace="workspace-guard",
                     plugin="workspace-guard"):
        root = Path(tmp)
        (root / "installed_plugins.json").write_text(json.dumps({
            "version": 2,
            "plugins": {
                f"{plugin}@{marketplace}": [
                    {"scope": "user", "version": installed},
                ]
            }}))
        (root / "known_marketplaces.json").write_text(json.dumps({
            marketplace: {"installLocation": str(root / "mkt")}}))
        clone = root / "mkt" / ".claude-plugin"
        clone.mkdir(parents=True)
        (clone / "plugin.json").write_text(json.dumps(
            {"name": plugin, "version": available}))
        return str(root)

    def test_flags_older_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._plugins_dir(tmp, installed="1.3.0", available="1.5.0")
            s = fr.check_staleness(d, "workspace-guard")
            self.assertEqual(s, {"plugin": "workspace-guard", "installed": "1.3.0",
                                 "available": "1.5.0", "marketplace": "workspace-guard"})

    def test_current_install_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._plugins_dir(tmp, installed="1.5.0", available="1.5.0")
            self.assertIsNone(fr.check_staleness(d, "workspace-guard"))

    def test_newer_install_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._plugins_dir(tmp, installed="1.6.0", available="1.5.0")
            self.assertIsNone(fr.check_staleness(d, "workspace-guard"))

    def test_all_plugin_skips_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._plugins_dir(tmp, installed="1.3.0", available="1.5.0")
            self.assertIsNone(fr.check_staleness(d, "all"))

    def test_missing_state_degrades_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(fr.check_staleness(tmp, "workspace-guard"))

    def test_falls_back_to_marketplace_manifest_version(self):
        # plugin.json name mismatches (multi-plugin marketplace); the per-plugin
        # version in marketplace.json is used instead.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "installed_plugins.json").write_text(json.dumps({
                "plugins": {"workspace-guard@mp": [{"version": "1.3.0"}]}}))
            (root / "known_marketplaces.json").write_text(json.dumps({
                "mp": {"installLocation": str(root / "mkt")}}))
            clone = root / "mkt" / ".claude-plugin"
            clone.mkdir(parents=True)
            (clone / "plugin.json").write_text(json.dumps(
                {"name": "other-plugin", "version": "9.9.9"}))
            (clone / "marketplace.json").write_text(json.dumps({
                "plugins": [{"name": "workspace-guard", "version": "1.5.0"}]}))
            s = fr.check_staleness(str(root), "workspace-guard")
            self.assertEqual(s["available"], "1.5.0")


class PrintTextTests(unittest.TestCase):
    """The path ranking is workspace-guard-scoped; say so under --plugin all."""

    REPORT = {
        'total': 2,
        'decisions': collections.Counter({'ask': 2}),
        'plugins': collections.Counter({'workspace-guard': 1, 'prod-guard': 1}),
        'categories': collections.Counter({'outside': 1, 'other': 1}),
        'paths': collections.Counter({'passwd': 1}),
        'commands': collections.Counter({'grep root passwd': 1}),
    }

    def _render(self, plugin):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fr.print_text(self.REPORT, 15, None, plugin)
        return buf.getvalue()

    def test_all_plugins_labels_path_scope(self):
        out = self._render('all')
        self.assertIn("Top offending paths (workspace-guard only, top 15):", out)
        self.assertIn('"other" =', out)

    def test_single_plugin_stays_unlabeled(self):
        out = self._render('workspace-guard')
        self.assertIn("Top offending paths (top 15):", out)
        self.assertNotIn('"other" =', out)

    def test_header_says_counts_are_floors(self):
        # A hook that returns early emits nothing and so is invisible here; the
        # header has to say so or a hidden path reads as a quiet guard (96).
        out = " ".join(self._render('workspace-guard').split())
        self.assertIn("coverage: Emitted decisions only", out)
        self.assertIn("these totals are floors", out)

    def test_all_plugins_disclaims_the_cross_guard_ranking(self):
        out = " ".join(self._render('all').split())
        self.assertIn("not a like-for-like ranking", out)
        self.assertNotIn("like-for-like",
                         " ".join(self._render('workspace-guard').split()))


def write_transcript(tmp):
    """Synthetic transcript: one tool_use + one matching hook attachment."""
    path = Path(tmp) / "s.jsonl"
    tool_use = {"message": {"content": [
        {"type": "tool_use", "name": "Bash", "id": "toolu_X",
         "input": {"command": "cd /etc && grep root passwd"}}]}}
    attach = {
        "type": "attachment", "cwd": "/home/u/proj",
        "timestamp": "2026-06-14T12:00:00.000Z",
        "attachment": {
            "type": "hook_success", "hookName": "PreToolUse:Bash",
            "toolUseID": "toolu_X",
            "command": 'python3 ".../scripts/bash-workspace-guard.py"',
            "stdout": json.dumps({"hookSpecificOutput": {
                "permissionDecision": "ask",
                "permissionDecisionReason":
                    "Outside-workspace path(s): passwd. Fix: x."}}),
        }}
    path.write_text(json.dumps(tool_use) + "\n" + json.dumps(attach) + "\n")
    return path


class EndToEndTests(unittest.TestCase):

    def test_join_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_transcript(tmp)
            decs, _ = fr.scan([str(Path(tmp) / "s.jsonl")],
                              'workspace-guard', None, '')
            self.assertEqual(len(decs), 1)
            d = decs[0]
            self.assertEqual(d['decision'], 'ask')
            self.assertEqual(d['command'], "cd /etc && grep root passwd")
            report = fr.build_report(decs, raw=True)
            self.assertEqual(report['categories']['outside'], 1)
            self.assertEqual(report['paths']['passwd'], 1)

    def test_plugin_filter_excludes_other_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_transcript(tmp)
            decs, _ = fr.scan([str(Path(tmp) / "s.jsonl")],
                              'branch-guard', None, '')
            self.assertEqual(decs, [])

    def test_all_plugins_categories_sum_to_friction(self):
        # Under --plugin all another guard's prompt has no recognizable reason;
        # it must land in 'other' rather than vanish from the table (issue 96).
        with tempfile.TemporaryDirectory() as tmp:
            path = write_transcript(tmp)
            other = {
                "type": "attachment", "cwd": "/home/u/proj",
                "timestamp": "2026-06-14T12:01:00.000Z",
                "attachment": {
                    "type": "hook_success", "hookName": "PreToolUse:Bash",
                    "toolUseID": "toolu_Y",
                    "command": 'python3 ".../scripts/bash-foreground-guard.py"',
                    "stdout": json.dumps({"hookSpecificOutput": {
                        "permissionDecision": "ask",
                        "permissionDecisionReason": "Long-running foreground command."}}),
                }}
            with path.open("a") as fh:
                fh.write(json.dumps(other) + "\n")

            decs, _ = fr.scan([str(path)], 'all', None, '')
            report = fr.build_report(decs, raw=True)
            self.assertEqual(report['plugins']['foreground-guard'], 1)
            self.assertEqual(sum(report['categories'].values()), 2)
            self.assertEqual(report['categories']['other'], 1)
            # Paths stay workspace-guard-scoped: the other guard adds no tokens.
            self.assertEqual(list(report['paths']), ['passwd'])


class EmptyResultTests(unittest.TestCase):
    """An empty result must name the filter that emptied it (issue 97), so a
    typo can't read the same as a guard with zero friction."""

    def _survey(self, tmp, plugin, cutoff=None, repo=''):
        write_transcript(tmp)
        decs, survey = fr.scan([str(Path(tmp) / "s.jsonl")], plugin, cutoff, repo)
        self.assertEqual(decs, [])
        return survey

    def test_unknown_plugin_names_the_labels_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self._survey(tmp, 'pr-sentinel')
            notes = "\n".join(fr.explain_empty(s, 'pr-sentinel', 'all', ''))
            self.assertIn("--plugin 'pr-sentinel' matched no guard", notes)
            self.assertIn("workspace-guard (1)", notes)
            # A label mismatch is not the only cause: a guard that emitted
            # nothing is equally absent from the labels we saw.
            self.assertIn("emitted nothing", notes)

    def test_unknown_repo_blames_the_repo_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self._survey(tmp, 'workspace-guard', repo='no-such-repo')
            notes = "\n".join(fr.explain_empty(s, 'workspace-guard', 'all',
                                               'no-such-repo'))
            self.assertIn("--repo 'no-such-repo' matched no cwd", notes)
            self.assertIn("workspace-guard's 1 decisions", notes)

    def test_empty_window_blames_since_and_dates_the_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            cutoff = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
            s = self._survey(tmp, 'workspace-guard', cutoff=cutoff)
            notes = "\n".join(fr.explain_empty(s, 'workspace-guard', '7d', ''))
            self.assertIn("--since 7d excluded all 1 matching decisions", notes)
            self.assertIn("2026-06-14", notes)

    def test_no_decisions_at_all_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "empty.jsonl").write_text("")
            decs, s = fr.scan([str(Path(tmp) / "empty.jsonl")], 'all', None, '')
            self.assertEqual(decs, [])
            notes = "\n".join(fr.explain_empty(s, 'all', 'all', ''))
            self.assertIn("no guard decisions in the scanned transcripts at all",
                          notes.lower())


class ExitCodeTests(unittest.TestCase):
    """A filter nothing can match exits non-zero; a real zero exits 0."""

    def _run(self, tmp, *extra):
        empty_plugins = Path(tmp) / "plugins"
        empty_plugins.mkdir(exist_ok=True)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--transcripts", tmp,
             "--plugins-dir", str(empty_plugins), *extra],
            capture_output=True, text=True)

    def test_unknown_plugin_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_transcript(tmp)
            p = self._run(tmp, "--since", "all", "--plugin", "totally-bogus-name")
            self.assertEqual(p.returncode, 2)
            self.assertIn("Guards found: workspace-guard (1)", p.stdout)

    def test_known_plugin_with_empty_window_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_transcript(tmp)
            p = self._run(tmp, "--plugin", "workspace-guard", "--since", "1h")
            self.assertEqual(p.returncode, 0)
            self.assertIn("--since 1h excluded all 1 matching decisions", p.stdout)

    def test_json_carries_the_explanation(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_transcript(tmp)
            p = self._run(tmp, "--since", "all", "--plugin",
                          "totally-bogus-name", "--json")
            self.assertEqual(p.returncode, 2)
            out = json.loads(p.stdout)
            self.assertEqual(out['total'], 0)
            self.assertEqual(out['guards_seen'], {'workspace-guard': 1})
            self.assertTrue(out['empty_because'])

    def test_json_carries_the_coverage_caveat(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_transcript(tmp)
            p = self._run(tmp, "--since", "all", "--plugin", "all", "--json")
            self.assertEqual(p.returncode, 0)
            out = json.loads(p.stdout)
            self.assertEqual(len(out['coverage']), 2)
            self.assertIn("floors", out['coverage'][0])


if __name__ == "__main__":
    unittest.main()
