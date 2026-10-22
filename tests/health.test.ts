import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expect, it } from 'vitest';
import {
  isHealthy,
  removeHeartbeat,
  writeHeartbeat,
  healthReport,
  runtimeHealth,
} from '../src/shared/health.js';
import { makeStatus, fixedNow } from './fixtures/monitoring.js';

it('requires a fresh heartbeat from a live process and removes it on shutdown', () => {
  const directory = mkdtempSync(join(tmpdir(), 'tse-health-'));
  const path = join(directory, 'health.json');
  try {
    expect(isHealthy(path)).toBe(false);
    const now = Date.now();
    writeHeartbeat(
      runtimeHealth(
        makeStatus({ lastProgressAt: now }),
        true,
        true,
        { lastSuccess: now, errors: 0 },
        now,
        5000,
        now,
      ),
      path,
    );
    expect(isHealthy(path)).toBe(true);
    expect(isHealthy(path, Date.now() + 91_000)).toBe(false);
    writeFileSync(
      path,
      JSON.stringify({ pid: process.pid, timestamp: Date.now() + 120_000 }),
    );
    expect(isHealthy(path)).toBe(false);
    writeFileSync(path, '{broken');
    expect(isHealthy(path)).toBe(false);
    removeHeartbeat(path);
    expect(isHealthy(path)).toBe(false);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

it('distinguishes idle, upstream failures and actual worker/database/Bale failure', () => {
  const directory = mkdtempSync(join(tmpdir(), 'tse-diagnostics-'));
  const path = join(directory, 'health.json');
  const heartbeat = (overrides: Parameters<typeof makeStatus>[0] = {}) =>
    runtimeHealth(
      makeStatus(overrides),
      true,
      true,
      { lastSuccess: fixedNow, errors: 0 },
      fixedNow,
      5000,
      fixedNow,
    );
  try {
    for (const [status, expected] of [
      [{ isMarketOpen: false }, 'MARKET_CLOSED'],
      [{ monitoredSymbols: 0 }, 'NO_MONITORED_SYMBOLS'],
    ] as const) {
      writeHeartbeat(heartbeat(status), path);
      expect(healthReport(path, fixedNow)).toMatchObject({
        healthy: true,
        market: expected,
      });
    }
    const symbol = {
      insCode: '123',
      consecutiveErrors: 1,
      lastSuccessfulFetch: null,
      lastVerifiedSnapshot: null,
      lastResult: 'transport-error' as const,
      reason: 'timeout',
      retryAt: fixedNow + 1000,
    };
    writeHeartbeat(heartbeat({ symbols: [symbol] }), path);
    expect(healthReport(path, fixedNow)).toMatchObject({
      healthy: true,
      market: 'TSETMC_TEMPORARY_FAILURE',
    });
    writeHeartbeat(
      heartbeat({ symbols: [{ ...symbol, consecutiveErrors: 5 }] }),
      path,
    );
    expect(healthReport(path, fixedNow)).toMatchObject({
      healthy: true,
      market: 'TSETMC_PERSISTENT_FAILURE',
    });
    for (const value of [
      { ...heartbeat(), monitorProgressAt: fixedNow - 46_000 },
      { ...heartbeat(), databaseOk: false },
      { ...heartbeat(), bale: 'CONNECTION_FAILURE' as const },
    ]) {
      writeHeartbeat(value, path);
      expect(healthReport(path, fixedNow).healthy).toBe(false);
    }
    writeHeartbeat(
      {
        ...heartbeat({ isMarketOpen: false }),
        monitorProgressAt: fixedNow - 46_000,
      },
      path,
    );
    expect(healthReport(path, fixedNow).reasons).toContain('MONITOR_STALLED');
    expect(
      runtimeHealth(
        makeStatus(),
        false,
        true,
        { lastSuccess: null, errors: 1 },
        fixedNow - 121_000,
        5000,
        fixedNow,
      ).bale,
    ).toBe('CONNECTION_FAILURE');
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
