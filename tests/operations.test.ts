import {
  mkdtempSync,
  rmSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  utimesSync,
  readdirSync,
  copyFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import { afterEach, expect, it } from 'vitest';
import Database from 'better-sqlite3';
import {
  backupDatabase,
  restoreDatabase,
  pruneBackups,
  backupPrefix,
} from '../src/database/backup.js';
import { acquireInstanceLock } from '../src/database/instance-lock.js';
import { parseEnv } from '../src/config/env.js';

const directories: string[] = [];
const temporary = () => {
  const directory = mkdtempSync(join(tmpdir(), 'tse-operations-'));
  directories.push(directory);
  return directory;
};
afterEach(() => {
  for (const directory of directories.splice(0))
    rmSync(directory, { recursive: true, force: true });
});
it('backs up a live WAL database consistently, uses unique files, restores only to a new path and prunes only owned expired backups', async () => {
  const directory = temporary();
  const source = join(directory, 'live.db');
  const backups = join(directory, 'backups');
  mkdirSync(backups);
  const db = new Database(source);
  try {
    db.pragma('journal_mode=WAL');
    db.pragma('wal_autocheckpoint=0');
    db.exec(
      "CREATE TABLE events(id INTEGER PRIMARY KEY, value TEXT NOT NULL); INSERT INTO events VALUES(1,'persisted-outbox')",
    );
    const first = await backupDatabase(source, backups);
    const second = await backupDatabase(source, backups);
    expect(first).not.toBe(second);
    expect(readFileSync(`${source}-wal`).length).toBeGreaterThan(0);
    const restored = join(directory, 'restored.db');
    await restoreDatabase(first, restored);
    const result = new Database(restored);
    try {
      expect(result.prepare('SELECT value FROM events').get()).toEqual({
        value: 'persisted-outbox',
      });
      expect(result.pragma('integrity_check', { simple: true })).toBe('ok');
      expect(result.pragma('journal_mode', { simple: true })).toBe('delete');
    } finally {
      result.close();
    }
    await expect(restoreDatabase(first, restored)).rejects.toThrow(
      'new absolute',
    );
    expect(db.prepare('SELECT count(*) AS count FROM events').get()).toEqual({
      count: 1,
    });
    const unrelated = join(backups, 'unrelated.db');
    writeFileSync(unrelated, 'keep');
    const expired = new Date(Date.now() - 15 * 86_400_000);
    utimesSync(first, expired, expired);
    utimesSync(unrelated, expired, expired);
    expect(pruneBackups(source, backups, 14)).toBe(1);
    expect(readdirSync(backups)).toEqual(
      expect.arrayContaining([second.split(/[\\/]/).at(-1), 'unrelated.db']),
    );
    expect(backupPrefix(source)).toMatch(/^stock-alert-[a-f\d]{12}-$/);
    await expect(backupDatabase('relative.db', backups)).rejects.toThrow(
      'absolute',
    );
  } finally {
    db.close();
  }
});
it('blocks concurrent application/maintenance ownership and releases the OS lock on process death', async () => {
  const path = join(temporary(), 'app.db');
  const release = acquireInstanceLock(path);
  try {
    expect(() => acquireInstanceLock(path)).toThrow('already owned');
  } finally {
    release();
  }
  const program = `import {acquireInstanceLock} from ${JSON.stringify(pathToFileURL(resolve('src/database/instance-lock.ts')).href)}; const release=acquireInstanceLock(${JSON.stringify(path)}); console.log('locked'); setInterval(()=>void release,1000);`;
  const child = spawn(
    process.execPath,
    ['--import', 'tsx', '--input-type=module', '-e', program],
    { stdio: ['ignore', 'pipe', 'pipe'] },
  );
  try {
    await new Promise<void>((resolveReady, reject) => {
      let output = '';
      child.stdout.on('data', (chunk: Buffer) => {
        output += chunk.toString();
        if (output.includes('locked')) resolveReady();
      });
      child.once('error', reject);
      child.once('exit', (code) => {
        if (code) reject(new Error('Lock child failed'));
      });
    });
    expect(() => {
      const unexpected = acquireInstanceLock(path);
      unexpected();
    }).toThrow('already owned');
  } finally {
    child.kill('SIGKILL');
    await new Promise((done) => child.once('close', done));
  }
  acquireInstanceLock(path)();
});
it('rejects unauthorized admins and production bypasses without echoing the token', () => {
  const required = {
    BALE_BOT_TOKEN: `123456:${'x'.repeat(35)}`,
    BALE_ALLOWED_USER_IDS: '123',
  };
  for (const invalid of [
    { BALE_ADMIN_USER_IDS: '456', BALE_CHANNEL_ID: '-1001234567890' },
    { NODE_ENV: 'production', MARKET_MONITOR_FORCE_ACTIVE: 'true' },
    { NODE_ENV: 'production', DATABASE_URL: ':memory:' },
    { NODE_ENV: 'production', DEBUG: '*' },
  ])
    expect(() => parseEnv({ ...required, ...invalid })).toThrow();
});
it('prints only safe configuration metadata in diagnostics, never IDs or secrets', () => {
  const token = `123456:${'x'.repeat(35)}`;
  const result = spawnSync(
    process.execPath,
    ['--import', 'tsx', 'src/diagnostics.ts'],
    {
      encoding: 'utf8',
      env: {
        ...process.env,
        NODE_ENV: 'production',
        BALE_BOT_TOKEN: token,
        BALE_ALLOWED_USER_IDS: '123456789',
        BALE_ADMIN_USER_IDS: '123456789',
        BALE_CHANNEL_ID: '-1001234567890',
        MARKET_MONITOR_FORCE_ACTIVE: 'false',
        DATABASE_URL: './data/stock-alert.db',
        DEBUG: '',
      },
    },
  );
  expect(result.status).toBe(0);
  expect(JSON.parse(result.stdout)).toMatchObject({
    configuration: 'VALID',
    administrators: 1,
    channelConfigured: true,
    liveBookContractVerified: false,
  });
  expect(result.stdout).not.toContain(token);
  expect(result.stdout).not.toContain('123456789');
});
it.skipIf(process.platform === 'win32')(
  'checks Ubuntu helper syntax and refuses unsafe operations before touching Docker resources',
  () => {
    const root = temporary();
    const scripts = join(root, 'scripts');
    const bin = join(root, 'bin');
    mkdirSync(scripts);
    mkdirSync(bin);
    for (const name of [
      'compose-common.sh',
      'backup-db.sh',
      'restore-db.sh',
      'deploy.sh',
      'healthcheck.sh',
    ]) {
      const source = resolve('scripts', name);
      expect(spawnSync('bash', ['-n', source]).status).toBe(0);
      copyFileSync(source, join(scripts, name));
    }
    writeFileSync(join(root, '.env'), 'FAKE_TEST_ONLY=yes\n');
    writeFileSync(join(root, 'compose.yaml'), 'services: {}\n');
    const log = join(root, 'docker.log');
    writeFileSync(
      join(bin, 'docker'),
      '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$DOCKER_TEST_LOG"\nif [[ "$*" == "context inspect"* ]]; then echo unix:///var/run/docker.sock; fi\nif [[ "$*" == *"ps --status running"* ]]; then echo running-bot; fi\n',
      { mode: 0o700 },
    );
    const env = {
      ...process.env,
      PATH: `${bin}:${process.env.PATH ?? ''}`,
      DOCKER_TEST_LOG: log,
    };
    const backup = join(root, 'backup.db');
    writeFileSync(backup, 'not-opened');
    expect(
      spawnSync(
        'bash',
        [
          join(scripts, 'restore-db.sh'),
          backup,
          '/app/data/new.db',
          '--confirm-restore',
        ],
        { env },
      ).status,
    ).toBe(1);
    expect(readFileSync(log, 'utf8')).not.toContain('run --rm');
    expect(
      spawnSync(
        'bash',
        [join(scripts, 'deploy.sh'), root, '/app/data/app.db'],
        { env },
      ).status,
    ).toBe(1);
    expect(
      spawnSync(
        'bash',
        [join(scripts, 'backup-db.sh'), root, '/app/data/../outside.db'],
        { env },
      ).status,
    ).toBe(1);
  },
);
