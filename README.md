<p align="center">
  <img src="assets/logo.png" width="230" alt="Pulley">
</p>

# Pulley

Self-hostable, bidirectional GitHub ↔ Slack PR integration. Opens a Slack
channel for every pull request, syncs comments and reviews in both directions,
and posts a live digest of each PR to a dedicated channel that updates
in place as the PR moves through its lifecycle.

## What it does

- **Per-PR channel** — auto-creates a Slack channel named `_pr-<repo>-<n>` (leading underscore so PR channels sort to the top), invites the author and requested reviewers (matched by linked GitHub↔Slack identity), posts state transitions, archives on close and unarchives on reopen.
- **Bidirectional sync** — GitHub review comments, review threads, and issue comments land in Slack (threaded); Slack channel messages and thread replies post back to GitHub. Thread replies on one side replay as thread replies on the other. Edits and deletions propagate too: editing or deleting a synced comment/message on either side updates or removes its mirror on the other.
- **Attributed posts** — Slack → GitHub posts use the user's OAuth token (appears as them on GitHub). GitHub → Slack posts use `chat:write.customize` to render with the user's Slack display name and avatar. Users who haven't linked fall back to bot-attributed posts.
- **Org-wide PR digest** — one Slack message per PR in a configurable channel, with a colored sidebar + status emoji. Updated in place on draft ↔ ready, approved, changes requested, merged, closed. Drafts are suppressed until marked ready.
- **Slash commands** — `/pulley open`, `/pulley me`, `/pulley team <name>`, `/pulley merge [squash|rebase|merge]`, `/pulley settings`; `/lgtm [comment]` to approve from Slack.
- **CI / deployment notifications** — workflow run failures and deployment statuses post to the configured CI channel.
- **Daily recap + stale reminders** — weekday PR summaries and stale-PR nudges, driven by the built-in scheduler or by an external cron hitting the internal endpoints (see [Configuration](#configuration)).
- **Repo allowlist / excludes** — `GITHUB_ALLOWED_REPOS` env var (comma-separated `owner/repo`) scopes the bot to a subset of installed repos for soft-launches; empty = all. `GITHUB_EXCLUDED_REPOS` (same format) drops events from specific repos even if they'd otherwise be allowed — exclusion wins over the allowlist.

## Quickstart (self-hosted)

You need a **public HTTPS domain** — GitHub and Slack only deliver webhooks over
TLS — and a host that runs Docker Compose.

```bash
cp .env.example .env
# edit .env: fill in your GitHub App + Slack App credentials (see Setup below)

docker compose up -d
```

Compose starts Postgres, runs database migrations once, and launches the app on
`127.0.0.1:8000`. Put a reverse proxy with TLS in front of it so your domain
terminates HTTPS and forwards to `127.0.0.1:8000`.

See **[docs/self-hosting.md](docs/self-hosting.md)** for the full walkthrough
(reverse proxy config, scheduling, upgrades, backups). You'll create the GitHub
and Slack apps in [Setup](#setup) before webhooks will flow.

## Configuration

All configuration is via environment variables (read from `.env`, or the process
environment).

| Variable | Default | Description |
| --- | --- | --- |
| `GITHUB_APP_ID` | — | GitHub App ID. |
| `GITHUB_APP_PRIVATE_KEY` | — | GitHub App private key, PEM contents inline. |
| `GITHUB_APP_PRIVATE_KEY_PATH` | — | Path to the PEM file, used if `GITHUB_APP_PRIVATE_KEY` is unset. |
| `GITHUB_CLIENT_ID` | — | GitHub App OAuth client ID (per-user "Connect GitHub"). |
| `GITHUB_CLIENT_SECRET` | — | GitHub App OAuth client secret. |
| `GITHUB_WEBHOOK_SECRET` | — | Secret used to verify inbound GitHub webhook signatures. |
| `SLACK_CLIENT_ID` | — | Slack App client ID (workspace install / OAuth). |
| `SLACK_CLIENT_SECRET` | — | Slack App client secret. |
| `SLACK_SIGNING_SECRET` | — | Secret used to verify inbound Slack request signatures. |
| `SLACK_BOT_TOKEN` | — | Fallback bot token only. Per-org bot tokens are stored in the database after OAuth install. |
| `DATABASE_URL` | `postgresql+asyncpg://pulley:pulley@localhost:5432/pulley` | Async SQLAlchemy Postgres URL. |
| `APP_BASE_URL` | `http://localhost:8000` | Public base URL of the deployment; used to build OAuth redirect URLs. Set to your HTTPS domain. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `GITHUB_ALLOWED_REPOS` | _(empty)_ | Comma-separated `owner/repo` allowlist. Empty = act on every installed repo. |
| `GITHUB_EXCLUDED_REPOS` | _(empty)_ | Comma-separated `owner/repo` exclude list. Exclusion wins over the allowlist. |
| `SCHEDULER_ENABLED` | `false` | Run the in-app scheduler for recap + stale reminders. Needs exactly one always-on instance. |
| `RECAP_CRON` | `0 9 * * 1-5` | Fallback recap schedule (5-field cron, UTC). Per-org override lives in `organizations.recap_cron`. |
| `STALE_REMINDER_CRON` | `0 14 * * 1-5` | Stale-reminder schedule for every org (5-field cron, UTC). |
| `INTERNAL_API_TOKEN` | _(empty)_ | Bearer token for `/internal/*`. Empty → those endpoints return `404`; set → they require `Authorization: Bearer <token>`. |

### Scheduling

The daily recap and stale-PR reminders can fire two ways:

- **In-app scheduler** — set `SCHEDULER_ENABLED=true`. Requires exactly one
  always-on instance (this is the Compose default). Simplest for a single VM.
- **External cron** — leave the scheduler off, set `INTERNAL_API_TOKEN`, and
  have an external scheduler `POST` to `/internal/recap` and
  `/internal/stale-reminders` with `Authorization: Bearer <token>`. This is the
  right choice on scale-to-zero platforms like Cloud Run (CPU is throttled
  outside of request handling, so an in-process scheduler won't fire reliably)
  and on multi-instance deployments.

Set **neither** and recap/stale reminders never fire. Firing the same job twice
(multiple instances, cron retries) is harmless — a database at-most-once guard
absorbs duplicates. Cron expressions are 5-field and evaluated in **UTC**.

## Setup

### 1. Create the GitHub App

Settings → Developer settings → GitHub Apps → New GitHub App.

Required permissions (Repository):
- Pull requests: read & write
- Issues: write
- Contents: read
- Metadata: read
- Checks: read
- Deployments: read
- Members: read
- Emails: read

Subscribe to events: pull_request, pull_request_review, pull_request_review_comment, pull_request_review_thread, issue_comment, check_suite, deployment_status, workflow_run.

Webhook URL: `https://<your-domain>/webhooks/github`. Set a webhook secret; save it as `GITHUB_WEBHOOK_SECRET`.

Download the private key (PEM); its contents go into `GITHUB_APP_PRIVATE_KEY` (or point `GITHUB_APP_PRIVATE_KEY_PATH` at the file). The App ID goes into `GITHUB_APP_ID`, and the OAuth client ID/secret into `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`.

Then **install the app on your organization**: on the app's settings page choose
**Install App**, pick your org, and select all repositories or just the ones
Pulley should manage (you can also scope later with `GITHUB_ALLOWED_REPOS`).
Creating an app does not install it — until this step, GitHub sends no webhooks.
Prefer installing after the service is running so the `installation.created`
webhook is received; if you install earlier, the organization record is
back-filled from the first PR event, and step 4 covers linking either way.

### 2. Create the Slack App

Use `slack-manifest.yaml` as the source of truth. First replace every
`https://YOUR-PULLEY-DOMAIN` placeholder in the file with your real HTTPS domain,
then visit `https://api.slack.com/apps`, choose **Create from manifest**, and
paste the edited file.

Copy the app's client ID/secret and signing secret into `SLACK_CLIENT_ID`,
`SLACK_CLIENT_SECRET`, and `SLACK_SIGNING_SECRET`, then (re)start the service —
it must be running and reachable at your domain before the next step, both so
Slack can verify the events URL and so the OAuth callback can be served.

Then **install the app to your workspace through Pulley's own OAuth flow**: as a
workspace admin, open `https://<your-domain>/auth/slack` in a browser and
approve. This is the actual installation — it runs Slack's OAuth grant and
persists the workspace bot token in Pulley's database. Do not use the **Install
to Workspace** button in the Slack app config instead: it performs an
equivalent grant, but the bot token it issues is only shown in the Slack UI and
never reaches Pulley, leaving the bot unable to post.

### 3. Per-user: Connect GitHub

Every Slack user opens the Pulley App Home and clicks **Connect GitHub**. One-time OAuth; creates the row in `users` that links their Slack ID ↔ GitHub identity, enabling `/pulley me`, `/pulley merge`, `/lgtm`, and post-as-user sync.

### 4. Link the workspace to the GitHub installation

If the GitHub App was installed while the service was live (so `installation.created` webhook was handled) *and* the Slack OAuth ran in the same browser session, the rows are already linked.

If the events landed out of order and you end up with two `organizations` rows (one GitHub-only, one Slack-only), an admin can click **Link organization** in the App Home. The flow uses user-level GitHub OAuth + `GET /user/installations?app_id=…` — only installations *you* have access to are shown, and if there's exactly one match the rows are merged automatically.

### 5. Configure channels

In any Slack channel where the bot is a member:

```
/pulley settings pr #channel      # org-wide PR digest
/pulley settings ci #channel      # CI / workflow failures, deployment statuses
/pulley settings recap #channel   # Daily PR recap at 9am UTC (weekdays)
```

`/pulley settings` (no args) shows current values + GitHub link status.

## Deployment

### Docker Compose (primary)

The included `docker-compose.yml` is the supported path: it runs Postgres 16 (no
public port), a one-shot `migrate` service that runs `alembic upgrade head`, and
the app bound to `127.0.0.1:8000` with `SCHEDULER_ENABLED=true`. Front it with a
reverse proxy that terminates TLS for your domain. Full instructions —
reverse-proxy examples, scheduling, upgrades, and backups — are in
**[docs/self-hosting.md](docs/self-hosting.md)**.

Upgrades:

```bash
docker compose pull && docker compose up -d
```

Migrations run automatically as part of `up`.

### Any container platform

The published image
[`ghcr.io/shelbylsmith/pulley`](https://github.com/shelbylsmith/pulley/pkgs/container/pulley)
(tags: `latest`, semver, `main`) is a plain uvicorn container listening on
`:8000` — no platform-specific assumptions. To run it anywhere (Kubernetes,
Nomad, ECS, Cloud Run, a bare `docker run`):

- **Run migrations as a dedicated deploy step**, once per deploy — e.g. a
  Cloud Run Job, a Kubernetes `Job`, or a CI step running `alembic upgrade head`.
  Do **not** run them from the app entrypoint. A Postgres advisory lock makes
  concurrent runs safe as a backstop, but the intent is one migration run per
  deploy.
- **On multi-instance or scale-to-zero platforms, use external cron** for the
  recap/stale jobs (set `INTERNAL_API_TOKEN`, leave `SCHEDULER_ENABLED` off) —
  see [Scheduling](#scheduling). On **Cloud Run** specifically, keep
  `SCHEDULER_ENABLED` off: CPU is throttled outside of request handling, so an
  in-process scheduler won't fire reliably. Drive recap/stale reminders with
  Cloud Scheduler hitting `/internal/recap` and `/internal/stale-reminders`.

## Local development

```bash
uv sync        # installs deps + dev group
just dev       # starts the Postgres container + uvicorn --reload on :8000
```

`just dev` brings up the compose `db` service (the compose dev override publishes
`5432` locally) and runs the app against it. For webhook testing, tunnel with
`ngrok http 8000` and point the GitHub App's webhook URL at the ngrok URL while
developing.

Common tasks (`just test`, `just lint`, `just fmt`, `just migrate-gen`,
`just migrate-up`) are documented in [CONTRIBUTING.md](CONTRIBUTING.md).

## Repository layout

```
src/
  routers/         FastAPI endpoints (webhooks, commands, events, auth, internal)
  services/        Business logic (channel_manager, sync_service, pr_digest_service, notification_service, …)
  models/          SQLAlchemy ORM models
  db/              Database session + Alembic migrations
  utils/           Signature verification, misc helpers
tests/
.github/workflows/ CI
```

## Design notes

- **Echo prevention.** Synced messages carry `<!-- pulley-sync -->` HTML comment (GitHub side) or `[via Slack]` prefix (Slack side); handlers check for the marker and drop. For edits/deletes the `message_mappings.origin` column is the guard: a sync-induced edit/delete on one platform lands on a row whose origin is the *other* side, so that side's handler ignores it (no loop).
- **Org self-healing.** `handle_pr_opened` back-fills the `organizations` row from the webhook payload if the `installation.created` event was missed. Slack OAuth and the App Home "Link organization" button cover the inverse.
- **Thread correspondence.** `thread_mappings` table keys `(pull_request_id, github_thread_id)` → `slack_thread_ts` so replies bounce to the right side. The root review comment's id is the `github_thread_id`.
- **Message correspondence.** `message_mappings` records one row per synced comment — `(pull_request_id, slack_ts)` ↔ `(github_comment_id, github_comment_type)` plus `origin` — so an edit/delete of any individual comment/message can be mirrored to its counterpart. Edits/deletes reuse the create-time attribution (the author's token if posted as them, else the bot).
- **State model.** `pull_requests.state` is `open|closed|merged`; `last_review_state` is `approved|changes_requested|commented|NULL`. The digest colors/labels derive from these plus `is_draft`.

## License

MIT — see [LICENSE](LICENSE).
