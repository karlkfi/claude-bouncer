#!/usr/bin/env python3
"""queue.py — read, check, order and migrate a per-item backlog store.

One file per item under `docs/queue/`, priority held in each item's `rank`
key rather than in its position in a table. Items never share a file, so two
sessions editing different items cannot conflict, whatever the merge algorithm
and with no merge driver installed.

Subcommands:
  render    the ordered backlog — the read path, one call for the whole queue
  next      the top ready item, as a session kickoff prompt
  lint      check the store (frontmatter, ids, ranks, references)
  claims    check every id this branch adds holds a claim on the remote
  metrics   replay git history into flow metrics
  migrate   convert a legacy `docs/STATUS.md` Queue/Deferred table into items
  rank      compute an order key for an insertion

Ranks are base-36 order keys compared as plain strings, under a magnitude-head
scheme a second implementation of the same algebra also targets, so a store
written by either tool reads and extends under the other. `rank-vectors.tsv`
holds that contract and its provenance; it sits beside this file in the
session-backlog skill and is not vendored with it.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
STATUSES = ("ready", "blocked", "deferred")
ID_RE = re.compile(r"^Q\d+$")

# A reference is a link at an item's file, not a bare id in prose. The two are
# different claims: a link becomes a live href in a rendered index and dangles
# once the item ships, while "the Q2 audit" is a sentence about history that
# stays true forever. Matching both made the store noisier with every item it
# cleared, which is backwards for a store that is supposed to drain.
ITEM_LINK_RE = re.compile(r"\[[^\]]*\]\((?:\./)?(Q\d+)\.md(?:#[^)]*)?\)")
# What a blocked row waits on is what it opens with — `Blocked by [Q3](Q3.md)`,
# or the same sentence in prose where the blocker is not an item. Anchored on
# purpose: an id quoted anywhere in the note is an example, and a check an
# unrelated example satisfies cannot fail when it should. `[\s*]` rather than
# `\s` because the trigger check below marks its opener in bold.
BLOCKER_RE = re.compile(r"[\s*]*Blocked[\s*]+(?:by|on)[\s*]+\S")
# Backticks are the store's escape for exhibiting syntax rather than using it,
# so a quoted id or link is neither a reference nor a blocker. The site build
# honours the same escape.
CODE_SPAN_RE = re.compile(r"`[^`]*`")

# A citation into the tree, in `grep -n` output order: the path, the line, and
# optionally the line's own text. The third field is what makes rot detectable —
# a line number alone is checkable only for resolving, and a file that grew
# moves the line under a pointer that keeps on resolving. It is terminated by a
# backtick rather than by end of line, because in prose nothing says where the
# quoted text stops; that is also why the fragment may not open with a space,
# which `grep -n` never emits either.
CITATION_RE = re.compile(
    r"\b([\w./-]+\.(?:go|py|sh|md|ya?ml|json|ts|js|rs|java)):(\d+)"
    r"(?::(\S[^`\n]*)(?=`))?")
# How far the fragment may have drifted before the line number stops doing its
# job. A number is worth writing because it lands a reader within a screen of
# the thing, so a fragment still visible from the cited line is a citation that
# works, and noting drift below that would cry wolf on every edit above it. The
# rot worth catching is a number pointing somewhere else entirely, which in the
# cases this was tuned against ran to tens of lines.
#
# The default rather than the value: `--citation-window` overrides it, because
# `--strict stale-citation` cannot. Promotion decides what a note costs and
# never what counts as one, so a caller that binds the class still cannot see a
# fragment that moved nine lines — and a clean run under that gate means *no
# citation has drifted more than this*, not *citations are exact*.
CITATION_WINDOW = 10
# Marks a citation the row is *about* rather than one it relies on, and `lint`
# then reads none of it. A row quoting a pointer as its subject — a stale one
# kept as an exhibit, most of all — is the case where reporting drift is worse
# than missing it: the note for a resolvable pointer names the line the fragment
# moved to, so the checker hands a sweeping session a one-character repair that
# deletes the exhibit and passes every gate. Nothing else separates the two
# populations here. `check-positional-citations.py` tells its own exhibits apart
# by whether the match sits inside a code span, which cannot work for a citation
# that always does. The prefix goes inside the span, immediately before the
# path, so it travels when the citation is copied into another row.
EXHIBIT_PREFIX = "exhibit:"

# What `lint` reports rather than fails on, one name per class. A note is
# advisory because the store alone cannot settle what it found: a link at an
# absent item is a shipped blocker and a typo at once, indistinguishable from
# the files, so failing on it would redden every store for the hours after any
# merge. A caller that needs certainty about one class asks for that class.
#
# Per class rather than one switch, because the classes have different callers
# and they disagree. An orchestrator linting a merged set wants `dangling-link`
# advisory precisely because that window is when links are legitimately in
# flight; a groom checking blockers wants certainty about its own class in the
# same run. One boolean makes each caller accept the other's promotion, and
# there is no reason for anyone to accept a class they did not ask for. For the
# same reason there is no `all`: it would carry every class added later into a
# gate that never chose them.
NOTE_CLASSES = (
    "dangling-link",     # a note links an item file the store does not hold
    "blocked-opener",    # a blocked item's note does not open with its blocker
    "deferred-trigger",  # a deferred item names no condition that revives it
    "stale-citation",    # a `file.ext:N` pointer that no longer finds its line
    "empty-store",       # no items loaded, the usual cause being a wrong --store
)

# The bottom of the space: head 'A' takes 26 digits after it. It is reserved
# rather than usable — fractional room sits above an integer and never below
# one, so a key occupying the lowest integer would be one nothing could be
# inserted below.
SMALLEST_INTEGER = "A" + DIGITS[0] * 26


# --- rank algebra ---------------------------------------------------------
#
# A rank is an order key: a magnitude head, an integer part whose length the
# head fixes, and an optional fraction. Plain string comparison orders two
# ranks, so placing an item names a string between its neighbours and writes
# only that item's own file.
#
# The head is what keeps keys short. Midpointing alone never runs out but
# degrades exactly where this process pushes hardest: inserting below the
# smallest key prepends a digit every few insertions, and flakes-first sends
# every new flake to the top. A head lets the integer part step whole
# magnitudes instead — "a0" to "Zz" to "Zy" — so head and tail insertion cost
# no length at all until a magnitude is exhausted. Heads 'a'..'z' carry integer
# lengths 2..27 upward, 'Z'..'A' the same downward, and uppercase sorting below
# lowercase is what puts the descending magnitudes underneath.
#
# The algebra is implemented twice, so a change here is half a change. The
# shared vectors in `rank-vectors.tsv` are what hold the two sides to one
# scheme — change them first, and read that file's header for which second
# implementation this is and where it stands.

def integer_length(head):
    if "a" <= head <= "z":
        return ord(head) - ord("a") + 2
    if "A" <= head <= "Z":
        return ord("Z") - ord(head) + 2
    raise ValueError(f"rank head {head!r} is not a magnitude character")


def integer_part(rank):
    n = integer_length(rank[0])
    if n > len(rank):
        raise ValueError(
            f"rank {rank!r} is shorter than the {n} characters its head requires")
    return rank[:n]


def check_rank(rank):
    """Raise ValueError unless rank is a well-formed order key."""
    if not rank:
        raise ValueError("rank is empty")
    if rank == SMALLEST_INTEGER:
        raise ValueError(f"rank {rank!r} is the reserved bottom of the space")
    frac = rank[len(integer_part(rank)):]
    if frac.strip(DIGITS):
        raise ValueError(
            f"rank {rank!r} holds a character outside base-36 after its integer part")
    # "x0" and "x" denote the same value, and midpointing toward a trailing
    # zero would not terminate.
    if frac and frac[-1] == DIGITS[0]:
        raise ValueError(
            f"rank {rank!r} ends in {DIGITS[0]!r}, which denotes the same value "
            f"as the rank without it")


def increment_integer(x):
    head, digs = x[0], list(x[1:])
    carry = True
    for i in range(len(digs) - 1, -1, -1):
        if not carry:
            break
        d = DIGITS.index(digs[i]) + 1
        if d == len(DIGITS):
            digs[i] = DIGITS[0]
            continue
        digs[i] = DIGITS[d]
        carry = False
    if not carry:
        return head + "".join(digs)
    if head == "Z":
        return "a" + DIGITS[0]
    if head == "z":
        raise ValueError(f"rank {x!r} is at the top of the space")
    nxt = chr(ord(head) + 1)
    if nxt > "a":
        digs.append(DIGITS[0])
    else:
        digs = digs[:-1]
    return nxt + "".join(digs)


def decrement_integer(x):
    head, digs = x[0], list(x[1:])
    borrow = True
    for i in range(len(digs) - 1, -1, -1):
        if not borrow:
            break
        d = DIGITS.index(digs[i]) - 1
        if d == -1:
            digs[i] = DIGITS[-1]
            continue
        digs[i] = DIGITS[d]
        borrow = False
    if not borrow:
        return head + "".join(digs)
    if head == "a":
        return "Z" + DIGITS[-1]
    if head == "A":
        raise ValueError(f"rank {x!r} is at the bottom of the space")
    prev = chr(ord(head) - 1)
    if prev < "Z":
        digs.append(DIGITS[-1])
    else:
        digs = digs[:-1]
    return prev + "".join(digs)


def _digit_at(s, i):
    """s[i], or the lowest digit once s has ended — what an unwritten
    fractional digit denotes."""
    return s[i] if i < len(s) else DIGITS[0]


def midpoint(lo, hi):
    """A fraction strictly between lo and hi, an empty hi meaning the top."""
    if hi:
        # Descend through the shared prefix: it constrains nothing, and
        # dropping it keeps the result minimal-length.
        n = 0
        while n < len(hi) and _digit_at(lo, n) == hi[n]:
            n += 1
        if n > 0:
            return hi[:n] + midpoint(lo[n:], hi[n:])
    lead = DIGITS.index(lo[0]) if lo else 0
    limit = DIGITS.index(hi[0]) if hi else len(DIGITS)
    # A gap in the leading digit is the common case, and ends it in one digit.
    if limit - lead > 1:
        return DIGITS[(lead + limit) // 2]
    # Leading digits are adjacent. Where hi has more to say, its own leading
    # digit already sits above lo and below hi.
    if len(hi) > 1:
        return hi[:1]
    # hi is a single digit or absent, so the room is below it: keep lo's
    # leading digit and place the rest above lo's tail.
    return DIGITS[lead] + midpoint(lo[1:], "")


def rank_between(lo, hi):
    """A rank strictly between lo and hi. None or "" means open-ended."""
    lo, hi = lo or "", hi or ""
    for name, r in (("lo", lo), ("hi", hi)):
        if r:
            try:
                check_rank(r)
            except ValueError as e:
                raise ValueError(f"{name}: {e}") from e
    if lo and hi and lo >= hi:
        raise ValueError(f"rank {lo!r} is not below {hi!r}")

    if not lo:
        if not hi:
            return "a" + DIGITS[0]
        ih = integer_part(hi)
        if ih == SMALLEST_INTEGER:
            return ih + midpoint("", hi[len(ih):])
        # Where hi carries a fraction its integer part already sits below it,
        # which costs no length.
        if ih < hi:
            return ih
        below = decrement_integer(ih)
        if below == SMALLEST_INTEGER:
            # The bottom magnitude is reserved, so the room left is fractional.
            return below + midpoint("", "")
        return below

    il = integer_part(lo)
    fl = lo[len(il):]

    if not hi:
        try:
            return increment_integer(il)
        except ValueError:
            # The top magnitude is exhausted, so the room left is fractional.
            return il + midpoint(fl, "")

    ih = integer_part(hi)
    if il == ih:
        return il + midpoint(fl, hi[len(ih):])
    nxt = increment_integer(il)
    if nxt < hi:
        return nxt
    return il + midpoint(fl, "")


def rank_series(count):
    """Successive keys for a bulk import, in order."""
    out = []
    cur = "a" + DIGITS[0]
    for _ in range(count):
        out.append(cur)
        cur = increment_integer(integer_part(cur))
    return out


# --- the store ------------------------------------------------------------

class Item:
    __slots__ = ("id", "rank", "labels", "status", "size", "target",
                 "title", "notes", "path")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def sort_key(self):
        # Ties break by numeric id so two sessions that never saw each other
        # cannot produce an order that depends on which side merged first.
        #
        # A tie is the intended outcome rather than drift, which is why nothing
        # reports one. `rank_between` returns a key in an open interval, so two
        # sessions minting the same key passed the same neighbours: both asked
        # for "somewhere between these two" and neither specified an order
        # against the other, having never seen it. Any third item holds a
        # distinct key and sorts strictly outside both, so tied items stay
        # adjacent however the tie falls, and every placement still holds.
        return (self.rank or "", int(self.id[1:]) if ID_RE.match(self.id or "") else 0)


def _parse_frontmatter(text, path):
    """Minimal YAML reader for the shapes this store writes. Returns (dict, body)."""
    problems = []
    if not text.startswith("---\n"):
        return None, "", [f"{path}: no frontmatter (line 1 is not '---')"]
    end = text.find("\n---\n", 3)
    if end == -1:
        return None, "", [f"{path}: frontmatter not closed with '---'"]
    head, body = text[4:end + 1], text[end + 5:]
    data, key = {}, None
    for raw in head.split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")) and raw.lstrip().startswith("- "):
            if key is None:
                problems.append(f"{path}: list item before any key")
                continue
            data.setdefault(key, [])
            if not isinstance(data[key], list):
                data[key] = []
            data[key].append(raw.lstrip()[2:].strip())
            continue
        if ":" not in raw:
            problems.append(f"{path}: frontmatter line is not 'key: value': {raw!r}")
            continue
        key, _, val = raw.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            data[key] = [v.strip() for v in val[1:-1].split(",") if v.strip()]
        elif val == "":
            data[key] = []
        else:
            data[key] = val
    return data, body, problems


def read_item(path):
    text = path.read_text(encoding="utf-8")
    data, body, problems = _parse_frontmatter(text, path.name)
    if data is None:
        return None, problems
    title, notes = "", ""
    for line in body.split("\n"):
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif title and line.strip():
            notes += (" " if notes else "") + line.strip()
    labels = data.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    item = Item(id=data.get("id"), rank=data.get("rank"), labels=labels,
                status=data.get("status"), size=data.get("size"),
                target=data.get("target") or None, title=title,
                notes=notes.strip(), path=path)
    return item, problems


def store_dir(root=None):
    root = Path(root) if root else Path(
        subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, check=True).stdout.strip())
    return root / "docs" / "queue"


def load(store):
    items, problems = [], []
    for path in sorted(Path(store).glob("Q*.md")):
        item, probs = read_item(path)
        problems.extend(probs)
        if item:
            items.append(item)
    items.sort(key=Item.sort_key)
    return items, problems


def write_item(store, item):
    lines = [f"id: {item.id}", f"rank: {item.rank}"]
    if item.labels:
        lines.append("labels:")
        lines += [f"    - {label}" for label in item.labels]
    lines.append(f"status: {item.status}")
    if item.size:
        lines.append(f"size: {item.size}")
    if item.target:
        lines.append(f"target: {item.target}")
    body = f"# {item.title}\n"
    if item.notes:
        body += f"\n{item.notes}\n"
    path = Path(store) / f"{item.id}.md"
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\n" + body, encoding="utf-8")
    return path


# The label for a row that ends in a choice rather than a task. It is a label
# and not a status because the row is genuinely ready the moment the question
# is answered, and `status` is what drives dispatch — a fourth status would
# take these out of selection permanently, which is the opposite of what
# marking them is for.
OPEN_QUESTION = "open-question"


# --- subcommands ----------------------------------------------------------

# An index cell is not the item. The store deliberately retired the Notes
# length cap so an item can hold its full context, and a table that renders
# that in full is squeezed by its own longest row: a browser sizes columns by
# content, so one long cell claims the width and every other column wraps into
# a ribbon. The full text is one click away on the item's own page.
NOTES_IN_TABLE = 140

# The title is capped where the note is not, because their homes differ. A note
# has a page of its own where length costs nothing, and the index summarizes it.
# A title has no such page: it renders whole in every index row, in `next`'s
# kickoff prompt, and in any session named after the item. 72 is the
# conventional commit-message wrap, doing the same job — one line that has to
# survive a list.
TITLE_MAX = 72


def summarize(notes, limit=NOTES_IN_TABLE):
    """The first sentence, or a clean truncation, whichever comes first."""
    notes = " ".join((notes or "").split())
    if len(notes) <= limit:
        return notes
    stop = notes.find(". ")
    if 0 < stop <= limit:
        return notes[:stop + 1]
    cut = notes.rfind(" ", 0, limit)
    return notes[:cut if cut > 0 else limit].rstrip(",;:") + " …"


def cmd_render(args):
    items, problems = load(args.store or store_dir())
    for p in problems:
        print(f"queue: {p}", file=sys.stderr)
    shown = [i for i in items if args.all or i.status != "deferred"]
    if args.label:
        shown = [i for i in shown if args.label in i.labels]
    if args.format == "table":
        print("| ID | Item | Labels | St | Sz | Notes |")
        print("|---|---|---|---|---|---|")
        mark = {"ready": "🔲", "blocked": "🚫", "deferred": "💤"}
        for i in shown:
            title = f"[{i.title}]({i.target})" if i.target else i.title
            labels = " ".join(f"`{label}`" for label in i.labels)
            notes = summarize(i.notes).replace("|", r"\|")
            # The id links to the item's own page: this table is what a reader
            # meets first, and the page is where the full text lives.
            print(f"| [{i.id}]({i.id}.md) | {title} | {labels} "
                  f"| {mark.get(i.status, '?')} | {i.size or ''} | {notes} |")
    else:
        for i in shown:
            labels = ",".join(i.labels)
            print(f"{i.status:<8} {i.id:<6} {i.size or '-':<2} {i.title}"
                  + (f"   [{labels}]" if labels else ""))
    return 1 if problems else 0


def cmd_next(args):
    items, _ = load(args.store or store_dir())
    ready = [i for i in items if i.status == "ready"]
    if not ready:
        print("queue: no ready item", file=sys.stderr)
        return 1
    top = ready[0]
    if args.title:
        print(f"{top.id}: {top.title}")
    else:
        # A session handed a row that ends in a choice either guesses at the
        # decision or stalls asking for it, and most of these need neither —
        # they are settleable from the repo. So the prompt carries the split
        # rather than just the warning.
        question = (" This item carries an open question: settle what the "
                    "repo's own evidence settles, and put it to the "
                    "maintainer only where it turns on their preference, "
                    "their authority, or a publish outside the repo."
                    if OPEN_QUESTION in top.labels else "")
        print(f"{top.id}: {top.title} — take this item from the top of the "
              f"backlog and work it per the repo process: check for an open PR "
              f"first, verify any blockers, do the work, then delete "
              f"docs/queue/{top.id}.md in the PR that completes it."
              + question
              + (f" Notes: {top.notes}" if top.notes else "")
              + (f" See: {top.target}" if top.target else ""))
    return 0


def cmd_lint(args):
    store = Path(args.store or store_dir())
    items, problems = load(store)
    strict = set(args.strict or ())
    # Off args rather than the constant, and clamped because a negative window
    # would slice backwards and match nothing. See CITATION_WINDOW for why this
    # is separate from `strict`.
    window = max(0, args.citation_window)

    def note(cls, msg):
        """Advisory by default; an error for a class the caller named."""
        if cls in strict:
            problems.append(msg)
        else:
            print(f"queue: note: {msg}", file=sys.stderr)

    seen_id = {}
    ids = {i.id for i in items}
    for i in items:
        where = i.path.name
        if not i.id or not ID_RE.match(i.id):
            problems.append(f"{where}: id {i.id!r} is not QNNN")
        elif i.path.stem != i.id:
            problems.append(f"{where}: filename does not match id {i.id}")
        elif i.id in seen_id:
            problems.append(f"{where}: duplicate id, also in {seen_id[i.id]}")
        else:
            seen_id[i.id] = where
        try:
            check_rank(i.rank or "")
        except ValueError as e:
            problems.append(f"{where}: {e}")
        # Deliberately no duplicate-rank check to pair with the duplicate-id one
        # above: two items sharing a key is the resolution, not the defect. See
        # Item.sort_key.
        if i.status not in STATUSES:
            problems.append(f"{where}: status {i.status!r} not one of {STATUSES}")
        if not i.title:
            problems.append(f"{where}: no title (body has no '# ' heading)")
        elif len(i.title) > TITLE_MAX:
            problems.append(
                f"{where}: title is {len(i.title)} characters (max {TITLE_MAX}); "
                f"move the detail into the body, which has no cap")
        prose = CODE_SPAN_RE.sub("", i.notes or "")
        # A dangling link in the opener is a different defect from one further
        # down: the blocker shipped and the frontmatter still says the row
        # waits. Re-pointing the href, which is what the generic message asks
        # for, keeps the dependency instead of clearing it.
        opener = prose.lstrip().split("\n", 1)[0]
        waits_on = (set(ITEM_LINK_RE.findall(opener))
                    if i.status == "blocked" and BLOCKER_RE.match(prose)
                    else set())
        for ref in ITEM_LINK_RE.findall(prose):
            if ref not in ids and ref != i.id:
                if ref in waits_on:
                    note("dangling-link",
                         f"{where} waits on {ref}.md, which is not in the "
                         f"store. If {ref} shipped and nothing else blocks this "
                         f"row, set `status: ready` and drop the blocker line — "
                         f"re-pointing the link keeps a dependency that is gone")
                else:
                    note("dangling-link",
                         f"{where} links {ref}.md, which is not in the store "
                         f"(shipped, or a typo); the href dangles")
        # An item can legitimately be blocked on something that is not another
        # item — a release landing, an upstream fix, a SHA that does not exist
        # yet — so the script asks only that the note open by saying what it
        # waits on, and leaves whether the condition is real to a reader.
        # Both branches, because the checker cannot tell them apart — the
        # evidence would have been the line that is missing — and they want
        # opposite repairs. A completion that took the opener and left the
        # status is the commoner one, and writing the line back re-adds a
        # dependency that has shipped.
        if i.status == "blocked" and not BLOCKER_RE.match(prose):
            note("blocked-opener",
                 f"{where} is blocked but does not open with what it waits on. "
                 f"If it still waits on something, start the note `Blocked by "
                 f"…` or `Blocked on …`. If the blocker has shipped, set "
                 f"`status: ready` instead — restoring the line would re-add a "
                 f"dependency that no longer exists")
        if i.target:
            resolved = (i.path.parent / i.target.split("#")[0]).resolve()
            if not resolved.exists():
                problems.append(f"{where}: target does not resolve: {i.target}")
        # A parked item is a standing query against the world that nothing
        # re-runs, so one with no stated trigger can never come back by a check.
        # A note rather than an error: whether the prose names a real condition
        # is a reader's call, and the table linter does not fail on it either.
        if i.status == "deferred" and not re.search(
                r"\*\*(Demand|Event|Decision)", i.notes or ""):
            note("deferred-trigger",
                 f"{where} is deferred but names no trigger; say what would "
                 f"revive it")
        # A `file.ext:N` pointer rots silently as the code moves, and which of
        # the four things below went wrong decides what a reader has to do about
        # it, so each says so. All four warn rather than fail: a bare filename is
        # genuinely ambiguous about which directory it was written against, and
        # a fragment is the row author's judgement about what was distinctive.
        notes = i.notes or ""
        for m in CITATION_RE.finditer(notes):
            # Marked as an exhibit, so every check below would report a defect
            # the row is deliberately showing — and hand over the repair.
            if notes[:m.start()].endswith(EXHIBIT_PREFIX):
                continue
            path, line, fragment = m.group(1), int(m.group(2)), m.group(3)
            target = next((base / path for base in (store.parent, store.parent.parent)
                           if (base / path).exists()), None)
            if target is None:
                note("stale-citation",
                     f"{where} cites {path}:{line}, which does not resolve from "
                     f"{store.parent}; re-point or drop it")
                continue
            # splitlines rather than split("\n"): the trailing newline every
            # source file ends with would otherwise add a phantom last line, and
            # a citation landing on it would read as resolving.
            body = target.read_text(encoding="utf-8", errors="replace").splitlines()
            if line > len(body):
                note("stale-citation",
                     f"{where} cites {path}:{line}, which is past the end of a "
                     f"{len(body)}-line file; re-point or drop it")
                continue
            if fragment is None:
                continue
            lo = max(0, line - 1 - window)
            if any(fragment in text for text in body[lo:line + window]):
                continue
            # Where the text still exists the note carries the new number, so a
            # drifted citation is a copy rather than a re-derivation. Reporting
            # the first match only: a fragment matching several lines is one
            # that was never distinctive, and naming them all buries that.
            at = next((n for n, text in enumerate(body, 1) if fragment in text), None)
            if at:
                note("stale-citation",
                     f"{where} cites {path}:{line}:{fragment}, which is now at "
                     f"line {at}; re-point it")
            else:
                note("stale-citation",
                     f"{where} cites {path}:{line}:{fragment}, which {path} no "
                     f"longer carries; re-derive it or drop the fragment")
    # An empty store is legal — every item may have shipped — so this is a note
    # rather than a failure. It is worth saying because the usual cause is a
    # --store pointed somewhere with no items in it, a table directory being the
    # likely one, and that reads as a clean pass on a store never loaded.
    if not items:
        note("empty-store",
             f"no Q*.md under {store}; either the backlog is empty or --store "
             f"is pointed at the wrong directory")
    for p in problems:
        print(f"queue: {p}", file=sys.stderr)
    if problems:
        return 1
    print(f"queue: {len(items)} item(s) OK")
    return 0


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, check=True).stdout


def cmd_metrics(args):
    # Both sides get resolved before the relative_to: git reports the real
    # path, and on macOS the usual temp roots (/tmp, /var/folders) are symlinks,
    # so an unresolved store path is not relative to the root git names.
    store = Path(args.store or store_dir()).resolve()
    root = Path(_git(["rev-parse", "--show-toplevel"], store).strip()).resolve()
    rel = store.relative_to(root)
    log = _git(["log", "--diff-filter=AD", "--name-status", "--date=short",
                "--pretty=format:C\t%H\t%ad\t%s", "--", str(rel)], root)
    filed, closed, reason = {}, {}, {}
    date, subject = None, ""
    for line in log.split("\n"):
        if line.startswith("C\t"):
            _, _, date, subject = line.split("\t", 3)
            continue
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        item = Path(path).stem
        if not ID_RE.match(item):
            continue
        if status.startswith("A"):
            filed[item] = date               # log is newest-first; last wins
        elif status.startswith("D"):
            if item not in closed:
                closed[item] = date
                verb = re.search(r"\b(complete|prune|merge|defer)\w*\b",
                                 subject, re.I)
                reason[item] = verb.group(1).lower() if verb else "removed"
    if args.events:
        print("id\tfiled\tclosed\tdays\treason")
        for item in sorted(filed, key=lambda q: int(q[1:])):
            c = closed.get(item, "")
            days = _days(filed[item], c) if c else ""
            print(f"{item}\t{filed[item]}\t{c}\t{days}\t{reason.get(item, 'open')}")
        return 0
    done = [q for q in closed if reason.get(q) == "complete"]
    pruned = [q for q in closed if reason.get(q) in ("prune", "merge")]
    spans = sorted(_days(filed[q], closed[q]) for q in closed if q in filed)
    open_now = [q for q in filed if q not in closed]
    print(f"filed        {len(filed)}")
    print(f"completed    {len(done)}")
    print(f"pruned       {len(pruned)}")
    print(f"open         {len(open_now)}")
    if spans:
        print(f"cycle time   median {spans[len(spans) // 2]}d  "
              f"mean {sum(spans) // len(spans)}d")
    if closed:
        print(f"prune ratio  {100 * len(pruned) // len(closed)}%")
    return 0


def _days(a, b):
    from datetime import date
    ya, ma, da = (int(x) for x in a.split("-"))
    yb, mb, db = (int(x) for x in b.split("-"))
    return (date(yb, mb, db) - date(ya, ma, da)).days


# --- claims ---------------------------------------------------------------
#
# `alloc-queue-id.sh` reserves an ID by creating `refs/queue-ids/QN` on the
# remote, which binds only the sessions that call it. This is the other half:
# every ID a branch *adds* must hold a claim, so a number someone read off the
# store and incremented fails at the gate that files the row rather than at the
# rebase that collides with it.
#
# Three properties it is built around.
#
#   1. NEW IS MEASURED AGAINST THE MERGE BASE, NEVER origin/main's TIP. A row
#      `main` deleted while this branch was behind is absent from the tip and
#      present at the base, so against the tip it reads as one this branch
#      filed — and the rule would then demand a claim for finished work.
#   2. AN UNREADABLE REMOTE SKIPS RATHER THAN FAILS, so an offline clone still
#      runs the gate. `--strict` turns every skip into a failure, and CI — which
#      always has a network — is where it is passed. Without that, the one place
#      the check is guaranteed to run is also a place it can silently not run.
#   3. IT IS ITS OWN SUBCOMMAND. `lint` is a pure function of a directory: no
#      git, no network, correct against any store, which is what makes it safe
#      in an edit loop and in tests over temp dirs. This check is a function of
#      the *branch* instead, so folding it in would put a merge base and a
#      network round trip behind every one of those calls.

REF_NS = "refs/queue-ids"

# git gets a deadline because the failure here is a hang rather than an error:
# an ssh remote that is not there sits in connect() for minutes, and an https
# one stops to ask for credentials — which GIT_TERMINAL_PROMPT=0 refuses.
REMOTE_TIMEOUT = 20


def _git_read(args, cwd, timeout=None):
    """(stdout, ok) — never raises, because every read here is one to skip on."""
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return "", False
    return p.stdout, p.returncode == 0


def _ids_at(rev, rel, root):
    out, ok = _git_read(["ls-tree", "-r", "--name-only", rev, "--", rel], root)
    if not ok:
        return None
    return {p for p in (Path(x).stem for x in out.split("\n")) if ID_RE.match(p)}


def cmd_claims(args):
    def skip(why):
        print(f"queue: claims: {why}", file=sys.stderr)
        if args.strict:
            print("queue: claims: --strict was passed, so that is a failure",
                  file=sys.stderr)
            return 1
        return 0

    store = Path(args.store or store_dir()).resolve()
    out, ok = _git_read(["rev-parse", "--show-toplevel"], store)
    if not ok:
        return skip(f"{store} is not in a git repository, so there is no branch "
                    f"to measure against")
    root = Path(out.strip()).resolve()
    try:
        rel = str(store.relative_to(root))
    except ValueError:
        return skip(f"{store} is outside {root}; point --store inside the repo")

    base, ok = _git_read(["merge-base", args.base, "HEAD"], root)
    if not ok:
        return skip(f"no merge base between {args.base} and HEAD — fetch it, or "
                    f"deepen a shallow clone, or pass --base")
    base = base.strip()
    before = _ids_at(base, rel, root)
    if before is None:
        return skip(f"cannot read {rel} at {base[:12]}")
    # The working tree rather than HEAD: the gate runs over a row that has been
    # written and not yet committed, which is when a hand-picked ID is cheapest
    # to fix — and it is the same reason the Makefile's file lists carry
    # --others.
    now = {p.stem for p in store.glob("Q*.md") if ID_RE.match(p.stem)}
    added = sorted(now - before, key=lambda q: int(q[1:]))
    if not added:
        print(f"queue: claims: no ids added since {base[:12]}")
        return 0

    out, ok = _git_read(["ls-remote", args.remote, f"{REF_NS}/*"], root,
                        timeout=REMOTE_TIMEOUT)
    if not ok:
        return skip(f"{args.remote} did not answer, so its claims are unknown "
                    f"and {len(added)} added id(s) went unchecked")
    # ls-remote exits non-zero when it cannot reach the remote, so exit 0 means
    # the remote answered and an empty list is a real "nothing is claimed"
    # rather than a read that never happened.
    claimed = set(re.findall(r"(Q\d+)$", out, re.M))
    raw = args.allow or [os.environ.get("QUEUE_CLAIMS_ALLOW", "")]
    allowed = set(",".join(raw).replace(",", " ").split())
    missing = [q for q in added if q not in claimed and q not in allowed]
    for q in missing:
        print(f"queue: {q}.md files an id holding no {REF_NS}/{q} on "
              f"{args.remote}: allocate one with alloc-queue-id.sh and rename "
              f"the file, or pass --allow {q} if it was claimed elsewhere",
              file=sys.stderr)
    if missing:
        return 1
    print(f"queue: claims: {len(added)} added id(s) hold a claim on {args.remote}")
    return 0


# Split a table row on unescaped pipes only. An escaped `\|` inside a cell is
# content, and splitting on it shifts every later column.
def _row_cells(line):
    out, cur, i = [], "", 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] in "|\\":
            cur += line[i + 1]
            i += 2
            continue
        if ch == "|":
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    out.append(cur.strip())
    return out


def rebase_link(target):
    """Rewrite a link written relative to `docs/` for a file in `docs/queue/`.

    An item page sits one directory below the table that held it, so every
    relative destination gains a `../`. A bare `#QNNN` anchor pointed at a row
    in the same table; the row is now a sibling page, so it becomes `QNNN.md` —
    which, unlike the anchor, also resolves on github.com.
    """
    if not target:
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*://", target) or target.startswith("/"):
        return target
    if target.startswith("#"):
        ref = target[1:]
        return f"{ref}.md" if ID_RE.match(ref) else target
    return "../" + target


def rebase_body_links(text):
    """Rewrite every markdown link in a Notes cell for its new depth.

    The `target:` field is not the only link a row carries: Notes routinely
    cite sibling rows as `#QNNN`, and those anchors exist only in the table.
    Left alone they resolve to nothing the moment the table is deleted, and
    nothing about the resulting page looks broken until someone clicks.
    """
    return re.sub(r"\]\(([^)]*)\)",
                  lambda m: f"]({rebase_link(m.group(1)) or m.group(1)})",
                  text)


def cmd_migrate(args):
    src = Path(args.source)
    if not src.is_file():
        # A mistyped path is an argument error, answered the same way the
        # old-format refusal below is rather than as a traceback out of
        # read_text. A directory raises from that same call, so it is refused
        # here too and told apart: "no such file" about one would be false.
        print(f"queue: {args.source}: "
              f"{'no such file' if not src.exists() else 'not a file'}",
              file=sys.stderr)
        return 1
    store = Path(args.store or store_dir())
    store.mkdir(parents=True, exist_ok=True)
    legacy = []
    section, rows = None, []
    for line in src.read_text(encoding="utf-8").split("\n"):
        if line.startswith("## "):
            head = line[3:].strip().lower()
            section = head if head in ("queue", "deferred") else None
            continue
        if not section or not line.startswith("|"):
            continue
        cells = _row_cells(line)
        if len(cells) < 4:
            continue
        raw_id = re.sub(r"<[^>]*>", "", cells[1]).strip()
        if not ID_RE.match(raw_id):
            continue
        # The status test below asks only whether the cell holds 🚫, so the
        # pre-counter format's ✅/▶/💤 all fall through to `ready` and put
        # shipped work back in the backlog. Unambiguous, too: the Deferred
        # table has no status column for one of these to appear in.
        if section == "queue" and len(cells) > 4:
            legacy += [(raw_id, m) for m in ("✅", "▶", "💤") if m in cells[4]]
        rows.append((section, cells, raw_id))
    if legacy:
        for item_id, mark in legacy:
            print(f"queue: {src.name}: {item_id} is {mark}, an old-format state "
                  f"with no mapping here; it would land as status: ready",
                  file=sys.stderr)
        print("queue: normalize the table to 🔲/🚫 with a ## Deferred section "
              "first, then migrate", file=sys.stderr)
        return 1
    ranks = rank_series(len(rows))
    written = 0
    for rank, (section, cells, item_id) in zip(ranks, rows):
        item_cell = cells[2]
        link = re.search(r"\[([^\]]*)\]\(([^)]*)\)", item_cell)
        target = rebase_link(link.group(2)) if link else None
        # The link is usually only *part* of the cell — "Audit [x](y) on the
        # eleven dimensions" — so the title is the whole cell with link markup
        # flattened to its text. Taking the link text alone silently drops
        # everything around it, and a truncated title still looks like a title.
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", item_cell)
        labels = re.findall(r"`([^`]+)`", cells[3]) if len(cells) > 3 else []
        if section == "queue":
            st = cells[4] if len(cells) > 4 else ""
            status = "blocked" if "🚫" in st else "ready"
            size = cells[5] if len(cells) > 5 else ""
            notes = cells[6] if len(cells) > 6 else ""
        else:
            status = "deferred"
            size = cells[4] if len(cells) > 4 else ""
            notes = cells[5] if len(cells) > 5 else ""
        item = Item(id=item_id, rank=rank, labels=labels, status=status,
                    size=size or None, target=target, title=title.strip(),
                    notes=rebase_body_links(notes.strip()))
        write_item(store, item)
        written += 1
    print(f"queue: wrote {written} item(s) to {store}")
    return 0


def cmd_rank(args):
    items, _ = load(args.store or store_dir())
    ranks = [i.rank for i in items if i.rank]
    if args.head:
        print(rank_between(None, ranks[0] if ranks else None))
    elif args.tail:
        print(rank_between(ranks[-1] if ranks else None, None))
    else:
        print(rank_between(args.after, args.before))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--store", help="item directory (default: <repo>/docs/queue)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="print the ordered backlog")
    r.add_argument("--format", choices=("text", "table"), default="text")
    r.add_argument("--all", action="store_true", help="include deferred items")
    r.add_argument("--label", metavar="LABEL",
                   help="only items carrying LABEL, e.g. " + OPEN_QUESTION)
    r.set_defaults(fn=cmd_render)

    n = sub.add_parser("next", help="print the top ready item")
    n.add_argument("--title", action="store_true")
    n.set_defaults(fn=cmd_next)

    li = sub.add_parser(
        "lint",
        help="check the store",
        description="Fails on what the files can settle by themselves and "
                    "notes what a reader has to. --strict promotes one named "
                    "note class to an error; it takes a class rather than "
                    "being a switch because the classes have different callers "
                    "and disagree about which should bind.")
    li.add_argument("--strict", action="append", metavar="CLASS",
                    choices=NOTE_CLASSES,
                    help="fail rather than note on CLASS; repeatable. One of: "
                         + ", ".join(NOTE_CLASSES))
    li.add_argument("--citation-window", type=int, default=CITATION_WINDOW,
                    metavar="N",
                    help="how far a fragment may sit from its cited line "
                         "before stale-citation notes it (default: "
                         f"{CITATION_WINDOW}). 0 requires the fragment on the "
                         "line itself. --strict promotes a note's severity and "
                         "never this window, so a gate wanting an exact check "
                         "asks for both.")
    li.set_defaults(fn=cmd_lint)

    c = sub.add_parser(
        "claims",
        help="check every id this branch adds holds a claim on the remote",
        description="Every id this branch adds against its merge base with "
                    "--base must hold a refs/queue-ids/QN ref on --remote, "
                    "which is what alloc-queue-id.sh creates. A read that "
                    "cannot be taken skips, so an offline clone still runs the "
                    "gate; pass --strict where a network is guaranteed.")
    c.add_argument("--remote", default="origin", help="holds the claims")
    c.add_argument("--base", default="origin/main",
                   help="branch this one is measured against")
    c.add_argument("--allow", action="append", metavar="QNNN",
                   help="an id claimed outside this remote; repeatable, and "
                        "QUEUE_CLAIMS_ALLOW is a comma-separated default")
    c.add_argument("--strict", action="store_true",
                   help="fail rather than skip when a read cannot be taken")
    c.set_defaults(fn=cmd_claims)

    m = sub.add_parser("metrics", help="flow metrics from git history")
    m.add_argument("--events", action="store_true")
    m.set_defaults(fn=cmd_metrics)

    g = sub.add_parser("migrate", help="convert a legacy STATUS.md table")
    g.add_argument("source", help="path to the old STATUS.md")
    g.set_defaults(fn=cmd_migrate)

    k = sub.add_parser(
        "rank",
        help="compute an order key",
        description="Generates a magnitude-head base-36 order key — a string "
                    "that sorts into the position you asked for. Give it the "
                    "neighbours to land between, or --head or --tail for the "
                    "ends of the store.")
    k.add_argument("--after", help="rank of the item this goes below")
    k.add_argument("--before", help="rank of the item this goes above")
    k.add_argument("--head", action="store_true", help="before every item")
    k.add_argument("--tail", action="store_true", help="after every item")
    k.set_defaults(fn=cmd_rank)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except ValueError as e:
        # The rank algebra refuses a malformed key by raising, and `rank` is the
        # subcommand run by hand: a typo'd neighbour is an argument error, not a
        # crash. ValueError alone, never OSError: a closed pipe would come
        # through a wider clause and be reported as an argument error, which is
        # what the sibling handler below exists to keep separate.
        print(f"queue: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # `render | head` closes the pipe once head has its line, which is the
        # consumer saying it has enough rather than anything going wrong — so
        # this exits 0 and says nothing. The dup2 is what makes that true, not
        # the return: the interpreter flushes stdout on the way out, and without
        # a null fd 1 that flush raises again with no handler left, printing
        # "Exception ignored while flushing sys.stdout" and exiting 120 over the
        # top of the 0 below.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
