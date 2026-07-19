set dotenv-load

# ── Local development ────────────────────────────────────

dev:
    docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db
    uv run uvicorn src.main:app --reload --port 8000

migrate *args:
    uv run alembic {{args}}

migrate-up:
    uv run alembic upgrade head

migrate-gen message:
    uv run alembic revision --autogenerate -m "{{message}}"

lint:
    uv run ruff check src/ tests/
    uv run ruff format --check src/ tests/

test *args:
    uv run pytest {{args}}

fmt:
    uv run ruff format src/ tests/
    uv run ruff check --fix src/ tests/

# ── Release ──────────────────────────────────────────────

release version message:
    git tag -a "v{{version}}" -m "{{message}}"
    git push origin "v{{version}}"
