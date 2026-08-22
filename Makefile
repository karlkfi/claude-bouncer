# Every target runs from the repository root.
PYTHON ?= python3
PLUGINS := workspace-guard branch-guard prod-guard exit-status-guard foreground-guard

.PHONY: check sync sync-check version-check lib-test plugin-tests validate images help \
        backlog backlog-next backlog-lint

help:
	@echo "make check         run everything CI runs"
	@echo "make sync          copy lib/bouncer_parse.py into each plugin"
	@echo "make sync-check    fail if a vendored copy has drifted"
	@echo "make version-check fail if a plugin's three version strings disagree"
	@echo "make lib-test      test the shared parser"
	@echo "make plugin-tests  test every plugin"
	@echo "make validate      validate the marketplace manifest"
	@echo "make images        rasterize the brand images from their SVG masters"
	@echo "make backlog       the queue in priority order (ARGS='--label prod-guard')"
	@echo "make backlog-next  the top ready item, as a session prompt"
	@echo "make backlog-lint  check docs/queue"

check: sync-check version-check backlog-lint lib-test plugin-tests

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
