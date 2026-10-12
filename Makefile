.PHONY: install test lint fmt eval serve check

install:
	cd backend && uv venv --python 3.11 .venv && uv pip install -e '.[dev]'

test:
	cd backend && .venv/bin/python -m pytest -q

lint:
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .

fmt:
	cd backend && .venv/bin/ruff format . && .venv/bin/ruff check --fix .

check: lint test

eval:
	cd backend && .venv/bin/python -m app.evaluation.run

serve:
	cd backend && .venv/bin/uvicorn app.main:app --reload
