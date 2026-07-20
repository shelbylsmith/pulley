# Plan: Microsoft Teams support for Pulley

Status: proposal (2026-07-19). Pulley today is Slack-only, with no chat-platform
abstraction — Slack is called inline from seven services, and Slack identifiers
(team ids, channel ids, message `ts` strings) are baked into the DB schema. This
plan covers (1) the architecture to support Teams alongside Slack, (2) a phased
implementation, (3) how to get a Teams test environment, and (4) the open
questions that need an empirical spike before committing to designs that depend
on them.

---

## 1. What Teams can and cannot do (verified against Microsoft docs)

Feasibility summary for Pulley's feature set. Nontrivial claims cite official
docs; four items are flagged as "verify empirically" in §6.

| Pulley feature | Teams mechanism | Fidelity |
| --- | --- | --- |
| Channel per PR, invite users | Graph `POST /teams/{id}/channels` + add members ([create channel](https://learn.microsoft.com/en-us/graph/api/channel-post?view=graph-rest-1.0)) | Full, but channels live **inside a team** (see below) |
| Archive on close / unarchive on reopen | Graph [`channel: archive`](https://learn.microsoft.com/en-us/graph/api/channel-archive?view=graph-rest-1.0) / `unarchive` (async, 202) | Full — permission model caveat in §6 |
| GitHub comments → channel, threaded | Bot Framework proactive message + thread reply (bot must be installed in the team; cache conversation refs) ([proactive messages](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages)) | Full |
| Channel messages → GitHub | Bot receives **all** channel messages without @mention via RSC `ChannelMessage.Read.Group` ([docs](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-messages-for-bots-and-agents)) | Full |
| Edit/delete propagation | Teams→GitHub: bot receives edit/delete events. GitHub→Teams: `UpdateActivity`/`DeleteActivity` on bot-owned messages, delegated PATCH on user-token messages | Full for Pulley's flows (Pulley only edits messages it created) |
| Attributed posts (user name + avatar) | Delegated Graph `ChannelMessage.Send` with a per-user OAuth token = genuine attribution. **No `chat:write.customize` equivalent** — a bot cannot override name/avatar | Partial: tier 1 (user token) ports; tier 2 (customize) is impossible; tier 3 (bot post, author named in text) is the fallback |
| PR digest updated in place | Adaptive Card posted by the bot, updated via `UpdateActivity` | Full |
| Slash commands | Native slash commands via bot `commandLists` with `triggers: ["slash"]` ([docs](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/create-a-bot-commands-menu)) | Full |
| App Home | Personal app: static tab (Pulley's FastAPI can serve the page) + personal-scope bot | Full (different UI stack) |
| Recap / stale reminders | Bot proactive sends from the existing scheduler | Full |
| Channel bookmarks (CI status, PR link) | No Teams equivalent of Slack bookmarks → replace with a bot-owned pinned "status card" message updated in place, or channel description | Redesign |

Structural constraints that shape the design:

- **No flat channel space.** Every Teams channel lives inside a team, capped at
  **1,000 channels per team including soft-deleted ones (which count for 30
  days)** ([limits](https://learn.microsoft.com/en-us/microsoftteams/limits-specifications-teams)).
  Pulley needs a designated **host team** per org, and a channel-lifecycle
  policy (or a thread-per-PR mode, §3) to stay under the cap.
- **Bots do not work in shared channels** (stated on the limits page). Per-PR
  channels must be standard (or private — see §6) channels.
- **No app-only Graph message send.** `POST /channels/{id}/messages` with
  application permissions is migration-mode only; there is no
  `ChannelMessage.Send.Group` RSC ([docs](https://learn.microsoft.com/en-us/graph/api/channel-post-messages?view=graph-rest-1.0)).
  All bot-identity posting goes through Bot Framework, not Graph.
- **Rate limits**: 50 RPS per app per tenant; per-thread budgets (7 msgs/s etc.)
  with 429s ([docs](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/rate-limit)).
  The existing `slack_service` retry pattern generalizes.
- **Recently de-risked**: Graph change-notification metering (model=A/B, E5
  requirement) for Teams message APIs was **retired 2025-08-25** — reading
  channel messages via Graph subscriptions is now license-free
  ([docs](https://learn.microsoft.com/en-us/graph/teams-licenses)).
- **Incoming webhooks (O365 connectors) retire May 18–22, 2026**; the Workflows
  replacement posts as "Flow bot" with no threading/editing — not a viable
  shortcut. Pulley needs a real bot from day one.

## 2. Architecture

### 2.1 ChatPlatform interface

`src/services/slack_service.py` is already the only module touching the Slack
SDK, and every function threads a per-org token — that is the seam. Introduce:

```
src/platforms/
  base.py        # ChatPlatform protocol + neutral types
  slack.py       # wraps today's slack_service, behavior-identical
  teams.py       # Bot Framework (botbuilder) + Graph client
```

Neutral types replace Slack-shaped arguments:

- `ChannelRef` — Slack: `channel_id`; Teams: `(team_id, channel_id)`.
- `MessageRef` — Slack: `(channel_id, ts)`; Teams:
  `(team_id, channel_id, message_id, reply_to_id)`. This kills the assumption
  that one `ts` string is simultaneously message key, thread anchor, and
  edit/delete handle.
- `RenderedMessage` — platform-specific payload produced by the renderer (§2.3),
  opaque to business logic.

Protocol surface (from the current 17 `slack_service` functions, minus
Slack-only ones): `create_channel`, `find_channel`, `archive_channel`,
`unarchive_channel`, `invite`, `set_topic`, `post_message`, `post_threaded`,
`update_message`, `delete_message`, `post_as_user`, `set_status_card`
(replaces bookmarks), `lookup_user_by_email`, `get_user_info`. Slack-only
surfaces (App Home publish, reactions) stay on the Slack impl and get
platform-conditional call sites until Teams equivalents land.

Business logic (`channel_manager`, `sync_service`, `notification_service`,
`pr_digest_service`, `command_service`) switches from `import slack_service` to
a platform instance resolved from the org: `platform_for(org)`. Slack error-string
handling (`name_taken`, `already_in_channel`, `thread_not_found`) becomes typed
exceptions raised by each impl (`ChannelNameTaken`, `AlreadyMember`,
`MessageGone`).

### 2.2 Data model

`Organization` gains `platform` (`slack` | `teams`, default `slack`). Since the
project is at its initial release, do a clean rename migration rather than
parallel columns:

| Today | Becomes | Notes |
| --- | --- | --- |
| `Organization.slack_team_id` (unique) | `chat_org_id` + `platform` (unique together) | Slack team id or Teams tenant id |
| `Organization.slack_bot_token` | `chat_credentials` (JSON) | Slack: bot token. Teams: bot app id/secret ref + host **team id** |
| `User.slack_user_id` / `slack_user_token` | `chat_user_id` / `chat_user_token` + `platform` | Teams: AAD object id / delegated Graph token (+ refresh token — Graph tokens expire, unlike Slack's) |
| `SlackChannel` model | `ChatChannel` | `channel_key` string: Slack `C…` id; Teams `{team_id}:{channel_id}` |
| `MessageMapping.slack_ts` / `slack_ts_extra` | `message_key` / `message_key_extra` | Opaque per-platform serialized `MessageRef` |
| `ThreadMapping.slack_thread_ts` | `thread_key` | Teams thread anchor = root message id |
| `PullRequest.pr_digest_ts` | `pr_digest_key` | For Teams also needs the activity id for `UpdateActivity` |
| `PullRequest.ci_bookmark_id` / `title_bookmark_id` | `status_card_key` | Bookmarks → status-card message |

Queries in `src/db/queries.py` rename accordingly (`get_org_by_slack_team` →
`get_org_by_chat_org`, etc.). `get_open_prs_for_org` stops keying org resolution
on `slack_team_id`.

New Teams-only table: `conversation_references` — Bot Framework proactive
messaging requires the serialized conversation reference captured when the bot
is installed / a channel is created; it must survive restarts.

### 2.3 Rendering

`src/utils/markdown.py` becomes direction-pair renderers per platform:

- Slack: existing `gfm_to_slack` / `slack_to_gfm` unchanged.
- Teams: `gfm_to_teams` (bot messages accept HTML/Markdown; digest and status
  cards are Adaptive Cards) and `teams_to_gfm` (Teams message bodies arrive as
  HTML — parse mentions, links, formatting back to GFM).
- Chunking: Slack's 3,800-char split stays in the Slack impl; Teams has
  different size limits — measured in the spike, handled in the Teams impl.
- Block Kit call sites (`pr_digest_service._render`,
  `notification_service._render_recap_attachment`, `app_home_service`) move
  behind the renderer so each platform produces its native payload.

### 2.4 Inbound adapters

- New router `src/routers/teams_activities.py`: single Bot Framework messaging
  endpoint (`POST /teams/messages`), JWT validation via the botbuilder SDK
  (replaces Slack HMAC verification for this path). Activity handlers map to
  the same business functions `slack_events.py` calls today: `message` →
  `sync_service.handle_platform_message`, message edit/delete activities →
  edit/delete sync, `conversationUpdate` (bot installed) → store conversation
  reference + org onboarding, `invoke` (slash command / card action) →
  `command_service`.
- The GitHub webhook side (`github_webhooks._dispatch_event`) is already
  platform-neutral — handlers just resolve the org's platform first.

### 2.5 Auth & install model

Pulley is self-hosted, which simplifies everything: each deployment serves one
customer, so the Teams side is **single-tenant** (multi-tenant Azure Bot
creation was deprecated July 2025 anyway — [quickstart](https://learn.microsoft.com/en-us/azure/bot-service/abs-quickstart)).
Per deployment:

- One Entra app registration + Azure Bot resource (F0, free) in the customer's
  tenant; credentials in env (`TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`,
  `TEAMS_TENANT_ID`).
- A `teams-manifest.json` template in the repo (analog of
  `slack-manifest.yaml`) declaring the bot, slash commands, personal tab, and
  RSC permissions: `ChannelMessage.Read.Group`, `Channel.Create.Group`,
  `Channel.Delete.Group`, `ChannelSettings.ReadWrite.Group`,
  `TeamsActivity.Send.Group`. RSC is consented by the **team owner at install**
  — no tenant-wide admin consent for the core flows
  ([RSC docs](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/rsc/resource-specific-consent)).
- Install flow: admin uploads the app zip (custom app upload), adds the app to
  the chosen host team → `conversationUpdate` fires → Pulley records
  `chat_org_id` (tenant) + host team id, then the existing `link:<org>` GitHub
  installation-linking flow proceeds as on Slack.
- Per-user attribution: "Connect Microsoft" OAuth (authorization-code flow
  against Entra, delegated `ChannelMessage.Send`) stored like
  `slack_user_token` today, plus refresh-token rotation. Identity auto-link by
  email mirrors the Slack `users.lookupByEmail` path via Graph user lookup.

### 2.6 Teams-native mode decision: channel-per-PR vs thread-per-PR

Slack's flat channel space makes channel-per-PR cheap. In Teams, channels are a
scarcer resource (1,000/team incl. 30-day-deleted; channel sprawl is also
culturally heavier in Teams). Two modes, org-configurable:

1. **`channel`** (parity mode): standard channel per PR in the host team,
   archived on close. Requires a lifecycle policy: hard-delete archived PR
   channels after N days (default 30) to stay under the cap.
2. **`thread`** (Teams-native mode, proposed default): one "Pull Requests"
   channel; each PR is a root post (the digest card) and all synced comments
   are replies in its thread. Avoids the cap entirely and matches how Teams
   teams actually work. The digest and per-PR conversation merge into one
   surface.

Recommend building `thread` mode first (it exercises every mechanism except
channel lifecycle), then `channel` mode for parity.

## 3. Implementation phases

**Phase 0 — test environment** (§4, do immediately; everything else can proceed
against the Agents Playground meanwhile).

**Phase 1 — platform abstraction, zero behavior change.** Extract
`ChatPlatform` + neutral types; move `slack_service` behind `platforms/slack.py`;
DB rename migration (§2.2); typed platform exceptions; renderer seam. Exit
criteria: full existing test suite green, a Slack deployment upgrades with only
the migration.

**Phase 2 — Teams skeleton.** botbuilder dependency, `/teams/messages` endpoint
+ JWT validation, manifest template, install flow (conversationUpdate → org
row), conversation-reference store, `gfm_to_teams` renderer, digest as
Adaptive Card with `UpdateActivity`, thread-per-PR mode: PR opened → root card
posted, GitHub comments → threaded replies. Exit criteria: PR opened/commented
in GitHub renders correctly in the test tenant.

**Phase 3 — bidirectional sync.** RSC all-message receive → Teams replies post
to GitHub as issue/review comments (with `[via Teams]` prefix and the existing
`SYNC_TAG` echo guard — both already platform-neutral); edit/delete activities
→ GitHub edit/delete; GitHub edit/delete → `UpdateActivity`/`DeleteActivity`;
`teams_to_gfm` renderer; MessageMapping/ThreadMapping on `MessageRef` keys.

**Phase 4 — commands, attribution, surfaces.** Slash commands (`/pulley`,
`/lgtm`) via `commandLists` + invoke handling; per-user Entra OAuth for
attributed posting (tier 1) with bot-attributed fallback (tier 3 — author name
rendered in the message/card since tier 2 is impossible on Teams); channel mode
(create/archive/unarchive/invite + lifecycle policy + status card replacing
bookmarks); personal tab served by the FastAPI app; recap + stale reminders via
proactive sends.

**Phase 5 — hardening & docs.** Rate-limit handling (429 + per-thread budgets),
`docs/self-hosting.md` Teams section (Entra app, Azure Bot, sideloading,
manifest upload), README, test matrix across both platforms (the fake-platform
impl from Phase 1 makes existing service tests platform-parameterizable).

Rough sizing: Phase 1 is the big refactor (touches every service + migration);
Phases 2–3 are the core new code; 4–5 are incremental. Phases 2+ are additive
and cannot regress Slack behavior once Phase 1 lands clean.

## 4. Test environment plan

Costs verified 2026-07-19. The bot side is free everywhere: **free Azure
account + Azure Bot resource on F0 — Teams is a free Standard channel with
unlimited messages** ([pricing](https://azure.microsoft.com/en-us/pricing/details/bot-services/)).
The only potential cost is the M365 tenant itself.

**Step 0 — before any tenant exists (day 1):** use the **Microsoft 365 Agents
Playground** (formerly Teams App Test Tool) — chat with the bot and render
Adaptive Cards locally with no M365 account, no tunnel, no registration
([docs](https://learn.microsoft.com/en-us/microsoftteams/platform/toolkit/agents-toolkit-fundamentals)).
Good enough for Phase 2 inner-loop work.

**Step 1 — pick the tenant.** Constraint: the tenant must be an isolated,
personally-administered sandbox — company (legacyco2.com) resources are not
used for testing. Free-first ladder:

1. **Free isolated E5 sandbox** — requires a Visual Studio
   Professional/Enterprise (standard, not monthly) subscription; the M365
   Developer Program grants a free 25-seat E5 sandbox with sideloading
   pre-enabled, auto-renewing with the VS subscription
   ([FAQ](https://learn.microsoft.com/en-us/office/developer-program/microsoft-365-developer-program-faq)).
   **Ruled out for this effort — no VS subscription is held.**
2. **Trials — ruled out by decision (no trials).** For the record: the
   Business Standard 1-month trial (credit card, auto-converts ~$14/user/month)
   and the Teams Exploratory experience were both options; Exploratory would
   not have worked anyway — eligibility requires the user to "belong to a
   tenant with a paid subscription", so a fresh free Entra tenant does not
   qualify, and its trial period is 1 month
   ([Exploratory docs](https://learn.microsoft.com/en-us/microsoftteams/teams-exploratory)).
3. **Chosen path: one Microsoft 365 Business Basic (with Teams) seat,
   month-to-month** — ~$8.40/user/month, cancel any month (~$7 on an annual
   commitment) ([pricing](https://www.microsoft.com/en-us/microsoft-365/business/microsoft-365-plans-and-pricing)).
   EEA note: the Teams unbundling forces separate Teams SKUs only on
   Enterprise (E1/E3/E5) suites — SMB Business suites are still offered **with
   Teams** in the EEA; pick the with-Teams variant at signup
   ([Microsoft licensing notice](https://www.microsoft.com/en-us/licensing/news/microsoft365-teams-eea)).
   Sign up with a personal/neutral email (not the work address) so a fresh
   `*.onmicrosoft.com` tenant is created with you as Global Admin.
   **Cost-minimizing sequence**: defer the purchase until Phase 2 needs real
   end-to-end testing — all earlier bot/card development runs $0 in the local
   Agents Playground — then buy the seat for the testing months only.

Not viable at $0: **Teams Free / personal accounts cannot upload custom apps**
(no Teams admin center). **Teams Exploratory** requires an already-paid tenant
(above). **ISV Success** grants the free E5 sandbox and is free for the first
12 months, but it enrolls the *company* into a Microsoft partner program
(corporate email, 3–5 day business verification, marketplace intent) —
excluded by the no-company-resources constraint
([ISV Success](https://www.microsoft.com/en-us/software-development-companies/offers-benefits/isv-success)).
Conclusion: with no VS subscription, no company enrollment, and no trials,
**a $0 real-Teams tenant does not exist in 2026** — the floor is one
month-to-month Business Basic seat, started as late as possible.

**Step 2 — tenant configuration (needs Teams Administrator; ~15 min + up to
24 h propagation):** Teams admin center → Manage apps → Org-wide app settings →
allow custom apps; Setup policies → Global → "Upload custom apps" = On
([docs](https://learn.microsoft.com/en-us/microsoftteams/teams-custom-app-policies-and-settings)).
Leave tenant RSC at its default (`ManagedByMicrosoft`). Create a host team
"Pulley Test".

**Step 3 — Azure side ($0):** free Azure account → Azure Bot resource, F0 tier,
single-tenant identity; note App ID/secret; messaging endpoint pointed at a
**dev tunnel**.

**Step 4 — local loop:** Microsoft 365 Agents Toolkit (VS Code extension,
formerly Teams Toolkit) scaffolds/validates the manifest and manages Dev
Tunnels; ngrok also works. Developer Portal (`dev.teams.microsoft.com`) for
manifest management. GitHub side reuses the existing dev setup from
`docs/self-hosting.md` (GitHub App webhooks → same tunnel).

## 5. Repo deliverables checklist

- `src/platforms/{base,slack,teams}.py`; `src/routers/teams_activities.py`
- Alembic migration renaming Slack-specific columns (§2.2) + `platform`
  discriminator + `conversation_references` table
- `teams-manifest.json` template (RSC permissions, slash commands, tab)
- Renderers: `gfm_to_teams`, `teams_to_gfm`, Adaptive Card builders
- Config: `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`, `TEAMS_TENANT_ID`,
  `PULLEY_TEAMS_MODE` (`thread`|`channel`)
- Docs: Teams section in `docs/self-hosting.md`; README feature-parity table
- Tests: fake `ChatPlatform` impl; platform-parameterized service tests

## 6. Open questions — spike in the test tenant before Phase 3/4 design freeze

1. **Bots in private channels** — current support is not clearly documented.
   Until verified, per-PR channels are **standard** channels. (Shared channels
   are definitively out — no bot support.)
2. **RSC message events in private channels** — a community claim says change
   notifications are blocked there; unconfirmed in primary docs. Test it.
3. **Channel archive permissions** — Graph archive/unarchive may fall outside
   RSC (no `Channel.Archive.Group` documented) and need tenant-wide consent.
   If so, channel mode falls back to delete-after-N-days instead of
   archive/unarchive, or requests one-time admin consent.
4. **Private-channel limits are tenant-dependent right now** — the 30 → 1,000
   private-channels-per-team increase rolled out April–May 2026 but the
   canonical limits page still shows old numbers; check the actual tenant.
5. **Reaction-driven reviewer self-assign (🔍)** — Teams bots receive
   `messageReaction` activities only on their own messages. Since synced posts
   are bot-owned this may suffice; verify the activity actually fires in
   channels, else drop the feature on Teams.
6. **Teams message size limits for chunking** — measure real limits to size the
   `split_for_slack` equivalent.
