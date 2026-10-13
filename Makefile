.PHONY: install test lint fmt check demo preflight eval eval-one serve record-replays \
	web-install web web-test web-build web-fmt web-lint dev

install:
	cd backend && uv venv --python 3.11 .venv && uv pip install -e '.[dev]'

test:
	cd backend && .venv/bin/python -m pytest -q

lint:
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .

fmt:
	cd backend && .venv/bin/ruff format . && .venv/bin/ruff check --fix .

check: lint test web-test

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

# Spends money: one real run per demo scenario, roughly 5,800 tokens each.
# Run it at the close of every phase that changes agent behavior — a stale
# replay keeps working while demonstrating the old agent.
#   make record-replays ARGS="--dry-run"
#   make record-replays ARGS="--only S01"
record-replays:
	cd backend && .venv/bin/python scripts/record_replays.py $(ARGS)

# --- frontend ---------------------------------------------------------------

web-install:
	cd frontend && npm install

web:
	cd frontend && npm run dev

# Skipped rather than failed when dependencies are absent, so `make check` on a
# fresh clone still runs the backend suite instead of stopping at a missing npm.
web-test:
	@if [ -d frontend/node_modules ]; then \
		cd frontend && npm test --silent; \
	else \
		echo "  (frontend deps missing; run make web-install)"; \
	fi

web-build:
	cd frontend && npm run build

web-fmt:
	cd frontend && npm run format

web-lint:
	cd frontend && npm run format:check && npm run typecheck

# The demo needs both halves: the API on 8000 and the Vite dev server on 5173,
# which proxies /demo to it so there is no CORS configuration in development.
dev:
	@echo "API      -> http://127.0.0.1:8000"
	@echo "Demo page -> http://localhost:5173"
	@cd backend && .venv/bin/uvicorn app.main:app --reload & \
	cd frontend && npm run dev
