import { databasePathSchema } from '../config/env.js';
import { createLogger } from '../shared/logger.js';
import { openDatabase } from './client.js';
import { acquireInstanceLock } from './instance-lock.js';

const logger = createLogger();
try {
  const path = databasePathSchema.parse(process.env.DATABASE_URL);
  const release = acquireInstanceLock(path);
  try {
    const { sqlite } = openDatabase(path);
    sqlite.close();
  } finally {
    release();
  }
  logger.info('Database migrations applied');
} catch (err) {
  logger.fatal({ err }, 'Database migration failed');
  process.exitCode = 1;
}
