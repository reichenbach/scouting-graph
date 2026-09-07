PY := .venv/bin/python

.PHONY: help venv data test run-sample ask audit clean

help:
	@echo "make venv        create .venv and install pinned deps"
	@echo "make data        regenerate the seeded synthetic sample data"
	@echo "make test        run the offline test suite"
	@echo "make run-sample  run the report pipeline on inbox/sample.csv with the stub model"
	@echo "make ask         ask the staff assistant a sample question with the stub model"
	@echo "make audit       print the audit table"

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

audit:
	$(PY) -m yds_graph audit

clean:
	rm -rf outbox errors state audit.sqlite .pytest_cache
