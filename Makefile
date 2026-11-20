PY ?= python3

.PHONY: install run serve test test-js lint format selfcheck samples screenshot clean

install:
	$(PY) -m pip install -r requirements.txt

run:
	$(PY) run.py

serve:
	$(PY) run.py --serve

test:
	$(PY) -m pytest

test-js:
	node --test tests/js/*.mjs

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

selfcheck:
	$(PY) scripts/make_dirty_dataset.py

samples:
	$(PY) scripts/make_field_samples.py

clean:
	rm -rf selfcheck_out out .pytest_cache .ruff_cache
