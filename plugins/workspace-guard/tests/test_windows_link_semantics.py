#!/usr/bin/env python3
"""Q114: PowerShell's `-Recurse` does not reach through a directory link.

Q76 gave `Remove-Item` an entry role -- a link operand is judged by the link
rather than by whatever it points at -- and `ps_bind_args` withdraws that role
for the whole segment whenever `-Recurse` is bound. The withdrawal rested on not
knowing whether `Remove-Item -Recurse` over a directory link unlinks the entry,
as POSIX `rm -rf dirlink` does, or walks into the target and empties it.

It unlinks the entry. Measured 2026-08-24 in CI run 32692159539, eight
observations -- both Windows jobs, both hosts, both link kinds -- and every one
reported the link gone and the target's file intact:

    Windows PowerShell 5.1.26100.33296   junction   link removed, target intact
    Windows PowerShell 5.1.26100.33296   symlink    link removed, target intact
    PowerShell 7.6.5                     junction   link removed, target intact
    PowerShell 7.6.5                     symlink    link removed, target intact

So the premise the entry role rests on holds for `-Recurse` too, and the
`recursive` pre-pass in `ps_bind_args` has nothing left to protect. Removing it
is Q114's residual rather than part of this file.

These assertions are known to be able to fail: the same run carried an inverted
copy of each one, asserting the target was reached, and all four went red on
these exact observations. That is what a platform probe has to establish, since
one asserting nothing passes just as quietly.

Two hosts, because they are separate products with separate histories --
`powershell.exe` is Windows PowerShell 5.1, shipped with the OS, and
`pwsh.exe` is PowerShell 7. The guard has to be right for whichever one the
user is in, so an answer from one of them would be half an answer.

Off Windows these skip, which costs nothing: the POSIX jobs pass no
`--max-skips`. On Windows they must run or fail, never skip -- the two Windows
jobs ratchet a skip ceiling, and a probe that stood down on the one platform it
is about would spend that ceiling to assert nothing.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

# The precedent in test_workspace_guard.py for a platform split.
WINDOWS_ONLY = unittest.skipIf(hasattr(os, "getuid"),
                               "Windows-only: PowerShell link semantics")

# `New-Item -ItemType SymbolicLink` needs admin or Developer Mode. The runner
# allowed it on 2026-08-24, so the symlink case measured a real answer, but
# another runner may refuse -- these are the refusals that are the OS declining
# rather than the experiment being wrong about where the link goes.
PRIVILEGE_MARKERS = (
    "sufficient privilege",
    "required privilege",
    "unauthorizedaccess",
    "symbolic link",
    "administrator",
)


def _host(name):
    """Absolute path to a PowerShell host.

    Fails rather than skipping. Both hosts ship on windows-latest, so a missing
    one is a runner change worth reddening -- and a skip here would come out of
    the ceiling the Windows jobs ratchet.
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


def _unlink_dir_link(link):
    """Remove a directory link without touching what it points at.

    Teardown cannot use the call under test. `os.rmdir` removes the reparse
    point itself -- it has no recursive mode and cannot descend -- so a bad
    answer to the experiment cannot be manufactured here.
    """
    try:
        if os.path.lexists(link):
            os.rmdir(link)
    except OSError:
        pass


def probe(host_name, item_type):
    """Run Q114's experiment and report what happened.

    A link pointing at a sibling directory holding one file, then
    `Remove-Item -Recurse` on the link. The reading is taken off the target
    before anything is cleaned up, so the verdict does not depend on teardown
    behaving.
    """
    host = _host(host_name)
    with tempfile.TemporaryDirectory() as base:
        target = os.path.join(base, "target")
        work = os.path.join(base, "work")
        link = os.path.join(work, "j")
        os.makedirs(target)
        os.makedirs(work)
        canary = os.path.join(target, "canary.txt")
        with open(canary, "w") as fh:
            fh.write("q114")

        made = _run(host, "$PSVersionTable.PSVersion.ToString()\n"
                          "New-Item -ItemType %s -Path '%s' -Target '%s' "
                          "| Out-Null" % (item_type, link, target), base)
        version = (made.stdout.splitlines() or [""])[0].strip()
        if made.returncode != 0 or not os.path.lexists(link):
            return {"host": host_name, "version": version,
                    "item_type": item_type, "created": False,
                    "create_stderr": made.stderr.strip()}

        removed = _run(host, "Remove-Item -Recurse -Confirm:$false '%s'" % link,
                       base)
        # Read the target BEFORE any cleanup: this is the answer.
        observation = {
            "host": host_name, "version": version, "item_type": item_type,
            "created": True,
            "remove_rc": removed.returncode,
            "remove_stderr": removed.stderr.strip(),
            "link_gone": not os.path.lexists(link),
            "target_dir_survives": os.path.isdir(target),
            "target_file_survives": os.path.isfile(canary),
        }
        _unlink_dir_link(link)
        return observation


def _report(observation):
    return json.dumps(observation, indent=2, sort_keys=True)


def _assert_entry_semantics(case, observation, label):
    """Both halves of the premise the entry role rests on.

    The link goes and the target stays. Asserting only the second would pass a
    `Remove-Item` that did nothing at all.
    """
    case.assertTrue(
        observation["link_gone"],
        "%s: the link survived, so nothing was measured.\n%s"
        % (label, _report(observation)))
    case.assertTrue(
        observation["target_file_survives"],
        "%s: `-Recurse` reached through the link and deleted the target's "
        "file. That reverses Q114 -- the `recursive` pre-pass in "
        "`ps_bind_args` is load-bearing again.\n%s"
        % (label, _report(observation)))


@WINDOWS_ONLY
class RecurseOverAJunctionTests(unittest.TestCase):
    """The load-bearing case. A junction needs no elevation, so it always runs.

    Q114's own experiment, and the shape `ps_bind_args` withholds the entry
    role from.
    """

    def assert_junction(self, host_name):
        observation = probe(host_name, "Junction")
        self.assertTrue(observation["created"],
                        "a junction could not be created, so the experiment "
                        "never ran:\n%s" % _report(observation))
        _assert_entry_semantics(self, observation, "Q114 %s junction"
                                % host_name)

    def test_junction_powershell_51_spares_the_target(self):
        self.assert_junction("powershell")

    def test_junction_pwsh_7_spares_the_target(self):
        self.assert_junction("pwsh")


@WINDOWS_ONLY
class RecurseOverASymlinkTests(unittest.TestCase):
    """The separate case: a directory symbolic link rather than a junction.

    `New-Item -ItemType SymbolicLink` needs admin or Developer Mode, so a
    runner may refuse it and this cannot be load-bearing. It still asserts in
    both branches rather than standing down: a refusal has to look like the OS
    declining the privilege, and any other creation failure reddens.
    """

    def assert_symlink(self, host_name):
        observation = probe(host_name, "SymbolicLink")
        if not observation["created"]:
            stderr = observation["create_stderr"].lower()
            self.assertTrue(
                any(m in stderr for m in PRIVILEGE_MARKERS),
                "a directory symlink could not be created, and the reason does "
                "not read as the OS declining the privilege:\n%s"
                % _report(observation))
            return
        _assert_entry_semantics(self, observation, "Q114 %s symlink"
                                % host_name)

    def test_symlink_powershell_51_spares_the_target(self):
        self.assert_symlink("powershell")

    def test_symlink_pwsh_7_spares_the_target(self):
        self.assert_symlink("pwsh")


if __name__ == "__main__":
    unittest.main()
