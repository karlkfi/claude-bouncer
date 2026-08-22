# Q70 — cut the test suite's wall-clock time

**Status: done.** A full run went from 28.8 s to 3.0 s on an 18-core box, and
from minutes to seconds when the box is busy.

## Goal

Make a full test run cheap enough that nobody batches it, without changing what
any test asserts.

## Where the time goes

The suite is its subprocesses. It makes 865 `subprocess.run` calls, almost all
of them a fresh `python3 scripts/bash-workspace-guard.py`, and on an idle
18-core macOS box (Python 3.14.6) one of those costs 35.4 ms:

| Spawned | Wall | CPU |
|---|---|---|
| `python3 -c pass` | 15.6 ms | 13.8 ms |
| `python3 scripts/bash-workspace-guard.py` | 35.4 ms | 33.0 ms |

865 × 35.4 ms is 30.6 s, and a serial `python3 -m unittest discover tests` takes
28.8 s at 94% CPU. There is nothing else in the run. Interpreter startup is over
half of each spawn, and loading the 4,655-line script is the rest — neither is
work the suite is trying to test.

Per class, the concentration is heavy: `HookEndToEndTests` alone is 17.4 s of a
41.7 s instrumented run (286 tests at 0.061 s each), and every class above a
rounding error invokes the hook as a subprocess. The ~500 pure unit tests
together account for under a second.

**Measure on an idle box.** The same suite timed 135 s and 578 s earlier in this
session while other work had the machine, and Q70's original 5m18s figure looks
like the same artifact. One process doing 30 s of CPU serially loses to whatever
else is running, so the serial number is really a measure of the box's load.
That is a second reason to shard: the run stops being a single long-lived
process competing for one core.

## What to change

**Shard the run across processes.** The subprocess-per-assertion design is what
makes the end-to-end layer worth having — it exercises the real hook boundary:
argv, stdin, environment, exit code. So the fix is to stop paying for those
spawns on one core, not to stop making them. Nothing about what a test asserts
changes.

The suite is safe to shard at test-method granularity: no `setUpClass`, no
`os.chdir`, and every fixture builds its own `TemporaryDirectory`.

Measured, one worker per core:

| Jobs | Wall |
|---|---|
| 1 | 29.8 s |
| 2 | 15.1 s |
| 4 | 8.4 s |
| 8 | 5.2 s |
| 18 | 3.3 s |
| 36 | 3.3 s |

Oversubscription buys nothing, which confirms the work is CPU-bound once it is
spread out. `os.cpu_count()` is the right default; the two-core CI runners get
the 2x and the four-core Windows runners the 4x.

**One entry point, `scripts/run-tests.py`.** It replaces `scripts/skip-ceiling.py`
rather than sitting beside it: once running the suite means fanning out across
processes, a second runner is either a duplicate of the sharding or a shim.
The ceiling itself is unchanged, and moves onto an optional `--max-skips` flag
with the same ratchet and the same exit behaviour.

`python3 -m unittest discover tests` keeps working — it is the fallback when the
runner itself is what's suspect, and one CI job keeps running it. Sharding hides
an order dependence between two tests exactly as a serial run hides a race
between them, so dropping the serial job would trade one blind spot for another
rather than closing it.

**Not doing: `-S` on the hook subprocess.** Skipping `site` looked like a 0.1 s
saving per spawn, but that was measured under load; idle it is 0.6 ms. There is
nothing to buy.

## What sharding surfaced

`SiblingSessionScratchE2ETests` failed under the first parallel run. Its fixture
plants `<claude-tmp-root>/<slug>/<session-id>/` so the hook's directory scan can
anchor, and the slug carried `os.getpid()` — but the scan
(`claude_session_project_dir`) keys on the **session id**, not the slug, and
returns the first slug whose listing holds it. Two workers planting the same
fixed id under their own slugs made the scan resolve to the other worker's
directory, and the sibling-read exemption stopped applying.

The ids now carry the pid too. This was a latent flaw in the fixture, not in the
hook: serial execution only ever had one of them on disk at a time.

## Acceptance

- [x] `scripts/run-tests.py` shards the suite across processes and aggregates
      failures, errors, skips, and expected failures into one verdict.
- [x] `--max-skips` reproduces the Q45 ceiling: `REGRESSION` over, `IMPROVED`
      under (with the `$GITHUB_STEP_SUMMARY` hint), non-zero exit on either a
      failure or a breached ceiling.
- [x] The CI jobs run through it; `CIWiringTests` asserts the new name, and a
      new `unittest-serial` job keeps the plain path green.
- [x] `python3 -m unittest discover tests` still passes unchanged.
- [x] `tests/test_run_tests.py` covers batching, aggregation, the failure
      report, the import-error path, and all three ceiling verdicts.
- [x] Docs updated: `CLAUDE.md`, `docs/development/release-process.md`.
