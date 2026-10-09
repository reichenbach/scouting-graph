PY := .venv/bin/python

.PHONY: help venv data test run-sample ask lookup audit review-demo hitl-demo desktop clean

help:
	@echo "make venv        create .venv and install pinned deps"
	@echo "make data        regenerate the seeded synthetic sample data"
	@echo "make test        run the offline test suite"
	@echo "make run-sample  run the report pipeline on inbox/sample.csv with the stub model"
	@echo "make ask         ask the staff assistant a sample question with the stub model"
	@echo "make lookup      ask the staff library a methodology question with the stub model"
	@echo "make audit       print the audit table"
	@echo "make review-demo interrupt, approve, and audit (stub; see docs/REVIEW_WALKTHROUGH.md)"
	@echo "make desktop     open YDS Review (per-user folder, stub unless a key is saved)"

venv:
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt

data:
	$(PY) scripts/make_sample_data.py

test:
	$(PY) -m pytest

run-sample:
	YDS_GRAPH_STUB=1 $(PY) -m yds_graph run inbox/sample.csv --coach sample

ask:
	YDS_GRAPH_STUB=1 $(PY) -m yds_graph ask "What do we have on the Friday matchup?"

lookup:
	YDS_GRAPH_STUB=1 $(PY) -m yds_graph lookup "How is chase rate defined in a report?"

audit:
	$(PY) -m yds_graph audit

desktop:
	$(PY) -m yds_graph.desktop

review-demo:
	@set -e; \
	out=$$(YDS_GRAPH_STUB=1 $(PY) -m yds_graph run inbox/sample.csv --coach sample); \
	echo "$$out"; \
	tid=$$(printf '%s\n' "$$out" | sed -n 's/^thread_id: //p'); \
	test -n "$$tid"; \
	YDS_GRAPH_STUB=1 $(PY) -m yds_graph resume "$$tid" --approve; \
	$(PY) -m yds_graph audit --limit 5

hitl-demo:
	@echo "hitl-demo is deprecated. Use make review-demo."
	@$(MAKE) --no-print-directory review-demo

clean:
	rm -rf outbox errors state audit.sqlite .pytest_cache
