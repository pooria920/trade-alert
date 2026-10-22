# TSE Stock Queue Alert Bot for Bale

[راهنمای فارسی اجرای پروژه روی VPS در کنار Caddy](VPS_DEPLOYMENT.fa.md)

Implemented through **Phase 6A** (production preparation): private alerts plus admin-managed Bale channel alerts, bounded composite queue conditions, shared collection, persistent recovery/cooldown, Persian management/history and the existing SQLite notification outbox.

**Live limitation:** endpoints responded during investigation, but BestLimits has no source timestamp or verified complete/incremental semantics. Captured bids conflict with same-day limits. `/queue` therefore refuses definitive queue volumes for this data, and the current live adapter cannot generate alert events or notifications. The engine/delivery path is verified offline with synthetic normalized snapshots and mocked Bale. See [observations and captured payloads](docs/TSETMC.md). There is no verification bypass.

## Setup

Use Node.js 24 LTS and pnpm 11.10.0. Native SQLite installation may require Python, make and a C++ compiler (Visual Studio Build Tools on Windows).

```sh
corepack enable
corepack prepare pnpm@11.10.0 --activate
pnpm install --frozen-lockfile
cp .env.example .env
# Set BALE_BOT_TOKEN and BALE_ALLOWED_USER_IDS. For local development use NODE_ENV=development.
pnpm dev
# Or compile and run:
pnpm build
pnpm start
```

Example: `BALE_ALLOWED_USER_IDS=123456789,987654321,456789123`. Whitespace is trimmed and duplicates removed; empty/malformed entries are rejected. IDs must fit Bale's [documented 52-bit range](https://docs.bale.ai/#user). There is no user-count limit. Use Bale IDs, not Telegram IDs. Legacy `TELEGRAM_*` settings and the singular allowlist setting are rejected.

Each user must initiate a private conversation. Authorization runs before registration, database access and handlers; unauthorized users/groups are ignored. Registration records the observed private chat ID and preserves existing preferences. Run only one process per bot token.

Node loads `.env` natively. Zod validates startup configuration. Pino emits JSON logs and redacts bot tokens from serialized errors. No secrets are bundled.

## Bale connection and fresh setup

This Node.js version targets the ordinary Bale Bot API at `https://tapi.bale.ai`, using grammY long polling. No web server or exposed port is needed. See the [official API documentation](https://docs.bale.ai/). There is no Telegram fallback or business/messaging API integration.

Create a bot through [@botfather in Bale](https://ble.ir/botfather), then fill `BALE_BOT_TOKEN` and `BALE_ALLOWED_USER_IDS` in your local `.env`. Optional channel mode also needs `BALE_ADMIN_USER_IDS` and `BALE_CHANNEL_ID`. Start with a new, empty SQLite path (inside `/app/data` in Docker); no old database is deleted or imported automatically. Users register by sending an authorized private message to this bot in Bale.

Applied SQL migrations and their historical names (`telegram_user_id`, `telegram_channels`) remain unchanged. In this fresh database those fields contain Bale identities only. Do not reuse a Telegram database or outbox. The Python implementation is separate and is not changed by this migration.

Bale does not document `setMyCommands`, so startup does not call it. Commands are listed in `/help`. Telegram-only polling parameters are removed before HTTP requests. Older Bale clients have callback IDs starting with `1`; their callback notices fall back to a private text reply, while buttons still run the authorized handler. API requests have a 45-second transport timeout; outbox sends and channel permission checks retain the shared 10-second deadline.

All automated checks intercept external API calls. Live Bale compatibility, token validity, channel permissions and account-specific sending limits are not verified by these offline tests. The current TSETMC evidence restriction still prevents real alert events; changing messengers does not verify market data.

## Commands

| Command           | Behavior                                                                                                     |
| ----------------- | ------------------------------------------------------------------------------------------------------------ |
| `/start`          | Information, help and personal notification preferences                                                      |
| `/help`           | Persian command guide                                                                                        |
| `/add خودرو`      | Search; select one of up to eight results with inline buttons                                                |
| `/list`           | Personal watchlist, ten symbols per page                                                                     |
| `/queue خودرو`    | Inspect a watched symbol; omit the argument for buttons                                                      |
| `/remove خودرو`   | Remove your membership and associated rules; omit the argument for buttons                                   |
| `/status`         | Aggregate collector/session state, API fetch time, verified source time and fresh-symbol coverage            |
| `/alert`          | Guided creation: watched symbol, condition, one/five-minute window, percentage, minimum volume, confirmation |
| `/alerts`         | Personal list and details; enable, disable, edit configuration or confirm deletion                           |
| `/history`        | Last ten personal events with triggering values and Tehran source time                                       |
| `/remove rule ID` | Delete an owned alert, including its history and pending notifications                                       |
| `/cancel`         | Cancel an unfinished alert draft                                                                             |
| `/admin`          | Private, configured-admin-only channel alert management and status                                           |

Queue/removal commands also accept an instrument code from your own watchlist. Search selections belong to their initiating user and expire after five minutes or restart. Alert drafts expire after ten minutes or restart; stale edit confirmations cannot overwrite a newer rule revision. Every callback rechecks ownership. Disabling personal notifications cancels pending delivery when claimed; enabling them does not replay cancelled occurrences.

## Configuration

| Variable                    | Default                      | Meaning                                            |
| --------------------------- | ---------------------------- | -------------------------------------------------- |
| `NODE_ENV`                  | `development`                | development/test/production                        |
| `BALE_BOT_TOKEN`            | required                     | Token from @botfather in Bale                      |
| `BALE_ALLOWED_USER_IDS`     | required                     | Comma-separated user IDs                           |
| `TSETMC_BASE_URL`           | `https://cdn.tsetmc.com/api` | HTTPS API base                                     |
| `TSETMC_REQUEST_TIMEOUT_MS` | `5000`                       | 1–120000 ms per request                            |
| `TSETMC_MAX_RETRIES`        | `2`                          | 0–3 retries after initial attempt                  |
| `MARKET_DATA_MAX_AGE_MS`    | `30000`                      | Maximum trusted source-data age; up to 300000 ms   |
| `MARKET_POLL_INTERVAL_MS`   | `10000`                      | Delay after each completed cycle; at least 1000 ms |
| `SNAPSHOT_RETENTION_HOURS`  | `24`                         | Source-time retention; 1–8760 hours                |
| `DATABASE_URL`              | `./data/stock-alert.db`      | Local SQLite path                                  |
| `LOG_LEVEL`                 | `info`                       | Pino level or silent                               |

The native Fetch client validates responses with Zod and retries network/timeouts, 429 and selected transient 5xx errors with exponential backoff. Permanent HTTP/schema failures are not retried. Retry-After is honored within a 30-second cap; longer waits fail rather than aggressively retry. No upstream polling rate is claimed to be validated.

## Collector and Tehran schedule

The collector starts with the application, including while Bale initializes. One async loop reads current authorized watchlists each cycle, fetches each unique instrument once, and waits the configured interval after completion. Cycles cannot overlap. Changes to watchlists apply without restarting. Revoked users' memberships are excluded. The concurrency limit is for symbol collections, each of which may make several sequential HTTP requests; “one fetch per symbol” does not mean one HTTP endpoint call.

Additional settings:

| Variable                             | Default    | Meaning                                                              |
| ------------------------------------ | ---------- | -------------------------------------------------------------------- |
| `MARKET_MAX_CONCURRENT_REQUESTS`     | `3`        | 1–10 simultaneous symbol collections                                 |
| `MARKET_REQUEST_TIMEOUT_MS`          | `5000`     | Whole-symbol deadline, including endpoint calls/retries; 1–120000 ms |
| `MARKET_MONITOR_FORCE_ACTIVE`        | `false`    | Testing override for schedule only                                   |
| `MARKET_SESSION_MARKET`              | `equities` | Selected session profile                                             |
| `MARKET_SESSIONS_JSON`               | `[]`       | Explicit Tehran sessions, closed by default                          |
| `MARKET_CLOSED_DATES`                | empty      | Comma-separated Gregorian Tehran dates for holidays/closures         |
| `SNAPSHOT_HISTORY_TOLERANCE_SECONDS` | `20`       | Maximum baseline age before the requested target; up to 300 seconds  |
| `SNAPSHOT_CLEANUP_INTERVAL_MS`       | `900000`   | Cleanup frequency; 60000–86400000 ms                                 |

No reliable exchange calendar or holiday feed has been established for this application. There are deliberately no assumed hours or weekends: **configure sessions before expecting normal polling**. The following shows syntax only, not verified exchange hours. Replace days/hours with an explicitly confirmed schedule and maintain closure dates for your instruments:

```env
MARKET_SESSION_MARKET=equities
MARKET_SESSIONS_JSON='[{"market":"equities","days":[1,2,3,6,7],"start":"09:00","end":"12:30"}]'
MARKET_CLOSED_DATES=2026-10-24,2026-10-25
MARKET_MONITOR_FORCE_ACTIVE=false
```

The dates above are arbitrary syntax examples, not asserted holidays. Days use ISO numbering (Monday 1 through Sunday 7), start is inclusive, end exclusive, and all calculations use `Asia/Tehran`, independently of VPS timezone. Multiple sessions/profile names are supported; split overnight hours into separate days. The selected profile applies to all watched instruments: per-instrument market classification is not yet verified. Closure dates suppress complete local days. Unannounced or intraday halts are not automatically supplied by this calendar; verified book/trading-state checks remain required.

`MARKET_MONITOR_FORCE_ACTIVE=true` is for isolated testing. It bypasses only the calendar, never timestamps, book completeness, trading-state or price-limit validation. With the current unverified live adapter, polling still stores **no queue snapshots**. Do not mistake API responses for actionable history.

Each failed/unusable symbol has capped exponential backoff (up to five minutes) with positive jitter. HTTP Retry-After pauses further collection across symbols; the shared client also prevents on-demand HTTP requests during a known server cooldown. Internal retries are bounded, and a collection deadline cancels both HTTP and retry delays. Invalid/stale data is never saved. Failures log sparsely (first, changed reason and powers of two); recovery logs once. A failing symbol does not stop other eligible symbols.

`/status` is global but shows only aggregate information. It distinguishes the latest parsed API fetch from detector-verified fresh source data, reports fresh-symbol coverage and counts consecutive cycles with attempted failures. Per-symbol diagnostics are available through the read-only service method; a returned copy cannot mutate internal state. Backoff-only or closed-session ticks do not count as additional failed cycles. Operational counters reset on restart, while snapshots persist. Health is never inferred merely from HTTP 200 or old stored history.

## Snapshot history and retention

`SnapshotRepository` stores detector-accepted states, including valid `none`/zero-volume states. Source `timestamp` and local `collectedAt` are UTC Unix milliseconds. Identical source updates are deduplicated; conflicting values with the same source timestamp are rejected rather than overwritten. Incremental/unknown books are not reconstructed speculatively and cannot enter history.

Use `getLatestSnapshot(insCode)`, `getSnapshotAtOrBefore(insCode, target)` or inclusive `getSnapshotsBetween(insCode, from, to)`. For one/five-minute baselines, pass `now - 60000` or `now - 300000` to the at-or-before method. It searches only `[target - tolerance, target]` and returns null when no baseline exists; it never selects a later update or substitutes arbitrarily old history. Latest-history lookup alone is not a freshness guarantee. The existing symbol/time unique index and retention timestamp index serve these queries.

Migration `0002_snapshot_collection.sql` adds nullable collection-time and symbol columns without rebuilding tables. Legacy rows remain intact with unknown provenance; repository comparison queries exclude them rather than fabricate collection times. Existing watchlists, rules, preferences and events are preserved. Normal retention can subsequently expire old historical rows, including legacy history.

Cleanup runs even outside sessions, normally every 15 minutes. It deletes only snapshots older than the configured source-time cutoff, in indexed batches of at most 1000 rows, yielding between batches. It never deletes user configuration, runs concurrently with asynchronous collection and is joined during shutdown. Collection and cleanup finish before SQLite closes.

## Alert rules and delivery

The four initial definitions are `BUY_QUEUE_DROP_PERCENT`, `SELL_QUEUE_DROP_PERCENT`, `BUY_QUEUE_DISAPPEARED` and `SELL_QUEUE_DISAPPEARED`. Each has a Zod-validated JSON configuration and a typed registry strategy for evaluation, historical window and recovery. Percentages are `(0, 100]`, with at most six decimal places for exact scaled-integer comparisons. Windows are one or five minutes; minimum volumes are safe nonnegative integers. Cooldowns are service-configurable from zero to 86400 seconds. The bot uses the default cooldown on creation and preserves it on edits.

Additional configuration:

| Variable                         | Default | Meaning                                                                                      |
| -------------------------------- | ------- | -------------------------------------------------------------------------------------------- |
| `ALERT_DEFAULT_COOLDOWN_SECONDS` | `300`   | Minimum interval between occurrences; 0–86400 seconds                                        |
| `ALERT_HISTORY_LIMIT`            | `10`    | Number of personal history events; 1–10                                                      |
| `NOTIFICATION_MAX_RETRIES`       | `5`     | Retries after the initial attempt; 0–10                                                      |
| `NOTIFICATION_RETRY_BASE_MS`     | `2000`  | Exponential retry base; 1–300000 ms, delays capped at five minutes except server retry_after |
| `NOTIFICATION_MAX_AGE_MINUTES`   | `15`    | Maximum source age for delivery; 1–1440 minutes                                              |

Only newly inserted, fresh, detector-accepted snapshots reach the engine. There are no per-rule TSETMC calls. Historical reads are cached across all rules for that update and restricted to the same persisted session identity (profile, Tehran date and interval). Existing history without session provenance is excluded. Percent drops require a positive original-side baseline at the configured at-or-before target within tolerance; the current state must be the same side or a detector-confirmed `none` (a 100% drop). An opposite-side queue is incompatible. A definitive `none` can trigger disappearance only after a recent confirmed original-side queue; failures, gaps and stale data cannot substitute for that evidence.

New rules, edits, re-enabling and session changes prime an `ARMED` state without immediately firing. Restarts retain the last source update and recovery state, while rejecting source updates before process startup. Older/duplicate updates never roll state backwards. While armed, percentage rules use the rolling window. A trigger freezes that comparison volume, enters `TRIGGERED`, then `WAITING_FOR_RECOVERY`. Recovery requires the original side to reach `baseline × (1 − max(0, threshold − 5) / 100)` **and** cooldown expiry. For example, a 30% trigger requires recovery to at least 75% of the frozen baseline. Thresholds at or below 5% require full recovery. Disappearance re-arms only when the original-side queue returns at or above its minimum (and positive), after cooldown. Recovery itself never triggers a new event. This prevents a rolling lower baseline or small fluctuations from rearming an active occurrence.

State, event payload and outbox insertion commit in one SQLite transaction; a unique key combines rule ID, revision, session and source timestamp. The independent worker claims one due item using an immediate transaction and a unique 60-second lease. Bale requests have a 10-second deadline. Other workers cannot claim a live lease; expired claims can resume after a crash. The application still runs one process per bot token for Bale long polling.

Transient transport/server failures retry with bounded exponential backoff. Bale 429 `retry_after` establishes a persisted global delivery cooldown measured from the failure response. Permanent 4xx failures are marked `FAILED`. Obsolete messages become `EXPIRED`; revoked authorization, disabled/edited rules and muted preferences become `CANCELLED`. Delivery sends Persian text without a parse-mode parameter (Bale applies its own Markdown rendering), reports source time in Tehran, and labels attempts later than the market freshness allowance as delayed. `/history` shows recorded occurrences even if delivery failed or was cancelled. Rule deletion (also when removing its watched symbol) deletes that rule's state, history and outbox through foreign keys; shared snapshots and other users' events remain.

Delivery is **at least once with best-effort deduplication**, not exactly once. A crash, timeout or suspension after Bale accepted a message but before success was recorded can cause a duplicate on retry. Pending retries survive restarts; unfinished interactive drafts do not. Events/outbox have no automatic retention in this phase; monitor SQLite disk usage and back it up. Snapshot cleanup cannot remove the frozen recovery baseline or event payload. Shutdown cancels and joins delivery and collection before closing SQLite.

To add a future condition, extend the discriminated schema, typed strategy registry, event presentation schema and creation UI. The generic engine keeps its state/transaction/delivery flow, and definitions require no additional SQL columns. Future price/volume conditions themselves are intentionally absent.

## Channel alerts and administration

The same single destination can also be a Bale group. Put its exact numeric ID in `BALE_CHANNEL_ID` and make the bot a group administrator. The existing service, collector and outbox are reused; no new database or environment variable is needed. Only the configured group/channel receives shared alerts. Manage its rules with `/admin` in an authorized administrator's private conversation. Messages and callbacks inside groups are still ignored; members are not registered or enumerated. Group checks require the bot's `administrator` status; `can_post_messages` is channel-only in the [Bale documentation](https://docs.bale.ai/#chatmemberadministrator). Permission checks still run at startup, save/enable and before each send, with the existing deadline and retry policy.

Channel mode is optional. Leave both new variables blank to retain private-only behavior. To configure the single initial destination:

```env
BALE_ALLOWED_USER_IDS=123456789,987654321
BALE_ADMIN_USER_IDS=123456789,987654321
BALE_CHANNEL_ID=-1001234567890
```

These are syntax examples, not real deployment credentials or a verified channel. Both channel/admin settings must be supplied together. IDs remain strings in SQLite; the channel must be a nonzero numeric identifier within Bale's documented 52-bit range. No Telegram-specific `-100` prefix is required. Username destinations and arbitrary callback-supplied destinations are rejected. Every configured admin must also be in the private allowlist; only configured admins can use channel management. No subscriber registration, membership list, fan-out or subscription system exists.

Add the bot as an administrator of the channel and grant posting permission. Startup, save/enable, channel status and each channel delivery use `getChat` and `getChatMember` to verify the exact channel and the bot's administrator status plus `can_post_messages`, following the [Bale API permission model](https://docs.bale.ai/#chatmemberadministrator). Verification performs no test post. An inaccessible channel does not disable private alerts. Permission checks and posting share the outbox's ten-second deadline and shutdown cancellation. After asynchronous checks, delivery rechecks rule revision, authorization and source age. Permanent posting failures become `FAILED`; temporary Bale/transport failures retain the existing retry and rate-limit policy.

Each administrator must first open a private conversation, then use `/admin`. The Persian menu offers creation, paginated lists (ten per page), details, individual condition editing, enable/disable, confirmed deletion and channel status. Menus prefer editing the existing message. Creation selects the configured channel, searches a symbol, selects a supported condition and validates numeric input; a readable summary precedes confirmation. Private watchlists are not required for channel symbols. Back/Cancel controls, refreshed per-step tokens, a ten-minute draft expiry, duplicate-save protection and optimistic revisions make stale interactions safe. A restart discards unfinished drafts without saving partial rules.

Current configured admins collectively manage the configured channel's rules. Creator identity is stored privately in `owner_id`; an edit/enable records the acting administrator. Rules whose creator is no longer an admin stop collection/evaluation/delivery until another current admin explicitly adopts them through management. Switching channel configuration suspends old-channel rules and pending deliveries without deleting them. Existing private rules retain their owner, preference, watchlist and history behavior. Private `/alerts`, `/history` and `/remove` cannot expose or mutate channel rules, even for their creator. Removing a private watched symbol cannot delete its channel rules.

A channel supports multiple independent rules, including several for the same symbol. Its eligible enabled symbols are unioned with private watchlists before the existing collector deduplicates instruments. There is no extra request per condition, administrator, rule or subscriber. One occurrence creates one outbox message for that rule's channel. Independent rules may intentionally publish separate messages for the same market update.

Channel status is admin-only and reports title, numeric channel ID, verified posting permission, total/eligible active rules, last successful delivery and latest sanitized delivery error. Delivery timestamps/error metadata persist in `application_state` independently of rule deletion. Deleting a rule still cascades only that rule's state/events/outbox. Removing a rule never deletes another rule or shared history.

### Composite queue conditions

The builder supports up to eight conditions, each editable/removable, using «همه شرط‌ها برقرار باشند» (AND) or «حداقل یکی از شرط‌ها برقرار باشد» (OR). It offers the four existing queue drop/disappearance types plus minimum **current queue volume** for buy/sell queues (`BUY_QUEUE_VOLUME_ABOVE`, `SELL_QUEUE_VOLUME_ABOVE`). This uses verified order-book queue volume, not trading volume. Minimum current volume is a positive safe integer and means at least the configured number of shares. Unsupported prices, traded-volume spikes and other indicators remain hidden and unimplemented.

The validated JSON tree uses `{ type: 'GROUP', operator: 'AND' | 'OR', conditions: [...] }` with typed leaves. Internal trees allow up to four group levels and eight total leaves; empty/invalid/excessive trees are rejected. The Bale builder creates flat groups only. Nested rules can be viewed, enabled, disabled or deleted; editing nested structures through Bale is intentionally unavailable.

All leaves evaluate the same fresh persisted snapshot/session. Historical baselines use the existing bounded tolerance and are shared across rules. Kleene three-valued logic is explicit: AND is FALSE if any child is FALSE; OR is TRUE if any child is TRUE; otherwise UNKNOWN propagates. Thus TRUE OR UNKNOWN is TRUE, FALSE AND UNKNOWN is FALSE, and unresolved overall UNKNOWN never triggers or rearms. Missing history is UNKNOWN; a confirmed `none` is FALSE for a positive current-volume minimum, while an opposite-side queue is UNKNOWN.

Cooldown, deduplication and recovery apply once to the whole expression. A trigger stores frozen evidence for its satisfied leaves in `alert_states.recovery_data`. During recovery, those leaves retain their original drop/disappearance policies; queue-volume minimum leaves stay latched until volume falls strictly below 95% of the configured minimum. Unlatched leaves evaluate normally. The entire expression must become conclusively FALSE and cooldown must expire before it rearms; UNKNOWN cannot rearm it. An AND rule can recover when one leaf clears, while an OR rule requires all branches to clear. New/edited/session states retain the existing priming behavior.

Channel messages send Persian text without a parse-mode parameter, show symbol, satisfied conditions, relevant volumes/actual drop percentages, comparison windows and Tehran source time, with delayed-delivery indication. They contain no rule IDs or administrator identities. The same durable outbox, unique occurrence keys, leases, retry budget, maximum source age and at-least-once limitations apply to both destinations. No successful live Bale posting is claimed; all automated delivery checks mock Bale.

## Ownership and market data

`users` stores string user IDs, observed chat IDs and preferences; `watchlists` has a composite user/instrument key. Rules carry an owner; events inherit ownership through their rule. NULL owners quarantine preserved Phase 1 data. `UserService` and `AlertService` scope operations by authorized actor. Delivery resolves only the observed private chat through `notificationTarget`, rechecking authorization, preference, membership, enabled state and rule revision immediately before sending.

Symbols and market history are shared. Personal removal never deletes shared instruments/history or another user's rules/events. One `MarketService` instance shares in-flight requests across on-demand commands and the collector. Completed/failed underlying requests are evicted so later checks fetch afresh.

`QueueDetector` requires verified complete books, coherent same-day price limits, fresh source timestamps and open trading state. It rejects invalid quantities, missing ranks, crossed books, stale and ambiguous data; invalid data is never a no-queue snapshot. Queues require valid orders exactly at the permitted boundary. The separate `FixtureMarketClient` always marks data unverified. Synthetic detector cases do not claim verified live semantics.

## Database and checks

SQLite uses WAL, foreign keys, a five-second busy timeout, string instrument IDs and UTC Unix milliseconds. Startup applies committed Drizzle migrations without deleting data. Do not edit applied migrations or use schema push for upgrades.

```sh
pnpm db:generate --name description_of_change
pnpm db:migrate
pnpm typecheck
pnpm lint
pnpm test
pnpm format:check
pnpm build
docker build -t tse-stock-alert-bot:local .
```

Migration CLI loads `.env` without requiring Bale credentials; generation uses only the schema. Builds copy migrations beside compiled code. Tests cover isolation, forged callbacks, legacy upgrades, persistence, shared requests/recovery, API validation, queue detection, token redaction, health and shutdown. They mock external APIs; there are no live calls in unit tests.

## Production deployment preparation

Phase 6A adds a digest-pinned Node 24 production image, resource-limited/read-only Compose service, database instance lock, progress-aware health, safe diagnostics and online backup/new-path restore tooling. No VPS deployment or live notification has been performed.

Follow [DEPLOYMENT.md](DEPLOYMENT.md) for exact first-install, update, migration, backup, restore and rollback commands in `/opt/stock-alert-bot`. Helpers operate only on the local Docker daemon and require explicit paths/confirmation flags; they never connect to a remote host or affect unrelated services.

`node dist/healthcheck.js` reports actual monitoring progress, database write availability and Bale polling connectivity. Closed markets and empty watchlists are healthy idle states; persistent TSETMC failures or unverified data are separately reported degraded conditions, not proof that alerts work. `node dist/diagnostics.js` validates configuration and prints counts/status only, without IDs/tokens, migrations or API requests.

The production image uses the compiled migration CLI `node dist/database/migrate.js`. Stop the bot before manual migrations; the application and migrator share an OS-backed instance lock beside the database. Online backups use SQLite's backup API while the app runs, then verify a portable artifact without WAL/SHM dependencies. Restore always requires a new database filename and explicit confirmation; it cannot overwrite existing data.

Compose-only settings: `COMPOSE_PROJECT_NAME` (helpers default to `stock-alert-bot`, keep it stable) and `STOCK_ALERT_IMAGE_TAG` (default `local`, set a retained rollback tag only when intentionally rolling back). The image sets `STOCK_ALERT_CONTAINER=true` internally to reject nonpersistent database paths outside `/app/data`; do not override it. Admin IDs must be a subset of allowed user IDs. Production rejects the schedule bypass, in-memory storage and DEBUG request logging.

Remaining risks: the unverified live-book contract still prevents live alerts; target-VPS connectivity/permissions and target architecture require operator verification. Delivery remains at least once, not exactly once. Events/outbox grow until deliberately managed; monitor disk space and retain tested off-host backups. No new market condition, infrastructure service or deployment is added in Phase 6A.
