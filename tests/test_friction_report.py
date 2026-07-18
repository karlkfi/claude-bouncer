#!/usr/bin/env python3
"""Tests for scripts/friction-report.py.

Run with: python3 -m unittest discover tests

Covers the pure parsing/normalization helpers and an end-to-end pass over a
synthetic transcript so the attachment-parsing and toolUseID join are pinned.
"""
import datetime as dt
import json
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

    def test_allow_reason_has_no_buckets(self):
        self.assertEqual(fr.categorize("Guarded commands target workspace/pipe only"), {})


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


class EndToEndTests(unittest.TestCase):
    """Synthetic transcript: one tool_use + one matching hook attachment."""

    def _transcript(self, tmp):
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

    def test_join_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._transcript(tmp)
            decs = list(fr.iter_decisions([str(Path(tmp) / "s.jsonl")],
                                          'workspace-guard', None, ''))
            self.assertEqual(len(decs), 1)
            d = decs[0]
            self.assertEqual(d['decision'], 'ask')
            self.assertEqual(d['command'], "cd /etc && grep root passwd")
            report = fr.build_report(decs, raw=True)
            self.assertEqual(report['categories']['outside'], 1)
            self.assertEqual(report['paths']['passwd'], 1)

    def test_plugin_filter_excludes_other_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._transcript(tmp)
            decs = list(fr.iter_decisions([str(Path(tmp) / "s.jsonl")],
                                          'branch-guard', None, ''))
            self.assertEqual(decs, [])


if __name__ == "__main__":
    unittest.main()
