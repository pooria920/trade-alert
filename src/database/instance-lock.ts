import { existsSync, mkdirSync, realpathSync, statSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import Database from 'better-sqlite3';

// OS-backed SQLite file lock: automatically released on process death, including container crashes.
// Separate from the WAL application database so normal reads/writes/online backups remain concurrent.
export function acquireInstanceLock(databasePath: string) {
  if (databasePath === ':memory:')
    throw new Error('An on-disk database is required for the instance lock');
  const input = resolve(databasePath);
  mkdirSync(dirname(input), { recursive: true });
  if (existsSync(input) && statSync(input).nlink > 1)
    throw new Error('Hard-linked database paths are not supported');
  const canonical = existsSync(input)
    ? realpathSync(input)
    : join(realpathSync(dirname(input)), basename(input));
  const path = `${canonical}.instance-lock.sqlite`;
  const lock = new Database(path);
  try {
    lock.pragma('busy_timeout=0');
    lock.exec('BEGIN EXCLUSIVE');
    return () => lock.close();
  } catch {
    lock.close();
    throw new Error(
      'Database is already owned by an application or maintenance process',
    );
  }
}
