.PHONY: install test lint fmt check demo preflight eval serve

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

preflight:
	cd backend && .venv/bin/python scripts/preflight.py

eval:
	cd backend && .venv/bin/python -m app.evaluation.run

serve:
	cd backend && .venv/bin/uvicorn app.main:app --reload
