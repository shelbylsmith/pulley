# Security Policy

## Supported versions

Only the latest release receives security fixes. Run a current version before
reporting.

## Reporting a vulnerability

Please report security vulnerabilities privately, **not** through public issues,
pull requests, or discussions.

Use GitHub's private vulnerability reporting: go to the repository's **Security**
tab and click **Report a vulnerability**. This opens a private advisory visible
only to the maintainers.

Include enough detail to reproduce the issue (affected version, configuration,
and steps). You will get an acknowledgement, and we will coordinate a fix and
disclosure timeline with you.

## Security-relevant surfaces

If you are self-hosting, these are the parts of Pulley worth understanding:

- **Webhook signature verification.** Inbound GitHub webhooks are verified with
  `GITHUB_WEBHOOK_SECRET` (HMAC signature). Inbound Slack requests are verified
  with `SLACK_SIGNING_SECRET`. Set both, and keep them secret — an attacker who
  knows them can forge events.
- **Internal endpoints.** `/internal/recap` and `/internal/stale-reminders` are
  gated by `INTERNAL_API_TOKEN`. When the token is unset the endpoints return
  `404`; when set they require `Authorization: Bearer <token>`. Only expose them
  over TLS and treat the token as a secret.
- **OAuth tokens in the database.** Per-user GitHub tokens and per-org Slack bot
  tokens are stored in Postgres so Pulley can act as the linked user. The
  database — and any backup of it — is therefore a secret. Restrict access and
  encrypt/protect backups.
