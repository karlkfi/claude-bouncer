#!/usr/bin/env python3
"""Q114: does PowerShell's `-Recurse` reach through a directory link?

Q76 gave `Remove-Item` an entry role -- a link operand is judged by the link
rather than by whatever it points at -- and `ps_bind_args` withdraws that role
for the whole segment whenever `-Recurse` is bound. The withdrawal rests on not
knowing whether `Remove-Item -Recurse` over a directory link unlinks the entry,
as POSIX `rm -rf dirlink` does, or walks into the target and empties it.

Q114 said the question could not be settled here. It could: this suite runs on
`windows-latest` twice, under pwsh and under Git Bash, and PowerShell is on the
PATH of both. So the experiment lives here rather than in a row, and the answer
is an assertion rather than a note somebody has to trust.

Two hosts, because they are separate products with separate histories --
`powershell.exe` is Windows PowerShell 5.1, shipped with the OS, and
`pwsh.exe` is PowerShell 7. The guard has to be right for whichever one the
user is in, so an answer from one of them is half an answer.

Off Windows these skip, which costs nothing: the POSIX jobs pass no
`--max-skips`. On Windows they must run or fail, never skip -- the two Windows
jobs ratchet a skip ceiling, and a probe that stands down on the one platform
it is about would spend that ceiling to assert nothing.
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

# `New-Item -ItemType SymbolicLink` needs admin or Developer Mode, so a runner
# may refuse it. These are the refusals that are the OS declining, as opposed
# to the experiment being wrong about where the link goes.
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

    A junction pointing at a sibling directory holding one file, then
    `Remove-Item -Recurse` on the junction. The reading is taken off the target
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


@WINDOWS_ONLY
class RecurseOverAJunctionTests(unittest.TestCase):
    """The load-bearing case. A junction needs no elevation, so it always runs.

    Q114's own experiment, and the shape `ps_bind_args` withholds the entry
    role from.
    """

    def assert_junction(self, host_name, spares_target):
        observation = probe(host_name, "Junction")
        self.assertTrue(observation["created"],
                        "a junction could not be created, so the experiment "
                        "never ran:\n%s" % _report(observation))
        self.assertEqual(
            observation["target_file_survives"], spares_target,
            "Q114 %s: expected the target to be %s.\n%s"
            % (host_name, "spared" if spares_target else "reached",
               _report(observation)))

    # PAIRED ON PURPOSE, for one run only. Neither answer was known when this
    # landed and both are real -- a reached target keeps the `recursive` guard,
    # a spared one retires it -- so rather than guess and read a green run that
    # would have been green either way, both directions assert and the Windows
    # job says which one holds. The loser comes out in the next commit, which
    # leaves the survivor with a demonstrated failing inverse: the trap a
    # platform probe falls into is passing while asserting nothing.

    def test_junction_powershell_51_spares_the_target(self):
        self.assert_junction("powershell", spares_target=True)

    def test_junction_powershell_51_reaches_the_target(self):
        self.assert_junction("powershell", spares_target=False)

    def test_junction_pwsh_7_spares_the_target(self):
        self.assert_junction("pwsh", spares_target=True)

    def test_junction_pwsh_7_reaches_the_target(self):
        self.assert_junction("pwsh", spares_target=False)


@WINDOWS_ONLY
class RecurseOverASymlinkTests(unittest.TestCase):
    """The separate case: a directory symbolic link rather than a junction.

    `New-Item -ItemType SymbolicLink` needs admin or Developer Mode, so the
    runner may refuse it and this cannot be load-bearing. It still asserts in
    both branches rather than standing down: a refusal has to look like the OS
    declining, and any other creation failure reddens.
    """

    def assert_symlink(self, host_name, spares_target):
        observation = probe(host_name, "SymbolicLink")
        if not observation["created"]:
            stderr = observation["create_stderr"].lower()
            self.assertTrue(
                any(m in stderr for m in PRIVILEGE_MARKERS),
                "a directory symlink could not be created, and the reason does "
                "not read as the OS declining the privilege:\n%s"
                % _report(observation))
            return
        self.assertEqual(
            observation["target_file_survives"], spares_target,
            "Q114 %s symlink: expected the target to be %s.\n%s"
            % (host_name, "spared" if spares_target else "reached",
               _report(observation)))

    # Paired for the same one run as the junction pair above.

    def test_symlink_powershell_51_spares_the_target(self):
        self.assert_symlink("powershell", spares_target=True)

    def test_symlink_powershell_51_reaches_the_target(self):
        self.assert_symlink("powershell", spares_target=False)

    def test_symlink_pwsh_7_spares_the_target(self):
        self.assert_symlink("pwsh", spares_target=True)

    def test_symlink_pwsh_7_reaches_the_target(self):
        self.assert_symlink("pwsh", spares_target=False)


if __name__ == "__main__":
    unittest.main()
