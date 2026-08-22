# Every target runs from the repository root.
PYTHON ?= python3
PLUGINS := workspace-guard branch-guard prod-guard exit-status-guard foreground-guard

.PHONY: check sync sync-check lib-test plugin-tests validate help

help:
	@echo "make check        run everything CI runs"
	@echo "make sync         copy lib/bouncer_parse.py into each plugin"
	@echo "make sync-check   fail if a vendored copy has drifted"
	@echo "make lib-test     test the shared parser"
	@echo "make plugin-tests test every plugin"
	@echo "make validate     validate the marketplace manifest"

check: sync-check lib-test plugin-tests

sync:
	$(PYTHON) scripts/sync-lib.py

# The vendored copies are what ship, so a drifted one is a shipped bug. This
# runs before the tests: a plugin suite passing against a stale copy is the
# failure mode the gate exists to catch.
sync-check:
	$(PYTHON) scripts/sync-lib.py --check

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
