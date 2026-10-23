# Trade Alert

Python 3.14+ Telegram bot for private, channel and composite queue alerts.
The Python app and tests now live at the repository root. Node has been removed.
Runtime: Telethon 1.45.0 (bot-only MTProto), HTTPX, SQLite and JSON logging.
There is no web server or exposed port.

**Market limitation:** BestLimits has no verified source timestamp/completeness
contract, so current live data cannot trigger alerts. Synthetic offline tests
verify the engine and delivery. See [TSETMC observations](docs/TSETMC.md).

Phase 8 adds inclusive buy/sell queue-volume thresholds, drops from the highest
observed session volume, and 1/5/15-minute historical comparisons. Private and
shared-destination wizards support the strategies; channel AND/OR rules reuse them.
Crossing readiness, session peaks and frozen recovery survive restart. Existing
definitions and Phase 7 quality/outcomes remain compatible. See
[configuration, Persian examples and limitations](docs/QUEUE-STRATEGIES.md) and the
[manual Phase 8 upgrade](DEPLOYMENT.md#manual-phase-8-upgrade).

Phase 9 tracks confirmed queue formation, weakening, disappearance and reformation
once per instrument/side. It adds Persian lifecycle rule choices and admin-only
private `/lifecycle` summaries. Transitions are source-update pulses in composites;
gaps, halts, closures and first observations cannot manufacture transitions.
Shared confirmation settings and lifecycle history retention are in `.env.example`.
See [lifecycle semantics and examples](docs/LIFECYCLE.md) and the
[manual Phase 9 upgrade](DEPLOYMENT.md#manual-phase-9-upgrade).

## Deploy

Follow the [Persian VPS guide](DEPLOYMENT.fa.md) for a fresh install with an
MTProto proxy, group setup, backup and new-path restore. Docker builds from
the root Dockerfile. The server needs Docker/Compose and Git, not host Python/Node.
Keep the Compose project name stable after installation; run one bot instance only.

## Develop and verify

From the repository root on Windows (Python 3.14+):

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m unittest discover -s tests -v
.venv/Scripts/python -m mypy
.venv/Scripts/python -m ruff check trade_alert tests
.venv/Scripts/python -m ruff format --check trade_alert tests
.venv/Scripts/python -m pip check
.venv/Scripts/python -m compileall -q trade_alert tests
docker build -t tse-stock-alert-bot:local .
.venv/Scripts/python tests/smoke_image.py
```

On Linux use `.venv/bin/python`. Tests use fake clients and temporary databases;
the image test uses network-disabled disposable containers and a UUID volume.
The independent [frozen TypeScript outputs](tests/fixtures/reference/README.md)
retain regression coverage without Node. Never regenerate expectations from Python.

## Run and configure

`python -m trade_alert` explicitly connects to real services. It reads process
environment and does not load `.env` automatically. Compose loads the root `.env`.
Copy [.env.example](.env.example) and supply the bot token, API ID/hash and allowed
user IDs. Admins must be authorized and privately registered. `NODE_ENV` retains
its original name for compatibility. Empty sessions (`[]`) mean closed.
For direct local execution use an absolute local `DATABASE_URL`.

With `TELEGRAM_PROXY_AUTO=true` and an empty `TELEGRAM_MTPROXY_URL`, the bot
downloads two public MTProxy lists and tries bounded, sequential connections.
It reuses Telethon's initial MTProto handshake before selecting a route and
rotates on connection failure. The example environment enables this mode;
missing `TELEGRAM_PROXY_AUTO` defaults to false for compatibility.
A manual URL takes priority and accepts `tg://proxy?...` or `https://t.me/proxy?...`,
including hex and URL-safe Base64 plain, dd or ee/Fake-TLS secrets. Fake-TLS SNI
is limited to 220 bytes. Only Telegram uses the proxy; no direct fallback occurs.
For explicit direct access, set auto=false and leave the URL blank. Public
list/proxy availability from Iran is not guaranteed. Tests use synthetic HTTP,
DNS, socket and MTProto replies without contacting real proxies.

Commands: `/start`, `/help`, `/add`, `/list`, `/queue`, `/remove`, `/status`,
`/alert`, `/alerts`, `/history`, `/cancel`, and admin-only `/admin` and `/stats`.
Private preferences and ownership remain separate from channel rules.

Basic groups and supergroups can also be the single configured shared destination
in `TELEGRAM_CHANNEL_ID`. Make the bot an administrator. Configured authorized
admins must first `/start` privately and manage rules with `/admin` in private.
All group/supergroup commands, callbacks and draft text are rejected before
registration or handlers. The configured group is a delivery destination only.
See [group setup](DEPLOYMENT.fa.md).

In private `/admin`, configured admins can compose a manual text message for the
shared destination, preview it and confirm sending. Text is limited to 1,500
characters; sends recheck bot permission and current admin authorization and have
no automatic retry. Optional shared symbol notes (500 characters) can be edited
after `/add` or in the shared alert wizard. Notes are stored per destination and
instrument and appended only to shared alerts at delivery, never private alerts.
Both editors expire after ten minutes/restart and support preview/cancel.
The additive `0005_channel_symbol_notes.sql` migration preserves existing data.

Original migration SQL and journal entries are preserved under `migrations/`;
new migrations append entries. Startup and
manual migration share an OS-backed instance lock. The image runs as `node`
(UID/GID 1000) for existing-volume compatibility; it contains Python, not Node.
SQLite lives at `/app/data/stock-alert.db` in the existing named volume.

```sh
python -m trade_alert.health
python -m trade_alert.maintenance migrate /absolute/stopped.db
python -m trade_alert.maintenance backup /absolute/source.db /absolute/backups
python -m trade_alert.maintenance restore /absolute/backup.db /absolute/new.db --confirm-restore
```

Health requires independent monitor progress, database write availability and
recent MTProto responses. Backups use online SQLite backup with integrity/FK
checks. Restore never overwrites a path or starts the bot. Delivery is at least
once and can duplicate accepted messages after a crash before success recording.

The local-only `python -m trade_alert.operations` helper supports backup, deploy,
restore and health. Deploy requires explicit confirmation and an existing Python
image; follow the VPS guide for the first installation.
See [migration history](MIGRATION.fa.md) and [behavior coverage](ACCEPTANCE.fa.md).

## Phase 7 alert quality

Existing rules keep normal severity, one message per recovered/rearmed occurrence,
escalation off and outcome tracking off. Admins can open **advanced settings**
from shared-rule details or their own private-rule details. Choose base severity,
optional drop thresholds, escalation interval/additional drop, cooldown and outcome
tracking; confirm the displayed summary to save. Drafts expire after ten minutes
or restart and reject stale edit revisions. Ordinary creation stays unchanged.

Escalation uses the original occurrence's frozen per-leaf baseline, respects rule
cooldown and its own interval, and requires the configured additional percentage
points since the previous message. Composite rules must remain overall TRUE;
UNKNOWN cannot rearm or escalate. Severity is a configurable notification priority,
not a market-validated risk measure or trading recommendation. Messages use Persian
numbers, large-number labels, Tehran source/announcement times and delayed-data
notices. Telegram parse mode is disabled, so names/notes are literal text.

Opted-in events schedule independent 5/15/30-minute queue observations from source
time, using existing accepted snapshots and polling cadence. The first same-session
snapshot in `[target, target + tolerance]` is eligible; prices are unavailable because
the pipeline has queue prices, not verified traded prices. No price return is
fabricated. Pending symbols stay in the shared monitor after watchlist/rule removal.
Outcomes expire when the session/window ends without evidence; out-of-session targets
are immediately insufficient. Restart can recover a timely persisted observation.
Outcome/suppression retention reuses bounded, yielding cleanup and leaves alert
history/configuration intact. Private `/history` and admin shared history paginate;
`/stats` reports actual 30-day channel aggregates and explicit missing-price samples.

See [Phase 7 semantics and limitations](docs/ALERT-QUALITY.md),
[additive migration](docs/MIGRATION.md#phase-7), and the
[manual upgrade procedure](DEPLOYMENT.md). No production deployment is automated.
