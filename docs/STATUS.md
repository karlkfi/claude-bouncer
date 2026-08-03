# Project Status

Single source of truth for progress and priorities in prod-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing` `coverage`
**Next ID:** Q15

## Queue

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q13"></a>Q13 | Surface a stale-version warning in the friction report | `docs` | 🔲 | S | Compare installed version (`installed_plugins.json`) against the marketplace clone's `plugin.json` and print "installed X, Y available", flagging staleness where users already look. From [#19](https://github.com/karlkfi/claude-prod-guard/issues/19). |
