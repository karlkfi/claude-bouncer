# Project Status

Single source of truth for progress and priorities in prod-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing` `coverage`
**Next ID:** Q16

## Queue

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q15"></a>Q15 | Check friction-report categories against the hook's live output | `tests` `coverage` | 🔲 | S | `CATEGORY_PATTERNS` is exercised only against hand-copied reason strings, so rewording a reason in the hook sends every real transcript to `other` with both suites green. Feed it live output. |
