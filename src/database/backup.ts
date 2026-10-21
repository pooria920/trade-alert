import { randomUUID, createHash } from 'node:crypto';
import {
  closeSync,
  existsSync,
  lstatSync,
  openSync,
  readdirSync,
  realpathSync,
  rmSync,
  statSync,
} from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import Database from 'better-sqlite3';
import { acquireInstanceLock } from './instance-lock.js';

function sourcePath(path: string) {
  if (!isAbsolute(path) || !statSync(path).isFile())
    throw new Error('Source must be an existing absolute file path');
  return realpathSync(path);
}
async function copyDatabase(source: string, target: string) {
  if (!isAbsolute(target) || !statSync(dirname(target)).isDirectory())
    throw new Error('Target requires an absolute path and existing directory');
  const handle = openSync(target, 'wx', 0o600);
  closeSync(handle);
  let db: Database.Database | undefined;
  try {
    db = new Database(source, { readonly: true, fileMustExist: true });
    db.pragma('busy_timeout=5000');
    if (db.pragma('quick_check', { simple: true }) !== 'ok')
      throw new Error('Source integrity check failed');
    await db.backup(target);
    const result = new Database(target, { fileMustExist: true });
    try {
      // Portable backup artifact: no WAL/SHM sidecars required on a read-only backup mount.
      result.pragma('journal_mode=DELETE');
      if (
        result.pragma('integrity_check', { simple: true }) !== 'ok' ||
        (result.pragma('foreign_key_check') as unknown[]).length
      )
        throw new Error('Backup integrity check failed');
    } finally {
      result.close();
    }
  } catch (error) {
    rmSync(target, { force: true });
    throw error;
  } finally {
    db?.close();
  }
}
export function backupPrefix(source: string) {
  return `stock-alert-${createHash('sha256').update(sourcePath(source)).digest('hex').slice(0, 12)}-`;
}
export async function backupDatabase(
  source: string,
  directory: string,
  now = Date.now(),
) {
  const path = sourcePath(source);
  if (!isAbsolute(directory) || !statSync(directory).isDirectory())
    throw new Error('Backup directory must already exist and be absolute');
  const target = join(
    realpathSync(directory),
    `${backupPrefix(path)}${new Date(now).toISOString().replace(/[:.]/g, '-')}-${randomUUID()}.db`,
  );
  await copyDatabase(path, target);
  return target;
}
export function pruneBackups(
  source: string,
  directory: string,
  days: number,
  now = Date.now(),
) {
  if (
    !isAbsolute(directory) ||
    !Number.isInteger(days) ||
    days < 1 ||
    days > 3650
  )
    throw new Error(
      'Use an absolute backup directory and 1–3650 retention days',
    );
  const prefix = backupPrefix(source);
  let removed = 0;
  for (const name of readdirSync(directory)) {
    if (
      !name.startsWith(prefix) ||
      !/^stock-alert-[a-f\d]{12}-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-\d{3}Z-[a-f\d-]{36}\.db$/.test(
        name,
      )
    )
      continue;
    const path = join(directory, name);
    const info = lstatSync(path);
    if (
      info.isFile() &&
      !info.isSymbolicLink() &&
      now - info.mtimeMs > days * 86_400_000
    ) {
      rmSync(path);
      removed++;
    }
  }
  return removed;
}
export async function restoreDatabase(source: string, target: string) {
  const path = sourcePath(source);
  if (
    !isAbsolute(target) ||
    existsSync(target) ||
    existsSync(`${target}-wal`) ||
    existsSync(`${target}-shm`) ||
    resolve(target) === path
  )
    throw new Error(
      'Restore requires a new absolute database path with no existing database or WAL/SHM files',
    );
  const release = acquireInstanceLock(target);
  try {
    await copyDatabase(path, target);
  } finally {
    release();
  }
  return target;
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  const [operation, source, target, retention, confirmation] =
    process.argv.slice(2);
  try {
    if (!source || !target)
      throw new Error('Explicit source and destination paths required');
    if (operation === 'backup') {
      if (
        (retention !== undefined || confirmation !== undefined) &&
        (confirmation !== '--confirm-prune' ||
          !Number.isInteger(Number(retention)) ||
          Number(retention) < 1 ||
          Number(retention) > 3650)
      )
        throw new Error(
          'Retention deletion requires 1–3650 DAYS --confirm-prune',
        );
      console.log(await backupDatabase(source, target));
      if (confirmation === '--confirm-prune')
        console.log(
          `Expired backups removed: ${pruneBackups(source, target, Number(retention))}`,
        );
      else if (confirmation !== undefined || retention !== undefined)
        throw new Error('Retention deletion requires DAYS --confirm-prune');
    } else if (
      operation === 'restore' &&
      retention === '--confirm-restore' &&
      confirmation === undefined
    )
      console.log(await restoreDatabase(source, target));
    else
      throw new Error(
        'Use backup SOURCE DIRECTORY [DAYS --confirm-prune] or restore BACKUP NEW_DATABASE --confirm-restore',
      );
  } catch (error) {
    console.error(
      error instanceof Error ? error.message : 'Database maintenance failed',
    );
    process.exitCode = 1;
  }
}
