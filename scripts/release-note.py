#!/usr/bin/env python3
"""Read the `## Release note` block out of a pull request body.

Reads the body on stdin, prints the note on stdout, exits 0 when the author
answered and 1 when they did not. Used two ways: the release-note CI check
runs it per pull request, and the release runbook runs it over every pull
request in a tag range (see docs/development/release-process.md).

Both callers need the same four-way answer, which is why the extraction lives
here rather than in a shell pipeline on either side:

  <a note>        the hook behaves differently for someone running it
  None            no decision moved; fold into the changelog link
  !! UNANSWERED   section present and empty -- nobody answered it
  !! NO SECTION   no section at all; predates the template, or was dropped
"""
import re
import sys

HEADING_RE = re.compile(r'^##\s+Release note\s*$', re.M)
NEXT_HEADING_RE = re.compile(r'^##\s', re.M)
# Non-greedy across newlines: the template's instructions are a multi-line
# comment, and GitHub keeps it in the raw body even once the author answers.
COMMENT_RE = re.compile(r'<!--.*?-->', re.S)

UNANSWERED = '!! UNANSWERED'
NO_SECTION = '!! NO SECTION'


def extract(body):
    """Return the note text, or an `!!` marker when there is no answer."""
    match = HEADING_RE.search(body or '')
    if not match:
        return NO_SECTION

    section = body[match.end():]
    nxt = NEXT_HEADING_RE.search(section)
    if nxt:
        section = section[:nxt.start()]

    note = '\n'.join(
        line.strip() for line in COMMENT_RE.sub('', section).splitlines()
        if line.strip()
    )
    return note or UNANSWERED


def main():
    note = extract(sys.stdin.read())
    print(note)
    return 1 if note in (UNANSWERED, NO_SECTION) else 0


if __name__ == '__main__':
    sys.exit(main())
