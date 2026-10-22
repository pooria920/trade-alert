import { readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { z } from 'zod';
import type { MonitoringStatus } from '../monitoring/monitoring-status.js';

export const healthFile = join(tmpdir(), 'tse-stock-alert-health.json');
const schema = z.object({
  pid: z.number().int().positive(),
  timestamp: z.number().int().nonnegative(),
  initialized: z.boolean(),
  databaseOk: z.boolean(),
  monitorRunning: z.boolean(),
  monitorProgressAt: z.number().nullable(),
  monitorMaxSilenceMs: z.number().positive(),
  market: z.enum([
    'MARKET_CLOSED',
    'NO_MONITORED_SYMBOLS',
    'TSETMC_TEMPORARY_FAILURE',
    'TSETMC_PERSISTENT_FAILURE',
    'FRESH_DATA',
    'DATA_UNVERIFIED_OR_STALE',
  ]),
  bale: z.enum([
    'STARTING',
    'POLLING',
    'TEMPORARY_FAILURE',
    'CONNECTION_FAILURE',
  ]),
});
export type RuntimeHealth = z.infer<typeof schema>;
export function runtimeHealth(
  status: MonitoringStatus,
  initialized: boolean,
  databaseOk: boolean,
  bale: { lastSuccess: number | null; errors: number },
  startedAt: number,
  requestTimeoutMs: number,
  now = Date.now(),
): RuntimeHealth {
  const failures = status.symbols.filter(
    (symbol) => symbol.lastResult === 'transport-error',
  );
  return {
    pid: process.pid,
    timestamp: now,
    initialized,
    databaseOk,
    monitorRunning: status.isRunning,
    monitorProgressAt: status.lastProgressAt,
    monitorMaxSilenceMs: status.pollIntervalMs + requestTimeoutMs + 30_000,
    market: !status.isMarketOpen
      ? 'MARKET_CLOSED'
      : status.monitoredSymbols === 0
        ? 'NO_MONITORED_SYMBOLS'
        : failures.some((symbol) => symbol.consecutiveErrors >= 5)
          ? 'TSETMC_PERSISTENT_FAILURE'
          : failures.length
            ? 'TSETMC_TEMPORARY_FAILURE'
            : status.freshSymbols
              ? 'FRESH_DATA'
              : 'DATA_UNVERIFIED_OR_STALE',
    bale:
      now - (bale.lastSuccess ?? startedAt) > 120_000
        ? 'CONNECTION_FAILURE'
        : bale.errors
          ? 'TEMPORARY_FAILURE'
          : initialized
            ? 'POLLING'
            : 'STARTING',
  };
}
export function writeHeartbeat(value: RuntimeHealth, path = healthFile) {
  writeFileSync(`${path}.tmp`, JSON.stringify(schema.parse(value)), {
    mode: 0o600,
  });
  renameSync(`${path}.tmp`, path);
}
export function removeHeartbeat(path = healthFile) {
  rmSync(path, { force: true });
  rmSync(`${path}.tmp`, { force: true });
}
export function healthReport(path = healthFile, now = Date.now()) {
  try {
    const value = schema.parse(
      JSON.parse(readFileSync(path, 'utf8')) as unknown,
    );
    const reasons: string[] = [];
    try {
      process.kill(value.pid, 0);
    } catch {
      reasons.push('PROCESS_NOT_RUNNING');
    }
    if (value.timestamp > now || now - value.timestamp > 90_000)
      reasons.push('STALE_HEARTBEAT');
    if (!value.initialized) reasons.push('STARTING');
    if (!value.databaseOk) reasons.push('DATABASE_FAILURE');
    if (
      !value.monitorRunning ||
      value.monitorProgressAt === null ||
      value.monitorProgressAt > now ||
      now - value.monitorProgressAt > value.monitorMaxSilenceMs
    )
      reasons.push('MONITOR_STALLED');
    if (value.bale === 'CONNECTION_FAILURE')
      reasons.push('BALE_CONNECTION_FAILURE');
    return {
      healthy: reasons.length === 0,
      reasons,
      market: value.market,
      bale: value.bale,
      database: value.databaseOk ? 'OK' : 'FAILURE',
    };
  } catch {
    return {
      healthy: false,
      reasons: ['HEALTH_UNAVAILABLE'],
      market: 'UNKNOWN',
      bale: 'UNKNOWN',
      database: 'UNKNOWN',
    };
  }
}
export function isHealthy(path = healthFile, now = Date.now()) {
  return healthReport(path, now).healthy;
}
