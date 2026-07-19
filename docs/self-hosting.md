# Self-hosting Pulley

A practical, single-page guide to running Pulley on your own host with Docker
Compose and a TLS reverse proxy.

## What you need

- A host (VM, droplet, small server) that runs **Docker** and **Docker Compose**.
- A **public HTTPS domain** pointing at that host. GitHub and Slack only deliver
  webhooks over TLS — there is no way around this.
- A **GitHub App** and a **Slack App** with their credentials. Create them by
  following [Setup in the README](../README.md#setup); come back here once you
  have the IDs, secrets, private key, and webhook/signing secrets.

## Walkthrough

Get the Compose file. Either clone the repo, or just grab `docker-compose.yml`
and `.env.example` into a working directory:

```bash
mkdir pulley && cd pulley
curl -O https://raw.githubusercontent.com/shelbylsmith/pulley/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/shelbylsmith/pulley/main/.env.example
```

Create your `.env`:

```bash
cp .env.example .env
```

Fill in, at minimum:

```
GITHUB_APP_ID=
GITHUB_APP_PRIVATE_KEY=            # PEM contents (or use GITHUB_APP_PRIVATE_KEY_PATH)
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=
GITHUB_WEBHOOK_SECRET=
SLACK_CLIENT_ID=
SLACK_CLIENT_SECRET=
SLACK_SIGNING_SECRET=
APP_BASE_URL=https://pulley.example.com
```

`DATABASE_URL` is already wired to the compose Postgres, so you can leave it
alone. See the [Configuration table](../README.md#configuration) for every
setting.

Start everything:

```bash
docker compose up -d
```

Compose brings up Postgres, runs the one-shot `migrate` service
(`alembic upgrade head`), and starts the app on `127.0.0.1:8000` with the in-app
scheduler enabled.

Check it's healthy (the app binds to loopback, so query it locally on the host):

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

## Reverse proxy & TLS

The app listens only on `127.0.0.1:8000`. You terminate HTTPS for your domain in
front of it and forward requests through.

### Caddy (recommended — automatic Let's Encrypt)

`Caddyfile`:

```
pulley.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy obtains and renews a Let's Encrypt certificate automatically. Run
`caddy run` (or the systemd service) and you're done.

### nginx + certbot (alternative)

Install nginx and use `certbot --nginx` to provision the certificate, then
`proxy_pass http://127.0.0.1:8000;` inside a `server` block for
`pulley.example.com`. Certbot writes the TLS config and sets up auto-renewal.

Once TLS is up, confirm webhooks reach you: `https://pulley.example.com/health`
should return `{"status":"ok"}` from the public internet.

## Scheduling

The daily recap and stale-PR reminders need a trigger. Pick one:

### In-app scheduler (compose default)

The compose `app` service runs with `SCHEDULER_ENABLED=true`. As long as the
container stays up, it fires the jobs itself on the configured cron. Nothing else
to do — this is the default for a single always-on host.

### External cron (scale-to-zero / multi-instance)

If your platform scales to zero or runs multiple instances, turn the in-app
scheduler off and drive the jobs externally. Set `INTERNAL_API_TOKEN` in `.env`,
then have any scheduler POST to the internal endpoints with the bearer token.

crontab on the host:

```cron
# recap 09:00 UTC weekdays, stale reminders 14:00 UTC weekdays
0 9  * * 1-5 curl -fsS -X POST -H "Authorization: Bearer $TOKEN" https://pulley.example.com/internal/recap
0 14 * * 1-5 curl -fsS -X POST -H "Authorization: Bearer $TOKEN" https://pulley.example.com/internal/stale-reminders
```

Or, for free hosted cron, a scheduled **GitHub Actions** workflow — store the
token as a repo secret (`INTERNAL_API_TOKEN`):

```yaml
name: pulley-recap
on:
  schedule:
    - cron: "0 9 * * 1-5"    # recap, 09:00 UTC weekdays
    - cron: "0 14 * * 1-5"   # stale reminders, 14:00 UTC weekdays
jobs:
  trigger:
    runs-on: ubuntu-latest
    steps:
      - name: Recap
        if: github.event.schedule == '0 9 * * 1-5'
        run: |
          curl -fsS -X POST \
            -H "Authorization: Bearer ${{ secrets.INTERNAL_API_TOKEN }}" \
            https://pulley.example.com/internal/recap
      - name: Stale reminders
        if: github.event.schedule == '0 14 * * 1-5'
        run: |
          curl -fsS -X POST \
            -H "Authorization: Bearer ${{ secrets.INTERNAL_API_TOKEN }}" \
            https://pulley.example.com/internal/stale-reminders
```

All cron expressions — the in-app ones and the schedulers above — are evaluated
in **UTC**. Firing a job more than once is safe: a database at-most-once guard
absorbs duplicates, so overlapping crons or retries won't double-post.

## Upgrades

```bash
docker compose pull && docker compose up -d
```

Pulling a new image and running `up` re-runs the `migrate` service, so schema
migrations are applied automatically before the app starts.

For predictable upgrades, pin a semver tag instead of `latest` in your
`docker-compose.yml` (e.g. `ghcr.io/shelbylsmith/pulley:1.2.0`) and bump it
deliberately.

## Backups

The database holds per-user GitHub OAuth tokens and per-org Slack bot tokens.
**Treat every backup as a secret** — encrypt it, and restrict who can read it.

Dump the compose Postgres:

```bash
docker compose exec -T db pg_dump -U pulley pulley | gzip > pulley-$(date +%F).sql.gz
```

Restore into a fresh database with `gunzip -c pulley-YYYY-MM-DD.sql.gz | docker
compose exec -T db psql -U pulley pulley`. Store the dumps encrypted and off-host.
