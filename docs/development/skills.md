# Agent reference: Skills this repo invokes

Some of this repo's process rules are carried by **skills** — packaged instruction sets an agent loads on demand instead of reading out of a doc. The ones this repo leans on are installed on the workstation rather than committed here, and their source repo is private, so a page that needs to name one has nowhere to link and a bare URL would return 404 for most readers.

This page is that link target. It records what each skill is for and where this repo invokes it, so another page can point a reader at an explainer with `skills.md#<name>`.

**It deliberately does not restate what a skill says.** A skill is written to be portable across repos, and the repo-specific half already lives in the page that invokes it; duplicating the general half here would leave two copies to correct. Every entry below is a pointer plus the local usage, and nothing more.

## A contributor without the skills loses nothing

Skills change how an *agent* works, never what a contributor must do. Every rule that matters is written out in this tree in full: an entry below is the map, not the territory. If a page ever reads as though the skill is required to follow it, that page has a defect — file it.

## Three sources, and only one of them is linkable

The distinction matters because getting it wrong produces a link that resolves for its author and for nobody else.

| Source | Where it lives | Linkable from `docs/`? |
|---|---|---|
| **Globally installed** | `~/.claude/skills/`, from the private `karlkfi/claude-skills` | **No.** Outside every repo, and private. Name it and link this page. |
| **Plugin** | `~/.claude/plugins/**/skills/`, from each plugin's own repo | **No.** Same reason; these are namespaced `plugin:skill`. |
| **Repo-local** | `.claude/skills/` in this repo | **Yes.** In-tree, so a relative link resolves for any reader with a checkout. This repo has none today. |

One case looks like the second row and is not: the friction report this plugin *ships* reaches users as `prod-guard:friction-report`, but its source is [`commands/friction-report.md`](../../commands/friction-report.md) right here. Link the in-tree path, not the installed namespace.

Unlike some sibling repos, prod-guard has **no link checker** — continuous integration (CI) runs `python3 -m unittest discover tests` and nothing else (`.github/workflows/tests.yml`). A dead relative link here rots as quietly as a dead URL. So the argument for relative links in this tree is not that a gate catches them; it is that a reader can follow them at all.

## The skills this repo uses

From `karlkfi/claude-skills`. Measured 2026-08-16: `session-backlog` is the only globally installed skill this repo names, so this list has one entry. Keep it that way — an entry with no local usage rots silently.

### `session-backlog`

The format and grooming process for the Queue in [`docs/STATUS.md`](../STATUS.md): identifier allocation, adding and completing and deferring items, and the commit discipline that keeps merge conflicts trivial. [`maintaining-backlog.md`](maintaining-backlog.md) is authoritative wherever the two overlap and holds this repo's wiring, and the repo vendors the skill's tooling (`scripts/lint-backlog.sh`, `scripts/next-task.sh`, `scripts/backlog-metrics.sh`) so the checks work whether or not the skill is installed.

## Keeping this page honest

A skill can be renamed or retired upstream without anything here going red, because no gate reads the skill set and an agent told to invoke a skill that is not installed gets no error — the rules simply do not get applied. That has already happened once: `backlog` became `session-backlog` (`karlkfi/claude-skills` 7fe76b9), and the three live references plus one in a completed plan document had to be found by hand.

Two things make that search harder than it looks, both worth repeating before trusting a sweep:

- **A wrapped reference is invisible to `grep`.** One of the four hits was `backlog` at the end of a line with `skill` at the start of the next, so every single-line pattern missed it. Search for the bare skill name, then read the hits, rather than matching a phrase that can straddle a break.
- **A clean sweep proves nothing on its own.** Run the same pattern against a copy that still holds the old name (`git show` of an earlier revision works) and confirm it matches there. Without that control, a pattern that never ran and a tree that is genuinely clean produce identical output.

When a page here names a skill, name it exactly and check this list. When the list itself is wrong, the tell is a name that no longer appears in `karlkfi/claude-skills`.
