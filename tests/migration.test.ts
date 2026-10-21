import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import Database from 'better-sqlite3';
import { readMigrationFiles } from 'drizzle-orm/migrator';
import { expect, it } from 'vitest';
import { openDatabase } from '../src/database/client.js';
import {
  alertRules,
  queueSnapshots,
  symbols,
  users,
  watchlists,
} from '../src/database/schema.js';
import { UserService } from '../src/users/user-service.js';
import {
  alertEvents,
  alertStates,
  notificationOutbox,
} from '../src/database/schema.js';

it('preserves Phase 1/2 data and known owners without claiming legacy records', () => {
  const directory = mkdtempSync(join(tmpdir(), 'tse-upgrade-'));
  const path = join(directory, 'legacy.db');
  const old = new Database(path);
  try {
    const migrationsFolder = new URL(
      '../src/database/migrations/',
      import.meta.url,
    );
    const migrations = readMigrationFiles({
      migrationsFolder: fileURLToPath(migrationsFolder),
    });
    const first = migrations[0];
    if (!first) throw new Error('Initial migration missing');
    // Reproduce the committed Phase 1 schema and its Drizzle migration ledger.
    old.transaction(() => {
      for (const statement of first.sql) old.exec(statement);
      old.exec(
        'CREATE TABLE __drizzle_migrations (id INTEGER PRIMARY KEY, hash TEXT NOT NULL, created_at NUMERIC)',
      );
      old
        .prepare(
          'INSERT INTO __drizzle_migrations (hash,created_at) VALUES (?,?)',
        )
        .run(first.hash, first.folderMillis);
    })();
    old
      .prepare(
        'INSERT INTO symbols (ins_code,symbol,created_at) VALUES (?,?,?)',
      )
      .run('65883838195688438', 'خودرو', 1);
    old
      .prepare(
        "INSERT INTO alert_rules (id,ins_code,type,enabled,armed,created_at) VALUES ('legacy-rule','65883838195688438','BUY_QUEUE_DISAPPEARED',1,0,1)",
      )
      .run();
    old
      .prepare(
        "INSERT INTO queue_snapshots (ins_code,timestamp,queue_type,volume,price,order_count) VALUES ('65883838195688438',1,'buy',100,110,2)",
      )
      .run();
    old
      .prepare(
        "INSERT INTO alert_events (id,rule_id,message,created_at) VALUES ('legacy-event','legacy-rule','old',1)",
      )
      .run();
    old
      .prepare(
        "INSERT INTO application_state (key,value,updated_at) VALUES ('legacy-state','retained',1)",
      )
      .run();
    const second = migrations[1];
    if (!second) throw new Error('Phase 2 migration missing');
    old.transaction(() => {
      for (const statement of second.sql) old.exec(statement);
      old
        .prepare(
          'INSERT INTO __drizzle_migrations (hash,created_at) VALUES (?,?)',
        )
        .run(second.hash, second.folderMillis);
      old
        .prepare(
          "INSERT INTO users (telegram_user_id,private_chat_id,notifications_enabled,created_at,updated_at) VALUES ('456','456',0,1,1)",
        )
        .run();
      old
        .prepare(
          "INSERT INTO watchlists (user_id,ins_code,created_at) VALUES ('456','65883838195688438',1)",
        )
        .run();
      old
        .prepare(
          "INSERT INTO alert_rules (id,owner_id,ins_code,type,created_at) VALUES ('owned-rule','456','65883838195688438','SELL_QUEUE_DISAPPEARED',1)",
        )
        .run();
    })();
    old.close();
    const upgraded = openDatabase(path);
    try {
      expect(upgraded.db.select().from(symbols).all()).toHaveLength(1);
      expect(upgraded.db.select().from(queueSnapshots).all()).toHaveLength(1);
      expect(upgraded.db.select().from(queueSnapshots).get()).toMatchObject({
        collectedAt: null,
        symbol: null,
      });
      expect(upgraded.db.select().from(alertRules).get()).toMatchObject({
        ownerId: null,
        enabled: true,
        armed: false,
      });
      expect(
        upgraded.sqlite.prepare('SELECT message FROM alert_events').get(),
      ).toEqual({ message: 'old' });
      expect(
        upgraded.sqlite.prepare('SELECT value FROM application_state').get(),
      ).toEqual({ value: 'retained' });
      expect(upgraded.db.select().from(users).get()).toMatchObject({
        telegramUserId: '456',
        privateChatId: '456',
        notificationsEnabled: false,
      });
      expect(upgraded.db.select().from(watchlists).get()).toMatchObject({
        userId: '456',
        insCode: '65883838195688438',
      });
      expect(
        upgraded.sqlite
          .prepare("SELECT owner_id FROM alert_rules WHERE id = 'owned-rule'")
          .get(),
      ).toEqual({ owner_id: '456' });
      expect(upgraded.sqlite.pragma('foreign_key_check')).toEqual([]);
      const definition = upgraded.sqlite
        .prepare("SELECT definition FROM alert_rules WHERE id='owned-rule'")
        .get() as { definition: string };
      expect(JSON.parse(definition.definition)).toEqual({
        type: 'SELL_QUEUE_DISAPPEARED',
        config: { minimumPreviousVolume: 1 },
      });
      expect(
        upgraded.db.select().from(alertRules).get()?.definition,
      ).toBeNull();
      const service = new UserService(upgraded.db, ['123', '456']);
      service.register('123', '123');
      expect(service.listRules('123')).toEqual([]);
      expect(service.listSymbols('123')).toEqual([]);
      expect(service.notificationTarget('123', 'legacy-rule')).toBeNull();
      expect(
        readFileSync(
          new URL(
            '../src/database/migrations/0001_multi_user.sql',
            import.meta.url,
          ),
          'utf8',
        ),
      ).not.toContain('DROP');
    } finally {
      upgraded.sqlite.close();
    }
  } finally {
    if (old.open) old.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

it('upgrades a populated Phase 4 database without resetting private rules, state or pending deliveries', () => {
  const directory = mkdtempSync(join(tmpdir(), 'tse-phase4-upgrade-'));
  const path = join(directory, 'phase4.db');
  const old = new Database(path);
  try {
    const migrations = readMigrationFiles({
      migrationsFolder: fileURLToPath(
        new URL('../src/database/migrations/', import.meta.url),
      ),
    });
    old.exec(
      'PRAGMA foreign_keys=ON; CREATE TABLE __drizzle_migrations (id INTEGER PRIMARY KEY, hash TEXT NOT NULL, created_at NUMERIC)',
    );
    for (const migration of migrations.slice(0, 4))
      old.transaction(() => {
        for (const statement of migration.sql) old.exec(statement);
        old
          .prepare(
            'INSERT INTO __drizzle_migrations(hash,created_at) VALUES (?,?)',
          )
          .run(migration.hash, migration.folderMillis);
      })();
    old.exec(`
      INSERT INTO users VALUES ('123','123',0,1,1);
      INSERT INTO symbols VALUES ('12345678901234567890','synthetic',1);
      INSERT INTO watchlists VALUES ('123','12345678901234567890',1);
      INSERT INTO alert_rules(id,owner_id,ins_code,type,definition,revision,created_at) VALUES ('phase4-rule','123','12345678901234567890','BUY_QUEUE_DISAPPEARED','{"type":"BUY_QUEUE_DISAPPEARED","config":{"minimumPreviousVolume":0}}',7,1);
      INSERT INTO queue_snapshots(ins_code,timestamp,collected_at,symbol,session_id,queue_type,volume,price,order_count) VALUES ('12345678901234567890',100,101,'synthetic','session-A','buy',1000,110,1);
      INSERT INTO alert_states VALUES ('phase4-rule',7,'session-A','WAITING_FOR_RECOVERY',100,99,1000);
      INSERT INTO alert_events(id,rule_id,message,created_at,occurrence_key,payload,rule_revision) VALUES ('phase4-event','phase4-rule','retained',100,'original-occurrence','{}',7);
      INSERT INTO notification_outbox(event_id,status,attempts,next_attempt_at,lease_until,lease_token,error_code) VALUES ('phase4-event','PENDING',2,200,NULL,NULL,'TRANSPORT');
    `);
    old.close();
    const upgraded = openDatabase(path);
    try {
      expect(upgraded.db.select().from(alertRules).get()).toMatchObject({
        id: 'phase4-rule',
        channelId: null,
        revision: 7,
      });
      expect(upgraded.db.select().from(alertStates).get()).toMatchObject({
        status: 'WAITING_FOR_RECOVERY',
        baselineVolume: 1000,
        lastEvaluatedAt: 100,
        recoveryData: null,
      });
      expect(upgraded.db.select().from(notificationOutbox).get()).toMatchObject(
        {
          status: 'PENDING',
          attempts: 2,
          nextAttemptAt: 200,
          errorCode: 'TRANSPORT',
        },
      );
      expect(upgraded.db.select().from(alertEvents).get()).toMatchObject({
        occurrenceKey: 'original-occurrence',
        ruleRevision: 7,
      });
      expect(upgraded.db.select().from(users).get()?.notificationsEnabled).toBe(
        false,
      );
      expect(upgraded.db.select().from(watchlists).all()).toHaveLength(1);
      expect(upgraded.db.select().from(queueSnapshots).get()?.sessionId).toBe(
        'session-A',
      );
      expect(upgraded.sqlite.pragma('foreign_key_check')).toEqual([]);
    } finally {
      upgraded.sqlite.close();
    }
  } finally {
    if (old.open) old.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
