# Contributing to Pulley

Thanks for your interest in improving Pulley. This guide covers the local
workflow.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — dependency management and task runner
- [just](https://github.com/casey/just) — command runner (recipes live in the `justfile`)
- Docker (with Docker Compose) — runs the local Postgres

## Setup

```bash
uv sync
```

This installs the runtime and dev dependency groups into a local `.venv`.

## Dev loop

```bash
just dev     # start Postgres (docker compose) + uvicorn with --reload
just test    # run the pytest suite
just lint    # ruff check + ruff format --check
just fmt     # ruff format + ruff check --fix
```

`just dev` brings up the compose `db` service and runs the app against it. For
webhook testing, tunnel with `ngrok http 8000` and point your GitHub App's
webhook URL at the ngrok URL while developing.

## Database migrations

Migrations are managed with Alembic.

```bash
just migrate-gen "describe the change"   # autogenerate a revision from model changes
just migrate-up                          # apply migrations (alembic upgrade head)
```

Review the generated migration before committing it — autogenerate is a starting
point, not the final word. Commit the migration file alongside the model change.

## Pull requests

Before opening a PR:

- Code is ruff-clean (`just lint` passes — both `ruff check` and format check).
- Tests pass (`just test`).
- Any schema change ships with its migration.

CI runs on every PR and must be green: it runs **lint**, **test** (against a
Postgres service), and **build** (Docker image). PRs are merged into `main`.
