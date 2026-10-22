# Trade Alert

Python 3.14+ Telegram bot for private, channel and composite queue alerts.
The Python app and tests now live at the repository root. Node has been removed.
Runtime: Telethon 1.45.0 (bot-only MTProto), HTTPX, SQLite and JSON logging.
There is no web server or exposed port.

**Market limitation:** BestLimits has no verified source timestamp/completeness
contract, so current live data cannot trigger alerts. Synthetic offline tests
verify the engine and delivery. See [TSETMC observations](docs/TSETMC.md).

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

Optional `TELEGRAM_MTPROXY_URL` accepts the complete `tg://proxy?...` or
`https://t.me/proxy?...` link, including plain, dd or ee/Fake-TLS hex secrets.
Fake-TLS SNI is limited to 220 bytes. Only Telegram uses this proxy; failures
never fall back to a direct connection. Leave blank for direct access. Keep the
link secret. Protocol tests use synthetic replies without opening sockets.

Commands: `/start`, `/help`, `/add`, `/list`, `/queue`, `/remove`, `/status`,
`/alert`, `/alerts`, `/history`, `/cancel`, and admin-only `/admin`.
Private preferences and ownership remain separate from channel rules.

Basic groups and supergroups can also be the single configured shared destination
in `TELEGRAM_CHANNEL_ID`. Make the bot an administrator. Configured authorized
admins must first `/start` privately; they can then use `/admin`, `/alert`, `/alerts`,
`/status`, `/cancel`, `/start` and `/help` in that group. Draft text must reply to
the current bot prompt. Personal commands/data and delivery-error details remain
private; other group members cannot operate the bot. See [group setup](DEPLOYMENT.fa.md).

In private `/admin`, configured admins can compose a manual text message for the
shared destination, preview it and confirm sending. Text is limited to 1,500
characters; sends recheck bot permission and current admin authorization and have
no automatic retry. Optional shared symbol notes (500 characters) can be edited
after `/add` or in the shared alert wizard. Notes are stored per destination and
instrument and appended only to shared alerts at delivery, never private alerts.
Both editors expire after ten minutes/restart and support preview/cancel.
The additive `0005_channel_symbol_notes.sql` migration preserves existing data.

Original migrations and journal live unchanged under `migrations/`. Startup and
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
