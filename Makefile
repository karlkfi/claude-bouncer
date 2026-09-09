# Every target runs from the repository root.
PYTHON ?= python3
PLUGINS := workspace-guard branch-guard prod-guard exit-status-guard foreground-guard

# An unreadable remote skips rather than fails, so an offline clone still runs
# the gate; CI always has a network, so there a skip is the failure. Actions
# sets CI itself, which is why nothing has to pass this by hand.
CLAIMS_FLAGS := $(if $(CI),--strict)

.PHONY: check sync sync-check version-check path-filter-check action-pin-check \
        install-ref-check lib-test plugin-tests \
        validate images help backlog backlog-next backlog-lint backlog-claims \
        backlog-claim

help:
	@echo "make check             run everything CI runs"
	@echo "make sync              copy lib/bouncer_parse.py into each plugin"
	@echo "make sync-check        fail if a vendored copy has drifted"
	@echo "make version-check     fail if a plugin's three version strings disagree"
	@echo "make path-filter-check fail if a plugin's CI jobs are unfiltered or misfiltered"
	@echo "make action-pin-check  fail if a workflow action is not pinned to a SHA"
	@echo "make install-ref-check fail if a README names a retired repo or marketplace"
	@echo "make lib-test          test the shared parser"
	@echo "make plugin-tests      test every plugin"
	@echo "make validate          validate the marketplace manifest"
	@echo "make images            rasterize the brand images from their SVG masters"
	@echo "make backlog           the queue in priority order (ARGS='--label prod-guard')"
	@echo "make backlog-next      the top ready item, as a session prompt"
	@echo "make backlog-lint      check docs/queue"
	@echo "make backlog-claim     claim a Q-ID (ARGS='The row title')"
	@echo "make backlog-claims    fail if an id this branch adds holds no claim"

check: sync-check version-check path-filter-check action-pin-check \
       install-ref-check backlog-lint backlog-claims lib-test plugin-tests

sync:
	$(PYTHON) scripts/sync-lib.py

# The vendored copies are what ship, so a drifted one is a shipped bug. This
# runs before the tests: a plugin suite passing against a stale copy is the
# failure mode the gate exists to catch.
sync-check:
	$(PYTHON) scripts/sync-lib.py --check

# The marketplace entry is what `claude plugin update` compares, so a bump
# that misses it ships nothing while the README announces the new version.
version-check:
	$(PYTHON) scripts/version-check.py

# A filter that omits a plugin makes its jobs go green by SKIPPING, which reads
# exactly like passing in a checks list. Runs beside the other two gates rather
# than inside the suite, so a broken workflow fails before the tests it gates.
path-filter-check:
	$(PYTHON) scripts/path-filter-check.py

# A tag is a pointer its owner can move, so an unpinned action runs whatever it
# points at on the day. Pinning alone only freezes that, which is why
# .github/dependabot.yml is the other half: it turns a pin into a reviewed bump.
action-pin-check:
	$(PYTHON) scripts/action-pin-check.py

# The retired repositories are still served, so a stale install instruction
# resolves and renders correctly while sending the reader to a marketplace
# that will never publish again. Reachability cannot see it; agreement with
# the manifest can.
install-ref-check:
	$(PYTHON) scripts/install-ref-check.py

lib-test:
	$(PYTHON) -m unittest discover tests

# branch-guard drives its hook through a shell harness rather than unittest.
plugin-tests:
	@set -e; for p in $(PLUGINS); do \
	  echo "--- $$p"; \
	  if [ -f plugins/$$p/test/run.sh ]; then \
	    ( cd plugins/$$p && bash test/run.sh ); \
	  else \
	    ( cd plugins/$$p && $(PYTHON) -m unittest discover tests ); \
	  fi; \
	done

validate:
	claude plugin validate .

# Deliberately outside `check`: it needs resvg, which CI does not install,
# and a raster only goes stale when its SVG master changes.
images:
	$(PYTHON) scripts/render-images.py

backlog:
	@$(PYTHON) scripts/queue.py render --all $(ARGS)

backlog-next:
	@$(PYTHON) scripts/queue.py next

# `dangling-link` stays advisory: a link across a live batch is legitimately in
# flight, and failing on it would redden the store after every merge.
backlog-lint:
	$(PYTHON) scripts/queue.py lint \
	  --strict blocked-opener --strict deferred-trigger --strict empty-store

# Reserving an ID binds only the sessions that ask, so a hand-picked number
# survives until the rebase it collides with -- which Q146 paid three times
# over. Keyed on ids added against the merge base rather than origin/main's
# tip, so the rows predating the allocator are never asked for a claim and
# this needs no backfill. An id claimed elsewhere takes `--allow QNNN`.
#
# Separate from backlog-lint because `lint` is a pure function of a directory,
# which is what keeps it usable in an edit loop. This one reads the remote.
backlog-claims:
	$(PYTHON) scripts/queue.py claims $(CLAIMS_FLAGS)

# ARGS is quoted here and nowhere else in this file: a title is free text where
# the others take flags, and unquoted the shell splits it, so the script reads
# one title per WORD and claims an id for each. A nine-word title took Q182
# through Q192 that way. A claim cannot be released.
backlog-claim:
	@[ -n "$(ARGS)" ] || { \
	  echo "usage: make backlog-claim ARGS='The row title'" >&2; exit 2; }
	@bash scripts/alloc-queue-id.sh "$(ARGS)"
