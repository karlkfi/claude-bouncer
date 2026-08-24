#!/usr/bin/env python3
"""Q121: how PowerShell's `mkdir` binds its operands.

`mkdir` and `md` deferred where `New-Item -ItemType Directory` asked, because
they are not aliases of the cmdlet -- `mkdir` is a FUNCTION that calls it, and
`md` is an alias of the function -- so `PS_ALIASES` never saw either and neither
did `New-Item`'s row.

`mkdir` gets its own PS_SPEC row rather than a PS_ALIASES entry, and this file
is what says that row is right. Two things it establishes, both of which the
first draft of the row got wrong from the documentation:

  - **`-Value` is named-only.** `mkdir two three` is a binding error -- "A
    positional parameter cannot be found that accepts argument 'three'" -- on
    5.1 and 7 alike, and neither directory is created. So there is one
    positional slot, as on `New-Item`, and a second operand repeats `-Path`.
    The draft had `positional=('path', 'value')` and would have let an outside
    path through in that shape.
  - **`-Name` exists.** The draft omitted it, which left its operand to fall
    into the positional list and be checked as a path to create.

Measured 2026-08-24 in CI run 32753995074, both hosts, both Windows jobs.

Q3 recorded the general form: an alias whose parameter set diverges from the row
it points at is the mistake. This is the probe that keeps Q121 from repeating
it.

Q114's lesson is the other half. That row sat for a day asserting the repo had
no Windows host while `.github/workflows/tests.yml` had been running two the
whole time, because the session that filed it measured the workstation. A
platform question this repo can answer belongs in a test rather than in a note
to a future session.

Two hosts, for the reason `test_windows_link_semantics.py` gives: Windows
PowerShell 5.1 and PowerShell 7 are separate products, and the guard has to be
right for whichever one the user is in.

Off Windows these skip. On Windows they must run or fail, never skip -- the two
Windows jobs ratchet a skip ceiling, and a probe that stood down on the one
platform it is about would spend that ceiling to assert nothing.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

WINDOWS_ONLY = unittest.skipIf(hasattr(os, "getuid"),
                               "Windows-only: PowerShell mkdir binding")


def _host(name):
    """Absolute path to a PowerShell host, failing rather than skipping.

    Same rule as the Q114 probe: both hosts ship on windows-latest, so a
    missing one is a runner change worth reddening.
    """
    found = shutil.which(name)
    if found is None:
        raise AssertionError(
            "%s is not on PATH. Both PowerShell hosts ship on windows-latest; "
            "this probe fails rather than skipping, because a skip on the one "
            "platform it is about asserts nothing." % name)
    return found


def _run(host, script, cwd):
    return subprocess.run([host, "-NoProfile", "-NonInteractive", "-Command",
                           script],
                          cwd=cwd, capture_output=True, text=True)


def probe(host_name):
    """Measure what `mkdir` is and how it binds two positional operands."""
    host = _host(host_name)
    with tempfile.TemporaryDirectory() as base:
        # `Get-Command` answers what the command IS; the filesystem answers what
        # it DID. Both are needed -- the first explains why PS_ALIASES missed
        # it, the second is what the row is built on.
        # `Get-Command` answers what the command IS; the filesystem answers what
        # it DID. The two `mkdir` calls are separate runs so a failure in the
        # second cannot be read off the first -- run together, the second's
        # binding error also stops `two` from being created, and a single rc
        # cannot say which statement refused.
        facts = _run(host, (
            "$PSVersionTable.PSVersion.ToString()\n"
            "(Get-Command mkdir).CommandType\n"
            "(Get-Command md).CommandType\n"
            "(Get-Command md).ResolvedCommand.Name\n"
            "((Get-Command mkdir).Parameters.Keys -join ',')\n"
        ), base)
        lines = [ln.strip() for ln in facts.stdout.splitlines() if ln.strip()]
        params = lines[4].lower().split(',') if len(lines) > 4 else []

        one = _run(host, "mkdir 'one' | Out-Null", base)
        two = _run(host, "mkdir 'two' 'three' | Out-Null", base)
        return {
            "host": host_name,
            "version": lines[0] if lines else "",
            "mkdir_type": lines[1] if len(lines) > 1 else "",
            "md_type": lines[2] if len(lines) > 2 else "",
            "md_resolves_to": lines[3] if len(lines) > 3 else "",
            "parameters": params,
            "facts_stderr": facts.stderr.strip(),
            "one_rc": one.returncode,
            "one_stderr": one.stderr.strip(),
            "two_rc": two.returncode,
            "two_stderr": two.stderr.strip(),
            "made_one": os.path.isdir(os.path.join(base, "one")),
            "made_two": os.path.isdir(os.path.join(base, "two")),
            "made_three": os.path.isdir(os.path.join(base, "three")),
        }


def _report(observation):
    return json.dumps(observation, indent=2, sort_keys=True)


@WINDOWS_ONLY
class MkdirIsAFunctionTests(unittest.TestCase):
    """Why `PS_ALIASES` never routed these, stated as a measurement."""

    def assert_shapes(self, host_name):
        o = probe(host_name)
        self.assertEqual("Function", o["mkdir_type"],
                         "`mkdir` is not a function here, so Q121's account of "
                         "why it deferred is wrong.\n%s" % _report(o))
        self.assertEqual("Alias", o["md_type"], _report(o))
        self.assertEqual("mkdir", o["md_resolves_to"].lower(),
                         "`md` resolves somewhere other than `mkdir`, so the "
                         "PS_ALIASES entry points at the wrong row.\n%s"
                         % _report(o))

    def test_powershell_51(self):
        self.assert_shapes("powershell")

    def test_pwsh_7(self):
        self.assert_shapes("pwsh")


@WINDOWS_ONLY
class MkdirPositionalBindingTests(unittest.TestCase):
    """The load-bearing case: how many positional slots `mkdir` has.

    One. `-Value` is named-only, so a second operand binds nowhere and the
    statement is refused. That is what puts `positional=('path',)` in the row,
    and it is the direction that matters: with a second slot bound to `-Value`,
    an outside path written there would be read as content and never checked.

    The over-check that follows is deliberate. Our binder repeats the last slot,
    so `mkdir a b` checks `b` as a path to create -- a prompt on a statement
    PowerShell will not run, which is the safe way to be wrong.
    """

    def assert_binding(self, host_name):
        o = probe(host_name)
        # Establishes the probe ran at all, and that one operand is accepted.
        self.assertEqual(0, o["one_rc"], _report(o))
        self.assertTrue(
            o["made_one"],
            "`mkdir one` created nothing, so nothing here was measured.\n%s"
            % _report(o))

        self.assertNotEqual(
            0, o["two_rc"],
            "`mkdir two three` was accepted, so `mkdir` has a second positional "
            "slot. If it binds `-Value`, the `mkdir` PS_SPEC row must NOT check "
            "a second operand as a path; if it repeats `-Path`, the row is "
            "right and this assertion is what changes. Read the created "
            "directories below before deciding.\n%s" % _report(o))
        self.assertFalse(
            o["made_three"],
            "`mkdir two three` created `three`, so position 1 is another "
            "`-Path` element.\n%s" % _report(o))
        self.assertIn(
            "positional parameter", o["two_stderr"].lower(),
            "`mkdir two three` failed for some reason other than binding, so "
            "this says nothing about the slot list.\n%s" % _report(o))

    def test_powershell_51(self):
        self.assert_binding("powershell")

    def test_pwsh_7(self):
        self.assert_binding("pwsh")


@WINDOWS_ONLY
class MkdirParameterSetTests(unittest.TestCase):
    """The parameters the row names, and the ones it deliberately does not.

    Naming a parameter the function rejects would invent a binding; omitting one
    it accepts would leave an operand unchecked.
    """

    def assert_parameters(self, host_name):
        o = probe(host_name)
        # `-Name` is here because the first draft of the row omitted it and this
        # is what said so: unnamed, its operand falls into the positional list
        # and gets checked as a path to create.
        for name in ("path", "name", "value", "force"):
            self.assertIn(name, o["parameters"],
                          "`mkdir` has no -%s, so the PS_SPEC row names a "
                          "parameter that does not exist.\n%s"
                          % (name, _report(o)))
        for name in ("literalpath", "itemtype"):
            self.assertNotIn(
                name, o["parameters"],
                "`mkdir` accepts -%s after all, so the row leaves an operand "
                "unbound.\n%s" % (name, _report(o)))

    def test_powershell_51(self):
        self.assert_parameters("powershell")

    def test_pwsh_7(self):
        self.assert_parameters("pwsh")


if __name__ == "__main__":
    unittest.main()
