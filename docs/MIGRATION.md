# Upgrade from Phase 1

This document records the historical Telegram schema upgrades. The Bale version starts with a fresh database and uses `BALE_*` settings; do not copy Telegram identities or pending deliveries. See [Bale setup](../README.md#bale-connection-and-fresh-setup). Applied migrations and historical SQL names remain intact.

Back up the stopped database volume before updating. Keep the volume and initial migration. Startup applies `0001_multi_user.sql`: it creates users/watchlists and adds a nullable owner foreign key/index to alert rules. No data is deleted or assigned to a guessed owner.

Replace `TELEGRAM_ALLOWED_USER_ID` with `TELEGRAM_ALLOWED_USER_IDS=123456789,987654321,456789123` in `.env`. Remove the old variable entirely; startup rejects it. Each user must initiate a private conversation. Their observed chat ID and individual preferences are then registered. Preferences default on, but no notifications are sent in Phase 2.

Old symbols/snapshots remain shared metadata/history. Existing rules retain all values, including enabled/armed state, but their owner is NULL. They and their events are quarantined: user-scoped queries and notification targeting cannot reach them. Watchlists start empty even with only one allowlisted user. Existing application state is retained and is not exposed through commands.

Confirmed owners can reconstruct their watchlists with `/add`. This does not claim historical rules. If the operator independently establishes a rule's owner, explicitly assign that individual rule after the owner initiates a private conversation. Stop the bot, back up, open the database with a SQLite client and replace every placeholder below with reviewed IDs. Repeat only for individually confirmed records:

```sql
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
SELECT * FROM users WHERE telegram_user_id = 'CONFIRMED_USER_ID';
SELECT * FROM alert_rules WHERE id = 'CONFIRMED_RULE_ID' AND owner_id IS NULL;

INSERT INTO watchlists (user_id, ins_code, created_at)
SELECT u.telegram_user_id, r.ins_code, CAST(strftime('%s', 'now') AS INTEGER) * 1000
FROM users AS u, alert_rules AS r
WHERE u.telegram_user_id = 'CONFIRMED_USER_ID'
  AND r.id = 'CONFIRMED_RULE_ID' AND r.owner_id IS NULL
ON CONFLICT (user_id, ins_code) DO NOTHING;

UPDATE alert_rules SET owner_id = 'CONFIRMED_USER_ID'
WHERE id = 'CONFIRMED_RULE_ID' AND owner_id IS NULL
  AND EXISTS (SELECT 1 FROM users WHERE telegram_user_id = 'CONFIRMED_USER_ID');
COMMIT;
PRAGMA foreign_key_check;
```

Events inherit ownership through their rule; do not rewrite IDs. If ownership cannot be established, leave records unassigned. Never assign everything to the first allowlisted user. Removing a user from configuration revokes access and notification-target resolution without deleting their history.

Phase 3 subsequently applies `0002_snapshot_collection.sql`, adding `collected_at` and `symbol` to snapshots. Existing records keep NULL provenance, are not used as verified historical baselines and remain subject to configured history retention. User records, preferences, memberships, rules and events are unchanged. Configure explicit sessions in `.env`; the collector defaults to closed rather than assuming exchange hours/holidays.

## Phase 4

`0003_alert_engine.sql` creates `alert_states` and `notification_outbox`, adds rule `definition`/`revision`, snapshot `session_id`, and event payload/revision/unique occurrence-key columns. It is additive: no table rebuild, DROP or deletion of existing records. Old snapshot session IDs remain NULL; no trading-session provenance is invented. New collector snapshots record the actual configured session. Foreign keys link state to rule and outbox to event with cascading deletion. Due-message and unique occurrence indexes support delivery and deduplication.

Already owned legacy rules receive a JSON definition built from their existing settings: drop threshold/window/minimum, or disappearance minimum previous volume. Their owners, enabled settings, legacy armed fields, preferences and events are preserved. New runtime states are primed on the first eligible update rather than replaying old triggers. NULL-owner records retain NULL definitions and remain quarantined. Existing events retain their messages and NULL payloads; they are never automatically enqueued for delivery.

The original constrained rule columns are retained as compatibility data. **`definition` is now authoritative**, including the alert type; new rules use the valid `BUY_QUEUE_DISAPPEARED` sentinel in the old `type` column. Legacy threshold/window/minimum/armed fields are not used by the engine. This avoids rebuilding parent tables and risking cascade deletion, while allowing future definitions without additional SQL type checks or columns. Do not query the old type field to infer a new rule's behavior. Read and validate the JSON instead.

If assigning a quarantined rule after Phase 4, the ownership SQL above alone does not enable it in the new engine: leave the historical rule preserved, and have the confirmed owner recreate the desired validated configuration using `/alert`. Do not manufacture a definition for an unknown owner or replay historical events. Invalid JSON definitions are excluded from management/evaluation until repaired through a reviewed service operation.

Back up the stopped database before the upgrade; retain the named volume and applied migrations. Startup migrates automatically. Verify `PRAGMA foreign_key_check;` returns no rows. Configure the new defaults from `.env.example`. No live events can be produced until the upstream book timestamp/completeness limitations in `TSETMC.md` are resolved.

## Phase 5

`0004_channel_alerts.sql` creates `telegram_channels` (string primary key, title, posting-permission status/check time and safe error code), adds nullable `alert_rules.channel_id` with a channel foreign key, indexes channel/enabled rule queries, and adds nullable `alert_states.recovery_data`. The generated migration uses only CREATE/ALTER ADD: no table rebuild, deletion, ownership reassignment or reset of existing states/events/outbox. A populated Phase 4 migration test verifies preservation of private configuration, preferences, snapshots, frozen state, dedup keys and retry counts.

Existing rules receive `channel_id = NULL`, meaning their original private owner; existing recovery data remains NULL and private evaluation is unchanged. Channel rules retain an admin creator in `owner_id` and reference a channel explicitly. Each rule has exactly one destination: original private owner for NULL channel, configured channel otherwise. Private service queries now require NULL channel, so membership/rule removal cannot cascade channel rules accidentally. Foreign keys and per-rule event/state/outbox cascades remain enabled.

Validated composite definitions reuse the authoritative JSON `definition` column; no per-condition SQL columns were added. Group recovery stores copied satisfied-leaf evidence in `recovery_data` and survives restart/normal snapshot retention. Edits reset only the edited rule's state and cancel its pending messages through the existing revision mechanism. Legacy JSON definitions, enabled flags, cooldowns, last evaluated source times and pending private delivery claims are preserved.

Startup upserts metadata only for the configured channel; other stored channels/rules remain untouched but are not eligible while unconfigured. Removing an admin suspends rules that they created; a current admin may explicitly edit/adopt a rule. Do not reassign old owners automatically. Channel delivery success/failure metadata uses channel-specific `application_state` keys and contains no credentials; last delivery statistics remain available after rule deletion.

Leave `TELEGRAM_ADMIN_USER_IDS` and `TELEGRAM_CHANNEL_ID` blank for private-only operation, or configure both, grant the bot posting permission, and initiate private admin conversations. No subscriber records or new infrastructure are needed. Back up the stopped volume, retain all migrations and verify foreign-key integrity as above. This phase does not deploy the application or fix the unverified TSETMC source contract.

## Phase 6A operational changes

No schema migration is introduced. Existing rules, states, events, snapshots and pending outbox records are preserved. Application startup and the dedicated migration CLI now acquire a separate OS-backed SQLite instance lock beside the configured database; it does not hold a transaction on the application's WAL database. A second app or manual migrator cannot own the same database path concurrently. Stop the bot before `node dist/database/migrate.js`; crash/exit releases the OS lock without deleting files. Use a persistent local volume and one process per bot token.

Use the online backup and new-path restore procedures in [DEPLOYMENT.md](../DEPLOYMENT.md). Do not copy a live database file without its WAL context. Backup artifacts are integrity/foreign-key checked and finalized in DELETE journal mode so a single backup file can be mounted read-only during restore. Restore never overwrites existing DB/WAL/SHM targets or restores automatically. Preserve original files and pair schema-compatible images/backups when rolling back. Admin IDs must now also appear in the authorized user list; update .env before starting the new release. Production refuses schedule bypass, DEBUG logging and in-memory databases.
