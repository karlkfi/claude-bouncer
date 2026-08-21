#!/usr/bin/env python3
"""Tests for scripts/bash-foreground-guard.py.

Run with: python3 -m unittest discover tests
     or:  python3 tests/test_foreground_guard.py

Three layers:
  * Unit tests import the module and exercise heredoc stripping,
    tokenization, segment splitting, and sleep-duration parsing.
  * End-to-end tests invoke the script as a subprocess with a fixture $HOME
    and (optionally) a fixture $CLAUDE_PROJECT_DIR holding a
    .claude/foreground-guard.json, and assert the emitted PreToolUse
    decision: deny / ask / defer (no output). Two invariants ride on every
    one of those calls: the decision is never `allow`, and the reason leads
    with the plugin's name — for a deny, in the form friction-report reads.
  * Wiring tests assert the plugin config (hooks.json, plugin.json,
    marketplace.json) is valid and points the hook at the real script.
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
SCRIPT = REPO / "scripts" / "bash-foreground-guard.py"

# Filename has dashes, so import by path.
_spec = util.spec_from_file_location("foreground_guard", SCRIPT)
guard = util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

# The reader is imported rather than mirrored. The opener is a contract between
# the two halves, so a regex copied here would let them drift apart in silence.
_rspec = util.spec_from_file_location("friction_report",
                                      REPO / "scripts" / "friction-report.py")
reader = util.module_from_spec(_rspec)
_rspec.loader.exec_module(reader)

# Convention rule 2: the opener is the plugin's own name, read from the one
# place that defines it.
with open(REPO / ".claude-plugin" / "plugin.json", encoding="utf-8") as _f:
    PLUGIN_NAME = json.load(_f)["name"]


def make_project(config=None):
    """Build a synthetic project dir, optionally holding a
    .claude/foreground-guard.json."""
    proj = tempfile.mkdtemp(prefix="fg-guard-test-proj-")
    if config is not None:
        cdir = os.path.join(proj, ".claude")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "foreground-guard.json"), "w",
                  encoding="utf-8") as f:
            json.dump(config, f)
    return proj


def run_hook(command, config=None, timeout_ms=None, run_in_background=None,
             env_extra=None, permission_mode=None, payload=None):
    """Invoke the hook as a subprocess; return (decision, reason) or
    (None, None) for defer. Uses a minimal, controlled environment so the
    developer's real ~/.claude config can never leak into a test verdict."""
    home = tempfile.mkdtemp(prefix="fg-guard-test-home-")
    env = {
        "HOME": home,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "FOREGROUND_GUARD_DEBUG": "1",
    }
    if config is not None:
        env["CLAUDE_PROJECT_DIR"] = make_project(config)
    if env_extra:
        env.update(env_extra)
    if payload is None:
        tool_input = {"command": command}
        if timeout_ms is not None:
            tool_input["timeout"] = timeout_ms
        if run_in_background is not None:
            tool_input["run_in_background"] = run_in_background
        payload = {"tool_name": "Bash", "tool_input": tool_input}
        if permission_mode:
            payload["permission_mode"] = permission_mode
    r = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload) if isinstance(payload, dict) else payload,
        capture_output=True, text=True, env=env, cwd=home, timeout=30)
    if r.returncode != 0:
        raise AssertionError("hook crashed: %s" % r.stderr)
    if not r.stdout.strip():
        return None, None
    out = json.loads(r.stdout)["hookSpecificOutput"]
    decision = out["permissionDecision"]
    reason = out["permissionDecisionReason"]
    # Invariant enforced on EVERY end-to-end call: the guard's only outputs
    # are deny, ask, or silence. An `allow` would ride past the user's
    # permission settings and the sibling guards.
    assert decision in ("ask", "deny"), \
        "guard emitted forbidden decision %r" % decision
    # Second invariant, same reach: every reason leads with the plugin's name,
    # and every deny is readable by the shipped reader. Claude Code names the
    # plugin in neither the ask prompt nor the deny text, so the opener is the
    # only attribution a human or the agent gets. For a deny it is also the only
    # key there is — a denied call never runs, so nothing writes a hook
    # attachment and `deny_from_result` has the error text and nothing else.
    assert reason.startswith(PLUGIN_NAME), \
        "reason does not lead with %r: %r" % (PLUGIN_NAME, reason[:80])
    if decision == "deny":
        m = reader.DENY_TEXT.match(reason)
        assert m and m.group(1) == PLUGIN_NAME, \
            "deny reason unreadable by friction-report: %r" % reason[:80]
    return decision, reason


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class HeredocStripTests(unittest.TestCase):
    def test_body_lines_dropped(self):
        raw = "cat <<EOF\ntail -f /var/log/syslog\nsleep 600\nEOF\necho done"
        out = guard.strip_heredoc_bodies(raw)
        self.assertNotIn("tail -f", out)
        self.assertNotIn("sleep 600", out)
        self.assertIn("echo done", out)

    def test_dash_variant_tab_terminator(self):
        raw = "cat <<-END\n\tsleep 600\n\tEND\necho after"
        out = guard.strip_heredoc_bodies(raw)
        self.assertNotIn("sleep 600", out)
        self.assertIn("echo after", out)

    def test_quoted_delimiter(self):
        raw = "cat <<'EOF'\nwatch date\nEOF\necho after"
        out = guard.strip_heredoc_bodies(raw)
        self.assertNotIn("watch date", out)
        self.assertIn("echo after", out)

    def test_here_string_not_a_heredoc(self):
        raw = "grep x <<< 'sleep 600'"
        self.assertEqual(guard.strip_heredoc_bodies(raw), raw)

    def test_unterminated_swallows_to_end(self):
        raw = "cat <<EOF\nsleep 600"
        out = guard.strip_heredoc_bodies(raw)
        self.assertNotIn("sleep 600", out)

    def test_multiple_heredocs_consume_in_order(self):
        raw = "cat <<A <<B\nbodyA\nA\nbodyB\nB\necho after"
        out = guard.strip_heredoc_bodies(raw)
        self.assertNotIn("bodyA", out)
        self.assertNotIn("bodyB", out)
        self.assertIn("echo after", out)

    def test_quoted_shift_operator_not_a_heredoc(self):
        # `<<` inside a quoted string is not a heredoc operator: it must not
        # arm and swallow the following foreground-poll line.
        raw = 'echo "a << b"\ntail -f /var/log/syslog'
        out = guard.strip_heredoc_bodies(raw)
        self.assertIn("tail -f", out)

    def test_arithmetic_shift_not_a_heredoc(self):
        # `$((a<<b))` left shift must not be mistaken for a heredoc start.
        raw = "echo $((1<<4))\ntail -f /var/log/syslog"
        out = guard.strip_heredoc_bodies(raw)
        self.assertIn("1<<4", out)
        self.assertIn("tail -f", out)

    def test_real_heredoc_still_stripped(self):
        # A genuine heredoc body is still dropped, terminator and all.
        raw = "cat <<EOF\nwatch date\nEOF\ntail -f /var/log/syslog"
        out = guard.strip_heredoc_bodies(raw)
        self.assertNotIn("watch date", out)
        self.assertIn("tail -f", out)


class SplitSegmentTests(unittest.TestCase):
    def segs(self, raw):
        return guard.split_segments(guard.tokenize(raw))

    def test_background_terminator(self):
        segs = self.segs("sleep 30 & make build")
        self.assertEqual(segs[0], (["sleep", "30"], "&"))
        self.assertEqual(segs[1][0], ["make", "build"])

    def test_redirect_does_not_split_before_ampersand(self):
        # The `&` must terminate `./server` (redirect glued), so the
        # startup-grace sleep isn't mistaken for a sandwiched poll.
        segs = self.segs("./server > server.log & sleep 2; curl localhost")
        self.assertEqual(segs[0], (["./server"], "&"))
        self.assertEqual(segs[1][0], ["sleep", "2"])

    def test_stderr_redirect_kept_in_segment(self):
        segs = self.segs("tail -f x.log 2>&1")
        self.assertEqual(segs[0][1], "")
        self.assertIn("tail", segs[0][0])

    def test_and_chain_is_not_background(self):
        segs = self.segs("make lint && make test")
        self.assertEqual(segs[0][1], "&&")


class SleepSecondsTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(guard.sleep_seconds(["sleep", "30"]), 30)

    def test_gnu_suffixes_sum(self):
        self.assertEqual(guard.sleep_seconds(["sleep", "1m", "30"]), 90)

    def test_infinity(self):
        self.assertEqual(guard.sleep_seconds(["sleep", "infinity"]),
                         float("inf"))

    def test_variable_is_unknown(self):
        self.assertIsNone(guard.sleep_seconds(["sleep", "$N"]))

    def test_fractional(self):
        self.assertEqual(guard.sleep_seconds(["sleep", "0.5"]), 0.5)


# ---------------------------------------------------------------------------
# Class A end-to-end: watch/follow forms
# ---------------------------------------------------------------------------

class WatchFormTests(unittest.TestCase):
    ASKS = [
        "gh pr checks --watch",
        "gh pr checks 123 --watch --interval 5",
        "gh run watch 456",
        "kubectl logs -f pod/api",
        "kubectl logs deploy/api --follow",
        "kubectl -n prod get pods -w",
        "kubectl get pods --watch",
        "oc logs -f pod/api",
        "tail -f /var/log/syslog",
        "tail -F app.log",
        "tail --follow=name app.log",
        "tail -fn50 app.log",
        "journalctl -f -u myservice",
        "journalctl --follow",
        "docker logs -f mycontainer",
        "docker logs --follow mycontainer",
        "podman logs -f c1",
        "docker compose logs -f",
        "watch kubectl get pods",
        "watch -n5 date",
        "sudo journalctl -f",
        "stdbuf -oL tail -f app.log",
        "echo start && gh run watch 456",
        "bash -c 'tail -f app.log'",
    ]
    DEFERS = [
        "gh pr checks 123",
        "gh run view 456",
        "gh run list",
        "kubectl get pods",
        "kubectl logs pod/api",
        "kubectl logs pod/api --tail=100",
        "tail -n 50 app.log",
        "tail app.log",
        "grep -f patterns.txt src/",
        "git log --follow -- README.md",
        "journalctl -n 100",
        "docker logs --tail 100 c1",
        "docker ps",
        "echo 'watch out for tail -f'",
        "make watch-docs.md",
    ]

    def test_watch_forms_ask(self):
        for cmd in self.ASKS:
            decision, reason = run_hook(cmd)
            self.assertEqual(decision, "ask", "expected ask for %r" % cmd)
            self.assertIn("foreground-guard", reason)
            self.assertIn("Monitor", reason)

    def test_non_watch_forms_defer(self):
        for cmd in self.DEFERS:
            decision, _ = run_hook(cmd)
            self.assertIsNone(decision, "expected defer for %r" % cmd)


# ---------------------------------------------------------------------------
# Class A end-to-end: loops, chains, sleeps
# ---------------------------------------------------------------------------

class LoopAndSleepTests(unittest.TestCase):
    def test_while_sleep_loop_asks(self):
        d, r = run_hook("while true; do gh pr checks 1; sleep 5; done")
        self.assertEqual(d, "ask")
        self.assertIn("loop", r)

    def test_until_sleep_loop_asks(self):
        d, _ = run_hook("until gh pr checks 1; do sleep 10; done")
        self.assertEqual(d, "ask")

    def test_for_sleep_loop_asks(self):
        d, _ = run_hook("for i in 1 2 3; do curl -s x; sleep 20; done")
        self.assertEqual(d, "ask")

    def test_multiline_loop_asks(self):
        d, _ = run_hook("while true; do\n  make status\n  sleep 5\ndone")
        self.assertEqual(d, "ask")

    def test_loop_without_sleep_defers(self):
        d, _ = run_hook("for f in *.py; do python3 -m py_compile $f; done")
        self.assertIsNone(d)

    def test_while_read_defers(self):
        d, _ = run_hook("while read -r line; do echo $line; done < input.txt")
        self.assertIsNone(d)

    def test_chained_repeat_with_short_sleep_asks(self):
        d, r = run_hook("gh pr checks 1; sleep 5; gh pr checks 1")
        self.assertEqual(d, "ask")
        self.assertIn("repeat-with-sleep", r)

    def test_leading_short_sleep_then_command_defers(self):
        d, _ = run_hook("sleep 2 && curl localhost:8080/health")
        self.assertIsNone(d)

    def test_bare_sleep_at_floor_asks(self):
        d, r = run_hook("sleep 10")
        self.assertEqual(d, "ask")
        self.assertIn("sleep", r)

    def test_bare_sleep_above_floor_asks(self):
        d, _ = run_hook("sleep 300")
        self.assertEqual(d, "ask")

    def test_bare_sleep_below_floor_defers(self):
        d, _ = run_hook("sleep 9")
        self.assertIsNone(d)

    def test_sleep_infinity_asks(self):
        d, _ = run_hook("sleep infinity")
        self.assertEqual(d, "ask")

    def test_sleep_unknown_duration_asks(self):
        d, _ = run_hook("sleep $DELAY")
        self.assertEqual(d, "ask")

    def test_sleep_floor_configurable(self):
        cfg = {"poll": {"sleep_floor_seconds": 60}}
        d, _ = run_hook("sleep 30", config=cfg)
        self.assertIsNone(d)
        d, _ = run_hook("sleep 60", config=cfg)
        self.assertEqual(d, "ask")

    def test_quoted_sleep_is_data_not_command(self):
        d, _ = run_hook("git commit -m 'sleep 30 fix'")
        self.assertIsNone(d)

    def test_ssh_remote_body_is_opaque(self):
        d, _ = run_hook("ssh host 'sleep 30'")
        self.assertIsNone(d)

    def test_bash_dash_c_loop_recursed(self):
        d, _ = run_hook("bash -c 'while true; do sleep 5; done'")
        self.assertEqual(d, "ask")

    def test_bash_script_file_stays_opaque(self):
        d, _ = run_hook("bash poll-forever.sh")
        self.assertIsNone(d)

    def test_eval_body_recursed(self):
        d, _ = run_hook("eval sleep 600")
        self.assertEqual(d, "ask")


# ---------------------------------------------------------------------------
# Class A end-to-end: exemptions
# ---------------------------------------------------------------------------

class ExemptionTests(unittest.TestCase):
    # One command per Class A finding type.
    POLLS = ("gh run watch 123", "tail -f x.log", "sleep 600",
             "while true; do sleep 5; done",
             "gh pr checks 1; sleep 5; gh pr checks 1")

    def test_run_in_background_does_not_exempt_class_a(self):
        # Backgrounding answers Class B, not Class A: a detached poll still
        # holds a task slot and returns output the agent cannot date.
        for cmd in self.POLLS:
            d, _ = run_hook(cmd, run_in_background=True)
            self.assertEqual(d, "ask", "expected ask for backgrounded %r" % cmd)

    def test_class_a_fixes_name_monitor_and_never_backgrounding(self):
        # A poll prompts backgrounded or not, so no Class A reason may offer
        # run_in_background as the way out — foreground included, where it
        # used to send the agent straight into a second prompt. Monitor is
        # the wait that holds no task slot and returns a dated event.
        for cmd in self.POLLS:
            for bg in (None, True):
                d, r = run_hook(cmd, run_in_background=bg)
                self.assertEqual(d, "ask",
                                 "expected ask for %r (bg=%s)" % (cmd, bg))
                self.assertIn("arm a Monitor", r)
                self.assertNotIn("run_in_background", r)

    def test_backgrounded_reasons_use_the_detached_wording(self):
        d, r = run_hook("sleep 600", run_in_background=True)
        self.assertEqual(d, "ask")
        self.assertIn("parks a background task", r)
        self.assertIn("holds a task slot", r)
        d, r = run_hook("while true; do sleep 5; done", run_in_background=True)
        self.assertIn("polls in the background", r)

    def test_class_a_exemptions_still_apply_when_backgrounded(self):
        for cmd in ("timeout 30 gh run watch 123", "sleep 5", "make build",
                    "tail -n 50 app.log"):
            d, _ = run_hook(cmd, run_in_background=True)
            self.assertIsNone(d, "expected defer for backgrounded %r" % cmd)

    def test_trailing_ampersand_detaches(self):
        for cmd in ("gh run watch 123 &", "tail -f x.log &",
                    "(while true; do sleep 5; done) &",
                    "while true; do sleep 5; done &"):
            d, _ = run_hook(cmd)
            self.assertIsNone(d, "expected defer for %r" % cmd)

    def test_segment_ampersand_exempts_that_segment(self):
        d, _ = run_hook("sleep 30 & make build")
        self.assertIsNone(d)

    def test_backgrounded_server_with_startup_grace_defers(self):
        d, _ = run_hook("./server > server.log 2>&1 & sleep 2; curl -s localhost")
        self.assertIsNone(d)

    def test_timeout_wrap_allows_through(self):
        for cmd in ("timeout 30 gh run watch 123",
                    "timeout 60 tail -f x.log",
                    "timeout -k 5 30 kubectl logs -f pod/x",
                    "timeout 30 sleep 600",
                    "timeout 300 bash -c 'while true; do sleep 5; done'"):
            d, _ = run_hook(cmd)
            self.assertIsNone(d, "expected defer for %r" % cmd)

    def test_heredoc_body_not_parsed_as_commands(self):
        d, _ = run_hook(
            "cat > notes.md <<EOF\ntail -f /var/log/syslog\nsleep 600\n"
            "while true; do sleep 5; done\nEOF")
        self.assertIsNone(d)

    def test_watch_after_heredoc_still_caught(self):
        d, _ = run_hook("cat <<EOF\nhello\nEOF\ngh run watch 1")
        self.assertEqual(d, "ask")

    def test_poll_after_quoted_shift_still_caught(self):
        # The `<<` in the quoted string must not over-arm and hide the poll.
        d, _ = run_hook('echo "a << b"\ngh run watch 1')
        self.assertEqual(d, "ask")

    def test_poll_after_arithmetic_shift_still_caught(self):
        # The `<<` in `$((1<<4))` must not over-arm and hide the poll.
        d, _ = run_hook("echo $((1<<4))\ngh run watch 1")
        self.assertEqual(d, "ask")

    def test_disable_env(self):
        d, _ = run_hook("gh run watch 123",
                        env_extra={"FOREGROUND_GUARD_DISABLE": "1"})
        self.assertIsNone(d)


# ---------------------------------------------------------------------------
# Class A config: enable flag, action escalation, extra patterns, hint
# ---------------------------------------------------------------------------

class PollConfigTests(unittest.TestCase):
    def test_poll_disabled_defers(self):
        d, _ = run_hook("gh run watch 123",
                        config={"poll": {"enabled": False}})
        self.assertIsNone(d)

    def test_action_escalates_to_deny(self):
        d, r = run_hook("gh run watch 123",
                        config={"poll": {"action": "deny"}})
        self.assertEqual(d, "deny")
        self.assertIn("FOREGROUND_GUARD_OVERRIDE", r)

    def test_override_downgrades_deny_to_ask(self):
        d, r = run_hook("FOREGROUND_GUARD_OVERRIDE=demo-run gh run watch 123",
                        config={"poll": {"action": "deny"}})
        self.assertEqual(d, "ask")
        self.assertIn("override acknowledged", r)
        # The reason string is echoed for the audit trail, not just its
        # presence recorded.
        self.assertIn("demo-run", r)

    def test_override_reason_multiword_echoed(self):
        d, r = run_hook(
            "FOREGROUND_GUARD_OVERRIDE='needs live tail' gh run watch 123",
            config={"poll": {"action": "deny"}})
        self.assertEqual(d, "ask")
        self.assertIn("needs live tail", r)

    def test_override_empty_reason_still_downgrades(self):
        # Present-but-empty override still downgrades (unchanged behavior),
        # and emits no dangling parenthetical for the missing reason.
        d, r = run_hook("FOREGROUND_GUARD_OVERRIDE= gh run watch 123",
                        config={"poll": {"action": "deny"}})
        self.assertEqual(d, "ask")
        self.assertIn("override acknowledged", r)
        self.assertNotIn("()", r)

    def test_no_override_reason_when_absent(self):
        # No override prefix: hard deny, and no override-acknowledged text.
        d, r = run_hook("gh run watch 123",
                        config={"poll": {"action": "deny"}})
        self.assertEqual(d, "deny")
        self.assertNotIn("override acknowledged", r)

    def test_extra_watch_pattern(self):
        cfg = {"poll": {"extra_watch_patterns": [r"^mytool\s+follow\b"]}}
        d, _ = run_hook("mytool follow --id 7", config=cfg)
        self.assertEqual(d, "ask")
        d, _ = run_hook("mytool status", config=cfg)
        self.assertIsNone(d)

    def test_exempt_watch_pattern_silences_builtin(self):
        # A built-in watch that would normally ask is quieted when its
        # segment matches an exempt allowlist entry.
        cfg = {"poll": {"exempt_watch_patterns": [r"^gh\s+run\s+watch\b"]}}
        d, _ = run_hook("gh run watch 456", config=cfg)
        self.assertIsNone(d)

    def test_exempt_watch_pattern_does_not_silence_others(self):
        # The allowlist wins only for matching segments; unlisted watches
        # still prompt.
        cfg = {"poll": {"exempt_watch_patterns": [r"^gh\s+run\s+watch\b"]}}
        d, _ = run_hook("tail -f app.log", config=cfg)
        self.assertEqual(d, "ask")

    def test_hint_appended(self):
        d, r = run_hook("gh run watch 123",
                        config={"hint": "pr-sentinel watches PRs here"})
        self.assertEqual(d, "ask")
        self.assertIn("pr-sentinel watches PRs here", r)



# ---------------------------------------------------------------------------
# Class B end-to-end: slow commands vs timeout
# ---------------------------------------------------------------------------

SLOW_CFG = {"slow": {"commands": {r"make test-race\b": 600000}}}


class SlowCommandTests(unittest.TestCase):
    def test_default_registry_is_empty(self):
        d, _ = run_hook("make test-race")
        self.assertIsNone(d)

    def test_slow_command_default_timeout_asks(self):
        d, r = run_hook("make test-race", config=SLOW_CFG)
        self.assertEqual(d, "ask")
        self.assertIn("600000", r)
        self.assertIn("default 120000 ms timeout", r)
        self.assertIn("run_in_background", r)

    def test_slow_command_low_timeout_asks(self):
        d, r = run_hook("make test-race", config=SLOW_CFG, timeout_ms=120000)
        self.assertEqual(d, "ask")
        self.assertIn("120000 ms timeout set on this call", r)

    def test_slow_command_adequate_timeout_defers(self):
        for t in (600000, 700000):
            d, _ = run_hook("make test-race", config=SLOW_CFG, timeout_ms=t)
            self.assertIsNone(d, "expected defer at timeout %d" % t)

    def test_slow_command_backgrounded_defers(self):
        d, _ = run_hook("make test-race", config=SLOW_CFG,
                        run_in_background=True)
        self.assertIsNone(d)

    def test_backgrounded_slow_command_that_polls_keeps_the_class_a_ask(self):
        # Backgrounding drops the Class B finding only; the poll still prompts.
        cfg = {"slow": {"commands": {r"gh run watch\b": 600000}}}
        d, r = run_hook("gh run watch 456", config=cfg, run_in_background=True)
        self.assertEqual(d, "ask")
        self.assertIn("watch/follow mode", r)
        self.assertNotIn("slow-command pattern", r)

    def test_slow_class_disabled_defers(self):
        cfg = {"slow": {"enabled": False,
                        "commands": {r"make test-race\b": 600000}}}
        d, _ = run_hook("make test-race", config=cfg)
        self.assertIsNone(d)

    def test_unmatched_command_defers(self):
        d, _ = run_hook("make build", config=SLOW_CFG)
        self.assertIsNone(d)

    def test_env_default_timeout_respected(self):
        d, _ = run_hook("make test-race", config=SLOW_CFG,
                        env_extra={"BASH_DEFAULT_TIMEOUT_MS": "900000"})
        self.assertIsNone(d)

    def test_pattern_anywhere_in_chain(self):
        d, _ = run_hook("cd sub && make test-race", config=SLOW_CFG)
        self.assertEqual(d, "ask")


# A bare script path is the registration a repo writes on the first try, so
# the mention-vs-execution split is tested against exactly that (#16).
GATE_CFG = {"slow": {"commands": {r"scripts/gate\.sh": 3600000}}}


class SlowCommandPositionTests(unittest.TestCase):
    def assert_runs(self, command, **kw):
        d, _ = run_hook(command, config=GATE_CFG, **kw)
        self.assertEqual(d, "ask", "expected ask for %r" % command)

    def assert_mention(self, command, **kw):
        d, r = run_hook(command, config=GATE_CFG, **kw)
        self.assertIsNone(d, "expected defer for %r, got %s" % (command, r))

    def test_invocation_forms_still_match(self):
        self.assert_runs("scripts/gate.sh")
        self.assert_runs("./scripts/gate.sh --all")
        self.assert_runs("CI=1 nohup scripts/gate.sh")
        self.assert_runs("bash scripts/gate.sh")
        self.assert_runs("bash -x scripts/gate.sh")
        self.assert_runs("git fetch && scripts/gate.sh")
        self.assert_runs("bash -c 'scripts/gate.sh --all'")
        self.assert_runs("/repo/scripts/gate.sh")
        self.assert_runs("timeout 600 scripts/gate.sh")

    def test_mentions_do_not_match(self):
        self.assert_mention('grep -n "read -p" scripts/gate.sh')
        self.assert_mention("wc -l scripts/gate.sh")
        self.assert_mention("cat scripts/gate.sh")
        self.assert_mention('git commit -m "run scripts/gate.sh before release"')
        self.assert_mention("ls -l scripts/gate.sh")

    def test_heredoc_body_mention_does_not_match(self):
        self.assert_mention("cat <<'EOF' > notes.md\nrun scripts/gate.sh\nEOF")

    def test_dot_star_prefix_opts_back_into_argument_matching(self):
        # The escape hatch for patterns that name an argument rather than a
        # command: `.*` puts the leftmost match at the command position.
        cfg = {"slow": {"commands": {r".*-race\b": 600000}}}
        d, _ = run_hook("go test -race ./...", config=cfg)
        self.assertEqual(d, "ask")

    def test_argument_pattern_without_prefix_does_not_match(self):
        cfg = {"slow": {"commands": {r"-race\b": 600000}}}
        d, _ = run_hook("go test -race ./...", config=cfg)
        self.assertIsNone(d)

    def test_anchored_pattern_still_matches(self):
        # The workaround repos wrote against the old whole-string search has
        # to keep working: it anchors at a position segmentation also finds.
        cfg = {"slow": {"commands": {r"(^|[;&|]\s*)scripts/gate\.sh": 3600000}}}
        d, _ = run_hook("git fetch && scripts/gate.sh", config=cfg)
        self.assertEqual(d, "ask")

    def test_unparseable_command_defers(self):
        # Unbalanced quotes: the segmentation can't run, so the guard defers
        # rather than fall back to guessing from the raw string.
        d, _ = run_hook("scripts/gate.sh 'unclosed", config=GATE_CFG)
        self.assertIsNone(d)


# The target-aware form the hook anchors itself: exact command word, whole-
# token glob per argument. A quoted argument that merely mentions the target
# must never fire (#21).
TARGET_CFG = {"slow": {"commands": {
    "make": {"e2e*": 1800000, "test-race": 600000}}}}


class SlowTargetTests(unittest.TestCase):
    def assert_runs(self, command, **kw):
        d, r = run_hook(command, config=TARGET_CFG, **kw)
        self.assertEqual(d, "ask", "expected ask for %r" % command)
        return r

    def assert_defer(self, command, **kw):
        d, r = run_hook(command, config=TARGET_CFG, **kw)
        self.assertIsNone(d, "expected defer for %r, got %s" % (command, r))

    def test_target_invocation_asks(self):
        r = self.assert_runs("make e2e")
        self.assertIn("slow-command target `make e2e*`", r)
        self.assertIn("1800000", r)

    def test_glob_prefix_and_flags(self):
        self.assert_runs("make -C sub e2e-test")
        self.assert_runs("make -j4 e2e")

    def test_second_target(self):
        r = self.assert_runs("make test-race")
        self.assertIn("600000", r)

    def test_quoted_argument_mention_defers(self):
        # The #21 repro: the target name inside a quoted argument is a
        # mention, not a target.
        self.assert_defer('make -n help NOTE="a note mentioning e2e somewhere"')
        self.assert_defer(
            'make queue-id TITLE="No per-spec filter on the e2e make target"')

    def test_other_target_defers(self):
        self.assert_defer("make build")

    def test_other_command_same_word_defers(self):
        self.assert_defer("ninja e2e")

    def test_command_word_matched_by_basename(self):
        self.assert_runs("/usr/bin/make e2e")

    def test_target_anywhere_in_chain(self):
        self.assert_runs("git pull && make e2e")

    def test_adequate_timeout_defers(self):
        self.assert_defer("make e2e", timeout_ms=1800000)
        # Adequate for test-race but not e2e: only e2e fires.
        r = self.assert_runs("make e2e", timeout_ms=600000)
        d, _ = run_hook("make test-race", config=TARGET_CFG, timeout_ms=600000)
        self.assertIsNone(d)
        self.assertIn("e2e*", r)

    def test_backgrounded_defers(self):
        self.assert_defer("make e2e", run_in_background=True)

    def test_mixed_registry_forms(self):
        cfg = {"slow": {"commands": {
            "make": {"e2e*": 1800000},
            r"go test ./\.\.\..*-race": 600000}}}
        d, r = run_hook("make e2e", config=cfg)
        self.assertEqual(d, "ask")
        self.assertIn("slow-command target", r)
        d, r = run_hook("go test ./... -race", config=cfg)
        self.assertEqual(d, "ask")
        self.assertIn("slow-command pattern", r)

    def test_target_maps_merge_across_config_files(self):
        # A user-level file registers one target, the project file another:
        # the per-command maps merge additively instead of replacing.
        extra = make_project({"slow": {"commands": {"make": {"bench*": 900000}}}})
        env = {"FOREGROUND_GUARD_CONFIG":
               os.path.join(extra, ".claude", "foreground-guard.json")}
        d, _ = run_hook("make bench-all", config=TARGET_CFG, env_extra=env)
        self.assertEqual(d, "ask")
        d, _ = run_hook("make e2e", config=TARGET_CFG, env_extra=env)
        self.assertEqual(d, "ask")

    def test_non_numeric_target_value_loses_itself(self):
        cfg = {"slow": {"commands": {"make": {"e2e*": "long", "test-race": 600000}}}}
        d, _ = run_hook("make e2e", config=cfg)
        self.assertIsNone(d)
        d, _ = run_hook("make test-race", config=cfg)
        self.assertEqual(d, "ask")


SLOW_DENY_CFG = {"slow": {"action": "deny",
                          "commands": {r"make test-race\b": 600000}}}


class SlowConfigTests(unittest.TestCase):
    def test_action_escalates_to_deny(self):
        d, r = run_hook("make test-race", config=SLOW_DENY_CFG)
        self.assertEqual(d, "deny")
        self.assertIn("FOREGROUND_GUARD_OVERRIDE", r)

    def test_default_action_stays_ask(self):
        d, r = run_hook("make test-race", config=SLOW_CFG)
        self.assertEqual(d, "ask")
        self.assertNotIn("FOREGROUND_GUARD_OVERRIDE", r)

    def test_invalid_action_stays_ask(self):
        cfg = {"slow": {"action": "block",
                        "commands": {r"make test-race\b": 600000}}}
        d, _ = run_hook("make test-race", config=cfg)
        self.assertEqual(d, "ask")

    def test_override_downgrades_deny_to_ask(self):
        d, r = run_hook("FOREGROUND_GUARD_OVERRIDE=ci-debug make test-race",
                        config=SLOW_DENY_CFG)
        self.assertEqual(d, "ask")
        self.assertIn("override acknowledged", r)
        self.assertIn("ci-debug", r)

    def test_override_works_with_poll_disabled(self):
        # The override prefix is parsed by Class A analysis; it must still
        # downgrade a Class B deny when poll is switched off entirely.
        cfg = {"poll": {"enabled": False},
               "slow": {"action": "deny",
                        "commands": {r"make test-race\b": 600000}}}
        d, r = run_hook("FOREGROUND_GUARD_OVERRIDE=ci-debug make test-race",
                        config=cfg)
        self.assertEqual(d, "ask")
        self.assertIn("override acknowledged", r)

    def test_deny_with_adequate_timeout_defers(self):
        d, _ = run_hook("make test-race", config=SLOW_DENY_CFG,
                        timeout_ms=600000)
        self.assertIsNone(d)


# ---------------------------------------------------------------------------
# Permission modes: unattended runs get a deny, not an unanswerable ask
# ---------------------------------------------------------------------------

class PermissionModeTests(unittest.TestCase):
    def test_unattended_modes_convert_ask_to_deny(self):
        for mode in ("auto", "dontAsk", "bypassPermissions"):
            d, _ = run_hook("gh run watch 123", permission_mode=mode)
            self.assertEqual(d, "deny", "expected deny in %s mode" % mode)

    def test_attended_modes_stay_ask(self):
        for mode in (None, "default", "acceptEdits", "plan"):
            d, _ = run_hook("gh run watch 123", permission_mode=mode)
            self.assertEqual(d, "ask", "expected ask in %s mode" % mode)

    def test_unattended_slow_command_converts_ask_to_deny(self):
        d, _ = run_hook("make test-race", config=SLOW_CFG,
                        permission_mode="auto")
        self.assertEqual(d, "deny")

    def test_unattended_backgrounded_poll_converts_ask_to_deny(self):
        # A backgrounded poll still prompts (Class A is not exempted by
        # run_in_background), so unattended it still denies — and the deny
        # cannot advise the fix the call already applied.
        d, r = run_hook("gh run watch 123", run_in_background=True,
                        permission_mode="auto")
        self.assertEqual(d, "deny")
        self.assertNotIn("run_in_background", r)

    def test_deny_names_the_override_and_the_report(self):
        d, r = run_hook("gh run watch 123", permission_mode="auto")
        self.assertEqual(d, "deny")
        self.assertIn("FOREGROUND_GUARD_OVERRIDE=<reason>", r)
        self.assertIn("friction-report", r)
        self.assertIn("github.com/karlkfi/claude-foreground-guard/issues", r)
        # The deny tail is appended to the fixes, not substituted for them.
        self.assertIn("Monitor", r)

    def test_ask_carries_no_deny_tail(self):
        # An answerable prompt needs neither the escape hatch nor the
        # report link — the human is right there.
        d, r = run_hook("gh run watch 123")
        self.assertEqual(d, "ask")
        self.assertNotIn("FOREGROUND_GUARD_OVERRIDE", r)
        self.assertNotIn("issues", r)

    def test_override_downgrades_auto_mode_deny(self):
        # The mode-escalated deny is downgradable exactly like the
        # config-escalated one: auto mode still shows the prompt.
        d, r = run_hook("FOREGROUND_GUARD_OVERRIDE=demo-run gh run watch 123",
                        permission_mode="auto")
        self.assertEqual(d, "ask")
        self.assertIn("override acknowledged", r)
        self.assertIn("demo-run", r)

    def test_override_inert_where_no_prompt_can_be_answered(self):
        # dontAsk/bypassPermissions: Claude Code never puts the prompt in
        # front of anyone, so the override must not claim a downgrade it
        # cannot deliver.
        for mode in ("dontAsk", "bypassPermissions"):
            d, r = run_hook(
                "FOREGROUND_GUARD_OVERRIDE=demo-run gh run watch 123",
                config={"poll": {"action": "deny"}}, permission_mode=mode)
            self.assertEqual(d, "deny", "expected deny in %s mode" % mode)
            self.assertNotIn("override acknowledged", r)
            self.assertIn("cannot downgrade this in %s mode" % mode, r)


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class RobustnessTests(unittest.TestCase):
    def test_non_bash_tool_defers(self):
        d, _ = run_hook(None, payload={"tool_name": "Read",
                                       "tool_input": {"file_path": "/x"}})
        self.assertIsNone(d)

    def test_empty_command_defers(self):
        d, _ = run_hook("   ")
        self.assertIsNone(d)

    def test_garbage_stdin_defers(self):
        d, _ = run_hook(None, payload="this is not json")
        self.assertIsNone(d)

    def test_unbalanced_quote_defers(self):
        d, _ = run_hook("echo 'unterminated")
        self.assertIsNone(d)

    def test_malformed_config_still_guards(self):
        proj = make_project()
        cdir = os.path.join(proj, ".claude")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "foreground-guard.json"), "w",
                  encoding="utf-8") as f:
            f.write("{not json")
        d, _ = run_hook("gh run watch 123",
                        env_extra={"CLAUDE_PROJECT_DIR": proj})
        self.assertEqual(d, "ask")

    def test_never_allow_battery(self):
        # A sweep of everything above: whatever the decision, it is never
        # "allow" (asserted inside run_hook on every call).
        for cmd in (WatchFormTests.ASKS + WatchFormTests.DEFERS
                    + ["sleep 600", "make test-race"]):
            run_hook(cmd, config=SLOW_CFG)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

class WiringTests(unittest.TestCase):
    def test_hooks_json_points_at_script(self):
        with open(REPO / "hooks" / "hooks.json", encoding="utf-8") as f:
            hooks = json.load(f)
        entries = hooks["hooks"]["PreToolUse"]
        self.assertEqual(entries[0]["matcher"], "Bash")
        cmd = entries[0]["hooks"][0]["command"]
        self.assertIn("bash-foreground-guard.py", cmd)
        rel = cmd.split("${CLAUDE_PLUGIN_ROOT}/")[1].rstrip('"')
        script = REPO / rel
        self.assertTrue(script.is_file(), "hook script missing: %s" % rel)
        self.assertTrue(os.access(script, os.X_OK),
                        "hook script must be executable: %s" % rel)

    def test_plugin_and_marketplace_agree(self):
        with open(REPO / ".claude-plugin" / "plugin.json",
                  encoding="utf-8") as f:
            plugin = json.load(f)
        with open(REPO / ".claude-plugin" / "marketplace.json",
                  encoding="utf-8") as f:
            market = json.load(f)
        self.assertEqual(plugin["name"], "foreground-guard")
        entry = market["plugins"][0]
        self.assertEqual(entry["name"], plugin["name"])
        self.assertEqual(entry["version"], plugin["version"])
        self.assertEqual(entry["source"]["repo"],
                         "karlkfi/claude-foreground-guard")


if __name__ == "__main__":
    unittest.main()
