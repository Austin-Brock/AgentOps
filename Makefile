.PHONY: install test lint fmt check demo preflight eval eval-one serve

install:
	cd backend && uv venv --python 3.11 .venv && uv pip install -e '.[dev]'

test:
	cd backend && .venv/bin/python -m pytest -q

lint:
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .

fmt:
	cd backend && .venv/bin/ruff format . && .venv/bin/ruff check --fix .

check: lint test

demo:
	cd backend && .venv/bin/python scripts/demo.py

eval-one:
	cd backend && .venv/bin/python -m app.evaluation.run --only S01 --concurrency 1

preflight:
	cd backend && .venv/bin/python scripts/preflight.py

# ARGS is forwarded, so `make eval ARGS="--only S01"` runs one scenario.
# Without this the flags were silently dropped and the full suite ran.
eval:
	cd backend && .venv/bin/python -m app.evaluation.run $(ARGS)

serve:
	cd backend && .venv/bin/uvicorn app.main:app --reload
