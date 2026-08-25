#!/usr/bin/env python3
"""Tests for scripts/friction-report.py.

Run with: python3 -m unittest discover tests
     or:  python3 tests/test_friction_report.py

The friction report is a read-only analyzer over the decisions Claude Code
persists in ~/.claude/projects/**/*.jsonl. These tests build synthetic
transcript files (attachment records + matching Bash tool_use blocks) and assert
the report's categorization, target extraction, and joins — never touching a real
transcript.

LiveCategoryTests is the exception to the literals: it runs the hook itself and
categorizes what comes back, so a reworded reason fails here rather than in six
weeks of transcripts silently counted as 'other'.

Fixture rule (same as test_prod_guard.py): synthetic names only
(`gke_acme_prod-us`, `kind-ci`, `bluefin`) — never real production targets.
"""
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "friction-report.py"

# Filename has dashes, so import by path.
_spec = util.spec_from_file_location("friction_report", SCRIPT)
fr = util.module_from_spec(_spec)
_spec.loader.exec_module(fr)

# The sibling suite's subprocess harness, for the live-output tests below.
# Imported as a module rather than by name: `from ... import *` would bind its
# TestCases here too and unittest would collect the whole prod-guard suite a
# second time.
import test_prod_guard as pg  # noqa: E402


# The reason strings the hook emits, one per builder, transcribed from a real
# run. LiveCategoryTests re-derives them from the hook and fails if a reword
# leaves these behind; they stay literals so the report tests below can build
# transcripts without paying for a subprocess each.
REASON_DENY_PROD = (
    "prod-guard: `kubectl delete ns x` targets kube-context "
    "'gke_acme_prod-us' (from --context), which matches a production pattern. "
    "Mutating commands against production targets are blocked. If this is "
    "intentional, prefix the command with PROD_GUARD_OVERRIDE=<reason> to "
    "downgrade the block to a confirmation prompt. Patterns: built-ins plus "
    ".claude/prod-guard.json (see the prod-guard README).")
REASON_ASK_UNKNOWN = (
    "prod-guard: `aws s3 rm s3://b` targets aws profile 'bluefin', which "
    "matches neither a production nor a non-production pattern — unknown "
    "targets are never silently allowed. Confirm it is safe, or add a nonprod "
    "pattern to .claude/prod-guard.json to classify it. Patterns: built-ins "
    "plus .claude/prod-guard.json (see the prod-guard README).")
REASON_ASK_UNKNOWN_VAR = (
    "prod-guard: `kubectl apply` targets kube-context '$CTX' (from --context), "
    "which matches neither a production nor a non-production pattern — unknown "
    "targets are never silently allowed. Confirm it is safe, or add a nonprod "
    "pattern to .claude/prod-guard.json to classify it. Patterns: built-ins "
    "plus .claude/prod-guard.json (see the prod-guard README).")
REASON_DENY_AMBIENT = (
    "prod-guard: `kubectl apply` relies on the ambient kube-context (currently "
    "'kind-ci') — shared mutable state that a parallel session can repoint "
    "before this command runs, so the true target is ambiguous at run time. "
    "Mutating commands must pin their target explicitly: add kubectl --context "
    "<ctx> and retry. To run against the ambient target anyway, prefix the "
    "command with PROD_GUARD_OVERRIDE=<reason> for a confirmation prompt.")
# deny_switch and ask_switch both carry 'is shared by every session', so only
# deny_switch's 'Switching shared state is blocked' separates them and the
# CATEGORY_PATTERNS iteration order decides which wins. Both are pinned against
# live output below.
REASON_DENY_SWITCH = (
    "prod-guard: `kubectx bluefin` repoints the shared kubeconfig "
    "current-context, which is shared by every session on this machine — "
    "parallel sessions relying on ambient context will silently follow it. "
    "Switching shared state is blocked: pin the target per command with "
    "kubectl --context <ctx> instead. If the switch is genuinely intended, "
    "prefix the command with PROD_GUARD_OVERRIDE=<reason> for a confirmation "
    "prompt.")
REASON_ASK_SWITCH = (
    "prod-guard: `kubectx -d` writes the shared kubeconfig, which is shared by "
    "every session on this machine — parallel sessions relying on ambient "
    "context will silently follow it. Prefer per-command pinning "
    "(--context/--project/--profile) over switching shared state.")
REASON_OVERRIDE = (
    "prod-guard: override acknowledged (PROD_GUARD_OVERRIDE is set) — downgraded "
    "from deny to a confirmation prompt. " + REASON_DENY_PROD)
REASON_SESSION_OVERRIDE = (
    "prod-guard: session override acknowledged (PROD_GUARD_SESSION_OVERRIDE is "
    "set) — downgraded from deny to a confirmation prompt. Approving records a "
    "session grant for target(s) 'gke_acme_prod-us': further "
    "PROD_GUARD_SESSION_OVERRIDE-prefixed commands against them in this session "
    "will not re-prompt (expires after 8 h). " + REASON_DENY_PROD)
# The pre-colon wording, as written by installs before the opener became the
# cross-guard attribution key. Old transcripts are still in the analyzed window,
# so both forms must land in the override counter.
REASON_OVERRIDE_LEGACY = (
    "prod-guard override acknowledged (PROD_GUARD_OVERRIDE is set) — downgraded "
    "from deny to a confirmation prompt. " + REASON_DENY_PROD)
REASON_SESSION_OVERRIDE_LEGACY = (
    "prod-guard session override acknowledged (PROD_GUARD_SESSION_OVERRIDE is "
    "set) — downgraded from deny to a confirmation prompt. " + REASON_DENY_PROD)
# A sibling guard's downgrade, as seen under --plugin all. Same phrasing, other
# owner — it must not land in prod-guard's override counter.
REASON_FOREIGN_OVERRIDE = (
    "foreground-guard override acknowledged (FOREGROUND_GUARD_OVERRIDE is set) "
    "— downgraded from deny to a confirmation prompt.")


def _decision_record(tooluseid, command, stdout, cwd="/home/u/proj", ts=None,
                     hook_cmd='python3 "/x/scripts/bash-prod-guard.py"'):
    """A transcript attachment record for one hook decision, plus the assistant
    tool_use record that command is joined from."""
    att = {
        "timestamp": ts or "2026-07-01T12:00:00Z",
        "cwd": cwd,
        "attachment": {
            "hookName": "PreToolUse:Bash",
            "command": hook_cmd,
            "toolUseID": tooluseid,
            "stdout": stdout,
        },
    }
    use = {
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "id": tooluseid,
             "input": {"command": command}}]},
    }
    return use, att


def _stdout(decision, reason):
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}})


def write_transcript(lines):
    """Write records (dicts) as a .jsonl transcript in a fresh temp dir; return
    the transcript root."""
    root = tempfile.mkdtemp(prefix="prod-guard-friction-")
    proj = os.path.join(root, "-home-u-proj")
    os.makedirs(proj)
    with open(os.path.join(proj, "session.jsonl"), "w", encoding="utf-8") as f:
        for rec in lines:
            f.write(json.dumps(rec) + "\n")
    return root


def write_plugins_dir(root, installed, available, plugin="prod-guard",
                      marketplace="claude-bouncer"):
    """A synthetic ~/.claude/plugins: one install record plus a marketplace
    clone advertising `available`. Returns the dir as a string."""
    root = Path(root)
    (root / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {
            f"{plugin}@{marketplace}": [{"scope": "user", "version": installed}],
        }}))
    (root / "known_marketplaces.json").write_text(json.dumps({
        marketplace: {"installLocation": str(root / "mkt")}}))
    clone = root / "mkt" / ".claude-plugin"
    clone.mkdir(parents=True)
    (clone / "plugin.json").write_text(json.dumps(
        {"name": plugin, "version": available}))
    return str(root)


class ParseSinceTests(unittest.TestCase):
    def test_relative_units(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.assertLess(fr.parse_since("7d"), now)
        self.assertAlmostEqual(
            (now - fr.parse_since("30m")).total_seconds(), 1800, delta=5)

    def test_absolute_date(self):
        self.assertEqual(fr.parse_since("2026-06-01").year, 2026)

    def test_bad_spec_exits(self):
        with self.assertRaises(SystemExit):
            fr.parse_since("banana")

    def test_empty_is_none(self):
        self.assertIsNone(fr.parse_since(None))


class GuardNameTests(unittest.TestCase):
    def test_strips_bash_prefix(self):
        self.assertEqual(
            fr.guard_name('python3 "/x/scripts/bash-prod-guard.py"'),
            "prod-guard")

    def test_workspace_guard(self):
        self.assertEqual(
            fr.guard_name('python3 "/x/bash-workspace-guard.py"'),
            "workspace-guard")

    def test_no_py_is_none(self):
        self.assertIsNone(fr.guard_name("kubectl get pods"))


class CategoryTests(unittest.TestCase):
    def test_each_builder(self):
        self.assertEqual(fr.category_of(REASON_DENY_PROD), "deny-prod")
        self.assertEqual(fr.category_of(REASON_ASK_UNKNOWN), "ask-unknown")
        self.assertEqual(fr.category_of(REASON_DENY_AMBIENT), "deny-ambient")
        self.assertEqual(fr.category_of(REASON_DENY_SWITCH), "deny-switch")
        self.assertEqual(fr.category_of(REASON_ASK_SWITCH), "ask-switch")

    def test_unmatched_is_other(self):
        self.assertEqual(fr.category_of("prod-guard: something novel"), "other")

    def test_override_keeps_deny_prod_category(self):
        # The override prefix does not change the underlying category.
        self.assertEqual(fr.category_of(REASON_OVERRIDE), "deny-prod")

    def test_session_override_keeps_deny_prod_category(self):
        # The session-override first-use ask carries the same 'override
        # acknowledged' signature, so it counts as an override downgrade too.
        self.assertEqual(fr.category_of(REASON_SESSION_OVERRIDE), "deny-prod")


# One command per category, with whatever ambient config that path reads. The
# category is decided by the reason string the hook prints, so these are the
# only cases that measure CATEGORY_PATTERNS against the thing it parses.
LIVE_CASES = (
    ("deny-prod",    "kubectl delete ns x --context gke_acme_prod-us", {}),
    ("ask-unknown",  "aws s3 rm s3://b --profile bluefin", {}),
    ("deny-ambient", "kubectl apply -f x.yaml", {"kubeconfig": pg.KUBECONFIG_KIND}),
    ("deny-switch",  "kubectx bluefin", {"kubeconfig": pg.KUBECONFIG_KIND}),
    ("ask-switch",   "kubectx -d bluefin", {"kubeconfig": pg.KUBECONFIG_KIND}),
)

LITERAL_BY_CATEGORY = {
    "deny-prod":    REASON_DENY_PROD,
    "ask-unknown":  REASON_ASK_UNKNOWN,
    "deny-ambient": REASON_DENY_AMBIENT,
    "deny-switch":  REASON_DENY_SWITCH,
    "ask-switch":   REASON_ASK_SWITCH,
}


class LiveCategoryTests(unittest.TestCase):
    """CATEGORY_PATTERNS against what the hook prints, not against a copy of it.

    A literal fixture pins the wording whoever wrote it transcribed. Only the
    subprocess pins the wording the hook emits, and the two drift silently: a
    reworded reason still parses, so both suites stay green while every real
    transcript falls through to 'other'.
    """

    def _live(self, command, home_kwargs):
        _decision, reason = pg.run_hook(command, home=pg.make_home(**home_kwargs))
        self.assertTrue(reason, "hook stayed silent on %r" % command)
        return reason

    def test_each_category_from_live_output(self):
        for want, command, home_kwargs in LIVE_CASES:
            with self.subTest(category=want):
                self.assertEqual(
                    fr.category_of(self._live(command, home_kwargs)), want)

    def test_every_pattern_has_a_live_case(self):
        # A pattern added without a command that provokes it is a pattern
        # nothing measures — which is the state deny-switch was in.
        self.assertEqual(sorted(c for c, _cmd, _kw in LIVE_CASES),
                         sorted(fr.CATEGORY_PATTERNS))

    def test_unresolvable_variable_reaches_the_reason(self):
        # REASON_ASK_UNKNOWN_VAR is hand-transcribed like its neighbours, and
        # the shape it stands for is the one Q128 measured: the assignment is
        # in the same command string but outside the nested quote context, so
        # the hook cannot resolve it and prints the variable name.
        reason = self._live(
            'C=gke_acme_prod-us; bash -c "kubectl --context $C delete pod x"', {})
        self.assertEqual(fr.category_of(reason), "ask-unknown")
        self.assertIn("$C", fr.targets_of(reason))

    def test_literal_fixtures_are_what_the_hook_emits(self):
        # The fixtures at the top of this file are transcribed by hand, which is
        # how one of them came to be a string no emit path produces: deny_switch's
        # opener spliced onto ask_switch's tail. It categorized as ask-switch and
        # was asserted green for as long as it stood. Equality is what keeps them
        # honest — on a deliberate reword, paste the new reason in.
        for want, command, home_kwargs in LIVE_CASES:
            with self.subTest(category=want):
                self.assertEqual(LITERAL_BY_CATEGORY[want],
                                 self._live(command, home_kwargs))


class TargetExtractionTests(unittest.TestCase):
    def test_extracts_quoted_target(self):
        self.assertIn("gke_acme_prod-us", fr.targets_of(REASON_DENY_PROD))
        self.assertIn("bluefin", fr.targets_of(REASON_ASK_UNKNOWN))

    def test_drops_placeholder(self):
        seg = "prod-guard: `docker push` targets image ref '<unresolved>'"
        self.assertEqual(fr.targets_of(seg), [])

    def test_variable_target_is_extracted(self):
        # targets_of still yields it — 'Top targets' ranks the friction.
        self.assertIn("$CTX", fr.targets_of(REASON_ASK_UNKNOWN_VAR))

    def test_pattern_candidate(self):
        self.assertTrue(fr.is_pattern_candidate("bluefin"))
        self.assertFalse(fr.is_pattern_candidate("$CTX"))

    def test_tool_of(self):
        self.assertEqual(fr.tool_of(REASON_DENY_PROD), "kubectl")
        self.assertEqual(fr.tool_of(REASON_ASK_UNKNOWN), "aws")
        self.assertIsNone(fr.tool_of("no action here"))


class BuildReportTests(unittest.TestCase):
    def _report(self, decisions):
        return fr.build_report(decisions)

    def test_outcomes_and_friction(self):
        r = self._report([
            {"plugin": "prod-guard", "decision": "deny", "reason": REASON_DENY_PROD,
             "command": "kubectl delete ns x"},
            {"plugin": "prod-guard", "decision": "ask", "reason": REASON_ASK_UNKNOWN,
             "command": "aws s3 rm s3://b"},
            {"plugin": "prod-guard", "decision": "defer", "reason": "",
             "command": "kubectl get pods"},
        ])
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["decisions"]["deny"], 1)
        self.assertEqual(r["decisions"]["ask"], 1)
        self.assertEqual(r["decisions"]["defer"], 1)

    def test_unknown_target_is_pattern_gap(self):
        r = self._report([
            {"plugin": "prod-guard", "decision": "ask", "reason": REASON_ASK_UNKNOWN,
             "command": "aws s3 rm s3://b"},
        ])
        self.assertEqual(r["unknown_targets"]["bluefin"], 1)
        # A prod-denied target is a top target but NOT a pattern-gap candidate.
        r2 = self._report([
            {"plugin": "prod-guard", "decision": "deny", "reason": REASON_DENY_PROD,
             "command": "kubectl delete ns x"},
        ])
        self.assertEqual(r2["unknown_targets"].get("gke_acme_prod-us"), None)
        self.assertEqual(r2["targets"]["gke_acme_prod-us"], 1)

    def test_variable_target_is_not_a_pattern_gap(self):
        """A nonprod pattern for '$CTX' would classify every context held in
        that variable, so the section that solicits one must not list it."""
        r = self._report([
            {"plugin": "prod-guard", "decision": "ask",
             "reason": REASON_ASK_UNKNOWN_VAR,
             "command": 'CTX=gke_acme_prod-us; bash -c "kubectl --context $CTX apply -f x"'},
        ])
        self.assertEqual(r["unknown_targets"].get("$CTX"), None)
        self.assertEqual(r["targets"]["$CTX"], 1)

    def test_override_counted(self):
        r = self._report([
            {"plugin": "prod-guard", "decision": "ask", "reason": REASON_OVERRIDE,
             "command": "PROD_GUARD_OVERRIDE=x kubectl delete ns y"},
        ])
        self.assertEqual(r["overrides"], 1)
        self.assertEqual(r["decisions"]["ask"], 1)

    def test_session_override_counted(self):
        r = self._report([
            {"plugin": "prod-guard", "decision": "ask",
             "reason": REASON_SESSION_OVERRIDE,
             "command": "PROD_GUARD_SESSION_OVERRIDE=x kubectl delete ns y"},
        ])
        self.assertEqual(r["overrides"], 1)

    def test_legacy_override_wording_still_counted(self):
        """Transcripts predating the colon are still in the analyzed window."""
        r = self._report([
            {"plugin": "prod-guard", "decision": "ask",
             "reason": REASON_OVERRIDE_LEGACY,
             "command": "PROD_GUARD_OVERRIDE=x kubectl delete ns y"},
            {"plugin": "prod-guard", "decision": "ask",
             "reason": REASON_SESSION_OVERRIDE_LEGACY,
             "command": "PROD_GUARD_SESSION_OVERRIDE=x kubectl delete ns y"},
        ])
        self.assertEqual(r["overrides"], 2)

    def test_foreign_guard_override_not_counted(self):
        r = self._report([
            {"plugin": "foreground-guard", "decision": "ask",
             "reason": REASON_FOREIGN_OVERRIDE,
             "command": "FOREGROUND_GUARD_OVERRIDE=x sleep 600"},
            {"plugin": "prod-guard", "decision": "ask", "reason": REASON_OVERRIDE,
             "command": "PROD_GUARD_OVERRIDE=x kubectl delete ns y"},
        ])
        self.assertEqual(r["overrides"], 1)

    def test_joined_reason_hits_both_categories(self):
        joined = REASON_DENY_AMBIENT + " | " + REASON_ASK_SWITCH
        r = self._report([
            {"plugin": "prod-guard", "decision": "deny", "reason": joined,
             "command": "kubectl apply -f x"},
        ])
        self.assertEqual(r["categories"]["deny-ambient"], 1)
        self.assertEqual(r["categories"]["ask-switch"], 1)

    def test_tool_breakdown(self):
        r = self._report([
            {"plugin": "prod-guard", "decision": "deny", "reason": REASON_DENY_PROD,
             "command": "kubectl delete ns x"},
            {"plugin": "prod-guard", "decision": "ask", "reason": REASON_ASK_UNKNOWN,
             "command": "aws s3 rm s3://b"},
        ])
        self.assertEqual(r["tools"]["kubectl"], 1)
        self.assertEqual(r["tools"]["aws"], 1)


class IterDecisionsTests(unittest.TestCase):
    def test_join_and_filter(self):
        use1, att1 = _decision_record(
            "toolu_1", "kubectl delete ns x", _stdout("deny", REASON_DENY_PROD))
        # A workspace-guard decision that must be excluded by --plugin prod-guard.
        use2, att2 = _decision_record(
            "toolu_2", "cat /etc/x", _stdout("ask", "Outside-workspace path(s): /etc/x. Fix: ..."),
            hook_cmd='python3 "/x/bash-workspace-guard.py"')
        root = write_transcript([use1, att1, use2, att2])
        paths = [str(p) for p in Path(root).rglob("*.jsonl")]

        got = list(fr.iter_decisions(paths, "prod-guard", None, ""))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["decision"], "deny")
        self.assertEqual(got[0]["command"], "kubectl delete ns x")

        # --plugin all sees both.
        self.assertEqual(len(list(fr.iter_decisions(paths, "all", None, ""))), 2)

    def test_repo_filter(self):
        use, att = _decision_record(
            "toolu_1", "kubectl delete ns x", _stdout("deny", REASON_DENY_PROD),
            cwd="/home/u/gateway")
        root = write_transcript([use, att])
        paths = [str(p) for p in Path(root).rglob("*.jsonl")]
        self.assertEqual(len(list(fr.iter_decisions(paths, "prod-guard", None, "gateway"))), 1)
        self.assertEqual(len(list(fr.iter_decisions(paths, "prod-guard", None, "nope"))), 0)

    def test_cutoff_filter(self):
        use, att = _decision_record(
            "toolu_1", "kubectl delete ns x", _stdout("deny", REASON_DENY_PROD),
            ts="2020-01-01T00:00:00Z")
        root = write_transcript([use, att])
        paths = [str(p) for p in Path(root).rglob("*.jsonl")]
        cutoff = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(len(list(fr.iter_decisions(paths, "prod-guard", cutoff, ""))), 0)

    def test_malformed_line_skipped(self):
        root = tempfile.mkdtemp(prefix="prod-guard-friction-bad-")
        with open(os.path.join(root, "s.jsonl"), "w", encoding="utf-8") as f:
            f.write("{not json\n")
            use, att = _decision_record(
                "toolu_1", "kubectl delete ns x", _stdout("deny", REASON_DENY_PROD))
            f.write(json.dumps(use) + "\n")
            f.write(json.dumps(att) + "\n")
        paths = [os.path.join(root, "s.jsonl")]
        self.assertEqual(len(list(fr.iter_decisions(paths, "prod-guard", None, ""))), 1)


class VersionTupleTests(unittest.TestCase):
    def test_dotted_release(self):
        self.assertEqual(fr.version_tuple("2.4.0"), (2, 4, 0))

    def test_prerelease_folds_to_base(self):
        self.assertEqual(fr.version_tuple("2.4.0-rc1"), (2, 4, 0))

    def test_ordering(self):
        self.assertLess(fr.version_tuple("1.1.0"), fr.version_tuple("2.0.0"))
        self.assertLess(fr.version_tuple("2.4.0"), fr.version_tuple("2.4.1"))

    def test_empty_and_nonnumeric(self):
        self.assertIsNone(fr.version_tuple(""))
        self.assertIsNone(fr.version_tuple(None))
        self.assertIsNone(fr.version_tuple("dev"))


class StalenessTests(unittest.TestCase):
    def test_flags_older_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = write_plugins_dir(tmp, installed="1.1.0", available="2.4.0")
            self.assertEqual(
                fr.check_staleness(d, "prod-guard"),
                {"plugin": "prod-guard", "installed": "1.1.0",
                 "available": "2.4.0", "marketplace": "claude-bouncer"})

    def test_current_install_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = write_plugins_dir(tmp, installed="2.4.0", available="2.4.0")
            self.assertIsNone(fr.check_staleness(d, "prod-guard"))

    def test_newer_install_not_flagged(self):
        # A local dev install ahead of the clone is not staleness.
        with tempfile.TemporaryDirectory() as tmp:
            d = write_plugins_dir(tmp, installed="2.5.0", available="2.4.0")
            self.assertIsNone(fr.check_staleness(d, "prod-guard"))

    def test_all_plugin_skips_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = write_plugins_dir(tmp, installed="1.1.0", available="2.4.0")
            self.assertIsNone(fr.check_staleness(d, "all"))

    def test_highest_version_across_scopes_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_plugins_dir(tmp, installed="1.1.0", available="2.4.0")
            (root / "installed_plugins.json").write_text(json.dumps({
                "plugins": {"prod-guard@claude-bouncer": [
                    {"scope": "project", "version": "1.1.0"},
                    {"scope": "user", "version": "2.4.0"},
                ]}}))
            self.assertIsNone(fr.check_staleness(str(root), "prod-guard"))

    def test_missing_state_degrades_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(fr.check_staleness(tmp, "prod-guard"))

    def test_unparseable_state_degrades_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "installed_plugins.json").write_text("{not json")
            self.assertIsNone(fr.check_staleness(tmp, "prod-guard"))

    def test_falls_back_to_marketplace_manifest_version(self):
        # plugin.json names another plugin (multi-plugin marketplace); the
        # per-plugin version in marketplace.json is used instead.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_plugins_dir(tmp, installed="1.1.0", available="9.9.9",
                              plugin="prod-guard", marketplace="guards")
            clone = root / "mkt" / ".claude-plugin"
            (clone / "plugin.json").write_text(json.dumps(
                {"name": "other-guard", "version": "9.9.9"}))
            (clone / "marketplace.json").write_text(json.dumps({
                "plugins": [{"name": "prod-guard", "version": "2.4.0"}]}))
            s = fr.check_staleness(str(root), "prod-guard")
            self.assertEqual(s["available"], "2.4.0")

    def test_conventional_clone_path_without_known_marketplaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "installed_plugins.json").write_text(json.dumps({
                "plugins": {"prod-guard@claude-bouncer": [{"version": "1.1.0"}]}}))
            clone = root / "marketplaces" / "claude-bouncer" / ".claude-plugin"
            clone.mkdir(parents=True)
            (clone / "plugin.json").write_text(json.dumps(
                {"name": "prod-guard", "version": "2.4.0"}))
            s = fr.check_staleness(str(root), "prod-guard")
            self.assertEqual(s["available"], "2.4.0")


class PrintTests(unittest.TestCase):
    def test_empty(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fr.print_text(fr.build_report([]), 15)
        self.assertIn("No prod-guard decisions", buf.getvalue())

    def test_sections_present(self):
        r = fr.build_report([
            {"plugin": "prod-guard", "decision": "ask", "reason": REASON_ASK_UNKNOWN,
             "command": "aws s3 rm s3://b"},
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            fr.print_text(r, 15)
        out = buf.getvalue()
        self.assertIn("decisions analyzed: 1", out)
        self.assertIn("pattern-gap candidates", out)
        self.assertIn("bluefin", out)

    def _print(self, decisions, plugin):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fr.print_text(fr.build_report(decisions), 15, plugin)
        return buf.getvalue()

    def test_override_line_shown_for_prod_guard(self):
        out = self._print([
            {"plugin": "prod-guard", "decision": "ask", "reason": REASON_OVERRIDE,
             "command": "PROD_GUARD_OVERRIDE=x kubectl delete ns y"},
        ], "prod-guard")
        self.assertIn("PROD_GUARD_OVERRIDE downgrades: 1", out)

    def test_header_names_scope_under_plugin_all(self):
        out = self._print([
            {"plugin": "prod-guard", "decision": "ask", "reason": REASON_ASK_UNKNOWN,
             "command": "aws s3 rm s3://b"},
            {"plugin": "foreground-guard", "decision": "ask",
             "reason": REASON_FOREIGN_OVERRIDE, "command": "sleep 600"},
        ], "all")
        self.assertIn("all-guard decisions analyzed: 2", out)
        self.assertNotIn("prod-guard decisions analyzed", out)

    def test_header_names_sibling_guard_scope(self):
        out = self._print([
            {"plugin": "foreground-guard", "decision": "ask",
             "reason": REASON_FOREIGN_OVERRIDE, "command": "sleep 600"},
        ], "foreground-guard")
        self.assertIn("foreground-guard decisions analyzed: 1", out)

    def test_empty_header_names_scope(self):
        self.assertIn("No all-guard decisions", self._print([], "all"))

    def test_stale_warning_shown(self):
        stale = {"plugin": "prod-guard", "installed": "1.1.0",
                 "available": "2.4.0", "marketplace": "claude-bouncer"}
        buf = io.StringIO()
        with redirect_stdout(buf):
            fr.print_text(fr.build_report([
                {"plugin": "prod-guard", "decision": "ask",
                 "reason": REASON_ASK_UNKNOWN, "command": "aws s3 rm s3://b"},
            ]), 15, "prod-guard", stale)
        out = buf.getvalue()
        self.assertIn("prod-guard 1.1.0 installed, 2.4.0 available", out)
        self.assertIn("claude plugin update prod-guard@claude-bouncer", out)

    def test_stale_warning_shown_when_no_decisions(self):
        stale = {"plugin": "prod-guard", "installed": "1.1.0",
                 "available": "2.4.0", "marketplace": "claude-bouncer"}
        buf = io.StringIO()
        with redirect_stdout(buf):
            fr.print_text(fr.build_report([]), 15, "prod-guard", stale)
        out = buf.getvalue()
        self.assertIn("No prod-guard decisions", out)
        self.assertIn("1.1.0 installed, 2.4.0 available", out)

    def test_no_warning_when_current(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fr.print_text(fr.build_report([]), 15, "prod-guard")
        self.assertNotIn("available in the local marketplace clone",
                         buf.getvalue())

    def test_override_line_omitted_under_plugin_all(self):
        out = self._print([
            {"plugin": "prod-guard", "decision": "ask", "reason": REASON_OVERRIDE,
             "command": "PROD_GUARD_OVERRIDE=x kubectl delete ns y"},
            {"plugin": "foreground-guard", "decision": "ask",
             "reason": REASON_FOREIGN_OVERRIDE,
             "command": "FOREGROUND_GUARD_OVERRIDE=x sleep 600"},
        ], "all")
        self.assertNotIn("PROD_GUARD_OVERRIDE downgrades", out)


class EndToEndTests(unittest.TestCase):
    def _run(self, root, *args):
        # An empty plugins dir keeps the run hermetic: without it the script
        # would read the developer's real ~/.claude/plugins for staleness.
        plugins = os.path.join(root, "plugins")
        os.makedirs(plugins, exist_ok=True)
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--transcripts", root,
             "--plugins-dir", plugins, "--since", "all", *args],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_text_report(self):
        use, att = _decision_record(
            "toolu_1", "aws s3 rm s3://b", _stdout("ask", REASON_ASK_UNKNOWN))
        root = write_transcript([use, att])
        out = self._run(root)
        self.assertIn("prod-guard decisions analyzed: 1", out)
        self.assertIn("ask-unknown", out)
        self.assertIn("bluefin", out)

    def test_json_report(self):
        use, att = _decision_record(
            "toolu_1", "kubectl delete ns x", _stdout("deny", REASON_DENY_PROD))
        root = write_transcript([use, att])
        out = self._run(root, "--json")
        data = json.loads(out)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["decisions"]["deny"], 1)
        self.assertEqual(data["categories"]["deny-prod"], 1)

    def test_override_attribution_across_plugin_scopes(self):
        use1, att1 = _decision_record(
            "toolu_1", "PROD_GUARD_OVERRIDE=x kubectl delete ns y",
            _stdout("ask", REASON_OVERRIDE))
        use2, att2 = _decision_record(
            "toolu_2", "FOREGROUND_GUARD_OVERRIDE=x sleep 600",
            _stdout("ask", REASON_FOREIGN_OVERRIDE),
            hook_cmd='python3 "/x/bash-foreground-guard.py"')
        root = write_transcript([use1, att1, use2, att2])

        self.assertIn("PROD_GUARD_OVERRIDE downgrades: 1", self._run(root))
        # The sibling guard's downgrade is neither counted nor attributed.
        self.assertEqual(
            json.loads(self._run(root, "--plugin", "foreground-guard",
                                 "--json"))["overrides"], 0)
        self.assertNotIn("PROD_GUARD_OVERRIDE downgrades",
                         self._run(root, "--plugin", "all"))

    def test_plugin_all_header_counts_every_guard(self):
        use1, att1 = _decision_record(
            "toolu_1", "aws s3 rm s3://b", _stdout("ask", REASON_ASK_UNKNOWN))
        use2, att2 = _decision_record(
            "toolu_2", "sleep 600", _stdout("ask", REASON_FOREIGN_OVERRIDE),
            hook_cmd='python3 "/x/bash-foreground-guard.py"')
        root = write_transcript([use1, att1, use2, att2])

        out = self._run(root, "--plugin", "all")
        self.assertIn("all-guard decisions analyzed: 2", out)
        self.assertIn("prod-guard decisions analyzed: 1", self._run(root))

    def test_stale_install_reported(self):
        use, att = _decision_record(
            "toolu_1", "aws s3 rm s3://b", _stdout("ask", REASON_ASK_UNKNOWN))
        root = write_transcript([use, att])
        plugins = write_plugins_dir(tempfile.mkdtemp(prefix="prod-guard-plugins-"),
                                    installed="1.1.0", available="2.4.0")

        def run(*args):
            r = subprocess.run(
                [sys.executable, str(SCRIPT), "--transcripts", root,
                 "--plugins-dir", plugins, "--since", "all", *args],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

        self.assertIn("prod-guard 1.1.0 installed, 2.4.0 available", run())
        self.assertEqual(json.loads(run("--json"))["stale"]["available"], "2.4.0")
        # --plugin all has no single plugin to check.
        self.assertNotIn("installed, 2.4.0 available", run("--plugin", "all"))

    def test_no_transcripts_errors(self):
        empty = tempfile.mkdtemp(prefix="prod-guard-friction-empty-")
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--transcripts", empty],
            capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No transcripts", r.stderr)


if __name__ == "__main__":
    unittest.main()
