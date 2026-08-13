# SolDucks

SolDucks is a Telegram community bot with two features:

- Welcome protection: new group members are muted until they solve a math CAPTCHA.
- Solana buy alerts: multiple SPL token mints are monitored through an authenticated Helius mainnet webhook, with animated alerts delivered to one Telegram chat.

Runtime configuration, operator access, watched tokens, webhook state, and processed transaction signatures are stored in PostgreSQL.

## Requirements

- Python 3.11 or Docker
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A PostgreSQL database
- A Helius API key with webhook access
- A public HTTPS URL (Render is supported)

For the CAPTCHA feature, make the bot a group administrator with Delete Messages and Restrict/Ban Members permissions. To post buy alerts, add it to the destination group or channel with permission to send messages and media.

## Configuration

Copy `.env.example` to `.env` for local development. On Render, set these six
variables in the service's **Environment** page:

| Variable | Value |
| --- | --- |
| `BOT_TOKEN` | Token issued by BotFather |
| `OWNER_USER_ID` | Positive numeric Telegram ID for the immutable owner |
| `DATABASE_URL` | PostgreSQL connection URL, including `sslmode=require` when required by the provider |
| `WEBHOOK_SECRET` | Long random Telegram webhook secret |
| `HELIUS_API_KEY` | Helius API key |
| `HELIUS_WEBHOOK_SECRET` | Long random value Helius will send in the `Authorization` header |

Render supplies `RENDER_EXTERNAL_URL` and `PORT` automatically for web
services. Do not place secret values directly in `render.yaml`; its
`sync: false` entries are populated in the Render dashboard. For an existing
Blueprint service, newly added `sync: false` variables must also be entered
manually in the dashboard.

All remaining variables are optional:

- `ALERT_CHAT_ID` seeds the destination on the first start. It can instead be
  configured later with `/setalert`.
- `WEBHOOK_PATH`, `HELIUS_WEBHOOK_PATH`, and `ALERT_ANIMATION_PATH` override
  their built-in paths.
- `HELIUS_MAX_PAYLOAD_BYTES`,
  `ALERT_WORKER_POLL_SECONDS`,
  `EVENT_CLEANUP_SECONDS`, `EVENT_RETENTION_DAYS`, and
  `DEAD_EVENT_RETENTION_DAYS` tune runtime limits and maintenance intervals.

The complete local-development template is:

```env
BOT_TOKEN=123456789:telegram-token
OWNER_USER_ID=123456789
DATABASE_URL=postgresql://user:password@host:5432/solducks
RENDER_EXTERNAL_URL=https://your-app.onrender.com
WEBHOOK_PATH=webhook
WEBHOOK_SECRET=long-random-telegram-secret
HELIUS_API_KEY=your-helius-api-key
HELIUS_WEBHOOK_SECRET=Bearer-long-random-helius-secret
HELIUS_WEBHOOK_PATH=helius/webhook
ALERT_CHAT_ID=-1001234567890
ALERT_ANIMATION_PATH=solducks_buy.mp4
HELIUS_MAX_PAYLOAD_BYTES=1048576
ALERT_WORKER_POLL_SECONDS=1
EVENT_CLEANUP_SECONDS=3600
EVENT_RETENTION_DAYS=30
DEAD_EVENT_RETENTION_DAYS=90
PORT=8080
```

`OWNER_USER_ID` is the immutable access owner. `ALERT_CHAT_ID` seeds the database only when no alert chat has been configured; the owner can later change it with `/setalert`.

Place the SolDucks alert animation at `solducks_buy.mp4`. If it is absent or Telegram rejects it, alerts fall back to an emoji-rich text message. The existing `welcome.mp4` remains the CAPTCHA welcome animation.

## Run

```bash
docker compose up -d --build
docker compose logs -f
```

The service exposes:

- `GET /` — health check
- `POST /webhook` — authenticated Telegram webhook
- `POST /helius/webhook` — authenticated Helius event receiver

On startup, SolDucks creates its database tables, registers Telegram command menus, discovers the SPL and Token-2022 accounts belonging to each watched mint, and reconciles two Helius webhooks. Token-account coverage is refreshed every 15 minutes as a fallback. Enhanced events also teach the bot about newly observed token accounts immediately, and account-creation activity wakes a full discovery pass. Helius is updated only when a monitored address set or remote configuration actually changed.

## Telegram commands

Only `/id` is registered publicly. It returns the caller's numeric Telegram user ID.

Allowlisted operators see these commands in the bot's private chat:

| Command | Description |
| --- | --- |
| `/menu` | Open the private button control panel |
| `/watch <mint>` | Validate and add an SPL or Token-2022 mint |
| `/unwatch <mint>` | Remove a watched mint |
| `/tokens` | List the global watchlist |
| `/status` | Show alert, webhook, and delivery status |

The configured owner additionally sees:

| Command | Description |
| --- | --- |
| `/allow <user_id>` | Add an operator |
| `/disallow <user_id>` | Remove an operator |
| `/users` | List owner and operators |
| `/exempt <user_id \| @username \| display_name>` | Skip the CAPTCHA for a Telegram user/bot (e.g. a music bot) |
| `/unexempt <user_id \| @username \| display_name>` | Re-enable the CAPTCHA for a user/bot |
| `/exemptlist` | List CAPTCHA-exempt users/bots |
| `/setalert <chat_id>` | Change the alert destination |
| `/retrydead` | Replay alerts that exhausted delivery retries |
| `/retryuncertain confirm` | Explicitly replay ambiguous Telegram sends |

Management commands are rejected outside the bot's DM and every invocation performs a server-side authorization check. Authorized users can use `/menu` or the buttons shown after `/id`; actions that need a mint, user ID, or chat ID continue as a private step-by-step wizard. Button callbacks and every wizard step repeat the same server-side authorization checks. A newly allowed user may need to send `/id` once before Telegram can display their personalized command menu.

## Alert behavior

Helius sends parsed `SWAP` and `BUY` transactions involving a watched mint or one of its discovered token accounts to the alert webhook. A second, mint-only `ANY` webhook observes account-creation activity without ingesting every ordinary token-account transfer. Only parsed `SWAP` and `BUY` events can produce alerts. For swaps, structured Helius inputs and outputs identify the receiving buyer and the actual SOL/WSOL/USDC/USDT input without counting account rent or unrelated transfers. Failed transactions, sells, and non-purchase events are ignored.

Incoming signatures are unique in PostgreSQL, preventing normal Helius retries from generating duplicate alerts. Each signature, mint, and buyer delivery moves through explicit `sending`, `delivered`, or `uncertain` states. An interrupted or ambiguous Telegram request is dead-lettered instead of being silently treated as delivered or automatically repeated. `/status` reports uncertain deliveries; `/retrydead` leaves them untouched, while `/retryuncertain confirm` explicitly accepts the possible duplicate risk. Processing claims use a ten-minute stale lease instead of being reset during startup, which prevents overlapping Render deploys from reclaiming active work. Events remain pending while no alert chat is configured. Definite failed deliveries use bounded exponential retries; setting a destination first verifies the bot's posting permission and automatically requeues dead events that do not have uncertain sends.

The Helius receiver authenticates, persists, and queues each bounded payload within a 0.8-second acknowledgement budget. Token-account extraction runs later in the refresher worker. If persistence cannot complete in that window, the endpoint returns a retryable error before Helius's one-second deadline.

Delivered and ignored payloads are retained for 30 days; dead-lettered payloads are retained for 90 days. These periods are configurable. Helius supports at most 100,000 addresses in one webhook, so SolDucks rejects a token if its mint and token accounts would exceed the combined limit.
