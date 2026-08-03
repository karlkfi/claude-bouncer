#!/usr/bin/env python3
"""Tests for scripts/bash-prod-guard.py.

Run with: python3 -m unittest discover tests
     or:  python3 tests/test_prod_guard.py

Three layers:
  * Unit tests import the module and exercise classification, tokenization,
    wrapper stripping, and flag parsing.
  * End-to-end tests invoke the script as a subprocess with a fixture $HOME
    (synthetic kubeconfig / gcloud / docker / azure configs) and assert the
    emitted PreToolUse decision: deny / ask / defer (no output).
  * Wiring tests assert the plugin config (hooks.json, plugin.json,
    marketplace.json) is valid and points the hook at the real script.

Fixture rule: never use real production names or real credentials paths in
fixtures. Synthetic names (`gke_acme_prod-us`, `kind-ci`, `bluefin`) exercise
identical code paths with zero risk.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "bash-prod-guard.py"

# Filename has dashes, so import by path.
_spec = util.spec_from_file_location("prod_guard", SCRIPT)
guard = util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


KUBECONFIG_KIND = "apiVersion: v1\nkind: Config\ncurrent-context: kind-ci\n"
KUBECONFIG_PROD = "apiVersion: v1\nkind: Config\ncurrent-context: gke_acme_prod-us\n"

# Full block-style kubeconfig where context names give nothing away: `blue-2`
# maps to a cluster whose server URL is production, `green-1` to a kind server.
# Only server-URL classification catches `blue-2`.
KUBECONFIG_SERVER = """apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: SYNTHETIC
    server: https://api.acme-prod-us.example.com:6443
  name: acme-prod-us-cluster
- cluster:
    server: https://127.0.0.1:6443
  name: kind-local
contexts:
- context:
    cluster: acme-prod-us-cluster
    namespace: default
    user: admin
  name: blue-2
- context:
    cluster: kind-local
    user: kind
  name: green-1
current-context: blue-2
"""


def make_home(kubeconfig=None, gcloud_project=None, docker_context=None,
              az_subscription=None, az_bom=False, argocd_context=None,
              aws_config=None):
    """Build a synthetic $HOME holding whichever ambient configs a test needs."""
    home = tempfile.mkdtemp(prefix="prod-guard-test-home-")
    if aws_config is not None:
        wdir = os.path.join(home, ".aws")
        os.makedirs(wdir)
        with open(os.path.join(wdir, "config"), "w", encoding="utf-8") as f:
            f.write(aws_config)
    if kubeconfig is not None:
        kube_dir = os.path.join(home, ".kube")
        os.makedirs(kube_dir)
        with open(os.path.join(kube_dir, "config"), "w", encoding="utf-8") as f:
            f.write(kubeconfig)
    if gcloud_project is not None:
        gdir = os.path.join(home, ".config", "gcloud", "configurations")
        os.makedirs(gdir)
        with open(os.path.join(home, ".config", "gcloud", "active_config"),
                  "w", encoding="utf-8") as f:
            f.write("default")
        with open(os.path.join(gdir, "config_default"), "w", encoding="utf-8") as f:
            f.write("[core]\nproject = %s\n" % gcloud_project)
    if docker_context is not None:
        ddir = os.path.join(home, ".docker")
        os.makedirs(ddir)
        with open(os.path.join(ddir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"currentContext": docker_context}, f)
    if az_subscription is not None:
        adir = os.path.join(home, ".azure")
        os.makedirs(adir)
        # The Azure CLI writes this file with a UTF-8 byte order mark (BOM);
        # az_bom reproduces that so the utf-8-sig read path is covered.
        enc = "utf-8-sig" if az_bom else "utf-8"
        with open(os.path.join(adir, "azureProfile.json"), "w", encoding=enc) as f:
            json.dump({"subscriptions": [
                {"name": az_subscription, "isDefault": True}]}, f)
    if argocd_context is not None:
        adir = os.path.join(home, ".config", "argocd")
        os.makedirs(adir)
        with open(os.path.join(adir, "config"), "w", encoding="utf-8") as f:
            f.write("current-context: %s\n" % argocd_context)
    return home


def pulumi_workspace_path(home, cwd, name, ext=".yaml", pulumi_home=None):
    """Write a `Pulumi<ext>` project file (project name `name`) in `cwd` and
    return the path where pulumi would keep this project's workspace settings —
    `<pulumi_home or home/.pulumi>/workspaces/<name>-<sha1(projpath)>-workspace.json`,
    keyed exactly as pulumi keys it (sha1 hex of the absolute project-file
    path). The caller writes the `{"stack": ...}` body (or nothing, to model an
    unselected/absent workspace) and passes `cwd` to run_hook."""
    proj_path = os.path.join(os.path.abspath(cwd), "Pulumi" + ext)
    with open(proj_path, "w", encoding="utf-8") as f:
        if ext == ".json":
            json.dump({"name": name, "runtime": "nodejs"}, f)
        else:
            f.write("name: %s\nruntime: nodejs\n" % name)
    ws_dir = os.path.join(pulumi_home or os.path.join(home, ".pulumi"),
                          "workspaces")
    os.makedirs(ws_dir, exist_ok=True)
    digest = hashlib.sha1(proj_path.encode("utf-8")).hexdigest()
    return os.path.join(ws_dir, "%s-%s-workspace.json" % (name, digest))


def run_hook(command, home=None, env_extra=None, cwd=None,
             permission_mode=None, payload=None):
    """Invoke the hook as a subprocess; return the decision string or None
    (defer). Uses a minimal, controlled environment so the developer's real
    kubeconfig / cloud configs can never leak into a test verdict."""
    if home is None:
        home = make_home()
    env = {
        "HOME": home,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PROD_GUARD_DEBUG": "1",
    }
    if env_extra:
        env.update(env_extra)
    if payload is None:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        if cwd:
            payload["cwd"] = cwd
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
    # Invariant enforced on EVERY end-to-end call: the guard's only outputs
    # are deny, ask, or silence. An `allow` would ride past the user's
    # permission settings and the sibling guards — see docs/design.md.
    assert decision in ("ask", "deny"), "guard emitted forbidden decision %r" % decision
    return decision, out["permissionDecisionReason"]


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        guard._PATTERNS = None  # patterns cache is per-process; reset per test

    def test_prod_names(self):
        for name in ("prod", "gke_acme_prod-us", "my-production-cluster",
                     "acme-prd-eu", "live-cluster", "PROD-US"):
            self.assertEqual(guard.classify(name), "prod", name)

    def test_nonprod_names(self):
        for name in ("kind-ci", "k3d-local", "minikube", "docker-desktop",
                     "dev-us", "staging-eu", "acme-qa", "127.0.0.1:5000/app",
                     "localhost:5000/app", "sandbox-2"):
            self.assertEqual(guard.classify(name), "nonprod", name)

    def test_unknown_names(self):
        for name in ("bluefin", "gke_acme_bluefin_us-central1"):
            self.assertEqual(guard.classify(name), "unknown", name)

    def test_prod_beats_nonprod_on_ambiguity(self):
        self.assertEqual(guard.classify("prod-staging-mirror"), "prod")

    def test_config_nonprod_beats_builtin_prod(self):
        # A human-vetted config nonprod entry clears a built-in prod heuristic
        # (issue #17): a repo slug that merely contains `prod` as a segment.
        with unittest.mock.patch.dict(
                os.environ,
                {"PROD_GUARD_NONPROD_PATTERNS": "karlkfi/claude-prod-guard"}):
            guard._PATTERNS = None
            self.assertEqual(
                guard.classify("karlkfi/claude-prod-guard"), "nonprod")
            # The remote-URL form resolves the same way (search, not anchored).
            self.assertEqual(
                guard.classify("git@github.com:karlkfi/claude-prod-guard.git"),
                "nonprod")

    def test_config_prod_beats_config_nonprod(self):
        # Within the config tier, prod still wins on ambiguity (fail-closed).
        with unittest.mock.patch.dict(
                os.environ,
                {"PROD_GUARD_PROD_PATTERNS": "acme-payments",
                 "PROD_GUARD_NONPROD_PATTERNS": "acme-payments"}):
            guard._PATTERNS = None
            self.assertEqual(guard.classify("acme-payments"), "prod")

    def test_config_prod_beats_builtin_nonprod(self):
        # A config prod entry beats everything, including a built-in nonprod
        # match — so a config nonprod clearing a heuristic can never be abused
        # to also mask an explicitly-vetted prod pattern.
        with unittest.mock.patch.dict(
                os.environ,
                {"PROD_GUARD_PROD_PATTERNS": "staging-that-is-really-prod"}):
            guard._PATTERNS = None
            # Matches builtin nonprod (`staging`) AND config prod -> prod.
            self.assertEqual(
                guard.classify("staging-that-is-really-prod"), "prod")

    def test_word_boundaries(self):
        # `prod` must not fire inside unrelated words.
        self.assertNotEqual(guard.classify("reproduce-bug"), "prod")
        self.assertNotEqual(guard.classify("latest"), "nonprod")  # not `test`

    def test_empty_is_unknown(self):
        self.assertEqual(guard.classify(None), "unknown")
        self.assertEqual(guard.classify(""), "unknown")


class ParsingTests(unittest.TestCase):
    def test_split_on_operators(self):
        tokens = guard.tokenize("echo hi && kubectl get pods | head")
        groups = guard.split_simple_commands(tokens)
        self.assertEqual(groups, [["echo", "hi"], ["kubectl", "get", "pods"], ["head"]])

    def test_command_substitution_splits(self):
        tokens = guard.tokenize("echo $(kubectl delete ns x)")
        groups = guard.split_simple_commands(tokens)
        self.assertIn(["kubectl", "delete", "ns", "x"], groups)

    def test_backticks_split(self):
        tokens = guard.tokenize("echo `kubectl delete ns x`")
        groups = guard.split_simple_commands(tokens)
        self.assertIn(["kubectl", "delete", "ns", "x"], groups)

    def test_unbalanced_quotes_return_none(self):
        self.assertIsNone(guard.tokenize("kubectl delete 'oops"))

    def test_env_prefix_extraction(self):
        env, argv = guard.extract_env_prefix(
            ["TF_WORKSPACE=dev", "PROD_GUARD_OVERRIDE=x", "terraform", "apply"])
        self.assertEqual(env, {"TF_WORKSPACE": "dev", "PROD_GUARD_OVERRIDE": "x"})
        self.assertEqual(argv, ["terraform", "apply"])

    def test_strip_wrappers(self):
        self.assertEqual(
            guard.strip_wrappers(["sudo", "-u", "root", "kubectl", "get"], {}),
            ["kubectl", "get"])
        self.assertEqual(
            guard.strip_wrappers(["timeout", "-s", "KILL", "30", "kubectl", "get"], {}),
            ["kubectl", "get"])
        self.assertEqual(
            guard.strip_wrappers(["xargs", "-n1", "kubectl", "delete", "ns"], {}),
            ["kubectl", "delete", "ns"])
        env = {}
        self.assertEqual(
            guard.strip_wrappers(["env", "AWS_PROFILE=dev", "aws", "s3", "ls"], env),
            ["aws", "s3", "ls"])
        self.assertEqual(env, {"AWS_PROFILE": "dev"})

    def test_flag_values_both_forms(self):
        argv = ["kubectl", "--context=a", "delete", "--namespace", "b"]
        self.assertEqual(guard.first_flag_value(argv, ("--context",)), "a")
        self.assertEqual(guard.first_flag_value(argv, ("--namespace",)), "b")

    def test_words_skip_flag_values(self):
        argv = ["kubectl", "-n", "kube-system", "delete", "pod", "x"]
        self.assertEqual(guard.words_of(argv, guard.KUBE_VALUE_FLAGS),
                         ["delete", "pod", "x"])


class HeredocTests(unittest.TestCase):
    """A heredoc with a quoted delimiter is literal — the shell expands
    nothing and runs nothing from the body — so the body must not be scanned
    for commands (issue #28). Everything else keeps its current treatment."""

    def strip(self, raw):
        return guard.strip_quoted_heredocs(raw)

    def test_quoted_delimiter_drops_body(self):
        for opener in ("<<'EOF'", '<<"EOF"', "<<\\EOF"):
            with self.subTest(opener=opener):
                self.assertEqual(
                    self.strip("git commit -F - %s\nkubectl delete ns x\nEOF"
                               % opener),
                    "git commit -F - %s\nEOF" % opener)

    def test_unquoted_delimiter_keeps_body(self):
        # <<EOF genuinely expands, so the body stays under inspection.
        raw = "cat <<EOF\nkubectl delete ns x\nEOF"
        self.assertEqual(self.strip(raw), raw)

    def test_tab_stripped_form(self):
        self.assertEqual(
            self.strip("cat <<-'EOF'\n\tkubectl delete ns x\n\tEOF"),
            "cat <<-'EOF'\n\tEOF")

    def test_missing_terminator_keeps_everything(self):
        # Unterminated: we may have misparsed, so strip nothing.
        raw = "cat <<'EOF'\nkubectl delete ns x"
        self.assertEqual(self.strip(raw), raw)

    def test_here_string_is_not_a_heredoc(self):
        raw = "cat <<< 'text'\nkubectl delete ns x"
        self.assertEqual(self.strip(raw), raw)

    def test_operator_inside_quotes_is_text(self):
        # `<<` inside a quoted word is not a redirection.
        raw = "echo \"see <<'EOF' below\"\nkubectl delete ns x\nEOF"
        self.assertEqual(self.strip(raw), raw)

    def test_left_shift_is_not_a_heredoc(self):
        raw = "x=$((1<<2)); kubectl delete ns x"
        self.assertEqual(self.strip(raw), raw)

    def test_multiple_heredocs_on_one_line(self):
        # Bodies arrive in opener order; only the quoted one is dropped.
        self.assertEqual(
            self.strip("cmd <<'A' <<B\nkubectl delete ns a\nA\n"
                       "kubectl delete ns b\nB"),
            "cmd <<'A' <<B\nA\nkubectl delete ns b\nB")

    def test_commands_after_the_body_still_parse(self):
        tokens = guard.tokenize("cat <<'EOF'\nnoise\nEOF\nkubectl delete ns x")
        self.assertIn(["kubectl", "delete", "ns", "x"],
                      guard.split_simple_commands(tokens))


class HeredocDecisionTests(unittest.TestCase):
    """End-to-end: the repros from issue #28."""

    def test_backticked_command_in_commit_message_defers(self):
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        decision, _ = run_hook(
            "git commit --dry-run -F - <<'EOF'\ntest: probe\n\n"
            "body mentions `helm uninstall` in backticks\nEOF", home=home)
        self.assertIsNone(decision)

    def test_command_shaped_line_in_commit_message_defers(self):
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        decision, _ = run_hook(
            "git commit -F - <<'EOF'\ndocs: pin the chart version\n\n"
            "    kubectl apply -f https://example.invalid/crd.yaml\nEOF",
            home=home)
        self.assertIsNone(decision)

    def test_python_heredoc_body_defers(self):
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        decision, _ = run_hook(
            "python3 - <<'PY'\nprint('run kubectl delete ns payments')\nPY",
            home=home)
        self.assertIsNone(decision)

    def test_unquoted_heredoc_body_still_judged(self):
        # <<EOF expands, so a command in the body is still evaluated.
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        decision, _ = run_hook(
            "cat <<EOF\n$(kubectl delete ns payments)\nEOF", home=home)
        self.assertEqual(decision, "deny")

    def test_command_after_a_heredoc_still_judged(self):
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        decision, _ = run_hook(
            "cat <<'EOF'\nnoise\nEOF\nkubectl delete ns payments", home=home)
        self.assertEqual(decision, "deny")

    def test_unterminated_heredoc_still_judged(self):
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        decision, _ = run_hook(
            "cat <<'EOF'\nkubectl delete ns payments", home=home)
        self.assertEqual(decision, "deny")


class ExpandVarsTests(unittest.TestCase):
    """Unit tests for the conservative $VAR / ${VAR} expander (Q11)."""

    def test_simple_and_braced(self):
        env = {"CTX": "kind-ci"}
        self.assertEqual(guard.expand_vars("$CTX", env), "kind-ci")
        self.assertEqual(guard.expand_vars("${CTX}", env), "kind-ci")
        self.assertEqual(guard.expand_vars("gke_${CTX}_x", env), "gke_kind-ci_x")

    def test_undefined_stays_literal(self):
        # Never blanked — an unresolvable ref must stay UNKNOWN, not vanish.
        self.assertEqual(guard.expand_vars("$NOPE", {}), "$NOPE")
        self.assertEqual(guard.expand_vars("${NOPE}", {}), "${NOPE}")

    def test_partial_expansion_leaves_undefined(self):
        env = {"P": "acme_dev"}
        self.assertEqual(guard.expand_vars("gke_${P}_${Z}", env), "gke_acme_dev_${Z}")

    def test_operator_and_substitution_forms_declined(self):
        # These $ forms are NOT simple refs: left untouched so the value stays
        # literal and errs toward a prompt/deny rather than a wrong expansion.
        env = {"V": "kind-ci"}
        for form in ("${V:-def}", "${V:+alt}", "${V:=def}", "${V?err}",
                     "${#V}", "${!V}", "$(get-ctx)", "$((1+2))",
                     "$1", "$@", "$$", "$?"):
            self.assertEqual(guard.expand_vars(form, env), form, form)

    def test_resolve_assignments_left_to_right(self):
        env = guard.resolve_assignments(
            [("P", "acme_prod"), ("Z", "us-east1"), ("CTX", "gke_${P}_${Z}")], {})
        self.assertEqual(env["CTX"], "gke_acme_prod_us-east1")


class VariableExpansionDecisionTests(unittest.TestCase):
    """End-to-end: a target pinned through a shell variable classifies by its
    value, not the literal `$CTX` (Q11). This both silences the dominant
    friction source and closes a hole where a prod target behind a variable
    downgraded from deny to ask."""

    def test_inline_nonprod_var_defers(self):
        decision, _ = run_hook("CTX=kind-ci kubectl --context $CTX delete ns x")
        self.assertIsNone(decision)

    def test_inline_prod_var_denies(self):
        decision, reason = run_hook(
            "CTX=gke_acme_prod-us kubectl --context $CTX delete ns x")
        self.assertEqual(decision, "deny")
        self.assertIn("gke_acme_prod-us", reason)

    def test_chained_nested_braces_prod_denies(self):
        decision, reason = run_hook(
            "P=acme_prod; Z=us-east1-b; C=c; "
            "CTX=gke_${P}_${Z}_${C} kubectl --context $CTX delete ns x")
        self.assertEqual(decision, "deny")
        self.assertIn("gke_acme_prod_us-east1-b_c", reason)

    def test_chained_nested_braces_nonprod_defers(self):
        decision, _ = run_hook(
            "P=acme_dev; Z=z; C=c; "
            "CTX=gke_${P}_${Z}_${C} kubectl --context $CTX delete ns x")
        self.assertIsNone(decision)

    def test_export_persists_across_segments(self):
        d1, _ = run_hook("export CTX=kind-ci; kubectl --context $CTX delete ns x")
        self.assertIsNone(d1)
        d2, r2 = run_hook("export CTX=gke_acme_prod; kubectl --context $CTX delete ns x")
        self.assertEqual(d2, "deny")
        self.assertIn("gke_acme_prod", r2)

    def test_undefined_var_still_prompts(self):
        # Unresolved → literal `$NOPE` → UNKNOWN explicit target → ask.
        decision, reason = run_hook("kubectl --context $NOPE apply -f m.yaml")
        self.assertEqual(decision, "ask")

    def test_bare_var_does_not_leak_into_child_shell(self):
        # A bare (unexported) shell var is NOT visible to a `bash -c` child, so
        # it must not expand there. The nested body then has no explicit target
        # and its unpinned mutation must not be silently deferred as nonprod.
        decision, _ = run_hook(
            "CTX=kind-ci; bash -c 'kubectl --context $CTX delete ns x'")
        self.assertNotEqual(decision, None,
                            "bare var leaked into child shell → false defer")

    def test_inline_var_is_exported_to_child_shell(self):
        # An inline `CTX=x bash -c ...` DOES export CTX to the child, so it
        # resolves there and a nonprod value defers.
        decision, _ = run_hook(
            "CTX=kind-ci bash -c 'kubectl --context $CTX delete ns x'")
        self.assertIsNone(decision)


class KubectlDecisionTests(unittest.TestCase):
    def test_prod_mutating_denied(self):
        decision, reason = run_hook("kubectl --context gke_acme_prod-us delete ns x")
        self.assertEqual(decision, "deny")
        self.assertIn("gke_acme_prod-us", reason)
        self.assertIn("PROD_GUARD_OVERRIDE", reason)

    def test_prod_readonly_defers(self):
        decision, _ = run_hook("kubectl --context gke_acme_prod-us get pods")
        self.assertIsNone(decision)

    def test_nonprod_explicit_defers(self):
        decision, _ = run_hook("kubectl --context kind-ci delete pod x")
        self.assertIsNone(decision)

    def test_unknown_explicit_asks(self):
        decision, reason = run_hook("kubectl --context bluefin apply -f m.yaml")
        self.assertEqual(decision, "ask")
        self.assertIn("bluefin", reason)

    def test_ambient_mutating_denied_with_pin_hint(self):
        # No --context: even a nonprod ambient context denies now (issue #10) —
        # the target is clobber-prone, so the guard makes the agent pin it. The
        # deny names the resolved context and the flag to add (self-heal).
        home = make_home(kubeconfig=KUBECONFIG_KIND)
        decision, reason = run_hook("kubectl delete pod x", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("kind-ci", reason)
        self.assertIn("--context", reason)
        self.assertIn("must pin", reason)

    def test_ambient_namespace_echoed_in_reason(self):
        # Point 2: the parsed namespace is machine-generated into the reason so
        # the prompt shows where the mutation lands.
        home = make_home(kubeconfig=KUBECONFIG_KIND)
        decision, reason = run_hook("kubectl delete pod x -n payments", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("payments", reason)

    def test_ambient_prod_denied(self):
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        decision, reason = run_hook("kubectl scale deploy x --replicas=0", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("gke_acme_prod-us", reason)

    def test_no_kubeconfig_mutating_denies(self):
        # No kubeconfig to resolve + no --context: unpinned mutation denies.
        decision, _ = run_hook("kubectl apply -f m.yaml")
        self.assertEqual(decision, "deny")

    def test_rollout_status_defers_restart_guarded(self):
        home = make_home(kubeconfig=KUBECONFIG_KIND)
        decision, _ = run_hook("kubectl rollout status deploy/x", home=home)
        self.assertIsNone(decision)
        decision, _ = run_hook("kubectl rollout restart deploy/x", home=home)
        self.assertEqual(decision, "deny")  # unpinned mutation

    def test_exec_is_mutating(self):
        decision, _ = run_hook("kubectl --context gke_acme_prod-us exec -it pod -- sh")
        self.assertEqual(decision, "deny")

    def test_unknown_verb_treated_as_mutating(self):
        decision, _ = run_hook("kubectl --context bluefin frobnicate x")
        self.assertEqual(decision, "ask")

    def test_use_context_prod_denied(self):
        decision, _ = run_hook("kubectl config use-context gke_acme_prod-us")
        self.assertEqual(decision, "deny")

    def test_use_context_nonprod_denied(self):
        # Repointing the shared current-context is denied even for a non-prod
        # target: the switch clobbers every parallel session.
        decision, reason = run_hook("kubectl config use-context kind-ci")
        self.assertEqual(decision, "deny")
        self.assertIn("kubectl --context <ctx>", reason)

    def test_config_set_current_context_denied(self):
        # `config set current-context` is `use-context` by another name.
        decision, reason = run_hook("kubectl config set current-context kind-ci")
        self.assertEqual(decision, "deny")
        self.assertIn("kubectl --context <ctx>", reason)
        decision, _ = run_hook(
            "kubectl config set current-context gke_acme_prod-us")
        self.assertEqual(decision, "deny")

    def test_config_set_other_property_asks(self):
        # Non-repointing kubeconfig edits still confirm rather than block.
        decision, _ = run_hook(
            "kubectl config set clusters.kind-ci.server https://127.0.0.1:6443")
        self.assertEqual(decision, "ask")

    def test_use_context_override_downgrades_to_ask(self):
        decision, reason = run_hook(
            "PROD_GUARD_OVERRIDE=demo-setup kubectl config use-context kind-ci")
        self.assertEqual(decision, "ask")
        self.assertIn("override acknowledged", reason)

    def test_config_view_defers(self):
        decision, _ = run_hook("kubectl config view")
        self.assertIsNone(decision)
        decision, _ = run_hook("kubectl config current-context")
        self.assertIsNone(decision)


class KubeServerClassificationTests(unittest.TestCase):
    """A prod cluster reached through an innocuously named context (`blue-2`)
    is caught by classifying the cluster's server URL, not just the name."""

    def setUp(self):
        guard._PATTERNS = None

    def test_parse_kubeconfig_maps(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "config")
        with open(p, "w", encoding="utf-8") as f:
            f.write(KUBECONFIG_SERVER)
        c2c, c2s = guard._parse_kubeconfig(p)
        self.assertEqual(c2c["blue-2"], "acme-prod-us-cluster")
        self.assertEqual(c2c["green-1"], "kind-local")
        self.assertEqual(c2s["acme-prod-us-cluster"],
                         "https://api.acme-prod-us.example.com:6443")

    def test_classify_kube_server_prod(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "config")
        with open(p, "w", encoding="utf-8") as f:
            f.write(KUBECONFIG_SERVER)
        seg = {"KUBECONFIG": p}
        self.assertEqual(guard.classify_kube("blue-2", seg), "prod")
        self.assertEqual(guard.classify_kube("green-1", seg), "nonprod")
        self.assertEqual(guard.classify_kube("not-in-config", seg), "unknown")

    def test_explicit_context_prod_server_denied(self):
        home = make_home(kubeconfig=KUBECONFIG_SERVER)
        decision, reason = run_hook(
            "kubectl --context blue-2 delete ns x", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("blue-2", reason)

    def test_explicit_context_prod_server_readonly_defers(self):
        home = make_home(kubeconfig=KUBECONFIG_SERVER)
        decision, _ = run_hook(
            "kubectl --context blue-2 get pods", home=home)
        self.assertIsNone(decision)

    def test_ambient_prod_server_denied(self):
        # current-context is blue-2 (prod server); no explicit pin.
        home = make_home(kubeconfig=KUBECONFIG_SERVER)
        decision, reason = run_hook("kubectl delete pod x", home=home)
        self.assertEqual(decision, "deny")

    def test_nonprod_server_context_defers(self):
        # green-1's server is a kind loopback: unknown NAME, nonprod SERVER,
        # so an explicit pin resolves to nonprod and defers.
        home = make_home(kubeconfig=KUBECONFIG_SERVER)
        decision, _ = run_hook(
            "kubectl --context green-1 delete pod x", home=home)
        self.assertIsNone(decision)

    def test_kubectx_prod_server_denied(self):
        home = make_home(kubeconfig=KUBECONFIG_SERVER)
        decision, _ = run_hook("kubectx blue-2", home=home)
        self.assertEqual(decision, "deny")

    def test_use_context_prod_server_denied(self):
        home = make_home(kubeconfig=KUBECONFIG_SERVER)
        decision, _ = run_hook("kubectl config use-context blue-2", home=home)
        self.assertEqual(decision, "deny")

    def test_helm_prod_server_denied(self):
        home = make_home(kubeconfig=KUBECONFIG_SERVER)
        decision, _ = run_hook(
            "helm --kube-context blue-2 upgrade r ./chart", home=home)
        self.assertEqual(decision, "deny")

    def test_unresolved_server_falls_back_to_name(self):
        # An unknown context not present in the kubeconfig keeps name-only
        # behavior: unknown name + mutating => ask (regression guard).
        home = make_home(kubeconfig=KUBECONFIG_SERVER)
        decision, _ = run_hook(
            "kubectl --context bluefin apply -f m.yaml", home=home)
        self.assertEqual(decision, "ask")

    def test_multi_path_kubeconfig_resolves_server(self):
        # Context in the first file, cluster/server in the second.
        home = make_home()
        d = tempfile.mkdtemp()
        a = os.path.join(d, "a")
        b = os.path.join(d, "b")
        with open(a, "w", encoding="utf-8") as f:
            f.write("contexts:\n- context:\n    cluster: pc\n  name: ctx-a\n"
                    "current-context: ctx-a\n")
        with open(b, "w", encoding="utf-8") as f:
            f.write("clusters:\n- cluster:\n    server: https://prod.example.com\n"
                    "  name: pc\n")
        decision, _ = run_hook(
            "kubectl --context ctx-a delete ns x", home=home,
            env_extra={"KUBECONFIG": a + ":" + b})
        self.assertEqual(decision, "deny")


class CompoundBypassTests(unittest.TestCase):
    def test_chained_after_echo_caught(self):
        decision, _ = run_hook(
            "echo hi && kubectl --context gke_acme_prod-us delete ns x")
        self.assertEqual(decision, "deny")

    def test_pipe_segment_caught(self):
        decision, _ = run_hook(
            "kustomize build overlays/prod | kubectl --context acme-production apply -f -")
        self.assertEqual(decision, "deny")

    def test_command_substitution_caught(self):
        decision, _ = run_hook(
            "echo $(kubectl --context acme-production delete ns x)")
        self.assertEqual(decision, "deny")

    def test_backtick_caught(self):
        decision, _ = run_hook(
            "echo `kubectl --context acme-production delete ns x`")
        self.assertEqual(decision, "deny")

    def test_bash_dash_c_caught(self):
        decision, _ = run_hook(
            "bash -c 'kubectl --context acme-production delete ns x'")
        self.assertEqual(decision, "deny")

    def test_eval_caught(self):
        decision, _ = run_hook(
            "eval kubectl --context acme-production delete ns x")
        self.assertEqual(decision, "deny")

    def test_semicolon_and_newline_caught(self):
        decision, _ = run_hook(
            "true; kubectl --context acme-production delete ns x")
        self.assertEqual(decision, "deny")
        decision, _ = run_hook(
            "true\nkubectl --context acme-production delete ns x")
        self.assertEqual(decision, "deny")

    def test_deny_beats_ask_in_chain(self):
        home = make_home(kubeconfig=KUBECONFIG_KIND)
        decision, _ = run_hook(
            "kubectl delete pod x && kubectl --context acme-production delete ns y",
            home=home)
        self.assertEqual(decision, "deny")

    def test_kustomize_alone_defers(self):
        decision, _ = run_hook("kustomize build overlays/prod")
        self.assertIsNone(decision)


class OverrideTests(unittest.TestCase):
    def test_override_downgrades_deny_to_ask(self):
        decision, reason = run_hook(
            "PROD_GUARD_OVERRIDE=incident-42 "
            "kubectl --context gke_acme_prod-us delete ns x")
        self.assertEqual(decision, "ask")
        self.assertIn("override acknowledged", reason)

    def test_override_does_not_touch_plain_ask(self):
        decision, reason = run_hook(
            "PROD_GUARD_OVERRIDE=x kubectl --context bluefin apply -f m.yaml")
        self.assertEqual(decision, "ask")
        self.assertNotIn("override acknowledged", reason)

    def test_disable_env_defers_everything(self):
        decision, _ = run_hook(
            "kubectl --context gke_acme_prod-us delete ns x",
            env_extra={"PROD_GUARD_DISABLE": "1"})
        self.assertIsNone(decision)


class HelmFluxArgocdTests(unittest.TestCase):
    def test_helm_prod_upgrade_denied(self):
        decision, _ = run_hook(
            "helm --kube-context acme-production upgrade api ./chart")
        self.assertEqual(decision, "deny")

    def test_helm_template_defers(self):
        decision, _ = run_hook("helm template api ./chart")
        self.assertIsNone(decision)

    def test_helm_list_defers(self):
        decision, _ = run_hook("helm list -A")
        self.assertIsNone(decision)

    def test_helm_ambient_install_denies(self):
        home = make_home(kubeconfig=KUBECONFIG_KIND)
        decision, reason = run_hook("helm install api ./chart", home=home)
        self.assertEqual(decision, "deny")  # unpinned mutation
        self.assertIn("--kube-context", reason)

    def test_flux_reconcile_prod_denied(self):
        decision, _ = run_hook(
            "flux reconcile kustomization app --context acme-production")
        self.assertEqual(decision, "deny")

    def test_flux_get_defers(self):
        decision, _ = run_hook("flux get kustomizations")
        self.assertIsNone(decision)

    def test_argocd_sync_prod_server_denied(self):
        decision, _ = run_hook(
            "argocd app sync api --server argocd.prod.acme.io")
        self.assertEqual(decision, "deny")

    def test_argocd_list_defers(self):
        decision, _ = run_hook("argocd app list")
        self.assertIsNone(decision)


class GcloudTests(unittest.TestCase):
    def test_delete_prod_project_denied(self):
        decision, _ = run_hook(
            "gcloud compute instances delete vm1 --project=acme-prod")
        self.assertEqual(decision, "deny")

    def test_list_prod_project_defers(self):
        decision, _ = run_hook(
            "gcloud compute instances list --project=acme-prod")
        self.assertIsNone(decision)

    def test_delete_dev_project_defers(self):
        decision, _ = run_hook(
            "gcloud compute instances delete vm1 --project=acme-dev")
        self.assertIsNone(decision)

    def test_ambient_prod_project_denied(self):
        home = make_home(gcloud_project="acme-prod")
        decision, reason = run_hook("gcloud compute instances delete vm1", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("acme-prod", reason)

    def test_ambient_dev_project_denies(self):
        # Nonprod ambient project still denies — pin --project (issue #10).
        home = make_home(gcloud_project="acme-dev")
        decision, reason = run_hook("gcloud compute instances delete vm1", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("--project", reason)

    def test_get_credentials_asks(self):
        decision, reason = run_hook(
            "gcloud container clusters get-credentials c --project acme-dev")
        self.assertEqual(decision, "ask")
        self.assertIn("kubeconfig", reason)

    def test_config_set_prod_project_denied(self):
        decision, _ = run_hook("gcloud config set project acme-prod")
        self.assertEqual(decision, "deny")

    def test_config_set_dev_project_denied(self):
        # Repointing the shared active configuration is denied even for a
        # non-prod value: the switch clobbers every parallel session.
        decision, reason = run_hook("gcloud config set project acme-dev")
        self.assertEqual(decision, "deny")
        self.assertIn("--project", reason)

    def test_auth_login_asks(self):
        # Credential writers have no per-command pin alternative: still ask.
        decision, _ = run_hook("gcloud auth login")
        self.assertEqual(decision, "ask")


class AwsDefaultProfileTests(unittest.TestCase):
    """Unit tests for the ~/.aws/config [default] profile resolver."""

    def _resolve(self, text):
        d = tempfile.mkdtemp(prefix="prod-guard-aws-")
        path = os.path.join(d, "config")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return guard.aws_default_profile({"AWS_CONFIG_FILE": path})

    def test_named_ordered_and_extra_split(self):
        named, extra = self._resolve(
            "[default]\nregion = us-east-1\n"
            "sso_start_url = https://acme-prod.awsapps.com/start\n"
            "output = json\n")
        # sso_start_url outranks region in display order despite file order.
        self.assertEqual(named[0], "https://acme-prod.awsapps.com/start")
        self.assertIn("us-east-1", named)
        self.assertEqual(extra, ["json"])  # non-echoable key -> classify-only

    def test_follows_sso_session(self):
        named, _ = self._resolve(
            "[default]\nsso_session = corp\n"
            "[sso-session corp]\nsso_start_url = https://x-prod.awsapps.com/start\n")
        self.assertIn("https://x-prod.awsapps.com/start", named)

    def test_missing_default_section(self):
        self.assertIsNone(self._resolve("[profile other]\nregion = us-east-1\n"))

    def test_missing_file(self):
        self.assertIsNone(
            guard.aws_default_profile({"AWS_CONFIG_FILE": "/no/such/aws/config"}))

    def test_named_profile_section(self):
        # A named profile lives under [profile NAME], not [NAME].
        d = tempfile.mkdtemp(prefix="prod-guard-aws-")
        path = os.path.join(d, "config")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[profile admin]\n"
                    "sso_start_url = https://acme-prod.awsapps.com/start\n")
        named, _ = guard.aws_profile_fields("admin", {"AWS_CONFIG_FILE": path})
        self.assertEqual(named[0], "https://acme-prod.awsapps.com/start")
        # The bare [admin] form (credentials-file style) is not read from config.
        self.assertIsNone(
            guard.aws_profile_fields("missing", {"AWS_CONFIG_FILE": path}))

    def test_named_profile_follows_sso_session(self):
        d = tempfile.mkdtemp(prefix="prod-guard-aws-")
        path = os.path.join(d, "config")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[profile ops]\nsso_session = corp\n"
                    "[sso-session corp]\n"
                    "sso_start_url = https://x-prod.awsapps.com/start\n")
        named, _ = guard.aws_profile_fields("ops", {"AWS_CONFIG_FILE": path})
        self.assertIn("https://x-prod.awsapps.com/start", named)


class AwsAzTests(unittest.TestCase):
    def test_terminate_prod_profile_denied(self):
        decision, _ = run_hook(
            "aws ec2 terminate-instances --instance-ids i-1 --profile prod")
        self.assertEqual(decision, "deny")

    def test_env_profile_dev_defers(self):
        decision, _ = run_hook(
            "AWS_PROFILE=dev aws ec2 terminate-instances --instance-ids i-1")
        self.assertIsNone(decision)

    def test_describe_defers(self):
        decision, _ = run_hook("aws ec2 describe-instances")
        self.assertIsNone(decision)

    def test_s3_rm_no_profile_denies(self):
        # No ~/.aws/config -> nothing to resolve -> unpinned mutation denies.
        decision, reason = run_hook("aws s3 rm s3://bucket/key")
        self.assertEqual(decision, "deny")
        self.assertIn("--profile", reason)

    def test_default_profile_sso_prod_denied(self):
        home = make_home(aws_config=(
            "[default]\n"
            "sso_start_url = https://acme-prod.awsapps.com/start\n"
            "sso_account_id = 111122223333\n"
            "region = us-east-1\n"))
        decision, reason = run_hook(
            "aws ec2 terminate-instances --instance-ids i-1", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("acme-prod", reason)

    def test_default_profile_role_arn_prod_denied(self):
        home = make_home(aws_config=(
            "[default]\nrole_arn = arn:aws:iam::123456789012:role/prod-admin\n"
            "source_profile = base\n"))
        decision, _ = run_hook("aws s3 rm s3://bucket/key", home=home)
        self.assertEqual(decision, "deny")

    def test_default_profile_sso_session_name_prod_denied(self):
        home = make_home(aws_config="[default]\nsso_session = acme-prod\n")
        decision, _ = run_hook("aws s3 rm s3://bucket/key", home=home)
        self.assertEqual(decision, "deny")

    def test_default_profile_follows_sso_session_ref(self):
        # The prod signal lives in the referenced [sso-session] block, not the
        # profile itself.
        home = make_home(aws_config=(
            "[default]\nsso_session = corp\nsso_account_id = 444455556666\n"
            "\n[sso-session corp]\n"
            "sso_start_url = https://acme-production.awsapps.com/start\n"
            "sso_region = us-east-1\n"))
        decision, _ = run_hook("aws s3 rm s3://bucket/key", home=home)
        self.assertEqual(decision, "deny")

    def test_default_profile_credential_process_prod_denied_no_echo(self):
        # A prod string in a non-echoable key denies but is never echoed.
        home = make_home(aws_config=(
            "[default]\ncredential_process = /opt/prod-creds/get.sh\n"))
        decision, reason = run_hook("aws s3 rm s3://bucket/key", home=home)
        self.assertEqual(decision, "deny")
        self.assertNotIn("prod-creds", reason)

    def test_default_profile_dev_still_denies_with_name(self):
        # A nonprod default profile now denies (unpinned), still naming what
        # resolved so the prompt shows where it would land.
        home = make_home(aws_config=(
            "[default]\nsso_start_url = https://acme-dev.awsapps.com/start\n"))
        decision, reason = run_hook("aws s3 rm s3://bucket/key", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("acme-dev", reason)

    def test_default_profile_unknown_still_denies_with_name(self):
        home = make_home(aws_config=(
            "[default]\nsso_start_url = https://bluefin.awsapps.com/start\n"))
        decision, reason = run_hook("aws s3 rm s3://bucket/key", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("bluefin", reason)

    def test_aws_config_file_env_override(self):
        home = make_home()
        cfg = os.path.join(home, "custom-aws-config")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("[default]\nsso_start_url = https://acme-prod.awsapps.com/start\n")
        decision, _ = run_hook("aws s3 rm s3://bucket/key", home=home,
                               env_extra={"AWS_CONFIG_FILE": cfg})
        self.assertEqual(decision, "deny")

    def test_explicit_profile_wins_over_default_config(self):
        # An explicit nonprod --profile defers even when the [default] is prod.
        home = make_home(aws_config=(
            "[default]\nsso_start_url = https://acme-prod.awsapps.com/start\n"))
        decision, _ = run_hook(
            "aws s3 rm s3://bucket/key --profile dev", home=home)
        self.assertIsNone(decision)

    def test_named_profile_sso_prod_denied(self):
        # Q9: an unknown-named --profile whose [profile NAME] account is prod
        # escalates from ask to deny, echoing the resolved identifier.
        home = make_home(aws_config=(
            "[profile admin]\n"
            "sso_start_url = https://acme-prod.awsapps.com/start\n"))
        decision, reason = run_hook(
            "aws ec2 terminate-instances --instance-ids i-1 --profile admin",
            home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("acme-prod", reason)
        self.assertIn("admin", reason)

    def test_named_profile_role_arn_prod_denied(self):
        home = make_home(aws_config=(
            "[profile ops]\nrole_arn = arn:aws:iam::123456789012:role/prod-admin\n"))
        decision, _ = run_hook("aws s3 rm s3://bucket/key --profile ops", home=home)
        self.assertEqual(decision, "deny")

    def test_named_profile_via_env_prod_denied(self):
        # AWS_PROFILE names the profile just like --profile does.
        home = make_home(aws_config=(
            "[profile admin]\n"
            "sso_start_url = https://acme-prod.awsapps.com/start\n"))
        decision, _ = run_hook(
            "AWS_PROFILE=admin aws s3 rm s3://bucket/key", home=home)
        self.assertEqual(decision, "deny")

    def test_named_profile_credential_process_prod_denied_no_echo(self):
        home = make_home(aws_config=(
            "[profile admin]\ncredential_process = /opt/prod-creds/get.sh\n"))
        decision, reason = run_hook(
            "aws s3 rm s3://bucket/key --profile admin", home=home)
        self.assertEqual(decision, "deny")
        self.assertNotIn("prod-creds", reason)

    def test_named_profile_follows_sso_session_ref(self):
        home = make_home(aws_config=(
            "[profile admin]\nsso_session = corp\n"
            "\n[sso-session corp]\n"
            "sso_start_url = https://acme-production.awsapps.com/start\n"))
        decision, _ = run_hook(
            "aws s3 rm s3://bucket/key --profile admin", home=home)
        self.assertEqual(decision, "deny")

    def test_nonprod_named_profile_defers_despite_prod_field(self):
        # The explicit nonprod NAME wins: content resolution is scoped to the
        # unknown-name case, so a shared prod sso_start_url can't flip a
        # user-named dev profile to deny.
        home = make_home(aws_config=(
            "[profile dev]\n"
            "sso_start_url = https://acme-prod.awsapps.com/start\n"))
        decision, _ = run_hook(
            "aws s3 rm s3://bucket/key --profile dev", home=home)
        self.assertIsNone(decision)

    def test_unknown_named_profile_no_section_asks(self):
        # No [profile admin] section -> resolves nothing -> unchanged by-name ask.
        home = make_home(aws_config=(
            "[default]\nsso_start_url = https://acme-dev.awsapps.com/start\n"))
        decision, reason = run_hook(
            "aws s3 rm s3://bucket/key --profile admin", home=home)
        self.assertEqual(decision, "ask")
        self.assertIn("admin", reason)

    def test_default_profile_malformed_config_denies(self):
        # Garbage config resolves nothing -> generic unpinned ambient deny
        # (fail-open on parsing still yields the pin-required deny).
        home = make_home(aws_config="}}}not ini at all{{{\n")
        decision, reason = run_hook("aws s3 rm s3://bucket/key", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("--profile", reason)

    def test_aws_configure_asks(self):
        decision, _ = run_hook("aws configure")
        self.assertEqual(decision, "ask")

    def test_update_kubeconfig_asks(self):
        decision, _ = run_hook(
            "aws eks update-kubeconfig --name c --profile dev")
        self.assertEqual(decision, "ask")

    def test_az_delete_prod_subscription_denied(self):
        decision, _ = run_hook(
            "az group delete -n rg --subscription acme-prod-sub")
        self.assertEqual(decision, "deny")

    def test_az_list_defers(self):
        decision, _ = run_hook("az vm list")
        self.assertIsNone(decision)

    def test_az_ambient_prod_denied(self):
        home = make_home(az_subscription="Acme Production")
        decision, _ = run_hook("az vm delete -n vm1 -g rg --yes", home=home)
        self.assertEqual(decision, "deny")

    def test_az_account_set_denied(self):
        decision, reason = run_hook("az account set --subscription acme-dev-sub")
        self.assertEqual(decision, "deny")
        self.assertIn("az --subscription <id>", reason)


class TerraformTests(unittest.TestCase):
    def test_plan_defers(self):
        decision, _ = run_hook("terraform plan")
        self.assertIsNone(decision)

    def test_apply_no_workspace_denies(self):
        decision, reason = run_hook("terraform apply")
        self.assertEqual(decision, "deny")  # unpinned mutation
        self.assertIn("TF_WORKSPACE", reason)

    def test_apply_prod_workspace_denied(self):
        decision, _ = run_hook("TF_WORKSPACE=prod terraform apply")
        self.assertEqual(decision, "deny")

    def test_apply_dev_workspace_defers(self):
        decision, _ = run_hook("TF_WORKSPACE=dev terraform apply")
        self.assertIsNone(decision)

    def test_selected_prod_workspace_denied(self):
        home = make_home()
        tfdir = os.path.join(home, ".terraform")
        os.makedirs(tfdir)
        with open(os.path.join(tfdir, "environment"), "w", encoding="utf-8") as f:
            f.write("prod")
        decision, _ = run_hook("terraform destroy", home=home, cwd=home)
        self.assertEqual(decision, "deny")

    def test_workspace_select_prod_denied(self):
        decision, _ = run_hook("terraform workspace select prod")
        self.assertEqual(decision, "deny")

    def test_workspace_list_defers(self):
        decision, _ = run_hook("terraform workspace list")
        self.assertIsNone(decision)

    def test_state_rm_denies(self):
        # state rm rewrites backend state; unpinned -> deny.
        decision, _ = run_hook("terraform state rm aws_instance.x")
        self.assertEqual(decision, "deny")

    def test_state_list_defers(self):
        decision, _ = run_hook("terraform state list")
        self.assertIsNone(decision)

    @staticmethod
    def _backend_home(config, btype="s3"):
        """A fixture $HOME whose cwd holds a .terraform/terraform.tfstate
        naming the given backend type and config."""
        home = make_home()
        tfdir = os.path.join(home, ".terraform")
        os.makedirs(tfdir)
        with open(os.path.join(tfdir, "terraform.tfstate"),
                  "w", encoding="utf-8") as f:
            json.dump({"version": 3,
                       "backend": {"type": btype, "config": config}}, f)
        return home

    def test_prod_backend_denies_despite_dev_workspace(self):
        # The weak-proxy catch: workspace says dev, the S3 bucket says prod.
        home = self._backend_home({"bucket": "acme-prod-tfstate",
                                    "key": "svc/terraform.tfstate"})
        decision, reason = run_hook("TF_WORKSPACE=dev terraform apply",
                                    home=home, cwd=home)
        self.assertEqual(decision, "deny")
        self.assertIn("acme-prod-tfstate", reason)

    def test_prod_backend_denies_ambient(self):
        home = self._backend_home({"bucket": "acme-prod-tfstate"})
        decision, _ = run_hook("terraform apply", home=home, cwd=home)
        self.assertEqual(decision, "deny")

    def test_prod_backend_key_path_denies(self):
        # Nonprod bucket, but the state key path is clearly prod.
        home = self._backend_home({"bucket": "shared-tfstate",
                                   "key": "env/prod/main.tfstate"})
        decision, _ = run_hook("terraform destroy", home=home, cwd=home)
        self.assertEqual(decision, "deny")

    def test_prod_gcs_backend_denies(self):
        home = self._backend_home({"bucket": "acme-prod-tfstate",
                                   "prefix": "svc"}, btype="gcs")
        decision, _ = run_hook("terraform apply", home=home, cwd=home)
        self.assertEqual(decision, "deny")

    def test_prod_tfc_workspace_denies(self):
        home = self._backend_home(
            {"organization": "acme",
             "workspaces": {"name": "networking-prod"}}, btype="remote")
        decision, _ = run_hook("terraform apply", home=home, cwd=home)
        self.assertEqual(decision, "deny")

    def test_unknown_backend_type_denies_without_echoing_value(self):
        home = self._backend_home({"conn_str": "host=prod-db.acme"},
                                  btype="pg")
        decision, reason = run_hook("terraform apply", home=home, cwd=home)
        self.assertEqual(decision, "deny")
        # Unknown types report the type only — never the raw (possibly
        # credential-bearing) value.
        self.assertNotIn("prod-db.acme", reason)
        self.assertIn("pg", reason)

    def test_nonprod_backend_still_defers_with_dev_workspace(self):
        home = self._backend_home({"bucket": "acme-dev-tfstate"})
        decision, _ = run_hook("TF_WORKSPACE=dev terraform apply",
                               home=home, cwd=home)
        self.assertIsNone(decision)

    def test_local_backend_unchanged(self):
        # A local backend has no remote target; behavior falls back to the
        # workspace-only path (no workspace pinned -> unpinned deny).
        home = self._backend_home({"path": "prod.tfstate"}, btype="local")
        decision, _ = run_hook("terraform apply", home=home, cwd=home)
        self.assertEqual(decision, "deny")

    def test_prod_workspace_wins_over_nonprod_backend(self):
        home = self._backend_home({"bucket": "acme-dev-tfstate"})
        decision, _ = run_hook("TF_WORKSPACE=prod terraform apply",
                               home=home, cwd=home)
        self.assertEqual(decision, "deny")

    def test_malformed_backend_state_fails_open(self):
        # Unparseable .terraform/terraform.tfstate -> resolve nothing ->
        # unchanged workspace-only behavior: no workspace pinned -> unpinned
        # deny (fail OPEN on the infra read still lands on the pin-required deny).
        home = make_home()
        tfdir = os.path.join(home, ".terraform")
        os.makedirs(tfdir)
        with open(os.path.join(tfdir, "terraform.tfstate"),
                  "w", encoding="utf-8") as f:
            f.write("{ not json")
        decision, _ = run_hook("terraform apply", home=home, cwd=home)
        self.assertEqual(decision, "deny")


class DockerTests(unittest.TestCase):
    def test_local_daemon_mutation_defers(self):
        decision, _ = run_hook("docker rm -f app")
        self.assertIsNone(decision)

    def test_explicit_prod_context_denied(self):
        decision, _ = run_hook("docker --context prod-swarm rm -f app")
        self.assertEqual(decision, "deny")

    def test_ambient_prod_context_denied(self):
        home = make_home(docker_context="prod-swarm")
        decision, _ = run_hook("docker rm -f app", home=home)
        self.assertEqual(decision, "deny")

    def test_ambient_desktop_context_defers(self):
        home = make_home(docker_context="desktop-linux")
        decision, _ = run_hook("docker system prune -f", home=home)
        self.assertIsNone(decision)

    def test_push_prod_registry_denied(self):
        decision, _ = run_hook("docker push registry.prod.acme.io/app:1")
        self.assertEqual(decision, "deny")

    def test_push_local_registry_defers(self):
        decision, _ = run_hook("docker push 127.0.0.1:5000/app:e2e-1")
        self.assertIsNone(decision)

    def test_push_unknown_registry_asks(self):
        decision, _ = run_hook("docker push ghcr.io/acme/app:1")
        self.assertEqual(decision, "ask")

    def test_ps_defers(self):
        decision, _ = run_hook("docker ps -a")
        self.assertIsNone(decision)

    def test_context_use_prod_denied(self):
        decision, _ = run_hook("docker context use prod-swarm")
        self.assertEqual(decision, "deny")

    def test_context_use_nonprod_denied(self):
        # Even a switch to a local-daemon context repoints shared state.
        decision, reason = run_hook("docker context use colima")
        self.assertEqual(decision, "deny")
        self.assertIn("docker --context <ctx>", reason)

    def test_local_build_defers(self):
        decision, _ = run_hook("docker build -t app:dev .")
        self.assertIsNone(decision)


def _prod_repo(home=None):
    """A git repo whose origin remote resolves to a prod-matching slug — gh's
    implied target when no `-R` is passed. Returns (home, repo_path)."""
    if home is None:
        home = make_home()
    repo = os.path.join(home, "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin",
                    "git@github.com:acme/prod-infra.git"], check=True)
    return home, repo


class GhTests(unittest.TestCase):
    def test_pr_create_defers_without_prod_remote(self):
        decision, _ = run_hook("gh pr create -t x -b y")
        self.assertIsNone(decision)

    def test_repo_delete_prod_name_denied(self):
        decision, _ = run_hook("gh repo delete acme/prod-infra --yes")
        self.assertEqual(decision, "deny")

    def test_pr_list_defers(self):
        decision, _ = run_hook("gh pr list")
        self.assertIsNone(decision)

    def test_prod_remote_merge_denied(self):
        home, repo = _prod_repo()
        decision, _ = run_hook("gh pr merge 1 --squash", home=home, cwd=repo)
        self.assertEqual(decision, "deny")

    def test_gh_api_get_defers(self):
        decision, _ = run_hook("gh api repos/acme/prod-infra")
        self.assertIsNone(decision)

    def test_gh_api_delete_prod_repo_flag_denied(self):
        decision, _ = run_hook(
            "gh api -X DELETE repos/x/y -R acme/prod-infra")
        self.assertEqual(decision, "deny")

    # --- Collaboration tier (issue #18): defers even against a prod repo. ---

    def test_issue_create_prod_repo_defers(self):
        decision, _ = run_hook(
            "gh issue create -t bug -b oops -R acme/prod-infra")
        self.assertIsNone(decision)

    def test_issue_create_prod_remote_defers(self):
        home, repo = _prod_repo()
        decision, _ = run_hook("gh issue create -t bug -b oops",
                               home=home, cwd=repo)
        self.assertIsNone(decision)

    def test_pr_create_prod_repo_defers(self):
        decision, _ = run_hook("gh pr create -t x -b y -R acme/prod-infra")
        self.assertIsNone(decision)

    def test_pr_comment_prod_repo_defers(self):
        decision, _ = run_hook("gh pr comment 5 -b lgtm -R acme/prod-infra")
        self.assertIsNone(decision)

    def test_pr_review_prod_repo_defers(self):
        decision, _ = run_hook(
            "gh pr review 5 --approve -R acme/prod-infra")
        self.assertIsNone(decision)

    def test_issue_edit_prod_repo_defers(self):
        decision, _ = run_hook(
            "gh issue edit 5 --add-label bug -R acme/prod-infra")
        self.assertIsNone(decision)

    def test_issue_close_prod_repo_defers(self):
        decision, _ = run_hook("gh issue close 5 -R acme/prod-infra")
        self.assertIsNone(decision)

    def test_label_create_prod_repo_defers(self):
        decision, _ = run_hook(
            "gh label create bug -c FF0000 -R acme/prod-infra")
        self.assertIsNone(decision)

    def test_gist_create_defers(self):
        home, repo = _prod_repo()
        decision, _ = run_hook("gh gist create notes.txt",
                               home=home, cwd=repo)
        self.assertIsNone(decision)

    # --- Strict tier: still denies on a prod repo. ---

    def test_issue_delete_prod_repo_denied(self):
        decision, _ = run_hook("gh issue delete 5 -R acme/prod-infra --yes")
        self.assertEqual(decision, "deny")

    def test_issue_transfer_prod_repo_denied(self):
        decision, _ = run_hook(
            "gh issue transfer 5 acme/other -R acme/prod-infra")
        self.assertEqual(decision, "deny")

    def test_pr_merge_prod_repo_denied(self):
        decision, _ = run_hook("gh pr merge 5 --squash -R acme/prod-infra")
        self.assertEqual(decision, "deny")

    def test_release_create_prod_repo_denied(self):
        decision, _ = run_hook("gh release create v1 -R acme/prod-infra")
        self.assertEqual(decision, "deny")

    def test_secret_set_prod_repo_denied(self):
        decision, _ = run_hook("gh secret set TOKEN -R acme/prod-infra")
        self.assertEqual(decision, "deny")

    def test_workflow_run_prod_repo_denied(self):
        decision, _ = run_hook("gh workflow run deploy -R acme/prod-infra")
        self.assertEqual(decision, "deny")

    def test_repo_edit_prod_repo_denied(self):
        decision, _ = run_hook(
            "gh repo edit --visibility public -R acme/prod-infra")
        self.assertEqual(decision, "deny")

    # --- gh_strict opt-in re-denies the collaboration tier. ---

    def test_gh_strict_config_denies_issue_create(self):
        home = make_home()
        cdir = os.path.join(home, ".claude")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "prod-guard.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"gh_strict": True}, f)
        decision, _ = run_hook(
            "gh issue create -t bug -b oops -R acme/prod-infra", home=home)
        self.assertEqual(decision, "deny")

    def test_gh_strict_env_denies_pr_comment(self):
        decision, _ = run_hook(
            "gh pr comment 5 -b hi -R acme/prod-infra",
            env_extra={"PROD_GUARD_GH_STRICT": "1"})
        self.assertEqual(decision, "deny")

    def test_gh_strict_off_by_default_defers_collab(self):
        decision, _ = run_hook(
            "gh issue create -t bug -b oops -R acme/prod-infra",
            env_extra={"PROD_GUARD_GH_STRICT": "0"})
        self.assertIsNone(decision)


class SshTests(unittest.TestCase):
    def test_prod_host_denied(self):
        decision, reason = run_hook("ssh prod-web-1")
        self.assertEqual(decision, "deny")
        self.assertIn("prod-web-1", reason)

    def test_prod_host_with_user_denied(self):
        decision, _ = run_hook("ssh deploy@prod-web-1 uptime")
        self.assertEqual(decision, "deny")

    def test_prod_host_with_flags_denied(self):
        decision, _ = run_hook("ssh -p 2222 -i ~/.ssh/id deploy@prd-db")
        self.assertEqual(decision, "deny")

    def test_prod_jump_host_denied(self):
        # The destination is innocuous but the -J bastion classifies prod.
        decision, _ = run_hook("ssh -J jump@prod-bastion app-1")
        self.assertEqual(decision, "deny")

    def test_ssh_url_scheme_prod_denied(self):
        decision, _ = run_hook("ssh ssh://root@prod-host:22/")
        self.assertEqual(decision, "deny")

    def test_nonprod_host_defers(self):
        decision, _ = run_hook("ssh dev-box uptime")
        self.assertIsNone(decision)

    def test_unknown_host_defers(self):
        # Denylist-only: an unrecognized host is not prompted (would be noise).
        decision, _ = run_hook("ssh bastion.example.com")
        self.assertIsNone(decision)

    def test_git_over_ssh_defers(self):
        decision, _ = run_hook("ssh -T git@github.com")
        self.assertIsNone(decision)

    def test_flag_value_not_mistaken_for_host(self):
        # -o's value must not be read as the destination.
        decision, _ = run_hook("ssh -o StrictHostKeyChecking=no dev-box")
        self.assertIsNone(decision)

    def test_version_flag_defers(self):
        decision, _ = run_hook("ssh -V")
        self.assertIsNone(decision)

    def test_prod_host_in_pipe_denied(self):
        decision, _ = run_hook("tar cf - . | ssh prod-web-1 'tar xf -'")
        self.assertEqual(decision, "deny")

    def test_override_downgrades_ssh_deny(self):
        decision, _ = run_hook("PROD_GUARD_OVERRIDE=incident-9 ssh prod-web-1")
        self.assertEqual(decision, "ask")


class ContextSwitcherTests(unittest.TestCase):
    def test_kubectx_prod_denied(self):
        decision, _ = run_hook("kubectx gke_acme_prod-us")
        self.assertEqual(decision, "deny")

    def test_kubectx_nonprod_denied(self):
        decision, reason = run_hook("kubectx kind-ci")
        self.assertEqual(decision, "deny")
        self.assertIn("kubectl --context <ctx>", reason)

    def test_kubectx_bare_defers(self):
        decision, _ = run_hook("kubectx")
        self.assertIsNone(decision)

    def test_kubens_with_arg_asks(self):
        decision, _ = run_hook("kubens kube-system")
        self.assertEqual(decision, "ask")

    def test_kubens_bare_defers(self):
        decision, _ = run_hook("kubens")
        self.assertIsNone(decision)


class EksctlDoctlTests(unittest.TestCase):
    def test_eksctl_delete_prod_cluster_denied(self):
        decision, _ = run_hook("eksctl delete cluster --cluster prod-main")
        self.assertEqual(decision, "deny")

    def test_eksctl_get_defers(self):
        decision, _ = run_hook("eksctl get cluster")
        self.assertIsNone(decision)

    def test_eksctl_ambient_default_profile_prod_denied(self):
        # Q10: unpinned mutating eksctl resolves the ~/.aws/config [default]
        # profile like eval_aws -> a prod default profile denies.
        home = make_home(aws_config=(
            "[default]\n"
            "sso_start_url = https://acme-prod.awsapps.com/start\n"))
        decision, reason = run_hook("eksctl delete cluster", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("acme-prod", reason)

    def test_eksctl_ambient_default_profile_dev_still_denies(self):
        home = make_home(aws_config=(
            "[default]\nsso_start_url = https://acme-dev.awsapps.com/start\n"))
        decision, reason = run_hook("eksctl delete cluster", home=home)
        self.assertEqual(decision, "deny")  # unpinned -> pin --profile
        self.assertIn("acme-dev", reason)

    def test_eksctl_ambient_no_config_denies(self):
        # No ~/.aws/config -> nothing to resolve -> generic unpinned deny.
        decision, reason = run_hook("eksctl delete cluster")
        self.assertEqual(decision, "deny")
        self.assertIn("--profile", reason)

    def test_eksctl_default_profile_env_prod_denied(self):
        # AWS_DEFAULT_PROFILE is treated as an explicit pin (parity with aws).
        decision, _ = run_hook(
            "AWS_DEFAULT_PROFILE=prod-admin eksctl delete cluster")
        self.assertEqual(decision, "deny")

    def test_doctl_delete_no_context_denies(self):
        # doctl's auth context isn't read from disk; unpinned -> deny (pin
        # --context) rather than prompt.
        decision, reason = run_hook("doctl kubernetes cluster delete c1")
        self.assertEqual(decision, "deny")
        self.assertIn("--context", reason)

    def test_doctl_list_defers(self):
        decision, _ = run_hook("doctl kubernetes cluster list")
        self.assertIsNone(decision)


class PulumiTests(unittest.TestCase):
    """Q4: pulumi targets the stack. Explicit --stack/-s is classified; no
    stack pinned resolves the per-project selected stack from disk (Q8) — a
    prod selection denies, anything else denies as an unpinned mutation (#10)."""

    def test_up_prod_stack_denied(self):
        decision, reason = run_hook("pulumi up --stack acme/prod --yes")
        self.assertEqual(decision, "deny")
        self.assertIn("prod", reason)

    def test_up_dev_stack_defers(self):
        decision, _ = run_hook("pulumi up -s dev --yes")
        self.assertIsNone(decision)

    def test_up_no_stack_denies(self):
        # No --stack and no resolvable selection -> unpinned deny (pin --stack).
        decision, reason = run_hook("pulumi up --yes")
        self.assertEqual(decision, "deny")
        self.assertIn("--stack", reason)

    def test_preview_prod_defers(self):
        decision, _ = run_hook("pulumi preview --stack acme/prod")
        self.assertIsNone(decision)

    def test_destroy_prod_denied(self):
        decision, _ = run_hook("pulumi destroy -s prod --yes")
        self.assertEqual(decision, "deny")

    def test_unknown_stack_asks(self):
        decision, reason = run_hook("pulumi up --stack bluefin --yes")
        self.assertEqual(decision, "ask")
        self.assertIn("bluefin", reason)

    def test_stack_select_prod_denied(self):
        decision, _ = run_hook("pulumi stack select acme/prod")
        self.assertEqual(decision, "deny")

    def test_stack_select_nonprod_asks(self):
        decision, reason = run_hook("pulumi stack select dev")
        self.assertEqual(decision, "ask")
        self.assertIn("selected pulumi stack", reason)

    def test_stack_rm_prod_denied(self):
        decision, _ = run_hook("pulumi stack rm prod --yes")
        self.assertEqual(decision, "deny")

    def test_stack_ls_defers(self):
        decision, _ = run_hook("pulumi stack ls")
        self.assertIsNone(decision)

    def test_config_set_prod_stack_denied(self):
        decision, _ = run_hook("pulumi config set foo bar --stack acme/prod")
        self.assertEqual(decision, "deny")

    def test_config_get_prod_stack_defers(self):
        decision, _ = run_hook("pulumi config get foo --stack acme/prod")
        self.assertIsNone(decision)

    def test_whoami_and_version_defer(self):
        for cmd in ("pulumi whoami", "pulumi version", "pulumi about"):
            with self.subTest(cmd=cmd):
                decision, _ = run_hook(cmd)
                self.assertIsNone(decision)

    def test_login_asks(self):
        decision, _ = run_hook("pulumi login")
        self.assertEqual(decision, "ask")

    def test_refresh_prod_denied(self):
        decision, _ = run_hook("pulumi refresh -s prod --yes")
        self.assertEqual(decision, "deny")

    # --- Q8: ambient selected-stack resolution from ~/.pulumi/workspaces ---

    def test_ambient_prod_selection_denied(self):
        home = make_home()
        cwd = tempfile.mkdtemp(prefix="prod-guard-pulumi-")
        wfile = pulumi_workspace_path(home, cwd, "myproj")
        with open(wfile, "w", encoding="utf-8") as f:
            json.dump({"stack": "acme/prod"}, f)
        decision, reason = run_hook("pulumi up --yes", home=home, cwd=cwd)
        self.assertEqual(decision, "deny")
        self.assertIn("selected pulumi stack", reason)
        self.assertIn("acme/prod", reason)

    def test_ambient_nonprod_selection_still_denies(self):
        # A nonprod selection does NOT defer — the per-project selection is
        # clobber-prone shared state, so the unpinned mutation denies (pin
        # --stack). The reason still names the resolved selection.
        home = make_home()
        cwd = tempfile.mkdtemp(prefix="prod-guard-pulumi-")
        wfile = pulumi_workspace_path(home, cwd, "myproj")
        with open(wfile, "w", encoding="utf-8") as f:
            json.dump({"stack": "dev"}, f)
        decision, reason = run_hook("pulumi up --yes", home=home, cwd=cwd)
        self.assertEqual(decision, "deny")
        self.assertIn("dev", reason)

    def test_ambient_pulumi_home_override_honored(self):
        home = make_home()
        cwd = tempfile.mkdtemp(prefix="prod-guard-pulumi-")
        phome = tempfile.mkdtemp(prefix="prod-guard-pulumi-home-")
        wfile = pulumi_workspace_path(home, cwd, "myproj", pulumi_home=phome)
        with open(wfile, "w", encoding="utf-8") as f:
            json.dump({"stack": "acme/prod"}, f)
        decision, _ = run_hook("pulumi destroy --yes", home=home, cwd=cwd,
                               env_extra={"PULUMI_HOME": phome})
        self.assertEqual(decision, "deny")

    def test_ambient_json_project_prod_selection_denied(self):
        # The project name is read from a Pulumi.json project file too.
        home = make_home()
        cwd = tempfile.mkdtemp(prefix="prod-guard-pulumi-")
        wfile = pulumi_workspace_path(home, cwd, "jsproj", ext=".json")
        with open(wfile, "w", encoding="utf-8") as f:
            json.dump({"stack": "prod-eu"}, f)
        decision, _ = run_hook("pulumi up --yes", home=home, cwd=cwd)
        self.assertEqual(decision, "deny")

    def test_explicit_stack_beats_prod_selection(self):
        # An explicit --stack wins; the on-disk prod selection is never read.
        home = make_home()
        cwd = tempfile.mkdtemp(prefix="prod-guard-pulumi-")
        wfile = pulumi_workspace_path(home, cwd, "myproj")
        with open(wfile, "w", encoding="utf-8") as f:
            json.dump({"stack": "acme/prod"}, f)
        decision, _ = run_hook("pulumi up --stack dev --yes", home=home, cwd=cwd)
        self.assertIsNone(decision)

    def test_no_stack_key_denies(self):
        # A workspace file with no selection recorded -> unresolved -> unpinned
        # deny (pin --stack).
        home = make_home()
        cwd = tempfile.mkdtemp(prefix="prod-guard-pulumi-")
        wfile = pulumi_workspace_path(home, cwd, "myproj")
        with open(wfile, "w", encoding="utf-8") as f:
            json.dump({}, f)
        decision, reason = run_hook("pulumi up --yes", home=home, cwd=cwd)
        self.assertEqual(decision, "deny")
        self.assertIn("--stack", reason)

    def test_malformed_workspace_json_fails_open_to_deny(self):
        # Fail OPEN on the read (resolve nothing) still lands on the unpinned
        # deny, since no stack is pinned.
        home = make_home()
        cwd = tempfile.mkdtemp(prefix="prod-guard-pulumi-")
        wfile = pulumi_workspace_path(home, cwd, "myproj")
        with open(wfile, "w", encoding="utf-8") as f:
            f.write("{not json")
        decision, _ = run_hook("pulumi up --yes", home=home, cwd=cwd)
        self.assertEqual(decision, "deny")

    def test_ambient_walk_up_to_parent_project(self):
        # pulumi finds the nearest Pulumi.yaml walking UP from cwd; the
        # workspace key hashes that parent project path.
        home = make_home()
        parent = tempfile.mkdtemp(prefix="prod-guard-pulumi-")
        sub = os.path.join(parent, "infra", "sub")
        os.makedirs(sub)
        wfile = pulumi_workspace_path(home, parent, "myproj")
        with open(wfile, "w", encoding="utf-8") as f:
            json.dump({"stack": "acme/prod"}, f)
        decision, _ = run_hook("pulumi up --yes", home=home, cwd=sub)
        self.assertEqual(decision, "deny")

    def test_ambient_prod_selection_readonly_verb_defers(self):
        # A prod selection is irrelevant to a read-only verb.
        home = make_home()
        cwd = tempfile.mkdtemp(prefix="prod-guard-pulumi-")
        wfile = pulumi_workspace_path(home, cwd, "myproj")
        with open(wfile, "w", encoding="utf-8") as f:
            json.dump({"stack": "acme/prod"}, f)
        decision, _ = run_hook("pulumi preview", home=home, cwd=cwd)
        self.assertIsNone(decision)


class AnsibleTests(unittest.TestCase):
    """Q4: ansible / ansible-playbook target the inventory. The inventory is
    authoritative; the host pattern and --limit can only escalate to deny."""

    def test_playbook_prod_inventory_denied(self):
        decision, reason = run_hook(
            "ansible-playbook -i inventories/prod/hosts site.yml")
        self.assertEqual(decision, "deny")
        self.assertIn("inventories/prod/hosts", reason)

    def test_playbook_dev_inventory_defers(self):
        decision, _ = run_hook(
            "ansible-playbook -i inventories/dev/hosts site.yml")
        self.assertIsNone(decision)

    def test_playbook_unknown_inventory_asks(self):
        decision, reason = run_hook("ansible-playbook -i hosts.ini site.yml")
        self.assertEqual(decision, "ask")
        self.assertIn("hosts.ini", reason)

    def test_limit_prod_escalates_to_deny(self):
        decision, _ = run_hook(
            "ansible-playbook -i hosts.ini --limit prod-web site.yml")
        self.assertEqual(decision, "deny")

    def test_unknown_pattern_with_nonprod_inventory_defers(self):
        # `webservers` is unknown, but the -i inventory resolves nonprod, so
        # the pattern must not force a prompt.
        decision, _ = run_hook(
            "ansible webservers -i inventories/dev/hosts -m service "
            "-a 'name=x state=restarted'")
        self.assertIsNone(decision)

    def test_adhoc_prod_pattern_shell_denied(self):
        decision, _ = run_hook("ansible prod-db -m shell -a reboot")
        self.assertEqual(decision, "deny")

    def test_adhoc_ping_prod_defers(self):
        # ping is read-only even against a prod-named pattern.
        decision, _ = run_hook("ansible prod-db -m ping")
        self.assertIsNone(decision)

    def test_no_inventory_no_config_denies(self):
        # No -i and no resolvable ambient inventory -> unpinned deny (add -i).
        decision, reason = run_hook("ansible-playbook site.yml")
        self.assertEqual(decision, "deny")
        self.assertIn("-i", reason)

    def test_env_inventory_prod_denied(self):
        decision, _ = run_hook(
            "ANSIBLE_INVENTORY=inventories/prod ansible-playbook site.yml")
        self.assertEqual(decision, "deny")

    def test_ambient_cfg_prod_denied(self):
        home = make_home()
        with open(os.path.join(home, "ansible.cfg"), "w", encoding="utf-8") as f:
            f.write("[defaults]\ninventory = ./inventories/prod\n")
        decision, _ = run_hook("ansible-playbook site.yml", home=home, cwd=home)
        self.assertEqual(decision, "deny")

    def test_ambient_cfg_dev_defers(self):
        home = make_home()
        with open(os.path.join(home, "ansible.cfg"), "w", encoding="utf-8") as f:
            f.write("[defaults]\ninventory = ./inventories/dev\n")
        decision, _ = run_hook("ansible-playbook site.yml", home=home, cwd=home)
        self.assertIsNone(decision)

    def test_syntax_check_defers(self):
        decision, _ = run_hook(
            "ansible-playbook -i inventories/prod/hosts --syntax-check site.yml")
        self.assertIsNone(decision)

    def test_list_hosts_defers(self):
        decision, _ = run_hook(
            "ansible-playbook -i inventories/prod/hosts --list-hosts site.yml")
        self.assertIsNone(decision)


class InfrastructureTests(unittest.TestCase):
    def test_uncovered_tool_defers(self):
        decision, _ = run_hook("ls -la")
        self.assertIsNone(decision)

    def test_non_bash_tool_defers(self):
        decision, _ = run_hook(None, payload={
            "tool_name": "Write",
            "tool_input": {"file_path": "/x", "content": "y"}})
        self.assertIsNone(decision)

    def test_empty_command_defers(self):
        decision, _ = run_hook("")
        self.assertIsNone(decision)

    def test_garbage_stdin_defers(self):
        decision, _ = run_hook(None, payload="this is not json")
        self.assertIsNone(decision)

    def test_unbalanced_quotes_defer(self):
        decision, _ = run_hook("kubectl delete 'oops")
        self.assertIsNone(decision)

    def test_bare_tool_name_defers(self):
        decision, _ = run_hook("kubectl")
        self.assertIsNone(decision)

    def test_bypass_permissions_upgrades_ask_to_deny(self):
        home = make_home(kubeconfig=KUBECONFIG_KIND)
        decision, _ = run_hook("kubectl delete pod x", home=home,
                               permission_mode="bypassPermissions")
        self.assertEqual(decision, "deny")

    def test_custom_config_patterns(self):
        home = make_home()
        cdir = os.path.join(home, ".claude")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "prod-guard.json"), "w", encoding="utf-8") as f:
            json.dump({"prod": ["^bluefin$"], "nonprod": ["^greenfin$"]}, f)
        decision, _ = run_hook("kubectl --context bluefin delete ns x", home=home)
        self.assertEqual(decision, "deny")
        decision, _ = run_hook("kubectl --context greenfin delete ns x", home=home)
        self.assertIsNone(decision)

    def test_gh_repo_config_nonprod_clears_builtin_prod(self):
        # issue #17: a repo slug matching the built-in prod heuristic (the
        # guard's own repo, `-prod-` segment) is denied for mutating gh
        # commands with no config, but a vetted config nonprod entry clears it.
        # Uses a strict-tier verb (`release create`): the collaboration tier
        # (issue #18) defers `issue create` regardless of repo, so it can no
        # longer exercise the prod-vs-nonprod precedence this test guards.
        decision, _ = run_hook(
            "gh -R karlkfi/claude-prod-guard release create v9 -t rel -b oops")
        self.assertEqual(decision, "deny")
        decision, _ = run_hook(
            "gh -R karlkfi/claude-prod-guard release create v9 -t rel -b oops",
            env_extra={"PROD_GUARD_NONPROD_PATTERNS": "karlkfi/claude-prod-guard"})
        self.assertIsNone(decision)

    def test_config_nonprod_clears_builtin_prod_kubectl(self):
        # The precedence fix is general, not gh-specific: a config nonprod
        # entry clears a built-in prod match for any covered tool.
        home = make_home()
        cdir = os.path.join(home, ".claude")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "prod-guard.json"), "w", encoding="utf-8") as f:
            json.dump({"nonprod": ["gke_acme_prod-legacy-sandbox"]}, f)
        decision, _ = run_hook(
            "kubectl --context gke_acme_prod-legacy-sandbox delete ns x", home=home)
        self.assertIsNone(decision)

    def test_config_prod_still_beats_config_nonprod_e2e(self):
        # A config prod entry wins even when a config nonprod entry also
        # matches — fail-closed within the config tier.
        home = make_home()
        cdir = os.path.join(home, ".claude")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "prod-guard.json"), "w", encoding="utf-8") as f:
            json.dump({"prod": ["acme-payments"],
                       "nonprod": ["acme-payments"]}, f)
        decision, _ = run_hook(
            "kubectl --context acme-payments delete ns x", home=home)
        self.assertEqual(decision, "deny")

    def test_broken_config_falls_back_to_builtins(self):
        home = make_home()
        cdir = os.path.join(home, ".claude")
        os.makedirs(cdir)
        with open(os.path.join(cdir, "prod-guard.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        decision, _ = run_hook(
            "kubectl --context gke_acme_prod-us delete ns x", home=home)
        self.assertEqual(decision, "deny")


class BypassBatteryTests(unittest.TestCase):
    """Adversarial command shapes: every case here is a form an agent (or a
    prompt-injected agent) could plausibly emit to slip a prod mutation past
    a naive first-token guard."""

    def test_absolute_path_to_tool(self):
        decision, _ = run_hook(
            "/usr/local/bin/kubectl --context gke_acme_prod-us delete ns x")
        self.assertEqual(decision, "deny")

    def test_flag_after_verb(self):
        decision, _ = run_hook("kubectl delete ns x --context gke_acme_prod-us")
        self.assertEqual(decision, "deny")

    def test_flag_equals_form_after_verb(self):
        decision, _ = run_hook("kubectl delete ns x --context=gke_acme_prod-us")
        self.assertEqual(decision, "deny")

    def test_sudo_wrapper(self):
        decision, _ = run_hook(
            "sudo kubectl --context gke_acme_prod-us delete ns x")
        self.assertEqual(decision, "deny")

    def test_command_wrapper(self):
        decision, _ = run_hook(
            "command kubectl --context gke_acme_prod-us delete ns x")
        self.assertEqual(decision, "deny")

    def test_env_wrapper_with_assignment(self):
        decision, _ = run_hook(
            "env KUBECONFIG=/nonexistent kubectl --context gke_acme_prod-us delete ns x")
        self.assertEqual(decision, "deny")

    def test_xargs_pipeline(self):
        # xargs is stripped to the underlying kubectl; no --context -> unpinned
        # deny even with a nonprod ambient context.
        home = make_home(kubeconfig=KUBECONFIG_KIND)
        decision, _ = run_hook("echo x | xargs kubectl delete ns", home=home)
        self.assertEqual(decision, "deny")

    def test_nested_shell_two_levels(self):
        decision, _ = run_hook(
            "bash -c \"sh -c 'kubectl --context gke_acme_prod-us delete ns x'\"")
        self.assertEqual(decision, "deny")

    def test_recursion_depth_bound_is_a_known_limit(self):
        # Beyond depth 3 the evaluator stops recursing — a DOCUMENTED false
        # negative (README Limitations). This test locks the bound so a
        # change to it is a conscious decision, not drift.
        guard._PATTERNS = None
        findings, override, _session = guard.evaluate_command_string(
            "kubectl --context gke_acme_prod-us delete ns x", {"cwd": "/"}, depth=4)
        self.assertEqual(findings, [])
        findings, _, _session = guard.evaluate_command_string(
            "kubectl --context gke_acme_prod-us delete ns x", {"cwd": "/"}, depth=3)
        self.assertEqual(len(findings), 1)

    def test_unexpanded_variable_target_asks_not_defers(self):
        # $CTX can't be resolved at hook time; it must classify UNKNOWN and
        # prompt — silently deferring would let `CTX=prod ...` through.
        decision, _ = run_hook("kubectl --context $CTX delete ns x")
        self.assertEqual(decision, "ask")

    def test_override_in_later_segment_applies(self):
        decision, reason = run_hook(
            "true && PROD_GUARD_OVERRIDE=drill kubectl --context gke_acme_prod-us delete ns x")
        self.assertEqual(decision, "ask")
        self.assertIn("override acknowledged", reason)

    def test_assignment_only_segment_then_mutation(self):
        decision, _ = run_hook(
            "FOO=1; kubectl --context gke_acme_prod-us delete ns x")
        self.assertEqual(decision, "deny")

    def test_subshell_group_caught(self):
        decision, _ = run_hook(
            "(cd /srv && kubectl --context gke_acme_prod-us delete ns x)")
        self.assertEqual(decision, "deny")

    def test_or_chain_caught(self):
        decision, _ = run_hook(
            "true || kubectl --context gke_acme_prod-us delete ns x")
        self.assertEqual(decision, "deny")

    def test_background_ampersand_caught(self):
        decision, _ = run_hook(
            "kubectl --context gke_acme_prod-us delete ns x &")
        self.assertEqual(decision, "deny")


class VerbSweepTests(unittest.TestCase):
    """Table-driven sweep: read-only verbs must defer even against an
    explicit prod target; mutating verbs must deny against it. Locks the
    verb tables against silent narrowing."""

    KUBECTL_RO = [
        "get pods", "describe pod x", "logs pod/x", "top pods", "diff -f m.yaml",
        "explain deploy", "api-resources", "api-versions", "events",
        "wait --for=condition=Ready pod/x", "auth can-i list pods",
        "cluster-info", "rollout status deploy/x", "rollout history deploy/x",
    ]
    KUBECTL_MUT = [
        "apply -f m.yaml", "delete ns x", "edit deploy/x", "patch deploy x -p {}",
        "replace -f m.yaml", "scale deploy x --replicas=0", "annotate pod x k=v",
        "label pod x k=v", "rollout restart deploy/x", "drain node-1",
        "cordon node-1", "uncordon node-1", "taint nodes node-1 k=v:NoSchedule",
        "exec -it pod-x -- sh", "cp pod-x:/f /tmp/f", "run tmp --image=busybox",
        "expose deploy x --port=80", "set image deploy/x c=img:2",
    ]

    def test_kubectl_readonly_defers_on_prod(self):
        for tail in self.KUBECTL_RO:
            with self.subTest(tail=tail):
                decision, _ = run_hook(
                    "kubectl --context gke_acme_prod-us %s" % tail)
                self.assertIsNone(decision)

    def test_kubectl_mutating_denies_on_prod(self):
        for tail in self.KUBECTL_MUT:
            with self.subTest(tail=tail):
                decision, _ = run_hook(
                    "kubectl --context gke_acme_prod-us %s" % tail)
                self.assertEqual(decision, "deny")

    def test_helm_sweep(self):
        for tail in ("list -A", "status api", "history api", "get values api"):
            with self.subTest(tail=tail):
                decision, _ = run_hook(
                    "helm --kube-context acme-production %s" % tail)
                self.assertIsNone(decision)
        for tail in ("install api ./chart", "upgrade api ./chart",
                     "uninstall api", "rollback api 1", "test api"):
            with self.subTest(tail=tail):
                decision, _ = run_hook(
                    "helm --kube-context acme-production %s" % tail)
                self.assertEqual(decision, "deny")

    def test_aws_sweep(self):
        for tail in ("ec2 describe-instances", "s3api list-buckets",
                     "s3 ls s3://b", "sts get-caller-identity",
                     "s3api head-object --bucket b --key k"):
            with self.subTest(tail=tail):
                decision, _ = run_hook("aws %s --profile prod" % tail)
                self.assertIsNone(decision)
        for tail in ("s3api put-object --bucket b --key k",
                     "ec2 modify-instance-attribute --instance-id i-1",
                     "s3api delete-bucket --bucket b", "s3 sync . s3://b",
                     "s3 cp f s3://b/f", "s3 mv s3://b/a s3://b/c",
                     "ec2 run-instances --image-id ami-1",
                     "ec2 start-instances --instance-ids i-1",
                     "rds reboot-db-instance --db-instance-identifier d"):
            with self.subTest(tail=tail):
                decision, _ = run_hook("aws %s --profile prod" % tail)
                self.assertEqual(decision, "deny")

    def test_gcloud_sweep(self):
        for tail in ("compute instances list", "compute instances describe vm1",
                     "projects get-iam-policy p", "run services list",
                     "logging read 'resource.type=gce_instance'",
                     "logging tail 'resource.type=gce_instance'"):
            with self.subTest(tail=tail):
                decision, _ = run_hook("gcloud %s --project acme-prod" % tail)
                self.assertIsNone(decision)
        for tail in ("compute instances update vm1", "run deploy svc --image i",
                     "projects add-iam-policy-binding p --member m --role r",
                     "projects set-iam-policy p policy.json",
                     "compute ssh vm1", "sql instances restart db1"):
            with self.subTest(tail=tail):
                decision, _ = run_hook("gcloud %s --project acme-prod" % tail)
                self.assertEqual(decision, "deny")

    def test_az_sweep(self):
        for tail in ("vm show -n v -g rg", "vm list", "group list"):
            with self.subTest(tail=tail):
                decision, _ = run_hook(
                    "az %s --subscription acme-prod-sub" % tail)
                self.assertIsNone(decision)
        for tail in ("vm update -n v -g rg", "vm start -n v -g rg",
                     "vm stop -n v -g rg", "vm restart -n v -g rg",
                     "vm deallocate -n v -g rg", "keyvault purge -n kv"):
            with self.subTest(tail=tail):
                decision, _ = run_hook(
                    "az %s --subscription acme-prod-sub" % tail)
                self.assertEqual(decision, "deny")

    def test_terraform_sweep(self):
        for verb in ("fmt", "validate", "show", "output", "providers", "graph"):
            with self.subTest(verb=verb):
                decision, _ = run_hook("TF_WORKSPACE=prod terraform %s" % verb)
                self.assertIsNone(decision)
        for verb in ("apply", "destroy", "import aws_x.y id", "taint aws_x.y",
                     "untaint aws_x.y", "refresh"):
            with self.subTest(verb=verb):
                decision, _ = run_hook("TF_WORKSPACE=prod terraform %s" % verb)
                self.assertEqual(decision, "deny")

    def test_tofu_alias(self):
        decision, _ = run_hook("TF_WORKSPACE=prod tofu apply")
        self.assertEqual(decision, "deny")

    def test_docker_sweep(self):
        for tail in ("images", "inspect c1", "logs c1", "stats", "events",
                     "history img"):
            with self.subTest(tail=tail):
                decision, _ = run_hook("docker --context prod-swarm %s" % tail)
                self.assertIsNone(decision)
        for tail in ("stop c1", "kill c1", "exec c1 sh", "run img",
                     "system prune -f", "rmi img", "restart c1"):
            with self.subTest(tail=tail):
                decision, _ = run_hook("docker --context prod-swarm %s" % tail)
                self.assertEqual(decision, "deny")

    def test_flux_sweep(self):
        for tail in ("get kustomizations", "logs", "check", "tree kustomization app",
                     "export source git app"):
            with self.subTest(tail=tail):
                decision, _ = run_hook("flux %s --context acme-production" % tail)
                self.assertIsNone(decision)
        for tail in ("suspend kustomization app", "resume kustomization app",
                     "reconcile kustomization app", "delete kustomization app",
                     "create source git app --url u"):
            with self.subTest(tail=tail):
                decision, _ = run_hook("flux %s --context acme-production" % tail)
                self.assertEqual(decision, "deny")


class AmbientFixtureTests(unittest.TestCase):
    """Edge cases in the local config-file readers."""

    def test_azure_profile_with_bom(self):
        home = make_home(az_subscription="Acme Production", az_bom=True)
        decision, _ = run_hook("az vm delete -n v -g rg --yes", home=home)
        self.assertEqual(decision, "deny")

    def test_kubeconfig_env_multi_path_uses_first(self):
        home = make_home()
        kc1 = os.path.join(home, "kc-prod")
        with open(kc1, "w", encoding="utf-8") as f:
            f.write(KUBECONFIG_PROD)
        decision, _ = run_hook(
            "kubectl delete pod x", home=home,
            env_extra={"KUBECONFIG": "%s:%s" % (kc1, os.path.join(home, "kc-other"))})
        self.assertEqual(decision, "deny")

    def test_kubeconfig_quoted_current_context(self):
        home = make_home(
            kubeconfig='apiVersion: v1\ncurrent-context: "gke_acme_prod-us"\n')
        decision, _ = run_hook("kubectl delete pod x", home=home)
        self.assertEqual(decision, "deny")

    def test_kubeconfig_prefix_assignment_unresolvable_denies(self):
        # Unresolvable ambient context + no --context -> unpinned deny.
        decision, _ = run_hook("KUBECONFIG=/nonexistent kubectl delete pod x")
        self.assertEqual(decision, "deny")

    def test_docker_config_without_current_context_defers(self):
        home = make_home()
        ddir = os.path.join(home, ".docker")
        os.makedirs(ddir)
        with open(os.path.join(ddir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"auths": {}}, f)
        decision, _ = run_hook("docker rm -f c1", home=home)
        self.assertIsNone(decision)

    def test_argocd_ambient_prod_denied(self):
        home = make_home(argocd_context="argocd.prod.acme.io")
        decision, _ = run_hook("argocd app sync api", home=home)
        self.assertEqual(decision, "deny")

    def test_argocd_ambient_unknown_denies(self):
        # Unknown ambient argocd context + no --server -> unpinned deny.
        home = make_home(argocd_context="argocd.internal")
        decision, reason = run_hook("argocd app sync api", home=home)
        self.assertEqual(decision, "deny")
        self.assertIn("--server", reason)


class SpecialCaseTests(unittest.TestCase):
    """Branches for commands that mutate shared local state (kubeconfig
    writers, credential/context switchers) and registry-classified pushes."""

    def test_eksctl_write_kubeconfig_asks(self):
        decision, reason = run_hook(
            "eksctl utils write-kubeconfig --cluster dev-main")
        self.assertEqual(decision, "ask")
        self.assertIn("kubeconfig", reason)

    def test_doctl_kubeconfig_save_asks(self):
        decision, reason = run_hook(
            "doctl kubernetes cluster kubeconfig save c1")
        self.assertEqual(decision, "ask")
        self.assertIn("kubeconfig", reason)

    def test_az_login_and_account_clear_ask(self):
        for cmd in ("az login", "az logout", "az account clear"):
            with self.subTest(cmd=cmd):
                decision, _ = run_hook(cmd)
                self.assertEqual(decision, "ask")

    def test_terraform_login_asks(self):
        decision, _ = run_hook("terraform login")
        self.assertEqual(decision, "ask")

    def test_terraform_state_push_denies(self):
        # state push rewrites backend state; unpinned (no workspace) -> deny.
        decision, _ = run_hook("terraform state push errored.tfstate")
        self.assertEqual(decision, "deny")

    def test_gcloud_auth_login_asks_list_defers(self):
        decision, _ = run_hook("gcloud auth login")
        self.assertEqual(decision, "ask")
        decision, _ = run_hook("gcloud auth list")
        self.assertIsNone(decision)

    def test_kubectl_config_delete_context_asks(self):
        decision, _ = run_hook("kubectl config delete-context old-ctx")
        self.assertEqual(decision, "ask")

    def test_kubectx_delete_asks_previous_denied(self):
        # -d edits the kubeconfig without repointing it: still ask.
        decision, _ = run_hook("kubectx -d old-ctx")
        self.assertEqual(decision, "ask")
        # `kubectx -` repoints to the previous context: denied like a named
        # switch (the previous context's identity isn't even resolvable).
        decision, _ = run_hook("kubectx -")
        self.assertEqual(decision, "deny")

    def test_helm_push_registry_classified(self):
        decision, _ = run_hook(
            "helm push chart.tgz oci://registry.prod.acme.io/charts")
        self.assertEqual(decision, "deny")
        decision, _ = run_hook(
            "helm push chart.tgz oci://127.0.0.1:5000/charts")
        self.assertIsNone(decision)
        decision, _ = run_hook(
            "helm push chart.tgz oci://ghcr.io/acme/charts")
        self.assertEqual(decision, "ask")

    def test_docker_build_push_tag_classified(self):
        decision, _ = run_hook(
            "docker build --push -t registry.prod.acme.io/app:1 .")
        self.assertEqual(decision, "deny")
        decision, _ = run_hook("docker build --push -t ghcr.io/acme/app:1 .")
        self.assertEqual(decision, "ask")
        decision, _ = run_hook("docker build --push -t 127.0.0.1:5000/app:1 .")
        self.assertIsNone(decision)

    def test_gh_repo_env_var_target(self):
        decision, _ = run_hook("GH_REPO=acme/prod-app gh workflow run deploy")
        self.assertEqual(decision, "deny")

    def test_gcloud_configurations_activate_denied(self):
        decision, _ = run_hook("gcloud config configurations activate other")
        self.assertEqual(decision, "deny")
        decision, _ = run_hook("gcloud config configurations list")
        self.assertIsNone(decision)


class HelpFlagTests(unittest.TestCase):
    """A help flag prints usage and exits, so a mutating verb's help page is
    not a mutating command — with the two exceptions where the flag isn't
    help at all (issue #28)."""

    def test_mutating_verb_help_defers(self):
        for cmd in ("helm upgrade --help",
                    "kubectl delete --help",
                    "kubectl delete -h",
                    "terraform destroy -help",
                    "gcloud compute instances delete --help",
                    "aws s3 rm --help",
                    "gh workflow run -h"):
            with self.subTest(cmd=cmd):
                decision, _ = run_hook(cmd, home=make_home(
                    kubeconfig=KUBECONFIG_PROD, gcloud_project="acme-prod-us"))
                self.assertIsNone(decision)

    def test_help_subcommand_defers(self):
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        for cmd in ("gcloud help compute instances delete",
                    "kubectl help delete"):
            with self.subTest(cmd=cmd):
                decision, _ = run_hook(cmd, home=home)
                self.assertIsNone(decision)

    def test_docker_short_h_is_hostname_not_help(self):
        # `-h` on docker/podman is --hostname; the run still mutates.
        home = make_home(docker_context="prod-swarm")
        for cmd in ("docker run -h box nginx", "podman run -h box nginx"):
            with self.subTest(cmd=cmd):
                decision, _ = run_hook(cmd, home=home)
                self.assertEqual(decision, "deny")
        # The long form is still help.
        decision, _ = run_hook("docker run --help", home=home)
        self.assertIsNone(decision)

    def test_ssh_long_help_is_a_remote_command(self):
        # OpenSSH has no long options: `--help` is the command run on the
        # remote, so the connection to prod still happens.
        decision, _ = run_hook("ssh deploy@prod-web-1 --help")
        self.assertEqual(decision, "deny")

    def test_help_does_not_leak_across_segments(self):
        # The flag exempts only its own simple command.
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        decision, _ = run_hook(
            "kubectl delete --help && kubectl delete ns payments", home=home)
        self.assertEqual(decision, "deny")

    def test_help_flag_as_a_value_is_not_help(self):
        # `--help` only counts as its own token, never as part of a value.
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        decision, _ = run_hook(
            "kubectl create configmap c --from-literal=note=--help", home=home)
        self.assertEqual(decision, "deny")


class AliasToolTests(unittest.TestCase):
    """Q1: oc shares kubectl's evaluator; podman/nerdctl/docker-compose share
    docker's. The alias must inherit the full decision matrix, plus the few
    alias-specific verbs (oc login/project, podman --connection)."""

    def test_oc_prod_mutation_denied(self):
        decision, _ = run_hook("oc --context gke_acme_prod-us delete project x")
        self.assertEqual(decision, "deny")

    def test_oc_readonly_defers(self):
        for cmd in ("oc --context gke_acme_prod-us get pods",
                    "oc status", "oc whoami", "oc projects",
                    "oc process -f template.yaml"):
            with self.subTest(cmd=cmd):
                decision, _ = run_hook(cmd)
                self.assertIsNone(decision)

    def test_oc_ambient_mutation_denies(self):
        # oc shares the kubectl evaluator; unpinned mutation -> deny (pin
        # --context) even against a nonprod ambient context.
        home = make_home(kubeconfig=KUBECONFIG_KIND)
        decision, _ = run_hook("oc new-app nginx", home=home)
        self.assertEqual(decision, "deny")

    def test_oc_login_asks(self):
        decision, reason = run_hook("oc login https://api.cluster.example:6443")
        self.assertEqual(decision, "ask")
        self.assertIn("kubeconfig", reason)

    def test_oc_project_switch_asks_bare_defers(self):
        decision, _ = run_hook("oc project other-ns")
        self.assertEqual(decision, "ask")
        decision, _ = run_hook("oc project")
        self.assertIsNone(decision)

    def test_kubectl_login_verb_stays_guarded(self):
        # kubectl has no `login`; via oc's rule it now asks rather than
        # falling into ambient resolution — still never a silent pass.
        decision, _ = run_hook("kubectl login")
        self.assertEqual(decision, "ask")

    def test_podman_local_defers_remote_denied(self):
        decision, _ = run_hook("podman rm -f c1")
        self.assertIsNone(decision)
        decision, _ = run_hook("podman --connection prod-host rm -f c1")
        self.assertEqual(decision, "deny")

    def test_podman_container_host_env_denied(self):
        decision, _ = run_hook(
            "CONTAINER_HOST=ssh://root@prod-host podman rm -f c1")
        self.assertEqual(decision, "deny")

    def test_podman_push_prod_registry_denied(self):
        decision, _ = run_hook("podman push registry.prod.acme.io/app:1")
        self.assertEqual(decision, "deny")

    def test_nerdctl_push_and_local(self):
        decision, _ = run_hook("nerdctl rm -f c1")
        self.assertIsNone(decision)
        decision, _ = run_hook("nerdctl push registry.prod.acme.io/app:1")
        self.assertEqual(decision, "deny")


class DockerComposeAndMultiTagTests(unittest.TestCase):
    """Q5: every -t on docker build --push is classified, and compose pushes
    (whose registry lives in the compose file) fail closed."""

    def test_multi_tag_push_prod_second_denied(self):
        decision, _ = run_hook(
            "docker build --push -t 127.0.0.1:5000/app:1 "
            "-t registry.prod.acme.io/app:1 .")
        self.assertEqual(decision, "deny")

    def test_multi_tag_push_all_nonprod_defers(self):
        decision, _ = run_hook(
            "docker build --push -t 127.0.0.1:5000/app:1 -t kind-local/app:1 .")
        self.assertIsNone(decision)

    def test_multi_tag_push_unknown_among_nonprod_asks(self):
        decision, reason = run_hook(
            "docker build --push -t 127.0.0.1:5000/app:1 -t ghcr.io/acme/app:1 .")
        self.assertEqual(decision, "ask")
        self.assertIn("ghcr.io/acme/app:1", reason)

    def test_push_without_tags_asks(self):
        decision, _ = run_hook("docker buildx build --push .")
        self.assertEqual(decision, "ask")

    def test_compose_push_asks(self):
        decision, reason = run_hook("docker compose push")
        self.assertEqual(decision, "ask")
        self.assertIn("compose file", reason)

    def test_compose_build_push_asks_plain_build_defers(self):
        decision, _ = run_hook("docker compose build --push")
        self.assertEqual(decision, "ask")
        decision, _ = run_hook("docker compose build")
        self.assertIsNone(decision)

    def test_compose_readonly_defers(self):
        for tail in ("ps", "config", "logs", "ls"):
            with self.subTest(tail=tail):
                decision, _ = run_hook("docker compose %s" % tail)
                self.assertIsNone(decision)

    def test_compose_up_local_defers_prod_context_denied(self):
        decision, _ = run_hook("docker compose up -d")
        self.assertIsNone(decision)
        home = make_home(docker_context="prod-swarm")
        decision, _ = run_hook("docker compose up -d", home=home)
        self.assertEqual(decision, "deny")

    def test_standalone_docker_compose_binary(self):
        decision, _ = run_hook("docker-compose push")
        self.assertEqual(decision, "ask")
        decision, _ = run_hook("docker-compose ps")
        self.assertIsNone(decision)


class RobustnessTests(unittest.TestCase):
    """The hook must never crash (PROD_GUARD_DEBUG=1 in run_hook re-raises
    any exception as a nonzero exit) and never emit `allow` (asserted inside
    run_hook on every call), whatever the input shape."""

    GARBAGE = [
        ";;;", "((((", "))))", "&& kubectl", "kubectl |", "| | |",
        "kubectl --context", "kubectl --context=",
        "kubectl üñîçødé delete",
        "a" * 10000,
        "kubectl " + "-x " * 500 + "delete",
        "docker push", "helm push", "gh api", "terraform", "eval", "bash -c",
        "bash -c ''", "xargs", "sudo", "env", "timeout 5",
        "kubectl delete\x0b\x0cpod",
    ]

    def test_garbage_corpus_never_crashes(self):
        for cmd in self.GARBAGE:
            with self.subTest(cmd=cmd[:40]):
                run_hook(cmd)  # run_hook raises on crash or forbidden output

    def test_long_chain_evaluates_all_segments(self):
        chain = " && ".join(["true"] * 50
                            + ["kubectl --context gke_acme_prod-us delete ns x"])
        decision, _ = run_hook(chain)
        self.assertEqual(decision, "deny")


def _event_payload(command, event, session_id, permission_mode=None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command},
               "hook_event_name": event, "session_id": session_id}
    if permission_mode:
        payload["permission_mode"] = permission_mode
    return payload


class SessionOverrideTests(unittest.TestCase):
    """PROD_GUARD_SESSION_OVERRIDE lifecycle: first use asks, an executed
    (approved) command records a target-scoped grant via the PostToolUse
    event, and later prefixed commands against granted targets defer. All
    subprocess-based: commands are JSON strings, never executed."""

    SID = "sess-1234"
    CMD = ("PROD_GUARD_SESSION_OVERRIDE=e2e-pool-rebuild "
           "kubectl --context gke_acme_prod-us delete ns x")

    def grants_file(self, home, sid=None):
        return Path(home) / ".claude" / "prod-guard" / "session-grants" / (
            (sid or self.SID) + ".json")

    def approve(self, home, command, sid=None):
        """Simulate the human approving the ask: the command ran, so the
        PostToolUse event fires and records the grant."""
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            command, "PostToolUse", sid or self.SID))
        self.assertIsNone(decision)  # the post branch never emits a decision

    def test_first_use_asks_and_names_the_grant(self):
        home = make_home()
        decision, reason = run_hook(None, home=home, payload=_event_payload(
            self.CMD, "PreToolUse", self.SID))
        self.assertEqual(decision, "ask")
        self.assertIn("session override acknowledged", reason)
        self.assertIn("gke_acme_prod-us", reason)

    def test_approved_command_records_grant_and_next_defers(self):
        home = make_home()
        self.approve(home, self.CMD)
        stored = json.loads(self.grants_file(home).read_text(encoding="utf-8"))
        self.assertEqual(stored["grants"][0]["target"], "gke_acme_prod-us")
        self.assertEqual(stored["grants"][0]["reason"], "e2e-pool-rebuild")
        # A DIFFERENT mutating command against the granted target now defers.
        cmd2 = ("PROD_GUARD_SESSION_OVERRIDE=e2e-pool-rebuild "
                "kubectl --context gke_acme_prod-us label node n1 kata=on")
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            cmd2, "PreToolUse", self.SID))
        self.assertIsNone(decision)

    def test_unprefixed_command_still_denies_after_grant(self):
        home = make_home()
        self.approve(home, self.CMD)
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            "kubectl --context gke_acme_prod-us delete ns x",
            "PreToolUse", self.SID))
        self.assertEqual(decision, "deny")

    def test_other_session_and_other_target_still_ask(self):
        home = make_home()
        self.approve(home, self.CMD)
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            self.CMD, "PreToolUse", "sess-other"))
        self.assertEqual(decision, "ask")
        other = ("PROD_GUARD_SESSION_OVERRIDE=e2e-pool-rebuild "
                 "kubectl --context acme-prd-eu delete ns x")
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            other, "PreToolUse", self.SID))
        self.assertEqual(decision, "ask")

    def test_expired_grant_asks_again(self):
        home = make_home()
        gfile = self.grants_file(home)
        gfile.parent.mkdir(parents=True)
        expired = time.time() - guard.SESSION_GRANT_TTL - 60
        gfile.write_text(json.dumps({"grants": [
            {"target": "gke_acme_prod-us", "reason": "old", "ts": expired}]}),
            encoding="utf-8")
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            self.CMD, "PreToolUse", self.SID))
        self.assertEqual(decision, "ask")

    def test_ambient_deny_is_never_grantable(self):
        # Ambient prod context: prefixed command asks, but approval records
        # nothing (ambient state can be repointed mid-session), so it asks
        # again every time.
        home = make_home(kubeconfig=KUBECONFIG_PROD)
        cmd = "PROD_GUARD_SESSION_OVERRIDE=r kubectl delete ns x"
        decision, reason = run_hook(None, home=home, payload=_event_payload(
            cmd, "PreToolUse", self.SID))
        self.assertEqual(decision, "ask")
        self.assertIn("No session grant applies", reason)
        self.approve(home, cmd)
        self.assertFalse(self.grants_file(home).exists())
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            cmd, "PreToolUse", self.SID))
        self.assertEqual(decision, "ask")

    def test_switch_deny_is_never_grantable(self):
        home = make_home()
        cmd = ("PROD_GUARD_SESSION_OVERRIDE=r "
               "kubectl config use-context gke_acme_prod-us")
        self.approve(home, cmd)
        self.assertFalse(self.grants_file(home).exists())
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            cmd, "PreToolUse", self.SID))
        self.assertEqual(decision, "ask")

    def test_multi_locator_grant_covers_all_components(self):
        home = make_home()
        cmd = ("PROD_GUARD_SESSION_OVERRIDE=r gcloud compute instances "
               "delete vm1 --project acme-prod --zone us-east9-x")
        self.approve(home, cmd)
        stored = json.loads(self.grants_file(home).read_text(encoding="utf-8"))
        self.assertEqual({g["target"] for g in stored["grants"]},
                         {"acme-prod", "us-east9-x"})
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            cmd, "PreToolUse", self.SID))
        self.assertIsNone(decision)
        # A new, ungranted locator alongside the granted project re-asks.
        cmd2 = ("PROD_GUARD_SESSION_OVERRIDE=r gcloud compute instances "
                "delete vm1 --project acme-prod --zone us-west9-y")
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            cmd2, "PreToolUse", self.SID))
        self.assertEqual(decision, "ask")

    def test_partial_grant_still_prompts_for_the_rest(self):
        home = make_home()
        self.approve(home, self.CMD)
        chain = (self.CMD +
                 " && kubectl --context acme-prd-eu delete ns y")
        decision, reason = run_hook(None, home=home, payload=_event_payload(
            chain, "PreToolUse", self.SID))
        self.assertEqual(decision, "ask")
        self.assertIn("acme-prd-eu", reason)

    def test_bypass_permissions_cannot_mint_or_use_first_ask(self):
        # No human can answer an ask in bypassPermissions: the first use is a
        # hard deny, so an unattended session can never mint a grant.
        home = make_home()
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            self.CMD, "PreToolUse", self.SID, permission_mode="bypassPermissions"))
        self.assertEqual(decision, "deny")

    def test_granted_target_defers_even_in_bypass(self):
        # The grant was minted by a human approval earlier in the session;
        # suppression happens before the bypass escalation.
        home = make_home()
        self.approve(home, self.CMD)
        decision, _ = run_hook(None, home=home, payload=_event_payload(
            self.CMD, "PreToolUse", self.SID, permission_mode="bypassPermissions"))
        self.assertIsNone(decision)

    def test_readonly_prefixed_command_records_nothing(self):
        home = make_home()
        self.approve(home, "PROD_GUARD_SESSION_OVERRIDE=r "
                           "kubectl --context gke_acme_prod-us get pods")
        self.assertFalse(self.grants_file(home).exists())

    def test_missing_session_id_degrades_to_per_command_ask(self):
        home = make_home()
        payload = {"tool_name": "Bash", "tool_input": {"command": self.CMD},
                   "hook_event_name": "PreToolUse"}
        decision, _ = run_hook(None, home=home, payload=payload)
        self.assertEqual(decision, "ask")


class SessionGrantStoreTests(unittest.TestCase):
    """Unit tests for the grant store: every infrastructure failure must fail
    toward MORE prompts (no grants loaded / nothing recorded), never fewer."""

    def with_home(self):
        home = tempfile.mkdtemp(prefix="prod-guard-grant-home-")
        return home, unittest.mock.patch.dict(os.environ, {"HOME": home})

    def test_record_then_load_roundtrip(self):
        home, patcher = self.with_home()
        with patcher:
            guard.record_session_grants("s1", {"tgt-a", "tgt-b"}, "why", now=1000.0)
            self.assertEqual(guard.load_session_grants("s1", now=1000.0),
                             {"tgt-a", "tgt-b"})
            # TTL boundary: valid at the edge, gone past it.
            edge = 1000.0 + guard.SESSION_GRANT_TTL
            self.assertEqual(guard.load_session_grants("s1", now=edge),
                             {"tgt-a", "tgt-b"})
            self.assertEqual(guard.load_session_grants("s1", now=edge + 1), set())

    def test_first_grant_timestamp_never_slides(self):
        home, patcher = self.with_home()
        with patcher:
            guard.record_session_grants("s1", {"tgt"}, "why", now=1000.0)
            guard.record_session_grants("s1", {"tgt"}, "again", now=5000.0)
            path = guard._session_grants_path("s1")
            grants = json.loads(Path(path).read_text(encoding="utf-8"))["grants"]
            self.assertEqual(len(grants), 1)
            self.assertEqual(grants[0]["ts"], 1000.0)

    def test_corrupt_store_loads_empty_and_recovers_on_write(self):
        home, patcher = self.with_home()
        with patcher:
            path = Path(guard._session_grants_path("s1"))
            path.parent.mkdir(parents=True)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(guard.load_session_grants("s1"), set())
            guard.record_session_grants("s1", {"tgt"}, "why", now=1000.0)
            self.assertEqual(guard.load_session_grants("s1", now=1000.0), {"tgt"})

    def test_unusable_session_ids_yield_no_store(self):
        for sid in (None, "", "   ", 42):
            self.assertIsNone(guard._session_grants_path(sid))
            self.assertEqual(guard.load_session_grants(sid), set())

    def test_session_id_is_sanitized_into_a_safe_filename(self):
        # Path separators are stripped, so a hostile session id cannot
        # escape the grants directory.
        home, patcher = self.with_home()
        with patcher:
            path = guard._session_grants_path("../../evil/../x y")
            grants_dir = os.path.join(home, ".claude", "prod-guard",
                                      "session-grants")
            self.assertEqual(os.path.dirname(path), grants_dir)
            self.assertEqual(os.path.basename(path), ".._.._evil_.._x_y.json")


class WiringTests(unittest.TestCase):
    def test_hooks_json_points_at_script(self):
        with open(REPO / "hooks" / "hooks.json", encoding="utf-8") as f:
            hooks = json.load(f)
        for event in ("PreToolUse", "PostToolUse"):
            entries = hooks["hooks"][event]
            self.assertEqual(len(entries), 1, event)
            self.assertEqual(entries[0]["matcher"], "Bash", event)
            cmd = entries[0]["hooks"][0]["command"]
            self.assertIn("scripts/bash-prod-guard.py", cmd, event)
        self.assertTrue(SCRIPT.exists())

    def test_plugin_and_marketplace_versions_match(self):
        with open(REPO / ".claude-plugin" / "plugin.json", encoding="utf-8") as f:
            plugin = json.load(f)
        with open(REPO / ".claude-plugin" / "marketplace.json", encoding="utf-8") as f:
            market = json.load(f)
        self.assertEqual(plugin["name"], "prod-guard")
        self.assertEqual(market["plugins"][0]["name"], "prod-guard")
        self.assertEqual(plugin["version"], market["plugins"][0]["version"])

    def test_friction_report_command_points_at_script(self):
        # The read-only analyzer command must invoke the real script name.
        cmd_md = REPO / "commands" / "friction-report.md"
        self.assertTrue(cmd_md.exists())
        body = cmd_md.read_text(encoding="utf-8")
        self.assertIn("scripts/friction-report.py", body)
        self.assertTrue((REPO / "scripts" / "friction-report.py").exists())


if __name__ == "__main__":
    unittest.main()
